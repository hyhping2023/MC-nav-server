#!/usr/bin/env python3
"""智能决策 agent（brain/SmartPolicy）跑 episode。

高层决策（Python brain：存活 > 战斗 > 任务）驱动 Lua 语义动作执行
（goto 挖穿/下挖、dig 自动装备、attack 战斗、eat 进食）。

用法：
  python mcl2_env/mcl2_env/scripts/smart_agent.py --repo <repo> --world m0world \
      --task collect_wood --steps 120 --spawn-client
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# ---- 包导入引导 ----
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.brain import SmartPolicy  # noqa: E402
from mcl2_env.scripts._common import (  # noqa: E402
    PLAYER,
    RUN_ID,
    build_begin_episode_spec,
    build_renderer,
    print_log_tail,
    read_world_seed,
    render_frame_to_episode,
    start_proc,
    stop_proc,
    verify_alignment,
)

DEFAULT_REPO = "/Users/hyhpinggongzuoban/Code/fake-mc"


def run_smart_episode(
    bridge,
    renderer: Any,
    episode_id: str,
    task_id: str,
    data_root: Path,
    world_seed: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """跑一个智能决策 episode：begin -> 策略循环（执行+observe 对齐）-> end。"""
    from mcl2_env.dataset.episode_writer import EpisodeWriter

    spec = build_begin_episode_spec(
        player=PLAYER, task_id=task_id, episode_id=episode_id,
        run_id=RUN_ID, world_seed=world_seed, task_seed=args.seed,
    )
    bridge.begin_episode(spec)

    writer = EpisodeWriter(str(data_root), RUN_ID, episode_id, images_only=True)
    episode_dir = data_root / "episodes" / episode_id
    policy = SmartPolicy(bridge, player=PLAYER, task_id=task_id)

    frames_written = 0
    steps = 0
    success = False
    deadline = time.monotonic() + getattr(args, "timeout", 120.0)

    def frame_step(obs: dict[str, Any] | None) -> None:
        nonlocal frames_written
        if obs is None:
            return
        if obs.get("episode") is None:
            return  # 任务成功 flush 后不再采样
        written, _ = render_frame_to_episode(writer, renderer, obs, episode_dir)
        if written:
            frames_written += 1

    obs = bridge.observe(player=PLAYER)
    frame_step(obs)

    while steps < args.steps and time.monotonic() < deadline:
        task = obs.get("task") or {}
        if task.get("success"):
            success = True
            print(f"      task.success at step {steps}")
            break

        actions = policy.plan(obs)
        for name, aargs in actions:
            print(f"      step {steps}: execute {name} {json.dumps(aargs, ensure_ascii=False)}")
            bridge.execute(name, aargs, player=PLAYER)

        obs = bridge.observe(player=PLAYER)
        steps += 1
        frame_step(obs)
        for ev in bridge.poll_events():
            print(f"      step {steps}: event: {json.dumps(ev, ensure_ascii=False)}")

    task = obs.get("task") or {}
    if task.get("success"):
        success = True
    if not success:
        print(f"      task not completed after {steps} steps (best-effort OK)")

    bridge.end_episode(success=success, player=PLAYER)
    align_ok, checks = verify_alignment(episode_dir)
    return {
        "episode_id": episode_id,
        "task": task_id,
        "success": success,
        "steps": steps,
        "frames": frames_written,
        "align_ok": align_ok,
        "checks": checks,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M3 smart decision agent (brain policy + Lua skills)")
    p.add_argument("--repo", default=DEFAULT_REPO, help="repo root (default: %(default)s)")
    p.add_argument("--world", default="m0world", help="world name under <repo>/luanti/worlds")
    p.add_argument("--task", default="collect_wood",
                   help="task id（collect_wood/collect_stone/collect_iron_ore/kill_animal/...）")
    p.add_argument("--steps", type=int, default=120, help="max policy steps (default: %(default)s)")
    p.add_argument("--renderer", choices=["engine_fork", "voxel", "none"],
                   default="engine_fork", help="renderer (default: %(default)s)")
    p.add_argument("--spawn-client", action="store_true", help="拉起 luanti 客户端以 bot1 连接")
    p.add_argument("--external-server", action="store_true",
                   help="复用外部已启动的服务器和客户端")
    p.add_argument("--fps", type=int, default=5, help="renderer 降采样帧率 (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=180.0, help="episode timeout in seconds")
    p.add_argument("--seed", type=int, default=42, help="task_seed (default: %(default)s)")
    p.add_argument("--out", default="", help="可选：导出 webdataset 目录（缺省不导出）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).resolve()
    world_dir = repo / "luanti" / "worlds" / args.world
    server_bin = repo / "luanti" / "bin" / "luantiserver"
    client_bin = repo / "luanti" / "bin" / "luanti"
    server_conf = world_dir / "server.conf"
    logfile = world_dir / "server.log"
    data_root = world_dir / "mcl2_agent" / "data"

    if not server_bin.exists():
        print(f"FAIL: luantiserver not found: {server_bin}")
        return 1

    from mcl2_env.bridge import BridgeError, FileBridgeClient

    server = client = None
    out_d = err_d = None
    renderer = build_renderer(args.renderer, args.fps)

    try:
        if args.external_server:
            if args.spawn_client:
                print("FAIL: --external-server cannot be combined with --spawn-client")
                return 1
            print(f"[1/6] using external server (world={args.world}, task={args.task}, steps={args.steps})")
        else:
            print(f"[1/6] starting luantiserver (world={args.world}, task={args.task}, steps={args.steps})")
            server, out_d, err_d = start_proc(server_bin, [
                str(server_bin), "--world", str(world_dir),
                "--config", str(server_conf), "--logfile", str(logfile),
            ], repo / "luanti")

        if args.spawn_client:
            if not client_bin.exists():
                print(f"FAIL: luanti client not found: {client_bin}")
                return 1
            print(f"[2/6] spawning client: luanti --go --address 127.0.0.1 --port 30000 --name {PLAYER}")
            client_cfg = repo / "luanti" / "mcl2_client.conf"
            client_cmd = [str(client_bin), "--go", "--address", "127.0.0.1",
                          "--port", "30000", "--name", PLAYER]
            if client_cfg.exists():
                client_cmd += ["--config", str(client_cfg)]
            client, _, _ = start_proc(client_bin, client_cmd, repo / "luanti")

        if renderer:
            renderer.start()
            print(f"[3/6] renderer={type(renderer).__name__} started")
        print("[3/6] waiting for ready.json ...")
        bridge = FileBridgeClient(world_dir, timeout=args.timeout)
        try:
            ready = bridge.wait_ready(timeout=args.timeout)
        except BridgeError as e:
            print(f"FAIL: {e}")
            if server is not None and server.poll() is not None:
                print(f"      server exited early (rc={server.returncode})")
            print_log_tail(logfile)
            return 1
        print(f"      ready = {json.dumps(ready, ensure_ascii=False)}")

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

        episode_id = f"ep-{int(time.time()) % 1000000:06d}"
        print(f"[4/6] begin_episode task={args.task} episode={episode_id}")
        result = run_smart_episode(
            bridge, renderer, episode_id, args.task, data_root,
            read_world_seed(world_dir), args,
        )
        print(f"[5/6] episode done: steps={result['steps']} frames={result['frames']} "
              f"success={result['success']} align={'OK' if result['align_ok'] else 'FAIL'}")

        for name, ok, detail in result["checks"]:
            print(f"      [{'OK' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

        ok = result["align_ok"]
        if not ok:
            print("FAIL: episode 未通过对齐断言")
            print_log_tail(logfile)
            return 1

        if args.out:
            from mcl2_env.dataset.export import ExportConfig, export_webdataset

            out_dir = Path(args.out).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            export_webdataset(ExportConfig(source_root=str(data_root), out_dir=str(out_dir),
                                           shard_size=1000, only=(episode_id,)))
            print(f"[6/6] exported -> {out_dir}")

        print(f"PASS: smart_agent episode={episode_id} task={args.task} success={result['success']}")
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
