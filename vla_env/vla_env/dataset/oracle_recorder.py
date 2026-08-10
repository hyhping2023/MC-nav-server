"""轨迹记录器（Oracle 生成器，DESIGN.md §11.5）。

StepRecorder 逐帧落盘 Oracle 轨迹：帧（JPEG 外置）+ 状态 + 原始动作 + 语义标签
+ 服务端权威 reward/progress，行号一一对应（每 step 一行 trajectory.jsonl），
复用 M8 的 lockstep.Aligner 逐 step 对齐断言与四者计数（frames==actions==
rewards==states）。

目录结构（单层，对齐 §11.1 字段语义）::

  <episode_dir>/
    meta.json              # 种子/版本/配置/oracle 参数/统计
    trajectory.jsonl       # 每 step 一行（action + semantic + state + reward 合一）
    frames/{step:06d}.jpg  # 与行号一一对应的 POV 帧
    align_assertions.jsonl # 每 step 对齐记录 + 汇总
    episode_summary.json   # 四者计数/对齐率/成功/模式分布

线程模型：采集主线程逐 step 调 on_step（写 JSONL/JPEG），帧由录帧线程
（demo_record.recorder 模式）消费 WS 帧流后调 on_frame 落盘——两者写不同
文件（trajectory.jsonl vs frames/），无共享状态；on_frame 的 step 序号由
调用方传入（录帧线程按到达顺序计数）。

崩溃安全：trajectory.jsonl 逐行 append（行原子），帧逐张写（文件原子）；
finalize 幂等（已存在则跳过）。
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, Optional

import numpy as np
from PIL import Image

from . import schema
from ..lockstep import Aligner

# 四者计数键（对齐 M8 collect_episodes.py）。
_COUNTERS = ("frames", "actions", "rewards", "states")


class StepRecorder:
    """逐帧轨迹记录器（每 episode 一个实例）。

    用法::

        rec = StepRecorder(episode_dir, ticks_per_step=2, tol=2)
        # 每 step（frame 到达后）:
        rec.on_step(step, tag, action, state, frame, step_result)
        # 结束:
        rec.finalize(meta_extra=None, success=..., ...)
    """

    def __init__(self, episode_dir: str, ticks_per_step: int = 2, tol: int = 2) -> None:
        self.dir = episode_dir
        os.makedirs(episode_dir, exist_ok=True)
        os.makedirs(os.path.join(episode_dir, "frames"), exist_ok=True)

        self.aligner = Aligner(ticks_per_step=ticks_per_step, tol=tol)
        self.counters: Dict[str, int] = {k: 0 for k in _COUNTERS}
        self._traj_path = os.path.join(episode_dir, "trajectory.jsonl")
        self._align_path = os.path.join(episode_dir, "align_assertions.jsonl")
        self._rows = 0
        self._frame_idx = 0
        # 录帧线程写入的最新帧（主线程 on_step 按 step 对齐消费）
        self._last_frame: Any = None
        self._last_frame_id = -1
        # 汇总统计（finalize 时写入 episode_summary.json）
        self.summary: Dict[str, Any] = {
            "frames": 0,
            "actions": 0,
            "rewards": 0,
            "states": 0,
            "align_rate": 0.0,
            "mismatch": 0,
            "max_diff": 0,
            "success": False,
            "terminated": False,
            "truncated": False,
            "max_progress": 0.0,
            "intent_counts": {},
        }

    # ---- 逐 step 落盘 ----

    def on_step(
        self,
        step: int,
        tag: Dict[str, Any],
        action: Dict[str, Any],
        state: Dict[str, Any],
        frame: Any,
        step_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """记录一个 step：对齐断言 + trajectory 行 + 帧。

        - `step`: 步序号（1-based）
        - `tag`: schema.label() 语义标签
        - `action`: 发出去的原始动作 dict（buttons/hotbar/camera）
        - `state`: gRPC get_state 返回的 dict（含 player/inventory/stats）
        - `frame`: client_ws.Frame（或 None——帧缺失则行记 frame_id=-1，计数不一致）
        - `step_result`: gRPC get_step_result 返回（权威 reward/progress/tick）

        返回对齐记录 dict（ok 标志，供调用方上报）。
        """
        # 对齐断言（M8 口径；错位不抛，Aligner 内部记录 mismatch）
        align_rec = self.aligner.check(frame, step_result) if frame is not None else {
            "frame_id": -1, "frame_tick": -1,
            "server_tick": step_result.get("server_tick", -1),
            "diff": 0, "ok": False,
        }
        self._append_align(align_rec)
        self.counters["rewards"] += 1
        self.counters["states"] += 1

        frame_id = align_rec["frame_id"] if frame is not None else -1
        frame_tick = align_rec["frame_tick"] if frame is not None else -1

        # 帧落盘（录帧线程已按到达顺序写好 frames/；此处若 recorder 自持帧则直接写）
        if frame is not None and frame.rgb is not None:
            self._save_frame(frame.rgb, step)
            self.counters["frames"] += 1

        # 语义动作（intent → DESIGN §7.3 语义名）
        semantic = schema.semantic_from_intent(
            tag.get("intent", "noop"),
            tag.get("target"),
        )

        row = {
            "step": step,
            "frame_id": frame_id,
            "frame_tick": frame_tick,
            "wall_nanos": int(getattr(frame, "wall_nanos", 0)) if frame is not None else 0,
            "action": action,
            "semantic": semantic,
            "label": tag,
            "state": state,
            "reward": float(step_result.get("reward", 0.0)),
            "progress": float(step_result.get("progress", 0.0)),
            "terminated": bool(step_result.get("terminated", False)),
            "truncated": bool(step_result.get("truncated", False)),
        }
        self._append_traj(row)
        self._rows += 1

        # 汇总
        self.counters["actions"] += 1
        prog = float(step_result.get("progress", 0.0))
        self.summary["max_progress"] = max(self.summary["max_progress"], prog)
        self.summary["terminated"] = self.summary["terminated"] or bool(step_result.get("terminated", False))
        self.summary["truncated"] = self.summary["truncated"] or bool(step_result.get("truncated", False))
        intent = tag.get("intent", "unknown")
        self.summary["intent_counts"][intent] = self.summary["intent_counts"].get(intent, 0) + 1

        return align_rec

    def on_frame(self, frame: Any) -> None:
        """录帧线程回调：缓存最新帧（供主线程 on_step 对齐/写盘）。

        只更新 frame_id 单调递增的帧（录帧线程满帧率消费，可能收到旧帧）。
        """
        if frame is None or getattr(frame, "rgb", None) is None:
            return
        fid = getattr(frame, "frame_id", -1)
        if fid > self._last_frame_id:
            self._last_frame = frame
            self._last_frame_id = fid

    # ---- 收尾 ----

    def finalize(self, meta: Dict[str, Any], success: bool = False) -> Dict[str, Any]:
        """写 meta.json / episode_summary.json（幂等；episode 结束调用）。"""
        # 对齐报告
        report = self.aligner.report()
        self.summary.update(
            frames=self.counters["frames"],
            actions=self.counters["actions"],
            rewards=self.counters["rewards"],
            states=self.counters["states"],
            align_rate=report["align_rate"],
            mismatch=report["mismatch"],
            max_diff=report["max_diff"],
            success=bool(success),
            rows=self._rows,
        )
        counts_consistent = len(set(self.counters.values())) == 1 and self.counters["frames"] > 0
        self.summary["counts_consistent"] = counts_consistent

        meta_path = os.path.join(self.dir, "meta.json")
        summary_path = os.path.join(self.dir, "episode_summary.json")
        if not os.path.exists(meta_path):
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        if not os.path.exists(summary_path):
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(self.summary, f, ensure_ascii=False, indent=2)

        return self.summary

    # ---- 内部 ----

    def _append_traj(self, row: Dict[str, Any]) -> None:
        with open(self._traj_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _append_align(self, rec: Dict[str, Any]) -> None:
        with open(self._align_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _save_frame(self, rgb: np.ndarray, step: int) -> None:
        path = os.path.join(self.dir, "frames", f"{step:06d}.jpg")
        Image.fromarray(rgb).save(path, quality=90)
