"""ModelAdapter 抽象基类：统一 VLA 模型 ↔ 环境的转换契约。

本模块**不加载任何模型**。真实推理（OpenVLA / Pi0 / GROOT / STEVE-1 / LLM）
由用户在其 GPU 服务器上实现：继承本基类并实现 `encode_obs` / `decode_action`，
再通过 `registry.get_adapter(name)` 注入 VLAServer 或 AgentClient 使用。

契约（m3m4_protocol.md §4.2）：
    encode_obs(obs)      obs -> 模型输入（图像 / tokens / 文本）
    decode_action(out)   模型输出 -> 环境动作 dict（primitive 或 semantic）
    is_available()       True 表示模型权重已加载、可推理
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ModelAdapter(ABC):
    """统一模型适配器接口。本机所有实现 `is_available()` 恒为 False（不推理）。"""

    name: str = "base"

    @abstractmethod
    def encode_obs(self, obs: dict[str, Any]) -> Any:
        """观测 -> 模型输入。obs 为 `_obs_to_json` 输出（JSON 可序列化）。

        :param obs: 完整观测 dict（含 image / player / world / task / ...）
        :return: 任意模型输入（numpy 张量、token id 序列、文本等）
        """

    @abstractmethod
    def decode_action(self, model_out: Any) -> dict[str, Any]:
        """模型输出 -> 环境动作。返回可直接 POST /step 的动作 dict。

        primitive 示例：{"forward": 1, "jump": 0, "camera": [0.1, -0.2]}
        semantic 示例：{"id": "goto", "args": {"pos": {"x": 2, "y": 40, "z": 0}}}
        """

    def is_available(self) -> bool:
        """是否已加载真实模型权重。未接入时恒 False。"""
        return False
