"""任务规划器：任务 -> Lua 语义动作序列（去重、轮询、下挖找矿）。

参考 mineflayer-collectblock 的贪婪采集循环（找最近目标 -> 前往 -> 挖 -> 拾取
掉落，失败则下挖/挖穿）与 ScriptedPolicy 的树列挖法。
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING, Any, Optional

from mcl2_env.brain.observation import Observation, Vec3

if TYPE_CHECKING:
    from mcl2_env.brain.policy import Context

# 任务 -> 目标方块（collect 类）
TASK_BLOCK_MAP: dict[str, str] = {
    "collect_wood": "mcl_trees:tree_oak",
    "collect_stone": "mcl_core:stone",
    "collect_iron_ore": "mcl_core:stone_with_iron",
    "collect_dirt": "mcl_core:dirt",
    "collect_cobblestone": "mcl_core:cobble",
}

# 地表没有、需要下挖才能遇到的资源（矿层/深层石头）
UNDERGROUND_RESOURCES = {"collect_stone", "collect_iron_ore"}

# 任务 -> 目标物品 + 数量 + 动作类型（与 mcl2_agent/tasks/*.lua 对齐）
TASK_ITEM_MAP: dict[str, tuple[str, int, str]] = {
    "craft_planks": ("mcl_trees:wood_oak", 4, "craft"),
    "craft_workbench": ("mcl_crafting_table:crafting_table", 1, "craft"),
    "smelt_iron": ("mcl_core:iron_ingot", 1, "craft"),
    "place_torch": ("mcl_torches:torch", 1, "place"),
}


def _pos_key(pos: dict[str, Any] | Vec3 | None) -> str | None:
    if pos is None:
        return None
    if isinstance(pos, Vec3):
        return f"{int(round(pos.x))},{int(round(pos.y))},{int(round(pos.z))}"
    return f"{int(round(pos.get('x', 0)))},{int(round(pos.get('y', 0)))},{int(round(pos.get('z', 0)))}"


class TaskPlanner:
    """按任务类型生成语义动作序列；无状态但通过 ctx 去重。"""

    def __init__(self, bridge, ctx: "Context", task_id: str = ""):
        self.bridge = bridge
        self.ctx = ctx
        self.task_id = task_id
        self._craft_issued = False

    # ------------------------------------------------------------ 入口

    def plan(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        task = obs.task
        tid = task.get("id") or self.task_id
        ttype = task.get("type")

        if tid == "craft_planks":
            return self._craft_planks()
        if ttype == "craft":
            return self._craft(obs)
        if ttype == "collect":
            return self._collect(obs)
        if ttype == "build":
            return self._build(obs)
        if ttype == "combat":
            return self._combat(obs)
        return self._generic(obs)

    # ------------------------------------------------------------ craft

    def _craft_planks(self) -> list[tuple[str, dict[str, Any]]]:
        if self._craft_issued:
            return []
        self._craft_issued = True
        return [("craft", {"item": "mcl_trees:wood_oak", "count": 4})]

    def _craft(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        if self._craft_issued:
            return []
        item = self._guess_craft_item(obs)
        if item is None:
            return []
        self._craft_issued = True
        return [("craft", {"item": item, "count": 1})]

    def _guess_craft_item(self, obs: Observation) -> Optional[str]:
        tid = obs.task.get("id") or self.task_id
        if tid in TASK_ITEM_MAP:
            return TASK_ITEM_MAP[tid][0]
        inst = obs.task.get("instruction") or ""
        for name, _, _ in TASK_ITEM_MAP.values():
            if name in inst:
                return name
        return None

    # ------------------------------------------------------------ collect

    def _collect(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        tid = obs.task.get("id") or self.task_id
        block_name = TASK_BLOCK_MAP.get(tid)

        # 树：挖够数量且在伸手范围内的最低原木
        if block_name == "mcl_trees:tree_oak":
            actions = self._collect_tree(obs)
            if actions:
                return actions
            drops = self._collect_nearby(obs)
            if drops:
                return drops
            return self._explore(obs)

        # 普通方块：最近目标 -> goto + dig
        if block_name:
            target = obs.nearest_block(block_name)
            if target is not None:
                key = _pos_key(target.pos)
                if key in self.ctx.issued_pos:
                    return self._maybe_dig_down(obs, block_name)
                self.ctx.issued_pos.add(key)
                return [("goto", {"pos": target.pos.to_dict()}),
                        ("dig", {"pos": target.pos.to_dict()})]

            # 地表没有：地下资源（石头/矿）下挖；否则探索找目标
            if tid in UNDERGROUND_RESOURCES:
                down = self._dig_down(obs)
                if down:
                    return down
            return self._explore(obs)

        # 未知 collect 任务：aimed_block / 掉落物
        return self._generic(obs)

    def _explore(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        """附近没找到目标资源：向轮转方向走一段（14 格），途中重新扫描。

        角度随步数轮转（45° 递增 -> 螺旋外扩），步数不同方向不同，避免原地打转。
        """
        n = self.ctx.step
        rad = math.radians((n * 45) % 360)
        dist = 14.0
        target = {
            "x": int(round(obs.player.pos.x + math.cos(rad) * dist)),
            "y": int(round(obs.player.pos.y)),
            "z": int(round(obs.player.pos.z + math.sin(rad) * dist)),
        }
        key = _pos_key(target)
        if key in self.ctx.issued_pos:
            return []
        self.ctx.issued_pos.add(key)
        return [("goto", {"pos": target})]

    def _collect_tree(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        logs = [b for b in obs.blocks if b.name == "mcl_trees:tree_oak"]
        col = self._nearest_tree_column(logs, obs.player.pos)
        if col is None:
            return []
        bottom_key = _pos_key(col[0].pos)
        if bottom_key in self.ctx.issued_pos:
            return []
        # 只挖够任务数量且在伸手范围内的最低原木（树不倒，高处够不到不硬挖）
        reach_y = obs.player.pos.y + 3.0
        targets = [b for b in col if b.pos.y <= reach_y][: self._need_logs(obs)]
        if not targets:
            return []
        self.ctx.issued_pos.add(bottom_key)
        for b in col:
            self.ctx.issued_pos.add(_pos_key(b.pos))
        actions = [("goto", {"pos": targets[0].pos.to_dict()})]
        for b in targets:  # 已按 y 升序
            actions.append(("dig", {"pos": b.pos.to_dict()}))
        return actions

    @staticmethod
    def _need_logs(obs: Observation) -> int:
        """任务还差几块原木（缺省 3）。"""
        task = obs.task
        if task.get("id") == "collect_wood":
            return max(1, 3 - obs.count_item("mcl_trees:tree_oak"))
        return 3

    @staticmethod
    def _nearest_tree_column(logs: list, player_pos: Vec3, min_logs: int = 1) -> list | None:
        """按 (x,z) 分列，返回最近的、且含足够伸手范围内原木的列。"""
        if not logs:
            return None
        px, pz = player_pos.x, player_pos.z
        cols: dict[tuple[int, int], list] = {}
        for b in logs:
            k = (int(round(b.pos.x)), int(round(b.pos.z)))
            cols.setdefault(k, []).append(b)
        for b in cols.values():
            b.sort(key=lambda x: x.pos.y)

        def pick(reach_only: bool):
            best, best_d = None, None
            for (cx, cz), lst in cols.items():
                reachable = [x for x in lst if x.pos.y <= player_pos.y + 3.0]
                if reach_only and len(reachable) < min_logs:
                    continue
                d = (cx - px) ** 2 + (cz - pz) ** 2
                if best_d is None or d < best_d:
                    best_d = d
                    best = lst
            return best

        return pick(True) or pick(False)

    def _maybe_dig_down(self, obs: Observation, block_name: str) -> list[tuple[str, dict[str, Any]]]:
        """目标方块已全部下发过：若任务需要地下资源则继续下挖，否则捡掉落。"""
        tid = obs.task.get("id") or self.task_id
        if tid in UNDERGROUND_RESOURCES:
            return self._dig_down(obs)
        return self._collect_nearby(obs)

    def _dig_down(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        """下挖找矿：挖掉脚下方块（Lua dig 动作朝下挖掘 + 自动装备镐）。"""
        p = obs.player.pos
        below = Vec3(int(round(p.x)), int(round(p.y)) - 1, int(round(p.z)))
        key = _pos_key(below)
        if key in self.ctx.issued_pos:
            return []
        self.ctx.issued_pos.add(key)
        return [("dig", {"pos": below.to_dict()})]

    def _collect_nearby(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        """走向地面掉落物（玩家靠近自动拾取）。"""
        for it in obs.items_on_ground:
            if it.pos is None:
                continue
            key = _pos_key({"x": it.pos.x, "y": it.pos.y, "z": it.pos.z})
            if key not in self.ctx.issued_pos:
                self.ctx.issued_pos.add(key)
                return [("goto", {"pos": {"x": it.pos.x, "y": it.pos.y, "z": it.pos.z}})]
        return []

    # ------------------------------------------------------------ build / generic

    def _build(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        item = self._guess_craft_item(obs)
        if item is None:
            return []
        target = self._place_target(obs)
        if target is None:
            return []
        key = _pos_key(target)
        if key in self.ctx.issued_pos:
            return []
        self.ctx.issued_pos.add(key)
        return [("look_at", {"pos": target}), ("place", {"item": item, "pos": target})]

    @staticmethod
    def _place_target(obs: Observation) -> dict[str, Any] | None:
        pl = obs.player.pos
        return {"x": int(round(pl.x)) + 1, "y": int(round(pl.y)) - 1, "z": int(round(pl.z))}

    def _generic(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        aimed = obs.aimed_block()
        if aimed and aimed.name not in ("", "air"):
            key = _pos_key(aimed.pos)
            if key not in self.ctx.issued_pos:
                self.ctx.issued_pos.add(key)
                return [("look_at", {"pos": aimed.pos.to_dict()}),
                        ("goto", {"pos": aimed.pos.to_dict()}),
                        ("dig", {"pos": aimed.pos.to_dict()})]
        return self._collect_nearby(obs)

    # ------------------------------------------------------------ combat 任务

    def _combat(self, obs: Observation) -> list[tuple[str, dict[str, Any]]]:
        mob = obs.nearest_mob()
        if mob is None:
            return self._explore(obs)   # 打猎：附近没怪，走动找
        # 敌对 -> target=auto（Lua 自动选最近敌对）；被动动物（如 kill_animal）
        # -> 显式传实体名
        target = "auto" if mob.hostile else mob.name
        mode = "kite" if obs.player.hp <= 12 else "melee"
        if self.ctx.fight_target_id == mob.id and self.ctx.step - self.ctx.fight_issued_step < 5:
            return []
        self.ctx.fight_target_id = mob.id
        self.ctx.fight_issued_step = self.ctx.step
        return [("attack", {"target": target, "mode": mode})]
