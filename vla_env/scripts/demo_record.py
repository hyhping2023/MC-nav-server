#!/usr/bin/env python3
"""Demo 视频录制：从客户端 WS 帧流录制 agent 第一视角，驱动 collect_wood 策略。

与 collect_wood_agent.py 的区别：
- 帧来自游戏 framebuffer（FrameGrabber→WS），只有游戏画面、无桌面/HUD，
  且必有真实变化（不是屏幕录制的遮挡缓存帧）。
- 录帧线程消费全部 WS 帧（~30fps）存 JPEG；主线程只发动作 + gRPC 结算
  （不用 env.step，避免 recv_frame_latest 抢走录帧线程的帧）。

用法（在 vla_env/ 目录内）：
    .venv/bin/python -u scripts/demo_record.py /tmp/vla_demo_frames [--max-steps 400]
之后用 ffmpeg 合成：ffmpeg -framerate 30 -i /tmp/vla_demo_frames/f_%06d.jpg ...
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import numpy as np
from PIL import Image

from vla_env.env import MinecraftEnv
# 本脚本以 `python scripts/demo_record.py` 运行，sys.path[0]=scripts/，
# 故 collect_wood_agent 是裸导入（scripts/ 不是包，vla_env.scripts 不可导入）。
from collect_wood_agent import (  # 复用 collect_wood 策略工具
    EYE_HEIGHT,
    OAK_LOG,
    REACH,
    WANDER_STEPS,
    find_logs,
    look_at,
    select_target,
    dist3,
)


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
        Image.fromarray(frame.rgb).save(f"{outdir}/f_{n:06d}.jpg", quality=85)
        n += 1
        if n % 150 == 0:
            print(f"[rec] frames={n}", flush=True)
    print(f"[rec] done, total={n}", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="collect_wood 第一视角 demo 录制")
    p.add_argument("outdir", help="帧输出目录（JPEG）")
    p.add_argument("--max-steps", type=int, default=400)
    p.add_argument("--ticks", type=int, default=2)
    p.add_argument("--half-extent", type=int, default=16)
    p.add_argument("--player", default="agent0")
    p.add_argument("--capture", default="native",
                   help="抓帧分辨率：native=游戏原始分辨率（保真+保留比例，推荐）或 WxH 如 1280x720")
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

        # M7.2：切换抓帧分辨率（native → 游戏 framebuffer 原始分辨率，保真+保留比例）
        if args.capture.lower() == "native":
            env.ws.send({"cmd": "set_capture", "width": 0, "height": 0})
        else:
            w, h = (int(x) for x in args.capture.lower().split("x"))
            env.ws.send({"cmd": "set_capture", "width": w, "height": h})
        # 等客户端渲染线程重建 FBO（下一帧起生效）
        time.sleep(0.5)

        # 录帧线程（消费全部 WS 帧；主线程只发动作/调 gRPC，不读 WS）
        stop_flag = threading.Event()
        rec_thread = threading.Thread(target=recorder, args=(env.ws, args.outdir, stop_flag),
                                      daemon=True)
        rec_thread.start()

        # ---- collect_wood 策略（与 collect_wood_agent.py 同逻辑，但不用 env.step）----
        mode = "none"
        target = None
        settle_steps = 0
        dig_try = 0
        after_break_pause = 0
        wander_left = 0
        wander_yaw = 0.0
        wander_jump = 0
        stuck_count = 0
        last_pos = tuple(obs["player"]["pos"])
        max_progress = 0.0
        logs_found = 0
        aim_cache = None

        for step in range(1, args.max_steps + 1):
            state = env.grpc.get_state(player=args.player)
            px, py, pz = (float(v) for v in state["player"]["pos"])
            last_pos_prev = last_pos
            last_pos = (px, py, pz)
            dist = -1.0

            if after_break_pause > 0:
                after_break_pause -= 1
                action = {"camera": [0.0, 0.0]}
                mode = "none"
                target = None
            elif wander_left > 0:
                wander_left -= 1
                env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                if wander_jump > 0:
                    wander_jump -= 1
                    action = {"forward": True, "jump": True, "camera": [0.0, 0.0]}
                else:
                    action = {"forward": True, "camera": [0.0, 0.0]}
                mode = "none"
            elif mode == "attack" and target is not None:
                bx, by, bz = target
                dist = dist3(px, py, pz, bx, by, bz)
                dig_try += 1
                if dig_try % 12 == 0:
                    palette, data, origin, _ = env.grpc.get_voxels(
                        player=args.player, half_extent=args.half_extent)
                    logs = find_logs(palette, data, origin)
                    if target not in logs:
                        after_break_pause = 2
                        mode = "none"
                        target = None
                        aim_cache = None
                        action = {"camera": [0.0, 0.0]}
                    else:
                        action = {"attack": True, "camera": [0.0, 0.0]}
                elif dig_try > 90:
                    print(f"  [dig] give up target {target}", flush=True)
                    mode = "none"
                    target = None
                    aim_cache = None
                    action = {"camera": [0.0, 0.0]}
                elif settle_steps < 3:
                    if settle_steps == 0:
                        yaw, pitch = look_at(px, py, pz, bx, by, bz)
                        env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                        aim_cache = (yaw, pitch)
                    settle_steps += 1
                    action = {"camera": [0.0, 0.0]}
                else:
                    action = {"attack": True, "camera": [0.0, 0.0]}
            else:
                palette, data, origin, size = env.grpc.get_voxels(
                    player=args.player, half_extent=args.half_extent)
                logs = find_logs(palette, data, origin)
                logs_found = len(logs)
                new_mode, new_target = select_target(logs, px, py, pz)
                if new_mode == "none":
                    wander_left = WANDER_STEPS
                    wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                    env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                    action = {"forward": True, "camera": [0.0, 0.0]}
                    mode = "none"
                    target = None
                else:
                    mode, target = new_mode, new_target
                    bx, by, bz = target
                    dist = dist3(px, py, pz, bx, by, bz)
                    if mode == "attack":
                        settle_steps = 0
                        dig_try = 0
                        aim_cache = None
                        yaw, pitch = look_at(px, py, pz, bx, by, bz)
                        env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                        aim_cache = (yaw, pitch)
                        settle_steps = 1
                        action = {"camera": [0.0, 0.0]}
                        print(f"  [attack] target={target} dist={dist:.2f}", flush=True)
                    else:
                        yaw, pitch = look_at(px, py, pz, bx, by, bz, pitch_clamp=30.0)
                        env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                        action = {"forward": True, "camera": [0.0, 0.0]}
                        print(f"  [approach] target={target} dist={dist:.2f}", flush=True)

            # 发动作 + 服务端结算（不读 WS 帧——录帧线程在消费）
            env.ws.send_action(action)
            step_res = env.grpc.get_step_result(player=args.player, await_ticks=args.ticks)
            progress = float(step_res["progress"])
            max_progress = max(max_progress, progress)

            moved = np.hypot(px - last_pos_prev[0], pz - last_pos_prev[2])
            if moved < 0.02 and mode != "attack" and after_break_pause == 0:
                stuck_count += 1
            else:
                stuck_count = 0
            if stuck_count >= 20:
                print(f"  [stuck] 游走", flush=True)
                wander_left = WANDER_STEPS
                wander_jump = 6
                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                mode = "none"
                target = None
                aim_cache = None
                stuck_count = 0

            if step % 20 == 0 or step_res["terminated"]:
                print(f"step={step} progress={progress:.2f} max={max_progress:.2f} "
                      f"mode={mode} logs={logs_found}", flush=True)
            if step_res["terminated"]:
                print(f"DEMO_OK steps={step} progress={progress:.2f}", flush=True)
                break
            if step_res["truncated"]:
                print("DEMO_TIMEOUT", file=sys.stderr, flush=True)
                break

        # 收尾：停录帧，稍等帧流排空
        time.sleep(0.5)
        stop_flag.set()
        rec_thread.join(timeout=3)
        return 0
    finally:
        env.close()


if __name__ == "__main__":
    sys.exit(main())
