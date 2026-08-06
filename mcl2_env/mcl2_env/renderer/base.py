"""渲染器抽象接口。

帧携带 server_tick，用于与 states.jsonl 对齐（DESIGN.md §9）。
"""

from __future__ import annotations

import abc
import dataclasses
from typing import Optional

import numpy as np


@dataclasses.dataclass
class Frame:
    """一帧视觉观测。"""

    image: np.ndarray            # (H, W, 3) uint8
    server_tick: int             # 与 Lua 侧 tick 对齐
    wall_time: float             # 渲染时间戳
    width: int
    height: int

    def to_rgb(self) -> np.ndarray:
        return self.image


class FrameSource(abc.ABC):
    """帧源：提供最新可用帧（渲染器输出端）。"""

    @abc.abstractmethod
    def get_frame(self) -> Optional[Frame]:
        """返回最新帧；无可用帧返回 None。"""


class Renderer(abc.ABC):
    """渲染器：连接引擎/客户端与 FrameSource。"""

    width: int
    height: int
    fov: int = 72

    @abc.abstractmethod
    def start(self) -> None:
        """启动渲染链路（连接共享内存 / 启动客户端等）。"""

    @abc.abstractmethod
    def stop(self) -> None:
        """停止渲染链路。"""

    @abc.abstractmethod
    def get_frame(self) -> Optional[Frame]:
        """同步取最新帧（降采样后）。"""
