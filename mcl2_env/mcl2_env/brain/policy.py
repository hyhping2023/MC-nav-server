"""SmartPolicy：行为树（存活 > 战斗）优先，否则交任务规划器。

每步 observe -> plan() 返回 [(语义动作, args)]；由 smart_agent.py 经
bridge.execute 下发。动作在 Lua 侧以队列异步执行，Python 只做高层决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from mcl2_env.brain.behaviors import CombatBehavior, EatBehavior
from mcl2_env.brain.observation import Observation
from mcl2_env.brain.planner import TaskPlanner

DEFAULT_PLAYER = "bot1"


@dataclass
class Context:
    """跨步共享的去重/节流状态（策略无状态，状态集中在这里）。"""

    player: str = DEFAULT_PLAYER
    task_id: str = ""
    step: int = 0
    issued_pos: set[str] = field(default_factory=set)   # 已下发采集/放置的方块
    eat_cooldown_until: int = 0
    fight_target_id: Optional[str] = None               # 正在攻击的实体 id
    fight_issued_step: int = -99
    flee_until_step: int = 0


class SmartPolicy:
    """组合 EatBehavior > CombatBehavior > TaskPlanner。"""

    def __init__(self, bridge, player: str = DEFAULT_PLAYER, task_id: str = ""):
        self.bridge = bridge
        self.ctx = Context(player=player, task_id=task_id)
        self.planner = TaskPlanner(bridge, self.ctx, task_id)
        self.behaviors = [EatBehavior(), CombatBehavior()]

    def plan(self, obs_dict: dict[str, Any] | Observation) -> list[tuple[str, dict[str, Any]]]:
        """根据观测生成下一步动作序列；空列表表示轮询等待。

        节流：Lua 语义动作队列非空时等待（防策略每帧灌入新动作把队列打爆）；
        但进食/战斗等紧急反应不受节流限制（先处理，任务动作排队靠后）。
        """
        obs = obs_dict if isinstance(obs_dict, Observation) else Observation.parse(obs_dict)
        self.ctx.step += 1

        # 存活/战斗为紧急行为，先走行为树（即使队列非空）
        for behavior in self.behaviors:
            if behavior.should_run(obs):
                actions = behavior.act(obs, self.ctx)
                if actions:
                    return actions

        # 任务动作节流：Lua 侧还有动作在跑/排队 -> 等待
        act = obs.raw.get("actions") or {}
        if act.get("current") or (act.get("queue") or 0) > 0:
            return []

        return self.planner.plan(obs)
