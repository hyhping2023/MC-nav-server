-- mcl2_agent / tasks/craft.lua
-- 合成/烧炼类任务示例。

mcl2agent.task.register({
    id = "smelt_iron",
    name = "Smelt Iron",
    instruction = "Smelt 1 iron ingot using a furnace.",
    instruction_zh = "用熔炉烧炼 1 个铁锭。",
    type = "craft",
    tags = {"mid_game", "furnace"},
    difficulty = 3,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 6,
        inventory = {
            clear = true,
            give = {
                "mcl_core:iron_lump 1",
                "mcl_furnaces:furnace 1",
                "mcl_core:coal_lump 4",
            },
        },
        timeofday = 0.5,
    },
    success_predicate = "inventory_contains",
    success_args = {item = "mcl_core:iron_ingot", count = 1},
    timeout_ticks = 2400,
})

mcl2agent.task.register({
    id = "craft_workbench",
    name = "Craft a Crafting Table",
    instruction = "Craft a crafting table from planks.",
    instruction_zh = "用木板合成一个工作台。",
    type = "craft",
    tags = {"early_game", "wood"},
    difficulty = 1,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 6,
        inventory = {clear = true, give = {"mcl_trees:wood_oak 4"}},
        timeofday = 0.5,
    },
    success_predicate = "inventory_contains",
    success_args = {item = "mcl_crafting_table:crafting_table", count = 1},
    timeout_ticks = 1200,
})
