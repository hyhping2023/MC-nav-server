#!/usr/bin/env python3
"""连续伐木 demo：bot 在世界里反复执行 collect_wood，持续采伐不停止。

循环 begin_episode(collect_wood) → SmartPolicy 采伐（智能寻路 + 挖树）→
任务 success → 自动开启下一轮。server + 真实客户端全程保持连接，客户端窗口
可实时观看；engine_fork 渲染器每步写帧并做 M2 对齐断言。

用法：
  python mcl2_env/mcl2_env/scripts/demo_continuous_wood.py \
      --world m0world --max-episodes 0 --steps 120

  --max-episodes 0 = 无限循环（Ctrl+C 停止）；--record 结束时合成 mp4。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# ---- 包导入引导 ----
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.scripts._common import (  # noqa: E402
    PLAYER,
    build_renderer,
    print_log_tail,
    read_world_seed,
    start_proc,
    stop_proc,
)
from mcl2_env.scripts.smart_agent import run_smart_episode  # noqa: E402

DEFAULT_REPO = "/Users/hyhpinggongzuoban/Code/fake-mc"
TASK = "collect_wood"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="连续伐木 demo（循环 collect_wood episode）")
    p.add_argument("--repo", default=DEFAULT_REPO, help="repo root (default: %(default)s)")
    p.add_argument("--world", default="m0world", help="world name (default: %(default)s)")
    p.add_argument("--max-episodes", type=int, default=0,
                   help="最多跑几轮 episode；0 = 无限直到 Ctrl+C (default: %(default)s)")
    p.add_argument("--steps", type=int, default=120, help="单轮 episode 最大策略步数 (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=120.0, help="单轮 episode 超时秒数 (default: %(default)s)")
    p.add_argument("--renderer", choices=["engine_fork", "voxel", "none"],
                   default="engine_fork", help="渲染器 (default: %(default)s)")
    p.add_argument("--fps", type=int, default=5, help="渲染器降采样帧率 (default: %(default)s)")
    p.add_argument("--warmup", type=float, default=4.0, help="区块加载预热秒数 (default: %(default)s)")
    p.add_argument("--no-spawn-client", action="store_true", help="不拉起真实客户端窗口")
    p.add_argument("--external-server", action="store_true",
                   help="复用外部已启动的服务器和客户端")
    p.add_argument("--seed", type=int, default=42, help="task_seed (default: %(default)s)")
    p.add_argument("--record", default="", metavar="OUT.mp4",
                   help="可选：结束时把抓到的帧合成 mp4（默认不录）")
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
    frames_dir = Path("/tmp/mcl2_demo_frames")

    try:
        if args.external_server:
            print(f"[1/5] using external server (world={args.world})")
        else:
            print(f"[1/5] starting luantiserver (world={args.world})")
            server, out_d, err_d = start_proc(server_bin, [
                str(server_bin), "--world", str(world_dir),
                "--config", str(server_conf), "--logfile", str(logfile),
            ], repo / "luanti")

        if not args.no_spawn_client:
            if not client_bin.exists():
                print(f"FAIL: luanti client not found: {client_bin}")
                return 1
            print(f"[2/5] spawning client: luanti --go --address 127.0.0.1 --port 30000 --name {PLAYER}")
            client_cfg = repo / "luanti" / "mcl2_client.conf"
            client_cmd = [str(client_bin), "--go", "--address", "127.0.0.1",
                          "--port", "30000", "--name", PLAYER]
            if client_cfg.exists():
                client_cmd += ["--config", str(client_cfg)]
            client, _, _ = start_proc(client_bin, client_cmd, repo / "luanti")

        if renderer:
            renderer.start()
            print(f"[3/5] renderer={type(renderer).__name__} started")
        print("[3/5] waiting for ready.json ...")
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

        print("[3.5/5] waiting for player session ...")
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
        if args.warmup > 0:
            print(f"      warmup {args.warmup:.0f}s for chunk meshing ...")
            time.sleep(args.warmup)

        world_seed = read_world_seed(world_dir)

        if args.record:
            if frames_dir.exists():
                for f in frames_dir.glob("*.png"):
                    f.unlink()
            frames_dir.mkdir(parents=True, exist_ok=True)

        print(f"[4/5] loop episodes: task={TASK} "
              f"max_episodes={args.max_episodes or '∞'} steps/ep={args.steps}")
        n = 0
        total_steps = 0
        ok_episodes = 0
        logs_total = 0
        try:
            while args.max_episodes == 0 or n < args.max_episodes:
                n += 1
                episode_id = f"ep-{int(time.time() * 10) % 10000000:07d}"
                print(f"\n=== episode #{n} {episode_id} begin ===")
                result = run_smart_episode(
                    bridge, renderer, episode_id, TASK, data_root,
                    world_seed, args,
                )
                total_steps += result["steps"]
                logs_total += _count_logs(bridge)
                if result["success"]:
                    ok_episodes += 1
                print(f"=== episode #{n} done: steps={result['steps']} "
                      f"frames={result['frames']} success={result['success']} "
                      f"align={'OK' if result['align_ok'] else 'FAIL'} "
                      f"(cumulative: ok={ok_episodes} steps={total_steps} logs~{logs_total}) ===")

                if args.record and renderer is not None:
                    _capture_frames(renderer, frames_dir)

                if not result["align_ok"]:
                    print(f"FAIL: episode {episode_id} 未通过对齐断言，停止循环")
                    print_log_tail(logfile)
                    break

                if n % 5 == 0:
                    print(f"--- progress: {n} episodes, {ok_episodes} ok, "
                          f"{total_steps} steps, ~{logs_total} logs ---")
        except KeyboardInterrupt:
            print(f"\nCtrl+C: stopping after {n} episodes")

        print(f"[5/5] demo finished: {n} episodes, {ok_episodes} success, "
              f"{total_steps} steps, ~{logs_total} logs collected")
        if args.record:
            ok_video = _compose_video(frames_dir, repo / args.record, args.fps)
            if not ok_video:
                return 1
        return 0 if ok_episodes > 0 else 1

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


def _count_logs(bridge) -> int:
    """当前会话玩家背包中的原木数量（best-effort，失败返回 0）。"""
    try:
        obs = bridge.observe(player=PLAYER)
        inv = (obs.get("player") or {}).get("inventory") or {}
        items = inv.get("main") or {}
        count = 0
        for stack in items:
            if isinstance(stack, dict) and stack.get("name") == "mcl_trees:tree_oak":
                count += int(stack.get("count") or 1)
        return count
    except Exception:
        return 0


def _capture_frames(renderer, frames_dir: Path) -> None:
    """从渲染器取一帧存到 /tmp（供视频合成）。"""
    try:
        frame = renderer.get_frame()
        if frame is None:
            return
        from PIL import Image
        n = len(list(frames_dir.glob("*.png")))
        Image.fromarray(frame.image).save(frames_dir / f"frame_{n:05d}.png")
    except Exception:
        pass


def _compose_video(frames_dir: Path, out: Path, fps: float) -> bool:
    if not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    n = len(list(frames_dir.glob("*.png")))
    if n == 0:
        print(f"WARN: no frames captured, skip video")
        return True
    print(f"composing video from {n} frames -> {out}")
    ff = subprocess.run([
        "ffmpeg", "-y", "-framerate", str(fps),
        "-i", str(frames_dir / "frame_%05d.png"),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-vf", "scale=448:448", str(out),
    ], capture_output=True, text=True)
    if ff.returncode != 0:
        print("ffmpeg failed:", ff.stderr[-500:])
        return False
    print(f"video saved: {out} ({out.stat().st_size // 1024} KB)")
    return True


if __name__ == "__main__":
    sys.exit(main())
