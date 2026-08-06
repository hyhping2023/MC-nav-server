"""tick/frame 对齐断言桩（M0 里程碑）。

职责：DESIGN.md §9.3 的 Lockstep 同步调度核心 —— 保证每个 step 的
``(frame_i, action_i, reward_i, tick_i)`` 一一对应：

    1. 发出 action_i 后，先等客户端 frame_i（含 last_server_tick）；
    2. 再等服务端 GetStepResult（阻塞至 action_i 的 k ticks 结算，含权威
       server_tick）；
    3. 断言两者 tick 差在容差内 → 记录对齐元组；
    4. 任一步超时/错位 → 该 step 标记 invalid 并可丢弃。

权威时钟：服务端 `Bukkit.getCurrentTick()`（20 TPS）；客户端侧经
`vla:tick` 插件消息获得（§5.6）。默认容差 2 tick。

依赖里程碑：M3（客户端帧上行）→ M7（env 闭环）正式启用；
M8（数据对齐）作为验收断言。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class Lockstep:
    """tick/frame 对齐调度器桩。

    - `begin_step(action)`：记录 step 起始，生成 frame_id。
    - `assert_frame(frame_meta)`：校验帧携带的 last_server_tick。
    - `settle(step_reply)`：与服务端权威 server_tick 对齐，返回对齐记录。
    - `check()`：校验上一 step 是否在容差内。
    """

    def __init__(self, tolerance_ticks: int = 2, ticks_per_step: int = 4) -> None:
        self.tolerance_ticks = tolerance_ticks
        self.ticks_per_step = ticks_per_step
        self.aligned_records: List[Dict[str, Any]] = []

    def begin_step(self, action: Any) -> Dict[str, Any]:
        """记录 step 起始（frame_id / 本地时间戳）。"""
        raise NotImplementedError("M7 实现：生成 step 元数据")

    def assert_frame(self, frame_meta: Dict[str, Any]) -> bool:
        """校验帧上行元数据（frame_id 连续性 + last_server_tick 存在）。

        依赖 M3：客户端帧上行携带 last_server_tick。
        """
        raise NotImplementedError("M3 实现：帧元数据校验")

    def settle(self, step_reply: Dict[str, Any]) -> Dict[str, Any]:
        """与服务端权威结算对齐，返回对齐记录或抛 AlignmentError。

        依赖 M7 + M8：|tick_frame - tick_reward| ≤ tolerance 断言。
        """
        raise NotImplementedError("M7 实现：tick 差断言与对齐记录")

    def check(self) -> bool:
        """查询上一 step 对齐是否通过。"""
        raise NotImplementedError("M8 实现：对齐断言（采集期 100% 通过）")


class AlignmentError(Exception):
    """帧与 tick 错位超容差时抛出（§9.3 step 标记 invalid）。"""
