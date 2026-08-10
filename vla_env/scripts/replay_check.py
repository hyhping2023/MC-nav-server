#!/usr/bin/env python3
"""M11 种子确定性验收（DESIGN.md §11.6）。

同 seed 两次 ResetWorld → 对比区域 checksum + 体素指纹（应完全一致）；
结束打印 `SEED_REPLAY_OK seed=N region_same=True voxels_same=True`。

用法（在 vla_env/ 目录内）：
    .venv/bin/python -u scripts/replay_check.py --seed 42 --task collect_stone
"""

from __future__ import annotations

import argparse
import sys

from vla_env.interact import SURVIVAL_KIT, SeedReplayApi


def main() -> int:
    p = argparse.ArgumentParser(description="M11 种子确定性验收")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--task", default="collect_stone")
    p.add_argument("--player", default="agent0")
    p.add_argument("--half-extent", type=int, default=16)
    args = p.parse_args()

    api = SeedReplayApi(player=args.player)
    try:
        det = api.verify_determinism(
            args.seed, task=args.task, items=SURVIVAL_KIT, half_extent=args.half_extent)
        print(f"seed={args.seed} checksums={det['checksums']}", flush=True)
        print(f"voxel_fps_same={det['voxels_deterministic']} "
              f"region_same={det['region_deterministic']}", flush=True)
        if not det["deterministic"]:
            print("SEED_REPLAY_FAIL: 同 seed 两次 reset 世界态不一致", file=sys.stderr)
            return 1
        print(f"SEED_REPLAY_OK seed={args.seed} task={args.task} "
              f"region_same={det['region_deterministic']} "
              f"voxels_same={det['voxels_deterministic']}", flush=True)
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
