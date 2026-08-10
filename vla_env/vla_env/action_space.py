"""原始动作映射（M0 骨架 + M7 Env 闭环实现）。

职责：DESIGN.md §7 定义的原始动作（tick 级，MineRL/VPT 对齐）映射：

- buttons（forward/back/left/right/jump/sneak/sprint/attack/use/drop/inventory）
  + hotbar 0-8 + camera [pitch_delta, yaw_delta]（度）。
- `random_action()`：随机按键 + camera 增量 + hotbar（随机策略/冒烟用）。
- `to_ws(action)`：动作 dict → WS 下行 `{"cmd":"action", ...}`（M2 客户端执行）。

注意：客户端 ActionCmd#fromJson 用 Gson `getAsBoolean()` 解析按键，整数
`1` 会被解析为 false（`parseBoolean("1")`），因此 `to_ws` 必须输出真布尔值。
"""

from __future__ import annotations

import random
from typing import Any, Dict

# camera 121 bin（11×11），对齐 MineRL/MineDojo/MineStudio（DESIGN.md §7.1）。
CAMERA_BINS = 121
CAMERA_BIN_SIDE = 11  # 11×11 = 121

# 原始按键集合（顺序与 VPT/MineRL 约定对齐）。
BUTTONS = (
    "forward",
    "back",
    "left",
    "right",
    "jump",
    "sneak",
    "sprint",
    "attack",
    "use",
    "drop",
    "inventory",
)

# 语义动作名（DESIGN.md §7.3，命名与 mineflayer 对齐）。
SEMANTIC_ACTIONS = (
    "goto",
    "look_at",
    "dig",
    "place",
    "equip",
    "select_slot",
    "craft",
    "attack_entity",
    "use_block",
    "eat",
)

# camera 增量范围（度/步，M2 客户端 setPitch/setYaw 增量应用）。
CAMERA_DELTA_MAX = 30.0

# M11 VPT/STEVE-1 离散 camera：11×11 = 121 bin（DESIGN.md §7.1）。
# bin 宽 = (2*30)/(11-1) = 6.0 度；bin 中心 = bin*6 - 30 ∈ [-30, 30]。
CAMERA_BIN_WIDTH = (CAMERA_DELTA_MAX * 2.0) / (CAMERA_BIN_SIDE - 1)


def camera_delta_to_bin(delta: float) -> int:
    """连续相机增量（度）→ 单轴 bin 0-10。"""
    b = int(round((float(delta) + CAMERA_DELTA_MAX) / CAMERA_BIN_WIDTH))
    return max(0, min(CAMERA_BIN_SIDE - 1, b))


def camera_bin_to_delta(bin_idx: int) -> float:
    """单轴 bin 0-10 → 中心增量（度）。"""
    return bin_idx * CAMERA_BIN_WIDTH - CAMERA_DELTA_MAX


def camera_to_bin(camera) -> int:
    """`[pitch_delta, yaw_delta]` → 121 bin id（pitch 为行、yaw 为列）。"""
    pitch, yaw = float(camera[0]), float(camera[1])
    return camera_delta_to_bin(pitch) * CAMERA_BIN_SIDE + camera_delta_to_bin(yaw)


def bin_to_camera(bin_id: int) -> list:
    """121 bin id → `[pitch_delta, yaw_delta]`（bin 中心）。"""
    return [
        camera_bin_to_delta(bin_id // CAMERA_BIN_SIDE),
        camera_bin_to_delta(bin_id % CAMERA_BIN_SIDE),
    ]


def encode_action(action: Dict[str, Any]) -> tuple:
    """原始动作 dict → VPT 分层 token `(button_mask, camera_bin, hotbar)`。

    button_mask：11-bit（bit i 对应 BUTTONS[i]，forward=bit0 … inventory=bit10）；
    camera_bin：121（pitch/yaw 各 11 bin）；hotbar：0-8 或 -1。
    """
    button_mask = 0
    for i, name in enumerate(BUTTONS):
        if action.get(name, False):
            button_mask |= 1 << i
    cam = action.get("camera")
    camera_bin = camera_to_bin(cam) if cam is not None else camera_to_bin([0.0, 0.0])
    hotbar = action.get("hotbar", -1)
    return button_mask, int(camera_bin), int(hotbar)


def decode_tokens(button_mask: int, camera_bin: int, hotbar: int = -1) -> Dict[str, Any]:
    """VPT 分层 token → 原始动作 dict（客户端 ActionCmd 可执行）。"""
    action: Dict[str, Any] = {}
    for i, name in enumerate(BUTTONS):
        action[name] = bool(button_mask & (1 << i))
    action["camera"] = bin_to_camera(int(camera_bin))
    action["hotbar"] = int(hotbar)
    return action


def random_action(rng: Any = None) -> Dict[str, Any]:
    """随机原始动作：随机按键 + camera 增量 + hotbar。

    返回动作 dict（键为 BUTTONS + hotbar + camera），供 random_agent 与
    gymnasium 随机策略使用。
    """
    rng = rng or random
    action: Dict[str, Any] = {
        button: bool(rng.choice([0, 1])) for button in BUTTONS
    }
    action["hotbar"] = int(rng.randrange(0, 9))
    action["camera"] = [
        float(rng.uniform(-CAMERA_DELTA_MAX, CAMERA_DELTA_MAX)),
        float(rng.uniform(-CAMERA_DELTA_MAX, CAMERA_DELTA_MAX)),
    ]
    return action


def to_ws(action: Dict[str, Any]) -> Dict[str, Any]:
    """原始动作 dict → WS 下行消息（客户端 ActionCmd 可执行）。

    按键转真布尔（见模块 docstring 的 getAsBoolean 坑）；hotbar/camera 数值透传。
    缺省按键视为 False，缺省 hotbar 不切换（-1）。
    """
    msg: Dict[str, Any] = {"cmd": "action"}
    for button in BUTTONS:
        msg[button] = bool(action.get(button, False))
    hotbar = action.get("hotbar")
    if hotbar is not None:
        msg["hotbar"] = int(hotbar)
    cam = action.get("camera")
    if cam is not None:
        msg["camera"] = [float(cam[0]), float(cam[1])]
    return msg


class ActionSpace:
    """原始/语义动作映射接口（M7：random_action / to_ws 已实现）。"""

    def __init__(self, mode: str = "discrete", camera_bins: int = CAMERA_BINS) -> None:
        self.mode = mode  # "discrete" | "continuous" | "vpt_token"
        self.camera_bins = camera_bins

    def random_action(self) -> Dict[str, Any]:
        """随机原始动作（委托模块级 random_action）。"""
        return random_action()

    def to_ws(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """动作 dict → WS 下行消息（委托模块级 to_ws）。"""
        return to_ws(action)

    def semantic_to_primitive(self, semantic: Dict[str, Any]) -> list:
        """语义动作 → 原始动作序列（M12 实现）。"""
        raise NotImplementedError("M12 实现：语义动作分解为原始动作序列")

    def vpt_token(self, action: Dict[str, Any]) -> tuple:
        """VPT 分层 token（M11）：返回 `(button_mask, camera_bin, hotbar)`（见 encode_action）。"""
        return encode_action(action)
