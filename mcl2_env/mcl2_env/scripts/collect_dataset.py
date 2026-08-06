#!/usr/bin/env python3
"""M2 小数据集采集脚本（docs/m2_protocol.md §4）。

起 luantiserver（worldmod mcl2_agent，文件 IPC）→ 可选客户端 → 按序采集
N 个 episode（任务按 --tasks 循环）→ 每个 episode 独立 episode_id、随机原始
动作循环（与 random_agent 相同逻辑）→ end_episode → 对齐断言 → 采集完调用
export_webdataset 导出到 --out 并打印产物清单。

用法：
  python mcl2_env/mcl2_env/scripts/collect_dataset.py --repo <repo> --world m0world \
      --episodes 4 --tasks craft_planks,collect_wood --out <repo>/datasets/m2_run

参数：
  --repo       仓库根目录（默认 /Users/hyhpinggongzuoban/Code/fake-mc）
  --world      世界名（默认 m0world）
  --episodes   采集 episode 数（默认 2）
  --tasks      逗号分隔任务 id，按序循环（默认 craft_planks,collect_wood）
  --out        导出目录（默认 <repo>/datasets/m2_run）
  --steps      每 episode 随机动作步数（默认 60）
  --renderer   engine_fork(回退 voxel) / voxel / none
  --spawn-client 拉起客户端以 bot1 连接
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

# ---- 包导入引导：兼容 `python -m`、直接运行、以及轻依赖环境 ----
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.scripts._common import (
    IMG_STD_THRESHOLD,
    PLAYER,
    build_begin_episode_spec,
    build_renderer,
    pos_distance,
    print_log_tail,
    random_primitive,
    read_world_seed,
    resolve_frame,
    start_proc,
    stop_proc,
    verify_alignment,
)

DEFAULT_REPO = "/Users/hyhpinggongzuoban/Code/fake-mc"
RUN_ID = "m2_dataset"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M2 dataset collection (canonical episodes + webdataset export)")
    p.add_argument("--repo", default=DEFAULT_REPO, help="repo root (default: %(default)s)")
    p.add_argument("--world", default="m0world", help="world name under <repo>/luanti/worlds")
    p.add_argument("--episodes", type=int, default=2, help="number of episodes to collect (default: %(default)s)")
    p.add_argument("--tasks", default="craft_planks,collect_wood",
                   help="comma-separated task ids, cycled (default: %(default)s)")
    p.add_argument("--out", default="", help="export out dir (default: <repo>/datasets/m2_run)")
    p.add_argument("--steps", type=int, default=60, help="random steps per episode (default: %(default)s)")
    p.add_argument("--renderer", choices=["engine_fork", "voxel", "none"],
                   default="engine_fork", help="renderer (default: %(default)s)")
    p.add_argument("--spawn-client", action="store_true",
                   help="spawn luanti client to attach bot1")
    p.add_argument("--fps", type=int, default=5, help="renderer fps (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=120.0, help="per-episode timeout in seconds")
    p.add_argument("--seed", type=int, default=42, help="base rng seed (default: %(default)s)")
    return p.parse_args()


def run_one_episode(
    bridge,
    renderer,
    episode_id: str,
    task_id: str,
    data_root: Path,
    world_seed: int,
    args: argparse.Namespace,
) -> dict:
    """采集单个 episode（begin → 随机原始动作循环 → end → 对齐断言）。"""
    from mcl2_env.dataset.episode_writer import EpisodeWriter

    spec = build_begin_episode_spec(
        player=PLAYER, task_id=task_id, episode_id=episode_id,
        run_id=RUN_ID, world_seed=world_seed, task_seed=args.seed,
    )
    bridge.begin_episode(spec)

    writer = EpisodeWriter(str(data_root), RUN_ID, episode_id, images_only=True)
    episode_dir = data_root / "episodes" / episode_id
    rng = random.Random(args.seed + int(episode_id[-3:]) if episode_id[-3:].isdigit() else args.seed)
    success = False
    frames_written = 0
    img_checked = img_passed = 0
    max_disp = 0.0
    last_pos = None
    deadline = time.monotonic() + args.timeout
    steps_done = 0

    for _ in range(args.steps):
        if time.monotonic() > deadline:
            print(f"      [ep {episode_id}] truncated by timeout")
            break
        steps_done += 1
        bridge.step(random_primitive(rng), player=PLAYER)
        obs = bridge.observe(player=PLAYER)

        if renderer is not None and hasattr(renderer, "set_camera"):
            pl = obs.get("player") or {}
            wd = obs.get("world") or {}
            renderer.set_camera(pl.get("pos"), pl.get("look"), wd.get("voxels"))
        frame = renderer.get_frame() if renderer else None
        if frame is not None:
            frame_no = resolve_frame(episode_dir, obs)
            if frame_no is not None:
                std = float(frame.image.std())
                img_checked += 1
                if std > IMG_STD_THRESHOLD:
                    img_passed += 1
                writer.write_frame(frame.image, frame_no)
                frames_written += 1

        pos = (obs.get("player") or {}).get("pos")
        if pos:
            cur = (pos["x"], pos["y"], pos["z"])
            if last_pos is not None:
                max_disp = max(max_disp, pos_distance(last_pos, cur))
            last_pos = cur

        task = obs.get("task") or {}
        if task.get("success"):
            success = True
            print(f"      [ep {episode_id}] task.success at step {steps_done}")
            break
        for ev in bridge.poll_events():
            print(f"      [ep {episode_id}] event: {json.dumps(ev, ensure_ascii=False)}")

    bridge.end_episode(success=success, player=PLAYER)
    align_ok, checks = verify_alignment(episode_dir)
    return {
        "episode_id": episode_id,
        "task": task_id,
        "success": success,
        "steps": steps_done,
        "frames": frames_written,
        "max_disp": round(max_disp, 2),
        "align_ok": align_ok,
        "checks": checks,
    }


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).resolve()
    world_dir = repo / "luanti" / "worlds" / args.world
    server_bin = repo / "luanti" / "bin" / "luantiserver"
    client_bin = repo / "luanti" / "bin" / "luanti"
    server_conf = world_dir / "server.conf"
    logfile = world_dir / "server.log"
    data_root = world_dir / "mcl2_agent" / "data"
    out_dir = Path(args.out).resolve() if args.out else repo / "datasets" / "m2_run"
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]

    if not server_bin.exists():
        print(f"FAIL: luantiserver not found: {server_bin}")
        return 1
    if not tasks:
        print("FAIL: --tasks must be a non-empty comma-separated list")
        return 1

    from mcl2_env.bridge import BridgeError, FileBridgeClient

    server = client = None
    out_d = err_d = None
    renderer = build_renderer(args.renderer, args.fps)
    results: list[dict] = []

    try:
        # ---- 1) 启动服务器（所有 episode 共用）----
        print(f"[1/6] starting luantiserver (world={args.world}, tasks={tasks}, "
              f"episodes={args.episodes}, steps/ep={args.steps})")
        server, out_d, err_d = start_proc(server_bin, [
            str(server_bin), "--world", str(world_dir),
            "--config", str(server_conf), "--logfile", str(logfile),
        ], repo / "luanti")

        # ---- 2) 可选客户端 ----
        if args.spawn_client:
            if not client_bin.exists():
                print(f"FAIL: luanti client not found: {client_bin}")
                return 1
            client_cfg = repo / "luanti" / "mcl2_client.conf"
            client_cmd = [str(client_bin), "--go", "--address", "127.0.0.1",
                          "--port", "30000", "--name", PLAYER]
            if client_cfg.exists():
                client_cmd += ["--config", str(client_cfg)]
            client, _, _ = start_proc(client_bin, client_cmd, repo / "luanti")

        # ---- 3) 渲染器 + ready.json ----
        if renderer:
            renderer.start()
            print(f"[3/6] renderer={type(renderer).__name__} started")
        bridge = FileBridgeClient(world_dir, timeout=args.timeout)
        try:
            ready = bridge.wait_ready(timeout=args.timeout)
        except BridgeError as e:
            print(f"FAIL: {e}")
            if server.poll() is not None:
                print(f"      server exited early (rc={server.returncode})")
            print_log_tail(logfile)
            return 1
        print(f"      ready = {json.dumps(ready, ensure_ascii=False)}")

        # ---- 3.5) 等 bot1 会话 ----
        print("[3.5/6] waiting for player session ...")
        player_ready = False
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                probe = bridge.observe(player=PLAYER)
            except BridgeError:
                probe = None
            if isinstance(probe, dict) and probe.get("player"):
                player_ready = True
                break
            time.sleep(0.5)
        if not player_ready:
            print(f"FAIL: player {PLAYER} did not connect within {args.timeout:.0f}s")
            print_log_tail(logfile)
            return 1
        print(f"      player {PLAYER} connected")

        # ---- 4/5) 按序采集 N 个 episode ----
        world_seed = read_world_seed(world_dir)
        ts = int(time.time())
        print(f"[4/6] collecting {args.episodes} episode(s) ...")
        for i in range(args.episodes):
            task_id = tasks[i % len(tasks)]
            episode_id = f"ep-{ts}-{i:03d}"
            print(f"[5/6] episode {i + 1}/{args.episodes}: task={task_id} episode={episode_id}")
            r = run_one_episode(bridge, renderer, episode_id, task_id, data_root, world_seed, args)
            results.append(r)
            ok = r["align_ok"]
            print(f"      -> steps={r['steps']} frames={r['frames']} success={r['success']} "
                  f"align={'OK' if ok else 'FAIL'}")
            for name, passed, detail in r["checks"]:
                print(f"         [{'OK' if passed else 'FAIL'}] {name}"
                      + (f"  ({detail})" if detail else ""))
            if not ok:
                print(f"FAIL: episode {episode_id} 对齐断言未通过")
                print_log_tail(logfile)
                return 1

        # ---- 6) 导出 webdataset + 产物清单 ----
        from mcl2_env.dataset.export import ExportConfig, export_webdataset

        print(f"[6/6] exporting to webdataset -> {out_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)
        export_webdataset(ExportConfig(source_root=str(data_root), out_dir=str(out_dir),
                                       shard_size=1000,
                                       only=tuple(r["episode_id"] for r in results)))
        print("      canonical episodes:")
        for r in results:
            print(f"        {r['episode_id']}  task={r['task']}  success={r['success']}  "
                  f"steps={r['steps']}  frames={r['frames']}")
        print(f"      webdataset shards ({out_dir}):")
        shards = sorted(out_dir.glob("shard-*.tar"))
        for sh in shards:
            print(f"        {sh.name}  {sh.stat().st_size} bytes")
        print(f"PASS: collected {len(results)} episode(s), exported {len(shards)} shard(s)")
        return 0

    except BridgeError as e:
        print(f"FAIL: bridge error: {e}")
        print_log_tail(logfile)
        return 1
    finally:
        if renderer:
            renderer.stop()
        stop_proc(client)
        stop_proc(server)
        if out_d:
            out_d.join(timeout=2)
        if err_d:
            err_d.join(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
