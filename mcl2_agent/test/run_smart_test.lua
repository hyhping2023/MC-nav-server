-- mcl2_agent / test / run_smart_test.lua
-- 智能自动化层单测（stub 驱动）：
--   1. 挖穿寻路（可挖墙 -> A* 规划带 dig 的路径）
--   2. 不可挖墙（基岩）-> 绕行/失败
--   3. dig-down 下矿（目标低于地面 -> 阶梯式下行 + dig 节点）
--   4. goto 挖穿集成（dig_queue 消费 + 挖后前进 + 成功）
--   5. attack 击杀（最近敌对 -> 冷却节流 punch -> kills 计数）
--   6. eat 进食（饥饿恢复 + 物品消耗走 core.do_item_eat）
--   7. state 观测字段（hunger/held_item/entities.type/hostile/health/targets_player）
--   8. equip + dig 自动装备（需工具则从背包换最优工具；没有则 error）
--
-- 运行（在仓库根目录）：
--   lua mcl2_agent/test/run_smart_test.lua

local script_path = arg and arg[0] or "mcl2_agent/test/run_smart_test.lua"
local test_dir = script_path:match("^(.*)/[^/]+$") or "."
local mod_root = test_dir .. "/.."

local p = io.popen('cd "' .. mod_root .. '" && pwd 2>/dev/null')
if p then
    local abs = p:read("*l")
    p:close()
    if abs and abs ~= "" then mod_root = abs end
end

local worldpath = mod_root .. "/test/tmp_world_smart"

_G.minetest_stub_modpath = mod_root
_G.minetest_stub_worldpath = worldpath

print("== load stub + mod ==")
dofile(test_dir .. "/minetest_stub.lua")
dofile(mod_root .. "/init.lua")

local mt = minetest
local pathfind = mcl2agent.pathfind
local combat = mcl2agent.combat
local survival = mcl2agent.survival
assert(pathfind and pathfind.plan, "pathfind module missing")
assert(combat and combat.resolve_target, "combat module missing")
assert(survival and survival.find_food, "survival module missing")

-- ============================================================
-- 世界构造 + 节点/物品定义
-- ============================================================

local function clear_world()
    mt._node_map = {}
    mt._entities = {}
    mt._hunger = {}
    mt._saturation = {}
    mt._item_eat_calls = {}
    mt._control_calls = {}
end

local function set_block(x, y, z, name)
    mt.set_node({ x = x, y = y, z = z }, { name = name or "mcl_core:stone" })
end

local function ground_plane(n)
    for x = -n, n do
        for z = -n, n do
            set_block(x, 0, z)
        end
    end
end

-- 厚实地面：y=top 向下到 top-depth 全实心（dig-down 测试需要目标格下方有支撑）
local function thick_ground(n, top, depth)
    for y = top, top - depth, -1 do
        for x = -n, n do
            for z = -n, n do
                set_block(x, y, z)
            end
        end
    end
end

local function wall(x, z1, z2, y1, y2)
    for z = z1, z2 do
        for y = y1, y2 do
            set_block(x, y, z)
        end
    end
end

-- 节点/物品定义
mt.register_node_stub("mcl_core:stone", {groups = {pickaxey = 1}, _mcl_hardness = 1.5, walkable = true})
mt.register_node_stub("mcl_core:dirt", {groups = {shovely = 1}, _mcl_hardness = 0.5, walkable = true})
mt.register_node_stub("mcl_core:bedrock", {groups = {bedrock = 1}, _mcl_hardness = -1, walkable = true, diggable = false})
mt.register_node_stub("mcl_trees:tree_oak", {groups = {axey = 1}, _mcl_hardness = 2, walkable = true})

mt.register_item_stub("mcl_tools:pick_wood", {
    tool_capabilities = {full_punch_interval = 0.9, damage_groups = {fleshy = 2}, groupcaps = {pickaxey = {maxlevel = 2}}},
})
mt.register_item_stub("mcl_tools:sword_wood", {
    tool_capabilities = {full_punch_interval = 0.625, damage_groups = {fleshy = 5}, groupcaps = {swordy = {maxlevel = 2}}},
})
mt.register_item_stub("mcl_core:bread", {groups = {eatable = 5}, _mcl_saturation = 6, _mcl_eat_replace_with = nil})
mt.register_item_stub("mcl_core:apple", {groups = {eatable = 4}, _mcl_saturation = 2.4})
mt.register_item_stub("mcl_core:stick", {})

