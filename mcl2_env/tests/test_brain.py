#!/usr/bin/env python3
"""brain（SmartPolicy / 行为树 / 规划器）单元测试（假 obs，无服务器）。

覆盖：
  - observation 解析（敌对/食物/目标方块/掉落物）
  - EatBehavior：饥饿低血触发进食、冷却
  - CombatBehavior：击杀/拉扯/逃跑、同目标去重
  - TaskPlanner：collect_wood 树列、collect_stone 下挖找矿、combat 任务按名攻击
  - SmartPolicy 优先级：存活 > 战斗 > 任务

运行方式（任选其一）：
    python3 -m pytest mcl2_env/tests/test_brain.py
    python3 mcl2_env/tests/test_brain.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.brain.behaviors import CombatBehavior, EatBehavior  # noqa: E402
from mcl2_env.brain.observation import Observation  # noqa: E402
from mcl2_env.brain.planner import TaskPlanner  # noqa: E402
from mcl2_env.brain.policy import Context, SmartPolicy  # noqa: E402


def make_obs(*, hp: float = 20, hunger: float = 20, held: str = "",
             entities: list = None, blocks: list = None, inventory: list = None,
             items: list = None, task_type: str = "collect", tid: str = "collect_wood",
             pos=(0.0, 41.0, 0.0), success: bool = False) -> dict:
    """构造最小 observe 响应。

    entities: [(id, name, x, y, z, hp, health, type, hostile)]
    blocks:   [(x, y, z, name)]
    inventory:[(item, count)]
    items:    [(item, x, y, z)]
    """
    return {
        "player": {"pos": {"x": pos[0], "y": pos[1], "z": pos[2]},
                   "hp": hp, "max_hp": 20, "hunger": hunger, "saturation": 5,
                   "held_item": held, "velocity": {"x": 0, "y": 0, "z": 0},
                   "on_ground": True},
        "world": {
            "nearby_blocks": [{"pos": {"x": b[0], "y": b[1], "z": b[2]}, "name": b[3]}
                              for b in (blocks or [])],
            "items_on_ground": [{"item": it[0], "pos": {"x": it[1], "y": it[2], "z": it[3]}}
                                for it in (items or [])],
            "aimed_block": None,
            "entities": [{"id": e[0], "name": e[1],
                          "pos": {"x": e[2], "y": e[3], "z": e[4]},
                          "hp": e[5], "health": e[6], "type": e[7],
                          "hostile": e[8], "targets_player": False, "is_player": False,
                          "is_mob": e[9] if len(e) > 9 else True}
                         for e in (entities or [])],
        },
        "inventory": {"main": [{"item": s[0], "count": s[1]} for s in (inventory or [])]},
        "task": {"id": tid, "type": task_type, "instruction": "", "success": success, "steps": 0},
        "episode": {"frame": 0},
    }


ZOMBIE = ("z1", "mcl_mobs:zombie", 3.0, 41.0, 0.0, 20, 20, "monster", True)
COW = ("c1", "mcl_mobs:cow", 3.0, 41.0, 0.0, 10, 10, "animal", False)


class FakeBridge:
    """record execute 调用（brain 只用 bridge 接口签名，不实际下发）。"""

    def __init__(self):
        self.executed = []

    def execute(self, action, args, player="bot1"):
        self.executed.append((action, args))


# ================================================================ observation

def test_observation_parse():
    obs = Observation.parse(make_obs(
        hp=15, hunger=9, held="mcl_tools:sword_wood",
        entities=[ZOMBIE, COW],
        blocks=[(5, 41, 0, "mcl_trees:tree_oak"), (5, 42, 0, "mcl_trees:tree_oak")],
        inventory=[("mcl_core:bread", 3)],
        items=[("mcl_trees:tree_oak", 4, 40, 0)],
    ))
    assert obs.player.hp == 15 and obs.player.hunger == 9
    assert obs.player.held_item == "mcl_tools:sword_wood"
    hostile = obs.hostile_mobs()
    assert len(hostile) == 1 and hostile[0].name == "mcl_mobs:zombie"
    assert obs.nearest_hostile().id == "z1"
    assert obs.nearest_mob().name == "mcl_mobs:zombie"  # 最近（不区分敌对）
    assert obs.has_food() is True
    assert len(obs.blocks_by_name("mcl_trees:tree_oak")) == 2
    assert obs.nearest_block("mcl_trees:tree_oak") is not None
    assert obs.items_on_ground[0].item == "mcl_trees:tree_oak"
    assert obs.items_on_ground[0].pos.x == 4


def test_observation_no_food():
    obs = Observation.parse(make_obs(inventory=[("mcl_core:stick", 2)]))
    assert obs.has_food() is False
    assert obs.nearest_hostile() is None


def test_nearest_mob_excludes_decorative():
    # 装饰实体（wieldview/掉落物等 is_mob=false）不应被当作攻击目标
    obs = Observation.parse(make_obs(tid="kill_animal", task_type="combat", entities=[
        ("w1", "mcl_wieldview:wieldview", 0.0, 41.0, 0.0, 10, 10, None, False, False),
        ("c1", "mcl_mobs:cow", 3.0, 41.0, 0.0, 10, 10, "animal", False, True),
    ]))
    assert obs.nearest_hostile() is None
    mob = obs.nearest_mob()
    assert mob is not None and mob.name == "mcl_mobs:cow"


def test_nearest_mob_excludes_dead():
    # 已死实体（luaentity.health <= 0，仍残留在地图上）不应再作为攻击目标
    obs = Observation.parse(make_obs(tid="kill_animal", task_type="combat", entities=[
        ("p1", "mobs_mc:pig", 3.0, 41.0, 0.0, 20, -2, "animal", False, True),
    ]))
    assert obs.nearest_mob() is None
    assert obs.hostile_mobs() == []


# ================================================================ eat

def test_eat_behavior_trigger():
    obs = Observation.parse(make_obs(hunger=6, inventory=[("mcl_core:bread", 3)]))
    ctx = Context()
    eat = EatBehavior()
    assert eat.should_run(obs) is True
    actions = eat.act(obs, ctx)
    assert actions == [("eat", {})]
    assert ctx.eat_cooldown_until == 3  # 步 1 + 3


def test_eat_behavior_cooldown():
    obs = Observation.parse(make_obs(hunger=6, inventory=[("mcl_core:bread", 3)]))
    ctx = Context(step=5, eat_cooldown_until=7)
    assert EatBehavior().act(obs, ctx) == []  # 冷却中


def test_eat_behavior_not_hungry():
    obs = Observation.parse(make_obs(hunger=15, inventory=[("mcl_core:bread", 3)]))
    assert EatBehavior().should_run(obs) is False


# ================================================================ combat

def test_combat_melee():
    obs = Observation.parse(make_obs(hp=20, entities=[ZOMBIE], held="mcl_tools:sword_wood"))
    ctx = Context()
    actions = CombatBehavior().act(obs, ctx)
    assert actions == [("attack", {"target": "auto", "mode": "melee"})]
    assert ctx.fight_target_id == "z1"


def test_combat_kite():
    obs = Observation.parse(make_obs(hp=10, entities=[ZOMBIE], held="mcl_tools:sword_wood"))
    ctx = Context()
    actions = CombatBehavior().act(obs, ctx)
    assert actions == [("attack", {"target": "auto", "mode": "kite"})]


def test_combat_flee():
    obs = Observation.parse(make_obs(hp=2, entities=[ZOMBIE], inventory=[]))
    ctx = Context()
    actions = CombatBehavior().act(obs, ctx)
    assert len(actions) == 1 and actions[0][0] == "goto"
    away = actions[0][1]["pos"]
    # 逃跑方向远离僵尸（僵尸在 +x 方向 -> 目标在 -x 方向）
    assert away["x"] < obs.player.pos.x


def test_combat_dedup():
    obs = Observation.parse(make_obs(hp=20, entities=[ZOMBIE], held="mcl_tools:sword_wood"))
    ctx = Context(step=10, fight_target_id="z1", fight_issued_step=12)  # 2 步前已下发
    assert CombatBehavior().act(obs, ctx) == []


# ================================================================ planner

def test_planner_collect_wood_tree_column():
    obs = Observation.parse(make_obs(
        tid="collect_wood",
        blocks=[(5, 41, 0, "mcl_trees:tree_oak"), (5, 42, 0, "mcl_trees:tree_oak"),
                (5, 43, 0, "mcl_trees:tree_oak")],
    ))
    ctx = Context()
    actions = TaskPlanner(FakeBridge(), ctx, "collect_wood").plan(obs)
    assert actions[0] == ("goto", {"pos": {"x": 5.0, "y": 41.0, "z": 0.0}})
    # 自下而上 dig 整列
    assert [a for a in actions if a[0] == "dig"] == [
        ("dig", {"pos": {"x": 5.0, "y": 41.0, "z": 0.0}}),
        ("dig", {"pos": {"x": 5.0, "y": 42.0, "z": 0.0}}),
        ("dig", {"pos": {"x": 5.0, "y": 43.0, "z": 0.0}}),
    ]
    # 重复 plan：该列已下发 -> 不再重复挖（可能探索找下一棵）
    next_actions = TaskPlanner(FakeBridge(), ctx, "collect_wood").plan(obs)
    assert not [a for a in next_actions if a[0] == "dig"], "should not re-dig issued column"


def test_planner_collect_stone_dig():
    obs = Observation.parse(make_obs(tid="collect_stone", blocks=[(4, 40, 0, "mcl_core:stone")]))
    ctx = Context()
    actions = TaskPlanner(FakeBridge(), ctx, "collect_stone").plan(obs)
    assert ("dig", {"pos": {"x": 4.0, "y": 40.0, "z": 0.0}}) in actions


def test_planner_collect_stone_dig_down():
    # 地表没有石头 -> 下挖脚下方块
    obs = Observation.parse(make_obs(tid="collect_stone", blocks=[]))
    ctx = Context()
    actions = TaskPlanner(FakeBridge(), ctx, "collect_stone").plan(obs)
    assert actions == [("dig", {"pos": {"x": 0.0, "y": 40.0, "z": 0.0}})]


def test_planner_combat_task_by_name():
    obs = Observation.parse(make_obs(tid="kill_animal", task_type="combat", entities=[COW]))
    ctx = Context()
    actions = TaskPlanner(FakeBridge(), ctx, "kill_animal").plan(obs)
    assert actions == [("attack", {"target": "mcl_mobs:cow", "mode": "melee"})]


def test_planner_explore_when_no_tree():
    # 附近没有树、没有掉落物 -> 探索（轮转方向走一段）
    obs = Observation.parse(make_obs(tid="collect_wood", blocks=[]))
    ctx = Context(step=10)
    actions = TaskPlanner(FakeBridge(), ctx, "collect_wood").plan(obs)
    assert actions and actions[0][0] == "goto"
    # step=10 -> 角度 (10*45)%360=90 -> dz>0（向 +z 走）
    assert actions[0][1]["pos"]["z"] > 0
    # 同一方向不重复下发（issued_pos 去重）
    assert TaskPlanner(FakeBridge(), ctx, "collect_wood").plan(obs) == []


# ================================================================ policy 优先级

def test_policy_eat_beats_combat():
    obs = Observation.parse(make_obs(hp=6, hunger=5, entities=[ZOMBIE],
                                     inventory=[("mcl_core:bread", 2)]))
    policy = SmartPolicy(FakeBridge(), task_id="collect_wood")
    actions = policy.plan(obs)
    assert actions == [("eat", {})]  # 存活优先于战斗


def test_policy_combat_beats_task():
    obs = Observation.parse(make_obs(hp=20, hunger=20, entities=[ZOMBIE],
                                     held="mcl_tools:sword_wood"))
    policy = SmartPolicy(FakeBridge(), task_id="collect_wood")
    actions = policy.plan(obs)
    assert actions[0][0] == "attack"


def test_policy_task_fallback():
    obs = Observation.parse(make_obs(hp=20, hunger=20, tid="collect_wood",
                                     blocks=[(5, 41, 0, "mcl_trees:tree_oak")]))
    policy = SmartPolicy(FakeBridge(), task_id="collect_wood")
    actions = policy.plan(obs)
    assert actions[0][0] == "goto"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nALL {len(fns)} BRAIN TESTS PASSED")
