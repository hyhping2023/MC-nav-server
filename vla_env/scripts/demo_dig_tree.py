#!/usr/bin/env python3
"""兼容入口：demo_task.py --task collect_wood 的别名（挖树寻路 demo）。

用法（在 vla_env/ 目录内）：
    .venv/bin/python -u scripts/demo_dig_tree.py [outdir] [--max-steps 600]
等价于 demo_task.py --task collect_wood；三类任务请用 demo_task.py --task。
"""

import sys

from demo_task import main

if __name__ == "__main__":
    sys.exit(main(default_task="collect_wood"))