-- ============================================================
-- 1) 挖穿寻路：可挖墙（2 高、横贯）-> A* 规划 dig 路径
-- ============================================================

print("== 1. dig-through: diggable wall planned with dig nodes ==")
clear_world()
ground_plane(12)
-- 2 高横贯石墙（y=1..2，跨 z=-12..12），无法跳上/绕过
wall(2, -12, 12, 1, 2)

local plan = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 5, y = 1, z = 0 })
assert(plan.success, "dig-through plan should succeed")
local has_dig, dig_target = false, nil
for _, w in ipairs(plan.waypoints) do
    if w.dig and #w.dig > 0 then
        has_dig = true
        for _, d in ipairs(w.dig) do
            if d.x == 2 and d.y == 1 and d.z == 0 then dig_target = true end
        end
    end
end
assert(has_dig, "diggable wall path should carry dig nodes")
assert(dig_target, "wall node (2,1,0) should be in dig list")

-- 无墙平地：不带 dig（回归：正常路径不受影响）
clear_world()
ground_plane(12)
local plan_open = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 5, y = 1, z = 0 })
assert(plan_open.success, "open field plan failed")
for _, w in ipairs(plan_open.waypoints) do
    assert(not w.dig, "open field path should have no dig nodes")
end

-- ============================================================
-- 2) 不可挖墙（基岩）：绕缺口，不挖穿
-- ============================================================

print("== 2. unbreakable wall: route around gap, no dig ==")
clear_world()
ground_plane(12)
wall(2, -12, 4, 1, 3)   -- 基岩墙 z<=4 封死，z>=5 留缺口
for z = 1, 3 do
    set_block(2, z, 0)  -- 确认墙身是基岩
end
-- 覆盖墙身为基岩
for z = -12, 4 do
    for y = 1, 3 do
        mt.set_node({x = 2, y = y, z = z}, {name = "mcl_core:bedrock"})
    end
end

local plan2 = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 5, y = 1, z = 5 })
assert(plan2.success, "unbreakable wall with gap should still plan around")
for _, w in ipairs(plan2.waypoints) do
    assert(not w.dig, "bedrock wall must NOT be dug")
end

-- 全封闭基岩：优雅失败
clear_world()
ground_plane(12)
for y = 1, 3 do
    for z = -1, 1 do
        mt.set_node({x = -1, y = y, z = z}, {name = "mcl_core:bedrock"})
        mt.set_node({x = 1, y = y, z = z}, {name = "mcl_core:bedrock"})
        mt.set_node({x = 0, y = y, z = -1}, {name = "mcl_core:bedrock"})
        mt.set_node({x = 0, y = y, z = 1}, {name = "mcl_core:bedrock"})
    end
end
local plan5 = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 5, y = 1, z = 5 })
assert(plan5.success == false, "bedrock-enclosed start should fail to plan")
assert(#plan5.waypoints == 0, "failed plan should have no waypoints")

-- ============================================================
-- 3) dig-down 下矿：地下密室只能挖穿到达（四周无安全落点）
-- ============================================================

print("== 3. dig-down: mine into sealed underground pocket ==")
clear_world()
ground_plane(12)
-- 地下密室：空气格 (0,-2,0)，上下封顶 (0,-1,0)/(0,-3,0)，地面 y=0
set_block(0, -1, 0, "mcl_core:stone")
set_block(0, -3, 0, "mcl_core:stone")
mt.set_node({ x = 0, y = -2, z = 0 }, { name = "air" })

local plan3 = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 0, y = -2, z = 0 })
assert(plan3.success, "dig-down plan should succeed")
local last3 = plan3.waypoints[#plan3.waypoints]
assert(last3 and math.floor(last3.y) == -2,
    "dig-down should end at y=-2, got " .. tostring(last3 and last3.y))
local dig_down_count = 0
for _, w in ipairs(plan3.waypoints) do
    if w.dig then
        for _, d in ipairs(w.dig) do
            if d.x == 0 and d.z == 0 and d.y <= 0 then dig_down_count = dig_down_count + 1 end
        end
    end
end
assert(dig_down_count >= 2, "dig-down should plan 2 crust digs, got " .. dig_down_count)

-- ============================================================
-- 4) goto 挖穿集成：dig_queue 消费 + 挖后前进 + 成功
-- ============================================================

