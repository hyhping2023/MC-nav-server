#!/usr/bin/env python3
"""视频 demo：自动寻路 + 挖树。

启动 server+client（真实画面）→ 预热等待区块加载/网格化完成 → 跑 scripted_agent
collect_wood（自动寻路挖树）→ 同时从共享内存抓帧 → ffmpeg 合成 mp4。

预热阶段用 FileBridgeClient 等 server ready + bot1 会话，再 sleep --warmup 秒，
避免早帧拍到的区块还是空白；demo 自管 client（不额外拉起第二个客户端），防止
重复登录 bot1 挤掉会话并双写 /tmp/mcl2_frames。
"""
from __future__ import annotations
import subprocess, sys, time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
from mcl2_env.renderer.engine_fork import EngineForkRenderer  # noqa: E402

DEFAULT_REPO = "/Users/hyhpinggongzuoban/Code/fake-mc"
WORLD = "m0world"


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--world", default=WORLD)
    p.add_argument("--task", default="collect_wood")
    p.add_argument("--steps", type=int, default=40)
    p.add_argument("--out", default="datasets/demo_dig_tree.mp4")
    p.add_argument("--fps", type=float, default=10.0, help="抓帧帧率")
    p.add_argument("--timeout", type=float, default=240.0)
    p.add_argument("--warmup", type=float, default=6.0, help="等待区块加载完成的预热秒数")
    args = p.parse_args()

    repo = Path(args.repo).resolve()
    world = repo / "luanti" / "worlds" / args.world
    frames_dir = Path("/tmp/mcl2_demo_frames")
    if frames_dir.exists():
        for f in frames_dir.glob("*.png"):
            f.unlink()
    frames_dir.mkdir(parents=True, exist_ok=True)

    # 1) 启动 server + client（真实客户端，抓帧开启）
    server = subprocess.Popen([str(repo/"luanti/bin/luantiserver"), "--world", str(world),
        "--config", str(world/"server.conf"), "--logfile", str(world/"server.log")],
        cwd=str(repo/"luanti"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = subprocess.Popen([str(repo/"luanti/bin/luanti"), "--go", "--address","127.0.0.1",
        "--port","30000","--name","bot1","--config", str(repo/"luanti/mcl2_client.conf")],
        cwd=str(repo/"luanti"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    renderer = EngineForkRenderer(fps=args.fps)
    renderer.start()

    # 1.5) 预热：等 server ready + bot1 会话 + 区块网格化（agent 不再自行拉起客户端，
    #      避免重复登录 bot1 挤掉会话并双写 /tmp/mcl2_frames）
    try:
        from mcl2_env.bridge import BridgeError, FileBridgeClient
        bridge = FileBridgeClient(world, timeout=args.timeout)
        bridge.wait_ready(timeout=args.timeout)
        print("warmup: server ready")
        print("warmup: waiting for player bot1 session ...")
        player_ready = False
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                probe = bridge.observe(player="bot1")
            except BridgeError:
                probe = None
            if isinstance(probe, dict) and probe.get("player"):
                player_ready = True
                break
            time.sleep(0.5)
        if not player_ready:
            raise BridgeError(f"player bot1 did not connect within {args.timeout:.0f}s")
        print("warmup: player bot1 connected")
        print(f"warmup: waiting {args.warmup}s for chunk meshing ...")
        time.sleep(args.warmup)
    except Exception as e:
        print(f"FAIL: warmup failed: {e}")
        if server.poll() is not None:
            print(f"      server exited early (rc={server.returncode})")
        renderer.stop()
        client.terminate(); server.terminate()
        for proc in (client, server):
            try: proc.wait(timeout=5)
            except Exception: proc.kill()
        return 1

    # 2) 后台跑 smart_agent（只选择可达原木，避免卡在树冠高度）
    agent = subprocess.Popen(["python3", "mcl2_env/mcl2_env/scripts/smart_agent.py",
        "--repo", str(repo), "--world", args.world, "--task", args.task,
        "--renderer", "engine_fork", "--external-server",
        "--steps", str(args.steps), "--timeout", str(args.timeout)],
        cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # 3) 抓帧循环（保存非重复帧）
    print("capturing frames ...")
    last = None
    count = 0
    while agent.poll() is None:
        f = renderer.get_frame()
        if f is not None:
            img = f.image
            # 跳过几乎相同的帧，节省体积
            import numpy as np
            if last is None or float(np.abs(img.astype(int)-last.astype(int)).mean()) > 2.0:
                from PIL import Image
                Image.fromarray(img).save(frames_dir / f"frame_{count:05d}.png")
                count += 1
                last = img.copy()
        time.sleep(0.1)

    agent_output, _ = agent.communicate(timeout=60)
    print(agent_output, end="")
    agent_ok = agent.returncode == 0 and "success=True" in agent_output
    renderer.stop()
    client.terminate(); server.terminate()
    for proc in (client, server):
        try: proc.wait(timeout=5)
        except Exception: proc.kill()

    n = len(list(frames_dir.glob("*.png")))
    print(f"captured {n} frames")
    if not agent_ok:
        print("FAIL: smart_agent did not complete collect_wood successfully")
        return 1

    # 4) ffmpeg 合成视频
    out = (repo / args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.run([
        "ffmpeg", "-y", "-framerate", str(args.fps),
        "-i", str(frames_dir / "frame_%05d.png"),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-vf", "scale=448:448", str(out),
    ], capture_output=True, text=True)
    if ff.returncode != 0:
        print("ffmpeg failed:", ff.stderr[-500:])
        return 1
    print(f"video saved: {out} ({out.stat().st_size//1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
