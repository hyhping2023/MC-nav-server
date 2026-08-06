"""VLA 模型适配器包：obs ↔ 动作转换契约（m3m4_protocol.md §4.2）。

所有适配器均**不加载/不运行任何模型**。真实推理（OpenVLA/Pi0/GROOT/STEVE-1）
由用户在其 GPU 服务器上启用；本机只提供 mock 与骨架。
"""

from __future__ import annotations

from .base import ModelAdapter
from .mock import MockAdapter
from .registry import get_adapter

__all__ = ["ModelAdapter", "MockAdapter", "get_adapter"]
