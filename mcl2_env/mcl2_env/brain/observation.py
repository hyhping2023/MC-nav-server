"""观测数据类：把 bridge.observe 返回的 dict 解析成类型化结构。

Python 决策层（行为树/规划器）只依赖这些字段；新增观测字段在
mcl2_agent/api/state.lua 补全后在此同步即可。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

# 常见 Mineclonia 食物名称子串（Lua 侧 survival.find_food 负责选最优，
# Python 只需判断"背包里有没有食物"）。按需扩充。
FOOD_NAME_HINTS: tuple[str, ...] = (
    "bread", "beef", "chicken", "pork", "mutton", "rabbit", "fish",
    "apple", "potato", "carrot", "melon", "cookie", "pie", "soup",
    "berry", "cooked", "steak", "mushroom_stew",
)


@dataclass
class Vec3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def dist(self, other: "Vec3") -> float:
        dx, dy, dz = self.x - other.x, self.y - other.y, self.z - other.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z}


@dataclass
class PlayerState:
    pos: Vec3
    hp: float
    max_hp: float
    hunger: float
    saturation: float
    held_item: str
    velocity: Vec3
    on_ground: bool


@dataclass
class MobEntity:
    id: str
    name: str
    pos: Vec3
    hp: Optional[float]          # 引擎 get_hp（mcl_mobs 下被冻结，不可靠）
    health: Optional[float]      # luaentity.health（mcl_mobs 真实血条）
    type: Optional[str]          # monster / animal / npc
    hostile: bool
    targets_player: bool
    is_player: bool
    is_mob: bool = True          # mcl_mobs 实体；False=装饰实体（wieldview/掉落物/载具）


@dataclass
class Block:
    pos: Vec3
    name: str
    param2: Optional[int]


@dataclass
class InventoryItem:
    item: str
    count: int
    pos: Optional[Vec3] = None   # 仅 items_on_ground 有；背包物品为 None


@dataclass
class Observation:
    player: PlayerState
    entities: list[MobEntity]
    blocks: list[Block]
    items_on_ground: list[InventoryItem]
    inventory: list[InventoryItem]
    task: dict[str, Any]
    raw: dict[str, Any]

    # ------------------------------------------------------------ 解析

    @classmethod
    def parse(cls, d: dict[str, Any]) -> "Observation":
        raw = d or {}
        pl = raw.get("player") or {}
        pos = pl.get("pos") or {}
        vel = pl.get("velocity") or {}
        player = PlayerState(
            pos=Vec3(pos.get("x", 0), pos.get("y", 0), pos.get("z", 0)),
            hp=pl.get("hp", 20),
            max_hp=pl.get("max_hp", 20),
            hunger=pl.get("hunger", 20),
            saturation=pl.get("saturation", 0),
            held_item=pl.get("held_item") or "",
            velocity=Vec3(vel.get("x", 0), vel.get("y", 0), vel.get("z", 0)),
            on_ground=bool(pl.get("on_ground", True)),
        )
        wd = raw.get("world") or {}
        entities = [
            MobEntity(
                id=e.get("id", ""),
                name=e.get("name", ""),
                pos=Vec3((e.get("pos") or {}).get("x", 0),
                         (e.get("pos") or {}).get("y", 0),
                         (e.get("pos") or {}).get("z", 0)),
                hp=e.get("hp"),
                health=e.get("health"),
                type=e.get("type"),
                hostile=bool(e.get("hostile", False)),
                targets_player=bool(e.get("targets_player", False)),
                is_player=bool(e.get("is_player", False)),
                is_mob=bool(e.get("is_mob", True)),   # 缺省按生物处理（旧观测兼容）
            )
            for e in wd.get("entities") or []
        ]
        blocks = [
            Block(
                pos=Vec3((b.get("pos") or {}).get("x", 0),
                         (b.get("pos") or {}).get("y", 0),
                         (b.get("pos") or {}).get("z", 0)),
                name=b.get("name", ""),
                param2=b.get("param2"),
            )
            for b in wd.get("nearby_blocks") or []
        ]
        items = [
            InventoryItem(
                it.get("item", ""),
                it.get("count", 1),
                pos=Vec3((it.get("pos") or {}).get("x", 0),
                         (it.get("pos") or {}).get("y", 0),
                         (it.get("pos") or {}).get("z", 0)),
            )
            for it in (wd.get("items_on_ground") or [])
        ]
        inv = [
            InventoryItem(s.get("item", ""), s.get("count", 0))
            for s in ((raw.get("inventory") or {}).get("main") or [])
        ]
        return cls(
            player=player,
            entities=entities,
            blocks=blocks,
            items_on_ground=items,
            inventory=inv,
            task=raw.get("task") or {},
            raw=raw,
        )

    # ------------------------------------------------------------ 查询辅助

    def _alive_mobs(self, entities: list[MobEntity]) -> list[MobEntity]:
        """过滤非玩家、非装饰实体，且未死亡（health 缺失视为存活）。"""
        return [e for e in entities
                if not e.is_player and e.is_mob
                and (e.health is None or e.health > 0)]

    def hostile_mobs(self, max_dist: float = 16.0) -> list[MobEntity]:
        out = []
        for e in self._alive_mobs(self.entities):
            if e.hostile:
                d = self.player.pos.dist(e.pos)
                if d <= max_dist:
                    out.append(e)
        out.sort(key=lambda e: self.player.pos.dist(e.pos))
        return out

    def nearest_hostile(self, max_dist: float = 16.0) -> Optional[MobEntity]:
        mobs = self.hostile_mobs(max_dist)
        return mobs[0] if mobs else None

    def nearest_mob(self, max_dist: float = 16.0) -> Optional[MobEntity]:
        mobs = self._alive_mobs(self.entities)
        mobs.sort(key=lambda e: self.player.pos.dist(e.pos))
        return mobs[0] if mobs and self.player.pos.dist(mobs[0].pos) <= max_dist else None

    def has_food(self) -> bool:
        return any(
            any(hint in it.item for hint in FOOD_NAME_HINTS)
            for it in self.inventory
        )

    def count_item(self, item: str) -> int:
        return sum(it.count for it in self.inventory if it.item == item)

    def has_item_prefix(self, prefix: str) -> bool:
        return any(it.item.startswith(prefix) for it in self.inventory)

    def blocks_by_name(self, name: str) -> list[Block]:
        return [b for b in self.blocks if b.name == name]

    def nearest_block(self, names: str | tuple[str, ...], max_dist: float = 64.0) -> Optional[Block]:
        if isinstance(names, str):
            names = (names,)
        best, best_d = None, None
        for b in self.blocks:
            if b.name not in names:
                continue
            d = self.player.pos.dist(b.pos)
            if d <= max_dist and (best_d is None or d < best_d):
                best, best_d = b, d
        return best

    def aimed_block(self) -> Optional[Block]:
        ab = (self.raw.get("world") or {}).get("aimed_block") or {}
        if not ab.get("pos") or not ab.get("name"):
            return None
        p = ab["pos"]
        return Block(pos=Vec3(p.get("x", 0), p.get("y", 0), p.get("z", 0)),
                     name=ab["name"], param2=ab.get("param2"))
