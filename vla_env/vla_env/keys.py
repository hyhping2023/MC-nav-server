"""M11 帧↔按键对齐：按键状态编解码（docs/p1_protocol.md §2.3 / §2.4）。

二进制帧头（M11 起 23B）：
    [4B frame_id BE][4B server_tick BE][8B wall_nanos BE]
    [2B buttons BE][1B hotbar][2B yaw_delta int16][2B pitch_delta int16][JPEG]

- buttons：11 按键位掩码（位序与 action_space.BUTTONS 一致：forward=bit0 …
  inventory=bit10）
- hotbar：0-8；0xFF = 无
- yaw/pitch delta：int16 定点（值 = 度 * 100），帧间相机增量（人类鼠标微动）

帧 N 的 KeyState 即该帧采集时刻的按键状态 → frame 与按键**按构造对齐**；
离散的按下/抬起事件另经 WS `key_event` 文本上行（精确时刻）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from .action_space import BUTTONS

# 帧头固定长度（frame_id 4 + server_tick 4 + wall_nanos 8 + buttons 2
# + hotbar 1 + yaw 2 + pitch 2 = 23B；JPEG 紧随其后）。
HEADER_BYTES = 23


@dataclass(frozen=True)
class KeyState:
    """帧采集时刻的按键状态快照。"""

    buttons: int            # 11 位掩码
    hotbar: int             # 0-8；-1 = 无
    yaw_delta: float        # 帧间相机 yaw 增量（度）
    pitch_delta: float      # 帧间相机 pitch 增量（度）

    def pressed(self, name: str) -> bool:
        """按键是否按下（name ∈ BUTTONS）。"""
        return bool(self.buttons & (1 << BUTTONS.index(name)))

    def pressed_dict(self) -> Dict[str, bool]:
        """11 按键布尔 dict（MineRL/VPT 动作头同构）。"""
        return {name: self.pressed(name) for name in BUTTONS}

    def as_dict(self) -> Dict[str, Any]:
        """完整动作形 dict：buttons + hotbar + camera[pitch, yaw]。"""
        d: Dict[str, Any] = self.pressed_dict()
        d["hotbar"] = self.hotbar
        d["camera"] = [self.pitch_delta, self.yaw_delta]
        return d


def mask_from_buttons(buttons: Dict[str, bool]) -> int:
    """按键布尔 dict → 位掩码（缺省按键视为 False）。"""
    mask = 0
    for name in BUTTONS:
        if buttons.get(name, False):
            mask |= 1 << BUTTONS.index(name)
    return mask


def buttons_from_mask(mask: int) -> Dict[str, bool]:
    """位掩码 → 11 按键布尔 dict。"""
    return {name: bool(mask & (1 << BUTTONS.index(name))) for name in BUTTONS}


def decode_keys(raw: bytes) -> KeyState:
    """解析 23B 帧头中的按键块（raw[16:23]）。"""
    buttons = int.from_bytes(raw[16:18], "big", signed=False)
    hotbar = raw[18]
    if hotbar == 0xFF:
        hotbar = -1
    yaw_delta = _iq(raw[19:21])
    pitch_delta = _iq(raw[21:23])
    return KeyState(buttons=buttons, hotbar=hotbar,
                    yaw_delta=yaw_delta, pitch_delta=pitch_delta)


def encode_keys(ks: KeyState) -> bytes:
    """KeyState → 7B 按键块（供协议测试/重放）。"""
    out = bytearray()
    out += ks.buttons.to_bytes(2, "big", signed=False)
    out += bytes([ks.hotbar if 0 <= ks.hotbar <= 8 else 0xFF])
    out += _q(ks.yaw_delta)
    out += _q(ks.pitch_delta)
    return bytes(out)


def _q(deg: float) -> bytes:
    """度 → int16 定点（*100，夹紧 ±327.67）。"""
    v = int(round(deg * 100.0))
    v = max(-32767, min(32767, v))
    return v.to_bytes(2, "big", signed=True)


def _iq(raw: bytes) -> float:
    """int16 定点 → 度。"""
    return int.from_bytes(raw, "big", signed=True) / 100.0
