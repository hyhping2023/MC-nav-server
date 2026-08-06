"""STEVE-1 适配器（骨架，无推理）。

STEVE-1：VPT (Video Pretraining) 策略 + 指令条件。动作由 VPT 转成鼠标/键盘按钮事件
（vpt_token 动作模式）。真实推理代码在用户 GPU 服务器启用（本机无 torch）。

TODO（用户服务器）：
    from steve1 import load_steve1
    model = load_steve1("STEVE-1/vpt-2x.model")  # 权重需自行下载
"""

from __future__ import annotations

from typing import Any

from .base import ModelAdapter

# VPT / MineStudio 的 8 个按钮（MineDojo 惯例；STEVE-1 常用子集）
_VPT_BUTTONS = ("forward", "back", "left", "right", "jump", "sneak", "sprint", "attack", "use", "drop")


class Steve1Adapter(ModelAdapter):
    """STEVE-1 / VPT：obs -> (pov=image, instruction)；模型输出 -> vpt 按钮动作。"""

    name = "steve1"

    # ------------------------------------------------------------ obs -> input
    def encode_obs(self, obs: dict[str, Any]) -> Any:
        """返回 {"pov": image, "instruction": instruction}。

        TODO: VPT 用 MineDojo Observation 预处理（resize 128x128 / 640x360）。
        """
        task = obs.get("task") or {}
        return {"pov": obs.get("image"), "instruction": task.get("instruction", "")}

    # ------------------------------------------------------------ output -> action
    def decode_action(self, model_out: Any) -> dict[str, Any]:
        """vpt_token 动作 -> primitive dict。

        :param model_out: {"vpt_token": int, "buttons": {...}, "camera": [dx, dy]}
        """
        act = {k: 0 for k in _VPT_BUTTONS}
        if isinstance(model_out, dict):
            buttons = model_out.get("buttons") or {}
            for k in _VPT_BUTTONS:
                if k in buttons:
                    act[k] = 1 if buttons[k] else 0
            cam = model_out.get("camera")
            act["camera"] = [float(cam[0]), float(cam[1])] if cam else [0.0, 0.0]
        # TODO（用户服务器）：vpt_token -> buttons/camera 的查表映射见 MineDojo VPT 代码
        return act

    def is_available(self) -> bool:
        # TODO（用户服务器）：try import steve1/minedojo；成功且有权重则返回 True
        try:
            import minedojo  # noqa: F401  # 在用户服务器启用
            return True
        except ImportError:
            return False
