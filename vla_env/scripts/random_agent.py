#!/usr/bin/env python3
"""M3 验收：random_agent —— 连客户端 WS，随机动作驱动并接收像素帧。

用法：
    .venv/bin/python scripts/random_agent.py --ws-url ws://127.0.0.1:30001 --steps 100

流程：连 WS → 发 `{"cmd":"mode","mode":"api"}` → 循环 N 步：
发随机 `{"cmd":"action", forward/jump/camera/hotbar}` → `recv_frame(timeout=2s)`
→ 打印 `step=i frame_id=f shape=(224,224,3)`。

结束判定：无超时且成功接收帧数 == steps → 打印 `M3_OK frames=N` exit 0；
否则打印错误并 exit 1（超时/异常如实上报，不伪造）。
"""

from __future__ import annotations

import argparse
import random
import sys

from vla_env.client_ws import ClientWs


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M3 random_agent 验收脚本")
    p.add_argument("--ws-url", default="ws://127.0.0.1:30001",
                   help="客户端 WS 地址（默认 ws://127.0.0.1:30001）")
    p.add_argument("--steps", type=int, default=100,
                   help="随机动作步数（默认 100）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    ok = 0
    try:
        with ClientWs(url=args.ws_url) as ws:
            mode_ok = ws.send_mode("api")
            print(f"mode_ok: mode={mode_ok.get('mode')} type={mode_ok.get('type')}")
            for step in range(args.steps):
                action = {
                    "cmd": "action",
                    "forward": random.choice([0, 1]),
                    "jump": random.choice([0, 1]),
                    "camera": [random.uniform(-30.0, 30.0), random.uniform(-30.0, 30.0)],
                    "hotbar": random.randint(0, 8),
                }
                ws.send(action)
                frame = ws.recv_frame(timeout=2.0)
                if frame is None:
                    print(f"step={step} TIMEOUT (no frame within 2s)", file=sys.stderr)
                    break
                ok += 1
                print(f"step={step} frame_id={frame.frame_id} shape={frame.rgb.shape}")
    except Exception as e:  # noqa: BLE001 —— 验收脚本需捕获并如实上报
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        print("M3_FAIL", file=sys.stderr)
        return 1

    if ok == args.steps:
        print(f"M3_OK frames={ok}")
        return 0

    print(f"M3_FAIL frames={ok}/{args.steps} (timeout/dropped)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
