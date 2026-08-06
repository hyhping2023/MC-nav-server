"""观测拼装（M0 骨架 + M7 Env 闭环实现）。

职责：DESIGN.md §8 定义的多模态观测拼装：

    obs = {
        "pov":      (224, 224, 3) uint8 第一人称 RGB 帧（无 HUD），
        "inventory": {"main", "selected_slot", "held_item"},
        "player":   {"pos", "hp", "hunger", "yaw", "pitch", "on_ground",
                     "dimension", "velocity"},
        "stats":    {"xp", "level", "playtime"},
        "task":     {"id", "instruction", "difficulty", "progress",
                     "success", "steps"},
        "agent":    {"episode_id", "server_tick", "frame_id", "wall_time"},
    }

M7 约定（对 §8 的工程化调整）：
- `pov` 为 HWC (H, W, 3) uint8（客户端 JPEG 解码即此形状；M3 random_agent
  断言 shape=(224,224,3)），VLA 模型侧再做 CHW/归一化。
- `build_obs(frame, state, task_info, **extra)` 模块级函数为 env 的统一入口。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import numpy as np
from PIL import Image


class ObsBuilder:
    """把 frame / state / task_info 拼装为完整观测 dict（M7 实现）。"""

    def __init__(self, res: int = 224, with_voxels: bool = False) -> None:
        self.res = res
        self.with_voxels = with_voxels

    def build(
        self,
        frame: Optional[Any] = None,
        state: Optional[Dict[str, Any]] = None,
        task_info: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """拼装完整观测。

        参数：
        - frame: client_ws.Frame（含 .rgb = (H,W,3) uint8），缺省时 pov 全零
        - state: 服务端 GetState 返回（player/inventory/stats）
        - task_info: GetStepResult.info（task 字段）+ 可选 progress/success 覆盖
        - extra: episode_id / server_tick / frame_id / wall_time 等 agent 字段

        返回 §8 的 dict schema。
        """
        pov = self.pov(frame) if frame is not None else \
            np.zeros((self.res, self.res, 3), dtype=np.uint8)

        state = state or {}
        task_info = task_info or {}

        # ---- player ----
        player_raw = state.get("player", {}) or {}
        pos = player_raw.get("pos")
        player = {
            "pos": [float(x) for x in pos] if pos else [0.0, 0.0, 0.0],
            "hp": float(player_raw.get("hp", 20.0)),
            "hunger": int(player_raw.get("hunger", 20)),
            "yaw": float(player_raw.get("yaw", 0.0)),
            "pitch": float(player_raw.get("pitch", 0.0)),
            "on_ground": bool(player_raw.get("on_ground", False)),
            "dimension": str(player_raw.get("dimension", "overworld")),
            "velocity": [float(v) for v in player_raw.get("velocity", [0, 0, 0])],
        }

        # ---- inventory ----
        inv_raw = state.get("inventory", {}) or {}
        inventory = {
            "main": inv_raw.get("main", []),
            "selected_slot": int(inv_raw.get("selected_slot", 0)),
            "held_item": str(inv_raw.get("held_item", "minecraft:air")),
        }

        # ---- stats ----
        stats_raw = state.get("stats", {}) or {}
        stats = {
            "xp": float(stats_raw.get("xp", 0.0)),
            "level": int(stats_raw.get("level", 0)),
            "playtime": float(stats_raw.get("playtime", 0.0)),
        }

        # ---- task ----
        task = {
            "id": str(task_info.get("task", "")),
            "instruction": str(task_info.get("instruction", "")),
            "difficulty": int(task_info.get("difficulty", 0)),
            "progress": float(task_info.get("progress", 0.0)),
            "success": bool(task_info.get("success", False)),
            "steps": int(task_info.get("steps", 0)),
        }

        # ---- agent ----
        agent = {
            "episode_id": str(extra.get("episode_id", "ep-000000")),
            "server_tick": int(extra.get(
                "server_tick", frame.server_tick if frame is not None else 0)),
            "frame_id": int(extra.get(
                "frame_id", frame.frame_id if frame is not None else 0)),
            "wall_time": float(extra.get("wall_time", time.time())),
        }

        obs: Dict[str, Any] = {
            "pov": pov,
            "inventory": inventory,
            "player": player,
            "stats": stats,
            "task": task,
            "agent": agent,
        }
        if self.with_voxels and "voxels" in extra:
            obs["voxels"] = extra["voxels"]
        return obs

    def pov(self, frame: Any) -> np.ndarray:
        """Frame.rgb → (res, res, 3) uint8（HWC；非目标尺寸则 resize）。"""
        rgb = np.asarray(frame.rgb, dtype=np.uint8)
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError(f"pov 需要 HWC RGB，收到 shape={rgb.shape}")
        if rgb.shape[:2] != (self.res, self.res):
            rgb = np.asarray(
                Image.fromarray(rgb).resize((self.res, self.res)), dtype=np.uint8
            )
        return rgb


def build_obs(
    frame: Optional[Any] = None,
    state: Optional[Dict[str, Any]] = None,
    task_info: Optional[Dict[str, Any]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """模块级观测拼装入口（M7 env 使用）。等价于 ObsBuilder().build(...)。"""
    return ObsBuilder().build(frame, state, task_info, **extra)
