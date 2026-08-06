"""tick/frame 对齐断言（M8 实现，DESIGN.md §9.3）。

Lockstep 同步调度的验收核心：保证每个 step 的 ``(frame_i, action_i, reward_i,
tick_i)`` 一一对应。权威时钟为服务端 ``Bukkit.getCurrentTick()``（20 TPS）；
客户端侧经 ``vla:tick`` 插件消息缓存最近一次权威 tick（§5.6），帧上行头
``[4B frame_id][4B last_server_tick][8B wall_nanos][JPEG]``（§9.2）。

本模块提供两级接口：

- ``assert_step_alignment(frame, step_result, ticks_per_step, tol)``：单 step
  断言 —— 帧渲染早于结算，差应在一步窗口内：:

      0 <= step_result.server_tick - frame.server_tick <= ticks_per_step + tol

  且 ``frame_id`` / ``server_tick`` 单调不减（错位/回退抛 ``AlignmentError``）。
- ``Aligner``：跨 step 累计 mismatch 计数与 max_diff，``report()`` 输出对齐率。

默认容差 2 tick（§9.3 规定的 tolerance）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# frame 的 server_tick 即客户端打标的 last_server_tick（帧渲染时最新已知权威 tick）。
_FRAME_ID = "frame_id"
_FRAME_TICK = "server_tick"


def frame_meta(frame: Any) -> Tuple[int, int]:
    """从 Frame（dataclass）或 dict 提取 ``(frame_id, server_tick)``。"""
    if isinstance(frame, dict):
        fid = int(frame.get(_FRAME_ID, -1))
        ftick = int(frame.get(_FRAME_TICK, frame.get("last_server_tick", -1)))
        return fid, ftick
    return int(frame.frame_id), int(frame.server_tick)


def step_server_tick(step_result: Any) -> int:
    """从 StepReply dict（gRPC get_step_result 返回）提取权威 server_tick。"""
    if isinstance(step_result, dict):
        return int(step_result["server_tick"])
    return int(step_result.server_tick)


def assert_step_alignment(
    frame: Any,
    step_result: Any,
    ticks_per_step: int,
    tol: int = 2,
    prev: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """断言单个 step 对齐（§9.3），返回对齐记录 dict。

    参数：
    - frame: 帧（Frame 对象或含 frame_id/server_tick 的 dict）
    - step_result: gRPC GetStepResult 返回（含权威 server_tick）
    - ticks_per_step: 每步服务端刻数
    - tol: 容差（默认 2 tick）
    - prev: 上一帧 ``(frame_id, server_tick)``（用于单调性校验；首帧传 None）

    断言：
    1. ``0 <= server_tick - frame_tick <= ticks_per_step + tol``（帧渲染早于结算，
       差应在一步窗口内）；
    2. ``frame_id`` 与 ``server_tick`` 单调不减（与 prev 比较）。

    违反任一断言抛 ``AlignmentError``；通过则返回
    ``{frame_id, frame_tick, server_tick, diff, ok=True}``。
    """
    frame_id, frame_tick = frame_meta(frame)
    server_tick = step_server_tick(step_result)
    diff = server_tick - frame_tick

    problems: list[str] = []
    window = ticks_per_step + tol
    if not (0 <= diff <= window):
        problems.append(
            f"tick 差 {diff} 超出窗口 [0, {window}] "
            f"(frame_tick={frame_tick}, server_tick={server_tick})"
        )
    if prev is not None:
        prev_id, prev_tick = prev
        if frame_id < prev_id:
            problems.append(f"frame_id 回退 {prev_id} -> {frame_id}")
        if frame_tick < prev_tick:
            problems.append(f"frame_tick 回退 {prev_tick} -> {frame_tick}")
    if problems:
        raise AlignmentError("; ".join(problems))

    return {
        "frame_id": frame_id,
        "frame_tick": frame_tick,
        "server_tick": server_tick,
        "diff": diff,
        "ok": True,
    }


class Aligner:
    """跨 step 累计对齐统计（M8 验收：10 episode 对齐率 100%）。

    - ``check(frame, step_result)``：逐 step 断言；错位**不抛**而是记录
      mismatch（让采集脚本可继续跑完全部 episode 并如实上报），返回记录 dict
      （含 ``ok`` 标志）。断言通过的 step 才推进 prev 单调性基准。
    - ``report()``：返回 ``{steps, mismatch, align_rate, max_diff, ...}``；
      ``max_diff`` 为全部 step 的 ``|server_tick - frame_tick|`` 最大值。
    """

    def __init__(self, ticks_per_step: int = 2, tol: int = 2) -> None:
        self.ticks_per_step = int(ticks_per_step)
        self.tol = int(tol)
        self.steps = 0
        self.mismatch = 0
        self.max_diff = 0
        self._prev: Optional[Tuple[int, int]] = None

    def check(self, frame: Any, step_result: Any) -> Dict[str, Any]:
        """断言一个 step 并累计统计；错位不抛，返回记录 dict。"""
        self.steps += 1
        try:
            rec = assert_step_alignment(
                frame, step_result, self.ticks_per_step, self.tol, prev=self._prev
            )
        except AlignmentError:
            self.mismatch += 1
            fid, ftick = frame_meta(frame)
            st = step_server_tick(step_result)
            rec = {
                "frame_id": fid,
                "frame_tick": ftick,
                "server_tick": st,
                "diff": st - ftick,
                "ok": False,
            }
        self.max_diff = max(self.max_diff, abs(rec["diff"]))
        # 单调性基准始终推进（frame_id 由客户端单调递增，即便某 step 错位也应记录）
        self._prev = (rec["frame_id"], rec["frame_tick"])
        return rec

    def report(self) -> Dict[str, Any]:
        """输出对齐率：``{steps, mismatch, align_rate, max_diff, ticks_per_step, tol}``。"""
        rate = 1.0 - (self.mismatch / self.steps) if self.steps else 0.0
        return {
            "steps": self.steps,
            "mismatch": self.mismatch,
            "align_rate": round(rate, 4),
            "max_diff": self.max_diff,
            "ticks_per_step": self.ticks_per_step,
            "tol": self.tol,
        }


class AlignmentError(Exception):
    """帧与 tick 错位超容差时抛出（§9.3 step 标记 invalid）。"""
