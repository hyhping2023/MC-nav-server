"""vla_env.dataset —— 轨迹数据管线（Oracle 生成器配套，DESIGN.md §11.5）。

- schema.py：语义标签枚举/构造/JSONL helper
- oracle_recorder.py：StepRecorder（逐帧落盘 + 对齐断言 + meta）
"""

from . import schema  # noqa: F401
