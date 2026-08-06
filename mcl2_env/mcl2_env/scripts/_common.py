#!/usr/bin/env python3
"""random_agent 与 collect_dataset 共享的服务器/客户端/对齐工具。

M2 帧/状态对齐协议（docs/m2_protocol.md §1）：
  每次 observe → Lua 写一行 states（含 frame + image 引用）→ Python 从
  渲染器取帧，按该行 frame 号写入同名 PNG → 循环结束断言
  states==actions==rewards==PNG 数且引用全存在。
"""

from __future__ import annotations

import json
import math
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# ---- 包导入引导：兼容 `python -m`、直接运行、以及轻依赖环境 ----
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.bridge import BridgeError, FileBridgeClient  # noqa: E402

PLAYER = "bot1"
RUN_ID = "m2_run"

# m2_protocol.md §2：begin_episode 需携带的 env 字段
ENGINE = {"name": "luanti", "version": "5.17.0-dev", "fork": "mcl2-agent-fork"}
GAME = {"name": "mineclonia", "version": "0.123.0"}
PYTHON = {"package": "mcl2_env", "version": "0.1.0"}

IMG_STD_THRESHOLD = 10.0  # m1_protocol §4：非纯色帧方差阈值


class _StreamDrainer(threading.Thread):
    """后台排空子进程 stdout/stderr，避免管道缓冲写满阻塞子进程。"""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.lines: list[str] = []

    def run(self) -> None:
        for line in self.stream:
            self.lines.append(line)


# ---------------------------------------------------------------- 进程管理

def start_proc(bin_path: Path, cmd: list[str], cwd: Path) -> tuple[subprocess.Popen, "_StreamDrainer", "_StreamDrainer"]:
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = _StreamDrainer(proc.stdout)
    err = _StreamDrainer(proc.stderr)
    out.start()
    err.start()
    return proc, out, err


def stop_proc(proc: subprocess.Popen | None) -> None:
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
        for line in logfile.read_text("utf-8", errors="replace").splitlines()[-n:]:
            print(line)
    except OSError as e:
        print(f"(cannot read log: {e})")
    print("-----------------------------------")


def resolve_server_bin(repo: Path) -> Path:
    """优先真实二进制；符号链接（build/bin/luantiserver）会因 RUN_IN_PLACE 找不到 games/。"""
    direct = repo / "luanti" / "bin" / "luantiserver"
    return direct if direct.exists() else repo / "luanti" / "build" / "bin" / "luantiserver"


# ---------------------------------------------------------------- 渲染器

def build_renderer(kind: str, fps: int):
    """按 --renderer 构建渲染器实例；无渲染器返回 None。

    engine_fork：CompositeRenderer(engine_fork 主, voxel 回退)——有真实帧用
    真实帧，无帧回退 voxel 合成帧（每步都有帧，保证 PNG 与 states 对齐）。
    voxel：纯合成渲染器。none：无渲染器（图像断言跳过，但对齐断言会 FAIL）。
    """
    if kind == "engine_fork":
        from mcl2_env.renderer.composite import CompositeRenderer
        from mcl2_env.renderer.engine_fork import EngineForkRenderer
        from mcl2_env.renderer.voxel import VoxelRenderer

        return CompositeRenderer(EngineForkRenderer(fps=fps), VoxelRenderer())
    if kind == "voxel":
        from mcl2_env.renderer.voxel import VoxelRenderer

        return VoxelRenderer()
    return None


# ---------------------------------------------------------------- 动作/观测

def random_primitive(rng: random.Random) -> dict[str, Any]:
    """随机原始动作（字段对齐 ActionPrimitive / mcl2_agent action.lua）。"""
    return {
        "forward": rng.random() < 0.3,
        "back": rng.random() < 0.1,
        "left": rng.random() < 0.1,
        "right": rng.random() < 0.1,
        "jump": rng.random() < 0.15,
        "sneak": False,
        "sprint": rng.random() < 0.1,
        "attack": rng.random() < 0.05,
        "use": rng.random() < 0.05,
        "drop": False,
        "hotbar": rng.randint(0, 8),
        "camera": [rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3)],
    }


def pos_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.dist(a, b)


# ---------------------------------------------------------------- begin_episode spec

