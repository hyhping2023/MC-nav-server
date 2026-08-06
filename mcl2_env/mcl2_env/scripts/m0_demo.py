#!/usr/bin/env python3
"""M0 端到端驱动：启动 luantiserver，经文件 IPC 跑通 craft_planks。

契约：docs/m0_protocol.md；验收断言见其 §6。

用法（二选一）：
    python -m mcl2_env.scripts.m0_demo --repo /Users/hyhpinggongzuoban/Code/fake-mc --world m0world
    python mcl2_env/mcl2_env/scripts/m0_demo.py --world m0world

参数：
    --repo      仓库根目录（默认 /Users/hyhpinggongzuoban/Code/fake-mc）
    --world     世界名，即 <repo>/luanti/worlds/<world>（默认 m0world）
    --task      任务 id（默认 craft_planks）
    --timeout   总超时秒数（默认 120）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---- 包导入引导：兼容 `python -m`、直接运行、以及依赖未安装三种情况 ----
# bridge.py 只用标准库；若 mcl2_env 包因缺依赖（pydantic/gymnasium）无法导入，
# 直接按文件路径加载 bridge.py，避免触发包 __init__ 的完整依赖链。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))


def _load_bridge():
    try:
        from mcl2_env.bridge import BridgeError, FileBridgeClient
        return BridgeError, FileBridgeClient
    except ImportError:
        import importlib.util

        bridge_path = _PROJECT_ROOT / "mcl2_env" / "bridge.py"
        spec = importlib.util.spec_from_file_location("_mcl2_env_bridge", bridge_path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod.BridgeError, mod.FileBridgeClient


BridgeError, FileBridgeClient = _load_bridge()

DEFAULT_REPO = "/Users/hyhpinggongzuoban/Code/fake-mc"
PLAYER = "bot1"
EPISODE_ID = "ep-000001"
RUN_ID = "m0_run"
TASK_SEED = 42
RESET_SEED = 43


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M0 end-to-end demo (file IPC)")
    p.add_argument("--repo", default=DEFAULT_REPO, help="repo root (default: %(default)s)")
    p.add_argument("--world", default="m0world", help="world name under <repo>/luanti/worlds")
    p.add_argument("--task", default="craft_planks", help="task id (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=120.0, help="overall timeout in seconds (default: %(default)s)")
    return p.parse_args()


class _StreamDrainer(threading.Thread):
    """后台排空子进程 stdout/stderr，避免管道缓冲写满阻塞服务器。"""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.lines: list[str] = []

    def run(self) -> None:
        for line in self.stream:
            self.lines.append(line)


def read_world_seed(world_dir: Path) -> int:
    """从 world.mt 读 seed，读不到返回 0。"""
    mt = world_dir / "world.mt"
    try:
        text = mt.read_text("utf-8", errors="replace")
    except OSError:
        return 0
    m = re.search(r"seed\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else 0


def print_log_tail(logfile: Path, n: int = 40) -> None:
    print(f"--- tail {n} lines of {logfile} ---")
    try:
        lines = logfile.read_text("utf-8", errors="replace").splitlines()
        for line in lines[-n:]:
            print(line)
    except OSError as e:
        print(f"(cannot read log: {e})")
    print("-----------------------------------")


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except OSError:
        pass


def verify_artifacts(world_dir: Path, spec: dict) -> bool:
    """验收断言（m0_protocol.md §6），全部满足返回 True。"""
    episode_dir = world_dir / "mcl2_agent" / "data" / "episodes" / EPISODE_ID
    all_ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal all_ok
        print(f"      [{'OK' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
        if not cond:
            all_ok = False

    check("episode dir exists", episode_dir.is_dir(), str(episode_dir))

    meta: dict = {}
    if episode_dir.is_dir():
        meta_path = episode_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text("utf-8"))
            except json.JSONDecodeError:
                meta = {}
        else:
            check("meta.json exists", False, str(meta_path))

    check("meta.json world_seed", meta.get("world_seed") == spec["world_seed"],
          f"expected {spec['world_seed']!r}, got {meta.get('world_seed')!r}")
    check("meta.json task_seed", meta.get("task_seed") == TASK_SEED,
          f"expected {TASK_SEED!r}, got {meta.get('task_seed')!r}")
    check("meta.json reset_seed", meta.get("reset_seed") == RESET_SEED,
          f"expected {RESET_SEED!r}, got {meta.get('reset_seed')!r}")

    for name in ("states", "actions", "rewards"):
        p = episode_dir / f"{name}.jsonl"
        n = 0
        if p.exists():
            try:
                n = sum(1 for line in p.open("r", encoding="utf-8") if line.strip())
            except OSError:
                n = 0
        check(f"{name}.jsonl non-empty", n >= 1, f"{n} line(s)")

    summary_path = episode_dir / "episode_summary.json"
    summary_ok = False
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text("utf-8"))
            summary_ok = bool(summary.get("success"))
        except (OSError, json.JSONDecodeError):
            summary_ok = False
    check("episode_summary.json success=true", summary_ok,
          "file missing" if not summary_path.exists() else f"success={summary_ok}")
    return all_ok


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).resolve()
    world_dir = repo / "luanti" / "worlds" / args.world
    # 优先真实二进制路径；符号链接（build/bin/luantiserver）会被引擎当作独立路径
    # 导致 RUN_IN_PLACE 下找不到 games/（报 "Game [] could not be found"）。
    server_bin = repo / "luanti" / "bin" / "luantiserver"
    if not server_bin.exists():
        server_bin = repo / "luanti" / "build" / "bin" / "luantiserver"
    server_conf = world_dir / "server.conf"
    logfile = world_dir / "server.log"

    proc: subprocess.Popen | None = None
    out_drainer = err_drainer = None

    try:
        # a) 启动服务器子进程
        if not server_bin.exists():
            print(f"FAIL: luantiserver not found: {server_bin} (is luanti/build/ ready?)")
            return 1
        print(f"[1/6] starting luantiserver (world={args.world}, task={args.task}, timeout={args.timeout:.0f}s)")
        cmd = [
            str(server_bin),
            "--world", str(world_dir),
            "--config", str(server_conf),
            "--logfile", str(logfile),
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo / "luanti"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out_drainer = _StreamDrainer(proc.stdout)
        err_drainer = _StreamDrainer(proc.stderr)
        out_drainer.start()
        err_drainer.start()

        # b) 等待 ready.json
        print("[2/6] waiting for ready.json ...")
        bridge = FileBridgeClient(world_dir, timeout=args.timeout)
        try:
            ready = bridge.wait_ready(timeout=args.timeout)
        except BridgeError as e:
            if proc.poll() is not None:
                print(f"FAIL: server exited early (rc={proc.returncode})")
            print(f"FAIL: {e}")
            print_log_tail(logfile)
            return 1
        print(f"      ready = {json.dumps(ready, ensure_ascii=False)}")

        # c) begin_episode
        world_seed = read_world_seed(world_dir)
        spec = {
            "player": PLAYER,
            "task_id": args.task,
            "run_id": RUN_ID,
            "episode_id": EPISODE_ID,
            "world_seed": world_seed,
            "task_seed": TASK_SEED,
            "reset_seed": RESET_SEED,
        }
        print(f"[3/6] begin_episode task={args.task} world_seed={world_seed}")
        res = bridge.begin_episode(spec)
        print(f"      begin_episode -> {json.dumps(res, ensure_ascii=False)}")

        # d) execute(craft) + observe 轮询 task.success
        print('[4/6] execute craft(mcl_trees:wood_oak x4) then observe until task.success')
        bridge.execute("craft", {"item": "mcl_trees:wood_oak", "count": 4})
        success = False
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            obs = bridge.observe()
            task = (obs or {}).get("task") or {}
            if task.get("success"):
                success = True
                break
            time.sleep(0.5)
        print(f"      task.success = {success}")

        # 消费 Lua 异步推送的事件（如 task_done），保持 ipc/events 目录干净
        for ev in bridge.poll_events():
            print(f"      event: {json.dumps(ev, ensure_ascii=False)}")

        # e) end_episode
        print(f"[5/6] end_episode(success={success})")
        bridge.end_episode(success=success)

        # f) 验收断言
        print("[6/6] verifying artifacts ...")
        if not verify_artifacts(world_dir, spec):
            print("FAIL: artifact checks failed (see above)")
            return 1
        print("PASS: M0 demo completed successfully")
        return 0

    except BridgeError as e:
        print(f"FAIL: bridge error: {e}")
        print_log_tail(logfile)
        return 1
    finally:
        stop_server(proc)
        if out_drainer:
            out_drainer.join(timeout=2)
        if err_drainer:
            err_drainer.join(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
