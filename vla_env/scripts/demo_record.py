#!/usr/bin/env python3
"""Demo 视频录制：从客户端 WS 帧流录制 agent 第一视角，驱动 collect_wood 策略（V2 A* 导航）。

与 collect_wood_agent.py 的区别：
- 帧来自游戏 framebuffer（FrameGrabber→WS），只有游戏画面、无桌面/HUD，
  且必有真实变化（不是屏幕录制的遮挡缓存帧）。
- 录帧线程消费全部 WS 帧（~30fps）存 JPEG；主线程只发动作 + gRPC 结算
  （step_fn 不用 env.step，避免 recv_frame_latest 抢走录帧线程的帧）。
- 复用 collect_wood_agent.collect_wood_policy（A* + 跳跃 3D 导航）。

用法（在 vla_env/ 目录内）：
    .venv/bin/python -u scripts/demo_record.py /tmp/vla_demo_frames [--capture native]
之后用 ffmpeg 合成（native 分辨率下不升采样）：
    ffmpeg -framerate 20 -start_number 1 -i /tmp/vla_demo_frames/f_%06d.jpg \
           -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -movflags +faststart out.mp4
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from PIL import Image

from vla_env.env import MinecraftEnv
# 本脚本以 `python scripts/demo_record.py` 运行，sys.path[0]=scripts/，
# 故 collect_wood_agent 是裸导入（scripts/ 不是包，vla_env.scripts 不可导入）。
from collect_wood_agent import collect_wood_policy  # noqa: E402


def recorder(ws, outdir, stop_flag) -> None:
    """录帧线程：消费全部 WS 帧，存 JPEG（f_%06d.jpg）。"""
    n = 0
    while not stop_flag.is_set():
        try:
            frame = ws.recv_frame(timeout=0.5)
        except Exception:  # noqa: BLE001
            continue
        if frame is None:
            continue
        Image.fromarray(frame.rgb).save(f"{outdir}/f_{n:06d}.jpg", quality=90)
        n += 1
        if n % 150 == 0:
            print(f"[rec] frames={n}", flush=True)
    print(f"[rec] done, total={n}", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="collect_wood 第一视角 demo 录制（A* 导航）")
    p.add_argument("outdir", help="帧输出目录（JPEG）")
    p.add_argument("--max-steps", type=int, default=600)
    p.add_argument("--ticks", type=int, default=2)
    p.add_argument("--half-extent", type=int, default=16)
    p.add_argument("--player", default="agent0")
    p.add_argument("--capture", default="native",
                   help="抓帧分辨率：native=游戏原始分辨率（保真+保留比例，推荐）或 WxH 如 1280x720")
    p.add_argument("--no-hud", action="store_true",
                   help="关闭 HUD 抓帧（默认开启：demo 视频含物品栏/血条/手/准星；"
                        "VLA 观测请用 --no-hud 保持纯净画面）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    env = MinecraftEnv(player=args.player, task="collect_wood", ticks_per_step=args.ticks)
    try:
        obs = None
        for attempt in range(30):
            try:
                obs, _ = env.reset()
                break
            except Exception as e:  # noqa: BLE001
                print(f"[reset] attempt {attempt + 1} failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
                time.sleep(2)
        if obs is None:
            print("DEMO_FAIL: env.reset 未成功", file=sys.stderr)
            return 1
        print(f"[reset] OK pos={obs['player']['pos']}", flush=True)

        # 切换抓帧分辨率（native → 游戏 framebuffer 原始分辨率，保真+保留比例）
        if args.capture.lower() == "native":
            env.ws.send({"cmd": "set_capture", "width": 0, "height": 0})
        else:
            w, h = (int(x) for x in args.capture.lower().split("x"))
            env.ws.send({"cmd": "set_capture", "width": w, "height": h})
        time.sleep(0.5)  # 等客户端渲染线程重建 FBO

        # M9.1：demo 视频需要完整 UI（HUD+手+准星）——切到 GameRenderer TAIL 抓帧；
        # 与 set_capture native 兼容（HUD 抓帧独立于分辨率）。
        env.ws.send({"cmd": "set_capture_ui", "hud": not args.no_hud})
        time.sleep(0.3)

        # 录帧线程（消费全部 WS 帧；主线程只发动作/调 gRPC，不读 WS）
        stop_flag = threading.Event()
        rec_thread = threading.Thread(target=recorder, args=(env.ws, args.outdir, stop_flag),
                                      daemon=True)
        rec_thread.start()

        def step_fn(action, ticks):
            """录帧版 step：发动作 + gRPC 结算，不读 WS 帧（录帧线程在消费）。"""
            env.ws.send_action(action)
            step = env.grpc.get_step_result(player=env.player, await_ticks=ticks)
            return {"progress": float(step["progress"]), "terminated": step["terminated"],
                    "truncated": step["truncated"], "reward": step["reward"]}

        ok, steps, max_progress = collect_wood_policy(
            env, step_fn, max_steps=args.max_steps, ticks=args.ticks,
            half_extent=args.half_extent)

        # 收尾：停录帧，稍等帧流排空
        time.sleep(0.5)
        stop_flag.set()
        rec_thread.join(timeout=3)

        if ok:
            print(f"DEMO_OK steps={steps} progress={max_progress:.2f}", flush=True)
            return 0
        print("DEMO_NOT_COMPLETE", file=sys.stderr, flush=True)
        return 1
    finally:
        env.close()


if __name__ == "__main__":
    sys.exit(main())
