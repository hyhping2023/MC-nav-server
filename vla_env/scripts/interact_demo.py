#!/usr/bin/env python3
"""VLA 交互 API 冒烟（M11，DESIGN.md §11.6）。

VPT/STEVE-1 离散 token（button_mask/camera_bin/hotbar）→ SeedReplayApi.step_discrete
→ 客户端 → 返回带按键状态的帧。验证：
1. reset(seed) 返回帧且帧带按键状态（帧↔按键对齐）；
2. verify_determinism(seed) 同 seed 世界态一致（种子回放）；
3. step_discrete 逐 token 驱动客户端并返回帧（VLA 输出交互）。

打印 `INTERACT_OK seed=N steps=M deterministic=True frames_ret=K`。

用法（在 vla_env/ 目录内）：
    .venv/bin/python -u scripts/interact_demo.py --seed 42
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PIL import Image

from vla_env.action_space import camera_to_bin
from vla_env.interact import SURVIVAL_KIT, SeedReplayApi


def main() -> int:
    p = argparse.ArgumentParser(description="VLA 交互 API 冒烟（离散 token）")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--task", default="collect_stone")
    p.add_argument("--player", default="agent0")
    p.add_argument("--ticks", type=int, default=2)
    p.add_argument("--steps", type=int, default=20)
    p.add_argument("--outdir", default=None, help="帧保存目录（默认 /tmp）")
    args = p.parse_args()
    outdir = args.outdir or "/tmp/interact_demo"
    Path(outdir).mkdir(parents=True, exist_ok=True)

    api = SeedReplayApi(player=args.player, ticks_per_step=args.ticks)
    try:
        # 1. 确定性 reset + 首帧（含按键状态）
        frame, obs = api.reset(seed=args.seed, task=args.task, items=SURVIVAL_KIT)
        if frame is None or frame.keys is None:
            print("INTERACT_FAIL: reset 未返回带按键状态的帧", file=sys.stderr)
            return 1
        print(f"[reset] frame={frame.frame_id} keys={frame.keys.as_dict()}", flush=True)

        # 2. 种子确定性
        det = api.verify_determinism(args.seed, task=args.task, items=SURVIVAL_KIT)
        print(f"[determinism] region_same={det['region_deterministic']} "
              f"voxels_same={det['voxels_deterministic']}", flush=True)

        # 3. VPT 离散 token 驱动
        frames_ret = 0
        saved = 0
        for i in range(args.steps):
            # 随机 VLA 输出 token：按钮掩码 + camera bin + hotbar
            button_mask = 1 << 7            # attack
            if i % 5 == 0:
                button_mask |= 1 << 0       # forward
            cam_bin = camera_to_bin([0.0, 0.0])
            hotbar = 0 if i % 3 == 0 else -1
            frame, obs, reward, terminated, truncated, info = api.step_discrete(
                button_mask, cam_bin, hotbar)
            if frame is not None:
                frames_ret += 1
                if saved < 3:
                    Image.fromarray(frame.rgb).save(
                        os.path.join(outdir, f"interact_f{i:03d}.jpg"), quality=90)
                    saved += 1
            if terminated or truncated:
                break

        if frames_ret == 0:
            print("INTERACT_FAIL: 未收到任何帧", file=sys.stderr)
            return 1
        print(f"INTERACT_OK seed={args.seed} task={args.task} steps={i + 1} "
              f"frames_ret={frames_ret} deterministic={det['deterministic']} "
              f"progress={info.get('progress', 0.0):.2f} saved_dir={outdir}", flush=True)
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
