-- mcl2_agent / tasks/collect.lua
-- 采集类任务示例。

mcl2agent.task.register({
    id = "collect_stone",
    name = "Collect Stone",
    instruction = "Mine 5 stone blocks.",
    instruction_zh = "挖 5 个石头。",
    type = "collect",
    tags = {"early_game", "stone"},
    difficulty = 1,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 6,
        inventory = {clear = true, give = {}},
        timeofday = 0.5,
    },
    success_predicate = "block_mined",
    success_args = {name = "mcl_core:stone", count = 5},  -- TODO: 判定器需计数逻辑
    timeout_ticks = 2400,
})

mcl2agent.task.register({
    id = "collect_iron_ore",
    name = "Collect Iron Ore",
    instruction = "Find and mine 2 iron ore blocks.",
    instruction_zh = "找到并挖 2 个铁矿石。",
    type = "collect",
    tags = {"mid_game", "ore"},
    difficulty = 4,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 8,
        inventory = {clear = true, give = {"mcl_tools:pick_stone 1"}},
        timeofday = 0.5,
    },
    success_predicate = "inventory_contains",
    success_args = {item = "mcl_core:iron_lump", count = 2},
    timeout_ticks = 6000,
})
