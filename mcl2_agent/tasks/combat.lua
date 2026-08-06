-- mcl2_agent / tasks/combat.lua
-- 战斗类任务示例。

mcl2agent.task.register({
    id = "kill_animal",
    name = "Hunt an Animal",
    instruction = "Kill a passive animal near you.",
    instruction_zh = "击杀你附近的一只被动动物。",
    type = "combat",
    tags = {"early_game", "combat"},
    difficulty = 2,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 8,
        inventory = {clear = true, give = {"mcl_tools:sword_wood 1"}},
        timeofday = 0.5,
        -- 在玩家附近生成被动动物供狩猎（reset.lua spawn_mobs）
        spawn_mobs = {
            {name = "mobs_mc:cow", count = 2, radius = 5},
        },
    },
    success_predicate = "entity_killed",
    success_args = {count = 1},
    timeout_ticks = 3600,
})

mcl2agent.task.register({
    id = "survive_night",
    name = "Survive the Night",
    instruction = "Survive for a full night cycle.",
    instruction_zh = "存活过一整夜。",
    type = "combat",
    tags = {"mid_game", "survival"},
    difficulty = 3,
    reset = {
        pos = {x = 0, y = 40, z = 0},
        area_radius = 8,
        inventory = {clear = true, give = {"mcl_tools:sword_wood 1", "mcl_core:bread 3"}},
        timeofday = 0.75,
    },
    success_predicate = "custom",   -- TODO: 注册 day_count 增长的判定器
    success_args = {},
    timeout_ticks = 12000,
})
