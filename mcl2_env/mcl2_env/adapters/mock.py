"""MockAdapter：本地冒烟用，无任何模型。

- encode_obs：返回观测摘要 dict（不搬图像，省内存）
- decode_action：返回一个全零原始动作（forward=0, jump=0, ...）
- is_available()：恒 False——只用于链路验证，不接真实推理
"""

from __future__ import annotations

from typing import Any

from .base import ModelAdapter


def _summary(obs: dict[str, Any]) -> dict[str, Any]:
    """提取观测摘要：任务指令、玩家位置/朝向、图像形状。"""
    task = obs.get("task") or {}
    player = obs.get("player") or {}
    image = obs.get("image")
    shape = None
    if isinstance(image, dict):  # base64 模式：{"encoding": "base64", "mime": ..., "data": ...}
        shape = image.get("shape")
    elif isinstance(image, list):  # list 模式：H x W x 3
        h = len(image)
        w = len(image[0]) if h and isinstance(image[0], list) else None
        shape = [h, w, 3] if w else [h]

    pos = player.get("pos") or {}
    look = player.get("look") or {}
    return {
        "task_id": task.get("id"),
        "instruction": task.get("instruction"),
        "success": task.get("success", False),
        "steps": task.get("steps", 0),
        "player_pos": [pos.get("x"), pos.get("y"), pos.get("z")] if pos else None,
        "yaw_pitch": [look.get("yaw"), look.get("pitch")] if look else None,
        "image_shape": shape,
        "has_image": image is not None,
    }


class MockAdapter(ModelAdapter):
    """Mock 适配器：观测摘要 + 全零原始动作。is_available() 恒 False。"""

    name = "mock"

    def encode_obs(self, obs: dict[str, Any]) -> dict[str, Any]:
        return _summary(obs)

    def decode_action(self, model_out: Any) -> dict[str, Any]:
        # model_out 忽略：mock 恒输出全零原始动作（VPT/MineStudio 按键风格）
        return {
            "forward": 0,
            "back": 0,
            "left": 0,
            "right": 0,
            "jump": 0,
            "sneak": 0,
            "sprint": 0,
            "attack": 0,
            "use": 0,
            "drop": 0,
            "hotbar": 0,
            "camera": [0.0, 0.0],
        }

    def is_available(self) -> bool:
        return False
