"""智能决策层（brain）：高层策略，驱动 Lua 语义动作（goto/dig/equip/attack/eat）。

架构（见 docs 与 m3m4_protocol）：
  - Python brain = 高层决策（行为树优先级：存活 > 战斗 > 任务）
  - Lua skills   = 低层执行（挖穿寻路、战斗、进食、装备）

模块：
  - observation.py  观测数据类（解析 observe dict）
  - behaviors.py    存活/战斗行为（进食、战斗/风筝/逃跑）
  - planner.py      任务规划（采集/合成/建造/战斗）
  - policy.py       SmartPolicy 组合行为树 + 任务规划
"""

from mcl2_env.brain.observation import (
    Block,
    InventoryItem,
    MobEntity,
    Observation,
    PlayerState,
    Vec3,
)
from mcl2_env.brain.policy import Context, SmartPolicy

__all__ = [
    "Block",
    "Context",
    "InventoryItem",
    "MobEntity",
    "Observation",
    "PlayerState",
    "SmartPolicy",
    "Vec3",
]
