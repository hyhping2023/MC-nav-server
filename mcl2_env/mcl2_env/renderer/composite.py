"""CompositeRenderer：主渲染器优先，无帧时回退到备用渲染器。

M1 决策（build-env 环境无 GPU 窗口上下文）：客户端仍能加入服务器并写帧，
但本环境帧内容静态。engine_fork 有真实帧则用之；无帧（客户端未运行 / 共享
内存不可用 / 尚未写帧）时回退 voxel 合成帧，保证视觉观测在线。

set_camera 转发给所有支持它的子渲染器（voxel 需要每步注入相机 + 体素网格）。
"""

from __future__ import annotations

from typing import Optional

from .base import Frame, Renderer


class CompositeRenderer(Renderer):
    """主渲染器优先；主渲染器无帧时回退 fallback。"""

    def __init__(self, primary: Renderer, fallback: Renderer):
        self.primary = primary
        self.fallback = fallback
        self.width = getattr(primary, "width", 224)
        self.height = getattr(primary, "height", 224)
        self.fov = getattr(primary, "fov", 72)

    @property
    def available(self) -> bool:
        """任一子渲染器可用即可（voxel 恒可用）。"""
        return (getattr(self.primary, "available", True)
                or getattr(self.fallback, "available", True))

    def start(self) -> None:
        self.primary.start()
        self.fallback.start()

    def stop(self) -> None:
        self.primary.stop()
        self.fallback.stop()

    def get_frame(self) -> Optional[Frame]:
        """优先主渲染器帧；无帧时回退 fallback（voxel）。"""
        frame = self.primary.get_frame()
        if frame is not None:
            return frame
        return self.fallback.get_frame()

    def set_camera(self, pos, look, voxels) -> None:
        """转发给所有支持 set_camera 的子渲染器（voxel 注入相机/体素）。"""
        for r in (self.primary, self.fallback):
            m = getattr(r, "set_camera", None)
            if m is not None:
                m(pos, look, voxels)
