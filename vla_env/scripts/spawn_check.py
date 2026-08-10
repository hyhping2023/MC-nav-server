#!/usr/bin/env python3
"""M11.5 自定义出生点冒烟（难点③）：reset(spawn=...) 后玩家应落在指定点附近。

用法（vla_env/ 内）：.venv/bin/python -u scripts/spawn_check.py --x -20 --y 71 --z -140
打印 `SPAWN_OK dist=<到指定点的水平距离>`（≤2 判过）。
"""

from __future__ import annotations

import argparse
import math
import sys

from vla_env.interact import SeedReplayApi
from vla_env.tasks import SURVIVAL_KIT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--x", type=float, required=True)
    ap.add_argument("--y", type=float, required=True)
    ap.add_argument("--z", type=float, required=True)
    ap.add_argument("--yaw", type=float, default=90.0)
    ap.add_argument("--task", default="collect_stone")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    api = SeedReplayApi(player="agent0", ticks_per_step=2)
    try:
        frame, obs = api.reset(seed=args.seed, task=args.task, items=SURVIVAL_KIT,
                               spawn=(args.x, args.y, args.z, args.yaw))
        px, py, pz = (float(v) for v in obs["player"]["pos"])
        d_xz = math.hypot(px - args.x, pz - args.z)
        ok = d_xz <= 2.0
        print(f"pos=({px:.1f}, {py:.1f}, {pz:.1f}) want=({args.x}, {args.y}, {args.z}) "
              f"dist_xz={d_xz:.2f}")
        print(f"{'SPAWN_OK' if ok else 'SPAWN_FAIL'} dist={d_xz:.2f}", flush=True)
        return 0 if ok else 1
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
