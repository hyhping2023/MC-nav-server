"""M11.5 人类式演示录制器（canonical 格式，DESIGN.md §17.4）。

目录结构（单 episode）：
    meta.json               种子/任务/工具包/渲染/服务端版本
    trajectory.jsonl        逐帧：frame_id/server_tick/wall_nanos/keys
                            （keys = 该帧采集时刻的按键状态，帧↔按键按构造对齐，
                            **VLA 训练对 (frame, action) 的唯一真值**）
    frames/f_%06d.jpg       JPEG 帧（与 trajectory 行按序一一对应，20fps 合成 mp4）
    keys.jsonl              离散按键事件（key_event，带归属 frame_id + tick，精确按/抬）
    actions.jsonl           Python 语义编排流（goto_path/pillar_up/dig_at/attack_burst/
                            place_use/stuck_decision/wander，§17.4）
    state.jsonl             每步服务端状态快照（server_tick 索引，可与帧 join）
    align_assertions.jsonl  对齐断言违规
    episode_summary.json    统计（frames / key_events / align_violations）

乱序处理（M11.5 修复）：key_event（文本）与帧（二进制）走两条 WS 通道，事件可先于
其归属帧到达——先入 pending 缓冲，帧号推进后冲销；只有 episode 结束仍未等到帧的
事件才计违规（老实现把这种良性乱序全部记违规 → 78 假阳性）。

事件转发（M11.5）：录制线程排空 WS 文本流时，把非 key_event 事件（goto_status/
pillar_status 等）转入线程安全队列，编排器经 poll_events() 消费——否则技能状态
会被录制线程吃掉（sim_human 时代的已知竞争）。
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from ..client_ws import Frame


class HumanRecorder:
    """后台线程消费全部 WS 帧 + 排空文本事件，落盘 canonical 数据。"""

    def __init__(self, outdir, meta: Dict[str, Any]) -> None:
        self.outdir = Path(outdir)
        self.frames_dir = self.outdir / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.meta = dict(meta)

        self._traj = open(self.outdir / "trajectory.jsonl", "w", encoding="utf-8")
        self._keys = open(self.outdir / "keys.jsonl", "w", encoding="utf-8")
        self._actions = open(self.outdir / "actions.jsonl", "w", encoding="utf-8")
        self._state = open(self.outdir / "state.jsonl", "w", encoding="utf-8")
        self._align = open(self.outdir / "align_assertions.jsonl", "w", encoding="utf-8")

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.frame_count = 0
        self.key_event_count = 0
        self.semantic_count = 0
        self._last_frame_id = -1
        self._violations = 0
        # 归属帧未到的 key_event（fid → events），帧号推进后冲销
        self._pending_keys: List[Dict[str, Any]] = []
        # 非 key_event 文本事件转发队列（goto_status / pillar_status …）
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    # ---- 录制线程 ----

    def start(self, ws) -> None:
        """启动后台录帧线程（消费全部 WS 帧 + 排空文本事件）。"""

        def run() -> None:
            while not self._stop.is_set():
                try:
                    frame = ws.recv_frame(timeout=0.5)
                except Exception:  # noqa: BLE001 —— 单帧异常不中断录制
                    continue
                if frame is None:
                    self._drain_text(ws)
                    continue
                self._on_frame(frame)
                self._drain_text(ws)
            self._drain_text(ws)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _on_frame(self, frame: Frame) -> None:
        with self._lock:
            if self._last_frame_id >= 0 and frame.frame_id <= self._last_frame_id:
                self._violations += 1
                self._align.write(json.dumps({
                    "type": "non_monotonic_frame",
                    "frame_id": frame.frame_id,
                    "last": self._last_frame_id,
                }, ensure_ascii=False) + "\n")
            self._last_frame_id = frame.frame_id
            row = {
                "frame_id": frame.frame_id,
                "server_tick": frame.server_tick,
                "wall_nanos": frame.wall_nanos,
                "keys": frame.keys.as_dict(),
            }
            self._traj.write(json.dumps(row, ensure_ascii=False) + "\n")
            Image.fromarray(frame.rgb).save(
                self.frames_dir / f"f_{self.frame_count:06d}.jpg", quality=90)
            self.frame_count += 1
            self._flush_pending_locked()

    def _drain_text(self, ws) -> None:
        for e in ws.drain_json(timeout=0):
            if e.get("type") == "key_event":
                self._on_key_event(e)
            else:
                # goto_status / pillar_status / *_ok 等 → 转发给编排器
                self._events.put(e)

    def _on_key_event(self, e: Dict[str, Any]) -> None:
        self.key_event_count += 1
        fid = e.get("frame_id")
        with self._lock:
            # 事件归属帧还没到（两通道乱序，良性）→ pending，帧到再落盘
            if isinstance(fid, int) and fid >= 0 and fid > self._last_frame_id:
                self._pending_keys.append(e)
                return
            self._keys.write(json.dumps(e, ensure_ascii=False) + "\n")

    def _flush_pending_locked(self) -> None:
        """帧号推进后冲销 pending 事件（调用方持锁）。"""
        if not self._pending_keys:
            return
        still: List[Dict[str, Any]] = []
        for e in self._pending_keys:
            if int(e.get("frame_id", -1)) <= self._last_frame_id:
                self._keys.write(json.dumps(e, ensure_ascii=False) + "\n")
            else:
                still.append(e)
        self._pending_keys = still

    # ---- 编排器接口 ----

    def poll_events(self) -> List[Dict[str, Any]]:
        """取走录制线程转发的非按键文本事件（goto_status/pillar_status…）。"""
        out: List[Dict[str, Any]] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def add_semantic(self, entry: Dict[str, Any]) -> None:
        """记录一条语义编排动作（§17.4 actions.jsonl：goto_path/pillar_up/dig_at…）。"""
        with self._lock:
            self._actions.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.semantic_count += 1

    def add_step(self, state: Dict[str, Any], server_tick: int,
                 progress: float = 0.0) -> None:
        """每步记录服务端状态快照（server_tick 索引，可与帧 join）。"""
        with self._lock:
            self._state.write(json.dumps({
                "server_tick": int(server_tick),
                "progress": float(progress),
                "state": state,
            }, ensure_ascii=False) + "\n")

    # ---- 收尾 ----

    def finalize(self, extra_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """停止录制、写 meta.json + episode_summary.json，返回统计。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        with self._lock:
            # episode 结束仍未等到归属帧的事件才是真违规
            for e in self._pending_keys:
                self._violations += 1
                self._align.write(json.dumps({
                    "type": "key_event_orphan_frame",
                    "frame_id": e.get("frame_id"),
                    "last": self._last_frame_id,
                }, ensure_ascii=False) + "\n")
                self._keys.write(json.dumps(e, ensure_ascii=False) + "\n")
            self._pending_keys = []
        self._traj.close()
        self._keys.close()
        self._actions.close()
        self._state.close()
        self._align.close()

        summary = {
            "frames": self.frame_count,
            "key_events": self.key_event_count,
            "semantic_actions": self.semantic_count,
            "align_violations": self._violations,
            "align_ok": self._violations == 0,
        }
        if extra_summary:
            summary.update(extra_summary)

        (self.outdir / "meta.json").write_text(
            json.dumps(self.meta, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.outdir / "episode_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return summary
