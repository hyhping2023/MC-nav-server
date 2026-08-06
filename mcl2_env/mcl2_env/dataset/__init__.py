"""dataset — canonical 数据格式读写（DESIGN.md §7）。

canonical 布局：
    <run_dir>/episodes/ep-XXXXXX/
        meta.json
        instructions.jsonl
        observations/000000.png
        states.jsonl
        actions.jsonl
        rewards.jsonl
        episode_summary.json
"""

from .episode_writer import EpisodeWriter
from .export import ExportConfig, export_huggingface, export_rlds, export_webdataset

__all__ = ["EpisodeWriter", "ExportConfig", "export_webdataset", "export_huggingface", "export_rlds"]
