"""M11.5 统一任务 profile（DESIGN.md §17.3 难点②）。

Python 侧任务知识的**单一来源**：目标块集合、方块→工具映射、hotbar 槽位、供给器
参数。此前散落在 task_runner.TOOL_FOR_BLOCK / interact.KIT_* / demo_human 里的
表全部收拢到这里；新增任务 = 服务端加一个 tasks/*.json + 此处加一个 profile。

kit 约束（M11/M11.7）：固定生存工具包 镐/剑/铲/泥土/斧（hotbar 0-4），任务局限在这五类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional

# M11 固定生存工具包（reset_world 经 items 参数授予，与服务端 TaskRegistry.SURVIVAL_KIT
# 顺序一致），hotbar 0-4。
SURVIVAL_KIT = [
    "minecraft:diamond_pickaxe",   # 0：挖石头
    "minecraft:diamond_sword",     # 1：近战（杀猪）
    "minecraft:diamond_shovel",    # 2：铲泥土
    "minecraft:dirt@64",           # 3：放置方块
    "minecraft:diamond_axe",       # 4：砍树（collect_wood）
]

# 工具类别 → kit hotbar 槽位。
KIT_TOOL_SLOT = {"pickaxe": 0, "sword": 1, "shovel": 2, "dirt": 3, "axe": 4}

# 方块 → 工具注册名（规划器写进 dig 计划；客户端 BlockTraits.toolFor 为运行时兜底）。
TOOL_FOR_BLOCK: Dict[str, str] = {
    # 镐
    "minecraft:stone": "minecraft:diamond_pickaxe",
    "minecraft:cobblestone": "minecraft:diamond_pickaxe",
    "minecraft:deepslate": "minecraft:diamond_pickaxe",
    "minecraft:granite": "minecraft:diamond_pickaxe",
    "minecraft:diorite": "minecraft:diamond_pickaxe",
    "minecraft:andesite": "minecraft:diamond_pickaxe",
    "minecraft:netherrack": "minecraft:diamond_pickaxe",
    "minecraft:coal_ore": "minecraft:diamond_pickaxe",
    "minecraft:iron_ore": "minecraft:diamond_pickaxe",
    "minecraft:copper_ore": "minecraft:diamond_pickaxe",
    "minecraft:gold_ore": "minecraft:diamond_pickaxe",
    # 铲
    "minecraft:grass_block": "minecraft:diamond_shovel",
    "minecraft:dirt": "minecraft:diamond_shovel",
    "minecraft:coarse_dirt": "minecraft:diamond_shovel",
    "minecraft:podzol": "minecraft:diamond_shovel",
    "minecraft:mycelium": "minecraft:diamond_shovel",
    "minecraft:sand": "minecraft:diamond_shovel",
    "minecraft:gravel": "minecraft:diamond_shovel",
    "minecraft:clay": "minecraft:diamond_shovel",
    # 斧
    "minecraft:oak_log": "minecraft:diamond_axe",
    "minecraft:oak_planks": "minecraft:diamond_axe",
    "minecraft:oak_leaves": "minecraft:diamond_axe",
}

# 工具注册名 → kit 槽位（在 kit 内则给出槽位；不在 kit 内的工具 → None）。
_TOOL_SLOT_BY_ID = {
    "minecraft:diamond_pickaxe": 0,
    "minecraft:diamond_sword": 1,
    "minecraft:diamond_shovel": 2,
    "minecraft:diamond_axe": 4,
}


def tool_for_block(block: str) -> Optional[str]:
    """方块注册名（可含 blockstate 后缀）→ 工具注册名；未知返回 None（徒手）。"""
    return TOOL_FOR_BLOCK.get(block.split("[")[0])


def kit_slot_for_tool(tool: Optional[str]) -> Optional[int]:
    """工具注册名 → kit hotbar 槽位（不在 kit 内返回 None）。"""
    if tool is None:
        return None
    return _TOOL_SLOT_BY_ID.get(tool)


@dataclass(frozen=True)
class TaskProfile:
    """一个任务的 Python 侧执行知识。

    - task_id：服务端任务 id（TaskRegistry / tasks/*.json）
    - kind：dig（挖块）/ kill（近战实体）/ place（放置）
    - target_blocks：dig 任务的目标块集合（体素扫描用）
    - entity：kill 任务的目标实体
    - place_block：place 任务放置的方块
    - tool_slot：主工具 hotbar 槽位（kit 内）
    - count：目标数量（供给器保底用；权威判定在服务端）
    """

    task_id: str
    kind: str
    tool_slot: int
    count: int
    target_blocks: FrozenSet[str] = field(default_factory=frozenset)
    entity: Optional[str] = None
    place_block: Optional[str] = None


# 演示任务名（demo_human --task）→ profile。
PROFILES: Dict[str, TaskProfile] = {
    "dig_stone": TaskProfile(
        task_id="collect_stone", kind="dig", tool_slot=KIT_TOOL_SLOT["pickaxe"], count=4,
        target_blocks=frozenset({"minecraft:stone"})),
    "dig_dirt": TaskProfile(
        task_id="dig_dirt", kind="dig", tool_slot=KIT_TOOL_SLOT["shovel"], count=4,
        target_blocks=frozenset({"minecraft:dirt"})),
    "kill_animal": TaskProfile(
        task_id="kill_animal", kind="kill", tool_slot=KIT_TOOL_SLOT["sword"], count=2,
        entity="minecraft:pig"),
    "place_dirt": TaskProfile(
        task_id="place_dirt", kind="place", tool_slot=KIT_TOOL_SLOT["dirt"], count=3,
        place_block="minecraft:dirt"),
    "collect_wood": TaskProfile(
        task_id="collect_wood", kind="dig", tool_slot=KIT_TOOL_SLOT["axe"], count=4,
        target_blocks=frozenset({"minecraft:oak_log"})),
}


def get_profile(name: str) -> TaskProfile:
    """按演示任务名或服务端任务 id 取 profile（两种键都支持）。"""
    if name in PROFILES:
        return PROFILES[name]
    for p in PROFILES.values():
        if p.task_id == name:
            return p
    raise KeyError(f"unknown task profile: {name}（可选：{sorted(PROFILES)}）")