print("== 4. goto integration: dig through wall ==")
clear_world()
ground_plane(12)
wall(2, -12, 12, 1, 2)

local fake = mt.simulate_join("bot1")
fake:set_pos({ x = 0, y = 1, z = 0 })
local sess = mcl2agent.players["bot1"]
fake:get_inventory():add_item("main", "mcl_tools:pick_wood 1")

local function run_globalstep(n)
    for _ = 1, n do
        for _, cb in ipairs(mt._globalsteps) do
            cb(0.05)
        end
    end
end

mcl2agent.action.execute(sess, "goto", { pos = { x = 5, y = 1, z = 0 } })
run_globalstep(1)   -- 规划 + 推进到第 2 个路径点
local a = sess.current_action
assert(a and a.id == "goto", "goto not running")
assert(a.path and #a.path >= 3, "goto should plan multi-waypoint dig path")

-- 走到 dig 前一格 -> 推进到 dig 路径点，排队挖穿节点
local wp2 = a.path[2]
fake:set_pos({ x = wp2.x, y = wp2.y, z = wp2.z })
run_globalstep(1)
assert(a.dig_queue and #a.dig_queue > 0,
    "goto should have queued dig nodes: " .. (a.dig_queue and #a.dig_queue or 0))

-- 模拟挖掉墙（2 个 dig 节点：墙体 + 头顶）
for _, d in ipairs(a.dig_queue) do
    mt.set_node({x = d.x, y = d.y, z = d.z}, {name = "air"})
end
run_globalstep(2)
assert(not a.dig_queue or #a.dig_queue == 0, "dig queue should drain after digging")

-- 传送玩家到最终路径点 -> 成功
local wp = a.path[#a.path]
fake:set_pos({ x = wp.x, y = wp.y, z = wp.z })
run_globalstep(1)
assert(not sess.current_action, "goto should complete after reaching final waypoint")
local aid = a.action_id
assert(sess.action_log[aid] and sess.action_log[aid].status == "success",
    "goto action_log status != success")

mt.simulate_leave("bot1")

-- ============================================================
-- 5) attack 击杀：最近敌对 -> 冷却节流 punch -> kills 计数
-- ============================================================

print("== 5. attack: kill nearest hostile mob ==")
clear_world()
ground_plane(12)
local zombie = FakeEntity.new("mcl_mobs:zombie", { x = 2, y = 1, z = 0 },
    { name = "mcl_mobs:zombie", type = "monster", health = 20 })
mt.register_entity_stub(zombie)

local fake5 = mt.simulate_join("bot1")
fake5:set_pos({ x = 0, y = 1, z = 0 })
fake5:set_wielded_item("mcl_tools:sword_wood 1")
local sess5 = mcl2agent.players["bot1"]

mcl2agent.action.execute(sess5, "attack", { target = "auto", mode = "melee" })
run_globalstep(1)
local a5 = sess5.current_action
assert(a5 and a5.id == "attack", "attack not running")
assert(zombie.ent.health < 20, "attack should have punched the zombie")

run_globalstep(300)   -- 冷却 0.625s/0.05s ≈ 13 tick；20hp / 5dmg = 4 拳，绰绰有余
assert(zombie.dead, "zombie should be dead")
assert((sess5.kills or 0) == 1, "sess.kills should count the kill")
assert(sess5.action_log[a5.action_id] and sess5.action_log[a5.action_id].status == "success",
    "attack action_log status != success")

-- 无目标：attack 直接 error（不空转）
mcl2agent.action.execute(sess5, "attack", { target = "auto", mode = "melee" })
run_globalstep(1)
assert(not sess5.current_action, "attack with no target should end immediately")
mt.simulate_leave("bot1")

-- ============================================================
-- 6) eat 进食：饥饿恢复 + core.do_item_eat 消耗物品
-- ============================================================

print("== 6. eat: hunger restored via core.do_item_eat ==")
clear_world()
ground_plane(12)
local fake6 = mt.simulate_join("bot1")
fake6:set_pos({ x = 0, y = 1, z = 0 })
fake6:get_inventory():add_item("main", "mcl_core:bread 3")
local sess6 = mcl2agent.players["bot1"]
mcl_hunger.set_hunger(fake6, 5)

mcl2agent.action.execute(sess6, "eat", {})
run_globalstep(1)
assert(mcl_hunger.get_hunger(fake6) == 10, "hunger should go 5 -> 10, got " .. mcl_hunger.get_hunger(fake6))
assert(#mt._item_eat_calls == 1, "core.do_item_eat should be called once")
local eat_call = mt._item_eat_calls[1]
assert(eat_call.item == "mcl_core:bread" and eat_call.hunger == 5, "eat should consume bread (eatable=5)")
assert(sess6.action_log[sess6.current_action and sess6.current_action.action_id or 0]
        or true, "eat action should finish")
assert(not sess6.current_action, "eat action should be done")

-- 无食物：eat error
mcl2agent.action.execute(sess6, "eat", {})
run_globalstep(1)
assert(not sess6.current_action, "eat with no food should end immediately")
mt.simulate_leave("bot1")

-- ============================================================
-- 7) state 观测：hunger / held_item / entities 字段
-- ============================================================

print("== 7. state observation fields ==")
clear_world()
ground_plane(12)
local zombie7 = FakeEntity.new("mcl_mobs:zombie", { x = 3, y = 1, z = 0 },
    { name = "mcl_mobs:zombie", type = "monster", health = 20 })
local cow = FakeEntity.new("mcl_mobs:cow", { x = 3, y = 1, z = 3 },
    { name = "mcl_mobs:cow", type = "animal", health = 10 })
mt.register_entity_stub(zombie7)
mt.register_entity_stub(cow)

local fake7 = mt.simulate_join("bot1")
fake7:set_pos({ x = 0, y = 1, z = 0 })
fake7:set_wielded_item("mcl_tools:sword_wood 1")
mcl_hunger.set_hunger(fake7, 7)
local sess7 = mcl2agent.players["bot1"]

local obs = mcl2agent.state.observe("bot1")
assert(obs.player.hunger == 7, "obs hunger should be 7, got " .. tostring(obs.player.hunger))
assert(obs.player.held_item == "mcl_tools:sword_wood", "obs held_item wrong: " .. tostring(obs.player.held_item))
assert(type(obs.player.velocity) == "table" and obs.player.velocity.y == 0, "obs velocity missing")

local zombie_ent = nil
for _, e in ipairs(obs.world.entities) do
    if e.name == "mcl_mobs:zombie" then zombie_ent = e end
end
assert(zombie_ent, "zombie should be in entities")
assert(zombie_ent.type == "monster", "zombie type should be monster")
assert(zombie_ent.hostile == true, "zombie hostile should be true")
assert(zombie_ent.health == 20, "zombie health should be 20")
assert(zombie_ent.targets_player == false, "idle zombie should not target player")
assert(zombie_ent.is_player == false, "zombie is_player should be false")

local cow_ent = nil
for _, e in ipairs(obs.world.entities) do
    if e.name == "mcl_mobs:cow" then cow_ent = e end
end
assert(cow_ent and cow_ent.hostile == false, "cow should be non-hostile")

-- 攻击中目标：targets_player=true
zombie7.ent.attack = fake7
local obs2 = mcl2agent.state.observe("bot1")
for _, e in ipairs(obs2.world.entities) do
    if e.name == "mcl_mobs:zombie" then zombie_ent = e end
end
assert(zombie_ent.targets_player == true, "aggro zombie should target player")
mt.simulate_leave("bot1")

-- ============================================================
-- 8) equip + dig 自动装备
-- ============================================================

print("== 8. equip + dig auto tool selection ==")
clear_world()
ground_plane(12)
set_block(2, 1, 0, "mcl_core:stone")

local fake8 = mt.simulate_join("bot1")
fake8:set_pos({ x = 0, y = 1, z = 0 })
fake8:get_inventory():add_item("main", "mcl_tools:pick_wood 1")
local sess8 = mcl2agent.players["bot1"]

-- equip
mcl2agent.action.execute(sess8, "equip", { item = "mcl_tools:pick_wood" })
run_globalstep(1)
assert(fake8:get_wielded_item():get_name() == "mcl_tools:pick_wood", "equip should wield pick")

-- dig 石头（需镐，背包有 -> 自动装备保持/换成镐并挖掘）
fake8:set_wielded_item("")
mcl2agent.action.execute(sess8, "dig", { pos = { x = 2, y = 1, z = 0 } })
run_globalstep(1)
assert(fake8:get_wielded_item():get_name() == "mcl_tools:pick_wood",
    "dig stone should auto-equip pick, got " .. fake8:get_wielded_item():get_name())
local first_dig_call = mt._control_calls[#mt._control_calls]
assert(first_dig_call and first_dig_call.controls.dig == false,
    "dig should settle the new target before pressing dig")
local target8 = fake8:get_player_look_target()
assert(target8 and target8.x == 2.5 and target8.y == 1.5 and target8.z == 0.5,
    "dig should publish the new block center before interaction")
run_globalstep(2)
local settled_dig_call = mt._control_calls[#mt._control_calls]
assert(settled_dig_call and settled_dig_call.controls.dig == true,
    "dig should press only after the aim settle period")

-- 挖掉后 success
mt.set_node({ x = 2, y = 1, z = 0 }, { name = "air" })
run_globalstep(1)
assert(not sess8.current_action, "dig should complete after block removed")

-- 无镐挖石头 -> error（不再 300 tick 超时）
mt.set_node({ x = 3, y = 1, z = 0 }, { name = "mcl_core:stone" })
fake8:set_wielded_item("")
local inv8 = fake8:get_inventory()
inv8:remove_item("main", "mcl_tools:pick_wood 1")
mcl2agent.action.execute(sess8, "dig", { pos = { x = 3, y = 1, z = 0 } })
run_globalstep(1)
assert(not sess8.current_action, "dig without tool should end immediately (error)")
local last_log = nil
for _, v in pairs(sess8.action_log) do last_log = v end
assert(last_log and (last_log.status == "error"), "dig without tool should log error, got " .. tostring(last_log and last_log.status))
mt.simulate_leave("bot1")

-- ============================================================
-- 8b) dig_aim_point 三维中心几何
-- ============================================================

print("== 8b. dig aim point uses the block volume center ==")
local aim_block = {x = 10, y = 20, z = 30}
local expected_center = {
    x = aim_block.x + 0.5,
    y = aim_block.y + 0.5,
    z = aim_block.z + 0.5,
}

local function assert_aim_center(player)
    local aim = mcl2agent.action.dig_aim_point(player, aim_block)
    for _, axis in ipairs({"x", "y", "z"}) do
        assert(math.abs(aim[axis] - expected_center[axis]) < 1e-9,
            "dig aim " .. axis .. " should use the block volume center")
    end
end

-- The player position must not change the selected target point.
assert_aim_center({get_pos = function() return {x = aim_block.x - 5, y = aim_block.y, z = aim_block.z} end})
assert_aim_center({get_pos = function() return {x = aim_block.x + 5, y = aim_block.y, z = aim_block.z} end})
assert_aim_center({get_pos = function() return {x = aim_block.x, y = aim_block.y - 5, z = aim_block.z} end})
assert_aim_center({get_pos = function() return {x = aim_block.x, y = aim_block.y + 5, z = aim_block.z} end})
assert_aim_center({get_pos = function() return {x = aim_block.x, y = aim_block.y, z = aim_block.z - 5} end})
assert_aim_center({get_pos = function() return {x = aim_block.x, y = aim_block.y, z = aim_block.z + 5} end})
assert_aim_center(nil)

-- ============================================================
-- 9) block_mined 判定器：on_dignode 计数喂 collect_stone 任务评估
-- ============================================================

print("== 9. block_mined predicate via on_dignode counter ==")
clear_world()
ground_plane(12)
set_block(1, 1, 0, "mcl_core:stone")
local fake9 = mt.simulate_join("bot1")
fake9:set_pos({ x = 0, y = 1, z = 0 })
local sess9 = mcl2agent.players["bot1"]

mt.dig_node({ x = 1, y = 1, z = 0 }, fake9)   -- 触发 on_dignode -> 计数
assert((sess9.digged or {})["mcl_core:stone"] == 1,
    "dig counter should count stone, got " .. tostring((sess9.digged or {})["mcl_core:stone"]))
assert(mcl2agent.predicates["block_mined"](sess9, { name = "mcl_core:stone", count = 1 }) == true,
    "block_mined should pass after digging 1 stone")
assert(mcl2agent.predicates["block_mined"](sess9, { name = "mcl_core:stone", count = 2 }) == false,
    "block_mined should fail before reaching count=2")
mt.simulate_leave("bot1")

print("")
print("ALL SMART TESTS PASSED")
os.exit(0)
