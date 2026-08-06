"""EpisodeWriter：把 Python 侧观测/动作写入 canonical episode 目录。

Lua 侧 record.lua 主要负责服务器内落盘；本类用于纯 Python 采集路径
（例如直接由 RemoteEnv 采集，不经 Lua 落盘），两者输出同构。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
from PIL import Image


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EpisodeWriter:
    def __init__(self, root: str, run_id: str, episode_id: str, images_only: bool = False):
        self.run_id = run_id
        self.episode_id = episode_id
        self.ep_dir = os.path.join(root, "episodes", episode_id)
        self.obs_dir = os.path.join(self.ep_dir, "observations")
        os.makedirs(self.obs_dir, exist_ok=True)

        self.frame = -1
        self._fh: dict[str, Any] = {}
        if not images_only:
            # 纯图像模式（random_agent 图片写盘）：JSONL 由 Lua 侧 record 维护，
            # Python 只写 observations/*.png，避免覆盖 Lua 的 states/actions/rewards。
            self._open("instructions")
            self._open("states")
            self._open("actions")
            self._open("rewards")

    def _open(self, name: str) -> None:
        self._fh[name] = open(os.path.join(self.ep_dir, f"{name}.jsonl"), "w")

    # ------------------------------------------------------------ meta

    def write_meta(self, meta: dict[str, Any]) -> None:
        """meta.json：种子 + 版本 + 配置（DESIGN.md §7.2）。"""
        default = {
            "schema_version": "1.0.0",
            "episode_id": self.episode_id,
            "run_id": self.run_id,
            "created_at": _now(),
        }
        default.update(meta or {})
        with open(os.path.join(self.ep_dir, "meta.json"), "w") as f:
            json.dump(default, f, indent=2)

    # ------------------------------------------------------------ data

    def write_instruction(self, instruction: str, tick: int, frame: int) -> None:
        self._append("instructions", {"tick": tick, "frame": frame, "text": instruction})

    def write_state(self, state: dict[str, Any], tick: int) -> None:
        """state 与图像同帧写入；本方法负责分配帧号。"""
        self.frame += 1
        fnum = self.frame
        row = dict(state)
        row["tick"] = tick
        row["frame"] = fnum
        row["image"] = f"observations/{fnum:06d}.png"
        self._append("states", row)

    def write_action(self, action: dict[str, Any], tick: int, frame: Optional[int] = None) -> None:
        row = dict(action)
        row["tick"] = tick
        row["frame"] = self.frame if frame is None else frame
        self._append("actions", row)

    def write_reward(self, reward: float, terminated: bool, truncated: bool, info: dict[str, Any], tick: int) -> None:
        self._append("rewards", {
            "tick": tick, "frame": self.frame,
            "reward": reward, "terminated": terminated, "truncated": truncated,
            "info": info,
        })

    def write_image(self, image: np.ndarray, quality: int = 95) -> str:
        """写 PNG（或 JPEG），返回相对路径。"""
        fnum = self.frame
        rel = f"observations/{fnum:06d}.png"
        path = os.path.join(self.ep_dir, rel)
        Image.fromarray(image).save(path, format="PNG", compress_level=1)
        return rel

    def write_frame(self, image: np.ndarray, frame: int, quality: int = 95) -> str:
        """按显式帧号写 PNG（用于与 Lua 侧 states.jsonl 的 frame 对齐）。"""
        self.frame = frame
        rel = f"observations/{frame:06d}.png"
        path = os.path.join(self.ep_dir, rel)
        Image.fromarray(image).save(path, format="PNG", compress_level=1)
        return rel

    # ------------------------------------------------------------ summary

    def finish(self, summary: dict[str, Any]) -> None:
        for h in self._fh.values():
            h.close()
        self._fh = {}
        with open(os.path.join(self.ep_dir, "episode_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)

    def _append(self, name: str, row: dict[str, Any]) -> None:
        self._fh[name].write(json.dumps(row, ensure_ascii=False) + "\n")
