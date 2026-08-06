#!/usr/bin/env python3
"""M2 random agent：真实玩家驱动 + 视觉观测循环 + 帧/状态对齐。

契约：docs/m2_protocol.md（§1 请求式采样对齐、§2 env 字段）；文件 IPC 与
episode 布局保持 M0（docs/m0_protocol.md §3/§5）。

流程：
  1. 启动 luantiserver 子进程（worldmod mcl2_agent，文件 IPC）。
  2. 可选 --spawn-client：拉起 luanti 客户端，以 bot1 连接。
  3. 等 ready.json → begin_episode(task，含 env 字段) → 随机原始动作循环
     N 步（step + observe）→ 每步从 renderer 取帧并按 states.jsonl 当前行
     的 frame 号写 PNG → 成功/超时 → end_episode。
  4. 对齐断言（M2 验收核心）：states 行数 == actions == rewards == PNG 数，
     且每行 image 引用存在；meta.json 含 env.engine/game/python 字段。

renderer：
  engine_fork  CompositeRenderer(真实帧优先, voxel 回退)——每步都有帧
  voxel       纯体素合成
  none        无渲染器（无 PNG，对齐断言会 FAIL——按协议这是正确行为）

用法：
  python mcl2_env/mcl2_env/scripts/random_agent.py --repo <repo> --world m0world \
      --renderer engine_fork --spawn-client --steps 120
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

# ---- 包导入引导：兼容 `python -m`、直接运行、以及轻依赖环境 ----
# 把 mcl2_env/（仓库内包目录）插入 sys.path，供 `from mcl2_env...` 解析。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.scripts._common import (
    IMG_STD_THRESHOLD,
    PLAYER,
    RUN_ID,
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M2 random agent (file IPC + renderer + alignment)")
    p.add_argument("--repo", default=DEFAULT_REPO, help="repo root (default: %(default)s)")
    p.add_argument("--world", default="m0world", help="world name under <repo>/luanti/worlds")
    p.add_argument("--steps", type=int, default=60, help="random primitive steps (default: %(default)s)")
    p.add_argument("--task", default="collect_wood", help="task id (default: %(default)s)")
    p.add_argument("--renderer", choices=["engine_fork", "voxel", "none"],
                   default="engine_fork",
                   help="renderer; engine_fork 无帧时自动回退 voxel (default: %(default)s)")
    p.add_argument("--spawn-client", action="store_true",
                   help="额外拉起 luanti 客户端以 bot1 连接（独立验证用）")
    p.add_argument("--fps", type=int, default=5, help="renderer 降采样帧率 (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=120.0, help="overall timeout in seconds")
    p.add_argument("--seed", type=int, default=42, help="task_seed / rng seed (default: %(default)s)")
    p.add_argument("--pos-threshold", type=float, default=0.5,
                   help="判定 player 发生移动的最小位移 (default: %(default)s)")
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
        print(f"FAIL: luantiserver not found: {server_bin} (is luanti/build ready?)")
        return 1

    from mcl2_env.bridge import BridgeError, FileBridgeClient

    server = client = None
    out_d = err_d = None
    renderer = build_renderer(args.renderer, args.fps)

    try:
        # ---- 1) 启动服务器 ----
        print(f"[1/6] starting luantiserver (world={args.world}, task={args.task}, steps={args.steps})")
        server, out_d, err_d = start_proc(server_bin, [
            str(server_bin),
            "--world", str(world_dir),
            "--config", str(server_conf),
            "--logfile", str(logfile),
        ], repo / "luanti")

        # ---- 2) 可选：拉起客户端 ----
        if args.spawn_client:
            if not client_bin.exists():
                print(f"FAIL: luanti client not found: {client_bin}")
                return 1
            print(f"[2/6] spawning client: luanti --go --address 127.0.0.1 --port 30000 --name {PLAYER}")
            client_cfg = repo / "luanti" / "mcl2_client.conf"
            client_cmd = [
                str(client_bin),
                "--go", "--address", "127.0.0.1", "--port", "30000", "--name", PLAYER,
            ]
            if client_cfg.exists():
                client_cmd += ["--config", str(client_cfg)]
            client, _, _ = start_proc(client_bin, client_cmd, repo / "luanti")

        # ---- 3) 渲染器 + 等待 ready.json ----
        if renderer:
            renderer.start()
            avail = getattr(renderer, "available", False)
            print(f"[3/6] renderer={type(renderer).__name__} started (available={avail})")
        print("[3/6] waiting for ready.json ...")
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

        # ---- 3.5) 等待 bot1 会话 ----
        print("[3.5/6] waiting for player session ...")
        player_ready = False
        player_deadline = time.monotonic() + args.timeout
        while time.monotonic() < player_deadline:
            try:
                probe = bridge.observe(player=PLAYER)
            except BridgeError:
                probe = None
            if isinstance(probe, dict) and probe.get("player"):
                player_ready = True
                break
            time.sleep(0.5)
        if not player_ready:
            print(f"FAIL: player {PLAYER} did not connect within {args.timeout:.0f}s "
                  f"(--spawn-client or integration must attach the client)")
            print_log_tail(logfile)
            return 1
        print(f"      player {PLAYER} connected")

        # ---- 4) begin_episode（含 m2_protocol §2 env 字段）----
        episode_id = f"ep-{int(time.time()) % 1000000:06d}"
        spec = build_begin_episode_spec(
            player=PLAYER,
            task_id=args.task,
            episode_id=episode_id,
            run_id=RUN_ID,
            world_seed=read_world_seed(world_dir),
            task_seed=args.seed,
        )
        print(f"[4/6] begin_episode task={args.task} episode={episode_id}")
        try:
            bridge.begin_episode(spec)
        except BridgeError as e:
            print(f"FAIL: begin_episode: {e}")
            print(f"      需要玩家会话：--spawn-client 或集成流程已拉起客户端 (bot1)")
            print_log_tail(logfile)
            return 1
        print("      begin_episode ok (engine/game/python env 已随请求下发)")

        # ---- 5) 随机原始动作循环（每次 observe 对齐一行 states + 一帧 PNG）----
        from mcl2_env.dataset.episode_writer import EpisodeWriter

        writer = EpisodeWriter(str(data_root), RUN_ID, episode_id, images_only=True)
        episode_dir = data_root / "episodes" / episode_id
        rng = random.Random(args.seed)
        positions: list[tuple[float, float, float]] = []
        max_disp = 0.0
        frames_written = 0
        img_checked = img_passed = 0
        success = False
        last_pos = None
        deadline = time.monotonic() + args.timeout

        print(f"[5/6] running {args.steps} random primitive steps ...")
        steps_done = 0
        for _ in range(args.steps):
            if time.monotonic() > deadline:
                print("      truncated by timeout")
                break
            steps_done += 1

            bridge.step(random_primitive(rng), player=PLAYER)
            obs = bridge.observe(player=PLAYER)

            # voxel 类渲染器需要每步注入相机 + 体素网格
            if renderer is not None and hasattr(renderer, "set_camera"):
                pl = obs.get("player") or {}
                wd = obs.get("world") or {}
                renderer.set_camera(pl.get("pos"), pl.get("look"), wd.get("voxels"))

            # 取帧 + 按当前 states 行的 frame 号写 PNG
            frame = renderer.get_frame() if renderer else None
            obs["image"] = frame.image if frame is not None else None
            if frame is not None:
                frame_no = resolve_frame(episode_dir, obs)
                if frame_no is not None:
                    img = frame.image
                    std = float(img.std())
                    img_checked += 1
                    if std > IMG_STD_THRESHOLD:
                        img_passed += 1
                    writer.write_frame(img, frame_no)
                    frames_written += 1

            # player.pos 变化跟踪
            pos = (obs.get("player") or {}).get("pos")
            if pos:
                cur = (pos["x"], pos["y"], pos["z"])
                positions.append(cur)
                if last_pos is not None:
                    max_disp = max(max_disp, pos_distance(last_pos, cur))
                last_pos = cur

            task = obs.get("task") or {}
            if task.get("success"):
                success = True
                print(f"      task.success at step {steps_done}")
                break

            for ev in bridge.poll_events():
                print(f"      event: {json.dumps(ev, ensure_ascii=False)}")

        print(f"      done: {steps_done} steps, max player displacement={max_disp:.2f}, "
              f"frames written={frames_written}")

        # ---- 6) end_episode + 对齐断言 ----
        print(f"[6/6] end_episode(success={success})")
        bridge.end_episode(success=success, player=PLAYER)

        obs_dir = episode_dir / "observations"
        pngs = sorted(obs_dir.glob("*.png")) if obs_dir.is_dir() else []

        checks: list[tuple[str, bool, str]] = []
        checks.append(("episode dir exists", episode_dir.is_dir(), str(episode_dir)))
        checks.append(("observations/*.png non-empty", len(pngs) > 0, f"{len(pngs)} png(s)"))
        if len(positions) >= 2:
            checks.append(("player.pos moved", max_disp > args.pos_threshold,
                           f"max_disp={max_disp:.2f}"))
        else:
            checks.append(("player.pos tracked", True, "insufficient samples, skipped"))
        if frames_written > 0:
            checks.append(("image non-solid (std>10)", img_checked > 0 and img_passed > 0,
                           f"passed {img_passed}/{img_checked}"))
        else:
            checks.append(("image std>10", True, "no frames available, skipped (degraded)"))

        # M2 验收核心：帧/状态对齐断言
        _, align_checks = verify_alignment(episode_dir)
        checks.extend(align_checks)

        all_ok = True
        for name, ok, detail in checks:
            print(f"      [{'OK' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
            if not ok:
                all_ok = False

        if all_ok:
            print(f"PASS: random_agent episode={episode_id} completed (aligned)")
            return 0
        print("FAIL: one or more checks failed (see above)")
        print_log_tail(logfile)
        return 1

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
