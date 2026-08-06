"""Pi0 适配器（骨架，无推理）。

Pi0 (Physical Intelligence)：image + language instruction -> 离散动作 token 流（flow matching）。
真实推理代码在用户 GPU 服务器启用（本机无 torch）。

TODO（用户服务器）：
    from pi0 import load_pi0
    model = load_pi0("physical-intelligence/pi0-base")
"""

from __future__ import annotations

from typing import Any

from .base import ModelAdapter


class Pi0Adapter(ModelAdapter):
    """Pi0：obs -> (image, instruction)；模型输出 -> primitive 动作。"""

    name = "pi0"

    # ------------------------------------------------------------ obs -> input
    def encode_obs(self, obs: dict[str, Any]) -> Any:
        """返回 (image, instruction)。

        TODO: Pi0 使用 PALME 动作分词器，image 需按 pi0 预处理器转 tensor。
        """
        task = obs.get("task") or {}
        return obs.get("image"), task.get("instruction", "")

    # ------------------------------------------------------------ output -> action
    def decode_action(self, model_out: Any) -> dict[str, Any]:
        """Pi0 输出的离散/连续动作 -> primitive dict。

        TODO: 按 pi0 的动作 token 表映射（连续量乘 scale 到 [-1,1] 区间）。
        :param model_out: 动作 token id 列表或归一化连续向量。
        """
        act = {
            "forward": 0, "back": 0, "left": 0, "right": 0,
            "jump": 0, "sneak": 0, "sprint": 0, "attack": 0, "use": 0,
            "drop": 0, "hotbar": 0, "camera": [0.0, 0.0],
        }
        # TODO（用户服务器）：根据 pi0 实际输出维度填充 act
        return act

    def is_available(self) -> bool:
        # TODO（用户服务器）：try import pi0；成功且有权重则返回 True
        try:
            import pi0  # noqa: F401  # 在用户服务器启用
            return True
        except ImportError:
            return False