def build_begin_episode_spec(
    player: str,
    task_id: str,
    episode_id: str,
    run_id: str,
    world_seed: int,
    task_seed: int,
    mapgen: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """begin_episode 请求参数（含 m2_protocol §2 env 字段）。"""
    return {
        "player": player,
        "task_id": task_id,
        "run_id": run_id,
        "episode_id": episode_id,
        "world_seed": world_seed,
        "task_seed": task_seed,
        "reset_seed": task_seed + 1,
        "mapgen": mapgen,
        "engine": ENGINE,
        "game": GAME,
        "python": PYTHON,
    }


# ---------------------------------------------------------------- 帧号对齐

def resolve_frame(episode_dir: Path, obs: Optional[dict[str, Any]]) -> Optional[int]:
    """当前观察对应的帧号（与 Lua 侧 states.jsonl 行号一致）。

    优先级（m2_protocol §1）：
      1. observe 响应的 episode 段 `frame`（lua-record 已加）；
      2. observe 响应顶层 `frame`；
      3. states.jsonl 当前行数 - 1（请求式采样下每 observe 恰好一行）。
    读不到任何来源返回 None（调用方跳过写盘）。
    """
    if obs is not None:
        ep = obs.get("episode") or {}
        if ep.get("frame") is not None:
            return int(ep["frame"])
        if obs.get("frame") is not None:
            return int(obs["frame"])
    sp = episode_dir / "states.jsonl"
    n = 0
    if sp.exists():
        try:
            n = sum(1 for line in sp.open("r", encoding="utf-8") if line.strip())
        except OSError:
            n = 0
    return max(n - 1, 0) if n > 0 else None


def count_jsonl(path: Path) -> int:
    """JSONL 行数（空/不存在返回 0）。"""
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.open("r", encoding="utf-8") if line.strip())
    except OSError:
        return 0


def verify_alignment(episode_dir: Path) -> tuple[bool, list[tuple[str, bool, str]]]:
    """M2 对齐断言（docs/m2_protocol.md §1 验收）：

      states.jsonl N == actions N == rewards N == observations/*.png 数 N
      ∀ row in states: os.path.exists(ep_dir/row.image)
      meta.json 含 env.engine.name/version、env.game.name/version、
      env.python.package/version

    返回 (all_ok, [(name, ok, detail), ...])。
    """
    checks: list[tuple[str, bool, str]] = []
    n_states = count_jsonl(episode_dir / "states.jsonl")
    n_actions = count_jsonl(episode_dir / "actions.jsonl")
    n_rewards = count_jsonl(episode_dir / "rewards.jsonl")

    # 引用检查：逐行解析 states 的 image 字段
    missing: list[str] = []
    n_img_ref = 0
    sp = episode_dir / "states.jsonl"
    if sp.exists():
        try:
            for line in sp.open("r", encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                ref = row.get("image")
                if not ref:
                    continue
                n_img_ref += 1
                if not (episode_dir / ref).is_file():
                    missing.append(ref)
        except (OSError, json.JSONDecodeError) as e:
            checks.append(("states.jsonl 可解析", False, str(e)))

    obs_dir = episode_dir / "observations"
    png_count = len(list(obs_dir.glob("*.png"))) if obs_dir.is_dir() else 0

    checks.append(("states == actions", n_states == n_actions,
                   f"states={n_states} actions={n_actions}"))
    checks.append(("states == rewards", n_states == n_rewards,
                   f"states={n_states} rewards={n_rewards}"))
    checks.append(("states == PNG 数", n_states == png_count,
                   f"states={n_states} png={png_count}"))
    checks.append(("每行 image 引用存在", len(missing) == 0,
                   f"{n_img_ref} 引用, {len(missing)} 缺失: {missing[:5]}"))

    # meta.json env 字段（m2_protocol.md §2）
    env_checks = {
        "env.engine.name": ENGINE["name"],
        "env.engine.version": ENGINE["version"],
        "env.game.name": GAME["name"],
        "env.game.version": GAME["version"],
        "env.python.package": PYTHON["package"],
        "env.python.version": PYTHON["version"],
    }
    meta_path = episode_dir / "meta.json"
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except json.JSONDecodeError:
            meta = {}
    env = meta.get("env") or {}
    for key, expected in env_checks.items():
        actual = env.get(key.split(".")[1], {}).get(key.split(".")[2]) if len(key.split(".")) == 3 else None
        checks.append((f"meta {key}", actual == expected, f"expected {expected!r}, got {actual!r}"))

    all_ok = all(ok for _, ok, _ in checks)
    return all_ok, checks


def render_frame_to_episode(
    writer,
    renderer: Any,
    obs: dict[str, Any],
    episode_dir: Path,
) -> tuple[bool, Optional[int]]:
    """一步收敛：注入相机 → 取帧 → 解析帧号 → 写 PNG。

    返回 (written, frame)：written 表示是否成功写盘一帧。
    """
    if renderer is None:
        return False, None
    if hasattr(renderer, "set_camera"):
        pl = obs.get("player") or {}
        wd = obs.get("world") or {}
        renderer.set_camera(pl.get("pos"), pl.get("look"), wd.get("voxels"))
    frame = renderer.get_frame()
    if frame is None:
        return False, None
    frame_no = resolve_frame(episode_dir, obs)
    if frame_no is None:
        return False, None
    writer.write_frame(frame.image, frame_no)
    return True, frame_no
