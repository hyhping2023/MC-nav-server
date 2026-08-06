-- mcl2_agent / tasks/build.lua
-- 建造类任务示例。

mcl2agent.task.register({
    id = "build_wooden_wall",
    name = "Build a Wooden Wall",
    instruction = "Build a wall of 5 wood blocks in front of you.",
    instruction_zh = "在你面前用 5 个木头搭一堵墙。",
    type = "build",
    tags = {"mid_game", "build"},
    difficulty = 3,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 8,
        inventory = {clear = true, give = {"mcl_trees:wood_oak 5"}},
        timeofday = 0.5,
    },
    success_predicate = "block_placed",
    success_args = {
        pos1 = {x = 1, y = 40, z = 0},
        pos2 = {x = 5, y = 41, z = 0},
        name = "mcl_trees:wood_oak", count = 5,
    },
    timeout_ticks = 2400,
})

mcl2agent.task.register({
    id = "build_shelter",
    name = "Build a Shelter",
    instruction = "Build a 3x3x3 shelter around you.",
    instruction_zh = "在你周围搭建一个 3x3x3 的庇护所。",
    type = "build",
    tags = {"mid_game", "build"},
    difficulty = 4,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 10,
        inventory = {clear = true, give = {"mcl_trees:wood_oak 20"}},
        timeofday = 0.5,
    },
    success_predicate = "block_placed",
    success_args = {
        pos1 = {x = -1, y = 39, z = -1},
        pos2 = {x = 1, y = 41, z = 1},
        name = "mcl_trees:wood_oak", count = 9,
    },
    timeout_ticks = 6000,
})
