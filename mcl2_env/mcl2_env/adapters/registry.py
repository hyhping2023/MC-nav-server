"""适配器工厂：名称 -> ModelAdapter 实例。

- "mock"：本地冒烟（无模型）
- "openvla" / "pi0" / "groot" / "steve1"：骨架实例（不加载模型，is_available() False）
"""

from __future__ import annotations

from typing import Any

from .base import ModelAdapter


def get_adapter(name: str, **kwargs: Any) -> ModelAdapter:
    """按名称构造适配器。未知名称抛 ValueError。

    :param name: "mock" | "openvla" | "pi0" | "groot" | "steve1"
    :param kwargs: 传给构造器的可选参数（用户服务器上加载模型路径等）
    """
    name = (name or "").strip().lower()
    if name == "mock":
        from .mock import MockAdapter

        return MockAdapter(**kwargs)
    if name == "openvla":
        from .openvla import OpenVLAAdapter

        return OpenVLAAdapter(**kwargs)
    if name == "pi0":
        from .pi0 import Pi0Adapter

        return Pi0Adapter(**kwargs)
    if name == "groot":
        from .groot import GrootAdapter

        return GrootAdapter(**kwargs)
    if name == "steve1":
        from .steve1 import Steve1Adapter

        return Steve1Adapter(**kwargs)
    raise ValueError(f"unknown adapter: {name!r} (mock/openvla/pi0/groot/steve1)")
