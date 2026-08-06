"""OpenVLA 适配器（骨架，无推理）。

OpenVLA：openvla.openvla 模型，输入 image + 任务指令（文本），输出离散动作 token 序列。
真实推理代码在用户 GPU 服务器启用（本机无 torch）。

TODO（用户服务器）：
    from transformers import AutoProcessor, AutoModelForVision2Seq
    model = AutoModelForVision2Seq.from_pretrained("openvla/openvla-7b")
    processor = AutoProcessor.from_pretrained("openvla/openvla-7b")
"""

from __future__ import annotations

from typing import Any

from .base import ModelAdapter

# 动作 token 的 MineStudio 风格映射：索引 -> (字段, 候选值)
# OpenVLA 输出 "forward 0/1, back 0/1, ..., camera 左右/上下 0..64, hotbar 0..8"
_ACTION_TOKEN_MAP: dict[int, str] = {
    0: "noop", 1: "forward", 2: "back", 3: "left", 4: "right",
    5: "jump", 6: "sneak", 7: "sprint", 8: "attack", 9: "use",
    10: "camera left", 11: "camera right", 12: "camera up", 13: "camera down",
    14: "hotbar 1", 15: "hotbar 2", 16: "hotbar 3", 17: "hotbar 4",
    18: "hotbar 5", 19: "hotbar 6", 20: "hotbar 7", 21: "hotbar 8", 22: "hotbar 9",
}


class OpenVLAAdapter(ModelAdapter):
    """OpenVLA：obs -> (image, instruction)；模型输出 token 序列 -> primitive 动作。"""

    name = "openvla"

    # ------------------------------------------------------------ obs -> input
    def encode_obs(self, obs: dict[str, Any]) -> Any:
        """返回 (image, instruction)。

        TODO: 真实实现需把 image 转 PIL.Image 并做尺寸/归一化对齐
        processor 的 transform；instruction 直接来自 obs["task"]["instruction"]。
        """
        image = obs.get("image")  # base64 模式下是 dict，需在真实实现里解码回 ndarray/PIL
        task = obs.get("task") or {}
        return image, task.get("instruction", "")

    # ------------------------------------------------------------ output -> action
    def decode_action(self, model_out: Any) -> dict[str, Any]:
        """模型输出的动作 token 序列 -> primitive dict。

        :param model_out: 预测动作 token id 列表（离散），如 [1, 12, 14] 表示
                          "forward" + "camera up" + "hotbar 1"。
        :return: {"forward": 1, "camera": [...], ...}
        """
        act = {
            "forward": 0, "back": 0, "left": 0, "right": 0,
            "jump": 0, "sneak": 0, "sprint": 0, "attack": 0, "use": 0,
            "drop": 0, "hotbar": 0, "camera": [0.0, 0.0],
        }
        camera = [0.0, 0.0]
        for token in model_out or []:
            if isinstance(token, dict):  # 容错：某些封装返回 {token_id, action_str}
                token = token.get("action", token.get("token_id", 0))
            token = int(token)
            label = _ACTION_TOKEN_MAP.get(token, "noop")
            if label == "noop":
                continue
            if label == "camera left":
                camera[0] += 0.2  # pitch delta（经验值，按相机灵敏度调）
            elif label == "camera right":
                camera[0] -= 0.2
            elif label == "camera up":
                camera[1] += 0.2  # yaw delta
            elif label == "camera down":
                camera[1] -= 0.2
            elif label.startswith("hotbar"):
                act["hotbar"] = int(label.split()[-1])
            elif label in act:
                act[label] = 1
        act["camera"] = camera
        return act

    def is_available(self) -> bool:
        # TODO（用户服务器）：try import openvla/transformers；成功且有权重则返回 True
        try:
            import openvla  # noqa: F401  # 在用户服务器启用
            return True
        except ImportError:
            return False
