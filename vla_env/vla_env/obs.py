"""观测拼装桩（M0 里程碑）。

职责：DESIGN.md §8 定义的多模态观测拼装：

    obs = {
        "pov":      [3, H, W] uint8 第一人称 RGB 帧（无 HUD），
        "compass":  {"yaw", "pitch"},
        "inventory": {"main", "selected_slot", "held_item"},
        "player":   {"pos", "hp", "hunger", "effects", "dimension",
                     "velocity", "on_ground", "relative_pos"},
        "voxels":   {"palette", "data"}（服务端 32³ 体素，可选），
        "task":     {"id", "instruction", "difficulty", "progress",
                     "success", "steps"},
        "stats":    {"xp", "kills", "playtime"},
        "agent":    {"episode_id", "server_tick", "wall_time", "frame_id"},
    }

关键约定：`relative_pos` 以 episode 出生点为原点（平移不变性，§14.6）；
`pov` 与 MineStudio `observation.pov` 字段对齐。
依赖里程碑：M3（客户端视觉：frame 解码）+ M4（服务端 GetState/GetVoxels）+
M5（任务信息）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ObsBuilder:
    """把 frame / state / task_info 拼装为完整观测 dict。

    M0 仅定义接口与输出 schema；M3/M4/M5 填充各数据源。
    """

    def __init__(self, res: int = 224, with_voxels: bool = False) -> None:
        self.res = res
        self.with_voxels = with_voxels

    def build(
        self,
        frame: Optional[bytes] = None,
        state: Optional[Dict[str, Any]] = None,
        task_info: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """拼装完整观测。

        参数：
        - frame: 客户端上行帧（JPEG bytes），M3 解码为 pov。
        - state: 服务端 GetState 返回（player/inventory/stats/compass），M4。
        - task_info: GetStepResult.info（task 字段），M5。

        返回 §8 的 dict schema。
        """
        raise NotImplementedError("M3 实现：frame 解码 + state/task 合并为观测 dict")

    def pov(self, frame: bytes) -> Any:
        """JPEG 帧 → (3, res, res) uint8 数组（pillow 解码，§6.1 依赖）。"""
        raise NotImplementedError("M3 实现：pillow 解码 + resize + CHW 转置")
