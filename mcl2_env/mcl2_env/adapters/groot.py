"""GROOT 适配器（骨架，无推理，字段对齐 MineStudio observation.pov / action.buttons）。

GROOT / MineStudio：observation 含 pov（第一人称图）+ state；action 为
`buttons`（forward/back/left/right/jump/sneak/sprint/attack/use/drop bool）+ `camera` + `hotbar`。
真实推理代码在用户 GPU 服务器启用（本机无 torch）。

TODO（用户服务器）：
    from minestudio.models import GrootModel
    model = GrootModel.from_pretrained("MineStudio/GROOT-0.8B")
"""

from __future__ import annotations

from typing import Any

from .base import ModelAdapter

# MineStudio action.buttons 字段顺序（与 GROOT 动作头一一对应）
_BUTTON_KEYS = ("forward", "back", "left", "right", "jump", "sneak", "sprint", "attack", "use", "drop")


class GrootAdapter(ModelAdapter):
    """GROOT：obs -> (pov=image, state=player/inventory)；模型输出 -> buttons+camera+hotbar。"""

    name = "groot"

    # ------------------------------------------------------------ obs -> input
    def encode_obs(self, obs: dict[str, Any]) -> Any:
        """返回 {"pov": image, "state": state, "instruction": instruction}。

        字段名对齐 MineStudio observation：pov=第一人称图像，state=玩家状态向量。
        """
        player = obs.get("player") or {}
        pos = player.get("pos") or {}
        look = player.get("look") or {}
        state = {
            "pos": [pos.get("x"), pos.get("y"), pos.get("z")],
            "yaw_pitch": [look.get("yaw"), look.get("pitch")],
            "inventory": obs.get("inventory") or {},
        }
        task = obs.get("task") or {}
        return {"pov": obs.get("image"), "state": state, "instruction": task.get("instruction", "")}

    # ------------------------------------------------------------ output -> action
    def decode_action(self, model_out: Any) -> dict[str, Any]:
        """MineStudio action（buttons dict + camera + hotbar）-> 环境动作。

        :param model_out: {"buttons": {...}, "camera": [pitch, yaw], "hotbar": int}
        """
        act = {k: 0 for k in _BUTTON_KEYS}
        if isinstance(model_out, dict):
            buttons = model_out.get("buttons") or {}
            for k in _BUTTON_KEYS:
                if k in buttons:
                    act[k] = 1 if buttons[k] else 0
            act["hotbar"] = int(model_out.get("hotbar", 0))
            cam = model_out.get("camera")
            act["camera"] = [float(cam[0]), float(cam[1])] if cam else [0.0, 0.0]
        # TODO（用户服务器）：真实 GROOT 输出是 logits/token，需先 argmax 再映射
        return act

    def is_available(self) -> bool:
        # TODO（用户服务器）：try import minestudio；成功且有权重则返回 True
        try:
            import minestudio  # noqa: F401  # 在用户服务器启用
            return True
        except ImportError:
            return False
