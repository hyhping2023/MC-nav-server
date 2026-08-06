-- mcl2_agent / tasks/init.lua
-- 任务定义加载：先注册基础教学任务，再注册生成器入口。
-- 任务 schema 见 DESIGN.md §6.1。

-- 基础教学任务（registry）
mcl2agent.task.register({
    id = "collect_wood",
    name = "Collect Wood",
    instruction = "Collect 3 wood logs from nearby trees.",
    instruction_zh = "从附近的树上收集 3 个原木。",
    type = "collect",
    tags = {"early_game", "wood"},
    difficulty = 0,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 6,
        inventory = {clear = true, give = {"mcl_tools:axe_wood 1"}},
        timeofday = 0.5,
        -- 出生点附近无树（沙漠等生物群系）时，传送到最近树旁（见 reset.lua teleport_near）
        teleport_to_block = {name = "mcl_trees:tree_oak", radius = 96},
    },
    success_predicate = "inventory_contains",
    success_args = {item = "mcl_trees:tree_oak", count = 3},
    timeout_ticks = 2400,
})

mcl2agent.task.register({
    id = "craft_planks",
    name = "Craft Planks",
    instruction = "Craft 4 planks from wood.",
    instruction_zh = "用木头合成 4 个木板。",
    type = "craft",
    tags = {"early_game", "wood"},
    difficulty = 1,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 6,
        inventory = {clear = true, give = {"mcl_trees:tree_oak 3"}},
        timeofday = 0.5,
    },
    success_predicate = "inventory_contains",
    success_args = {item = "mcl_trees:wood_oak", count = 4},
    timeout_ticks = 1200,
})

mcl2agent.task.register({
    id = "place_torch",
    name = "Place a Torch",
    instruction = "Place a torch on the ground in front of you.",
    instruction_zh = "在你面前的地面上放置一个火把。",
    type = "build",
    tags = {"early_game", "light"},
    difficulty = 1,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 6,
        inventory = {clear = true, give = {"mcl_torches:torch 1"}},
        timeofday = 0.5,
    },
    success_predicate = "block_placed",
    success_args = {pos1 = {x = -3, y = 38, z = -3}, pos2 = {x = 3, y = 42, z = 3}, name = "mcl_torches:torch"},
    timeout_ticks = 1200,
})

-- 程序化生成：把 Mineclonia 常见合成物注入任务集（示例，按需扩充）
mcl2agent.task.generate_craft_tasks({
    ["mcl_core:stick"] = {count = 4, difficulty = 1},
    ["mcl_tools:sword_wood"] = {count = 1, difficulty = 2},
    ["mcl_tools:pick_wood"] = {count = 1, difficulty = 2},
    ["mcl_furnaces:furnace"] = {count = 1, difficulty = 3},
})
