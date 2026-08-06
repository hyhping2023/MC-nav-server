"""mcl2_env.renderer — 视觉观测渲染器。

三种实现（设计见 DESIGN.md §9）：
- base.Renderer        : 抽象接口 + 帧结构
- engine_fork          : fork 客户端抓帧（推荐，Craftium 同思路）
- voxel                : 服务端体素合成（fallback / debug）
- composite            : engine_fork 优先、voxel 回退（M1 默认组合）
"""

from .base import Renderer, Frame, FrameSource
from .composite import CompositeRenderer

__all__ = ["Renderer", "Frame", "FrameSource", "CompositeRenderer"]
