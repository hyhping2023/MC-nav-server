-- mcl2_agent / test / run_pathfind_test.lua
-- 寻路单测（stub 驱动）：8 方向 A* + 平滑 + 方块目标校正 + goto 集成。
--
-- 运行（在仓库根目录）：
--   lua mcl2_agent/test/run_pathfind_test.lua
-- 任何断言失败会打印原因并以非 0 退出码结束。

local script_path = arg and arg[0] or "mcl2_agent/test/run_pathfind_test.lua"
local test_dir = script_path:match("^(.*)/[^/]+$") or "."
local mod_root = test_dir .. "/.."

local p = io.popen('cd "' .. mod_root .. '" && pwd 2>/dev/null')
if p then
    local abs = p:read("*l")
    p:close()
    if abs and abs ~= "" then mod_root = abs end
end

local worldpath = mod_root .. "/test/tmp_world_pf"

_G.minetest_stub_modpath = mod_root
_G.minetest_stub_worldpath = worldpath

print("== load stub + mod ==")
dofile(test_dir .. "/minetest_stub.lua")
dofile(mod_root .. "/init.lua")

local mt = minetest
local pathfind = mcl2agent.pathfind
assert(pathfind, "pathfind module missing")
assert(pathfind.plan and pathfind.smooth and pathfind.segment_clear, "pathfind API incomplete")

-- ============================================================
-- 世界构造工具
-- ============================================================

local function clear_world()
    mt._node_map = {}
end

local function set_block(x, y, z, name)
    mt.set_node({ x = x, y = y, z = z }, { name = name or "mcl_core:stone" })
end

-- 铺地面（y=0 实心平面，范围 x/z in [-N, N]）
local function ground_plane(n)
    for x = -n, n do
        for z = -n, n do
            set_block(x, 0, z)
        end
    end
end

-- 竖直墙：x 固定，z in [z1,z2]，y in [y1,y2]
local function wall(x, z1, z2, y1, y2)
    for z = z1, z2 do
        for y = y1, y2 do
            set_block(x, y, z)
        end
    end
end

local function hdist2(a, b)
    local dx, dz = a.x - b.x, a.z - b.z
    return math.sqrt(dx * dx + dz * dz)
end

local function cell_key(c)
    return math.floor(c.x) .. "," .. math.floor(c.y) .. "," .. math.floor(c.z)
end

-- 收集路径里所有身体格（整数化）
local function path_cells(plan)
    local out = {}
    for _, w in ipairs(plan.waypoints) do
        out[#out + 1] = { x = math.floor(w.x), y = math.floor(w.y), z = math.floor(w.z) }
    end
    return out
end

-- ============================================================
-- 1) 开放平地：寻路成功 + 平滑合并 + 任意角度（非纯正东西）
-- ============================================================

print("== 1. open field: plan + smooth + diagonal ==")
clear_world()
ground_plane(12)

local plan = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 5, y = 1, z = 5 })
assert(plan.success, "open field plan failed")
assert(#plan.waypoints >= 2, "open field waypoints empty")
assert(#plan.waypoints <= 3,
    "smooth should merge open-field path, got " .. #plan.waypoints .. " waypoints")

-- 任意角度：存在一段 dx/dz 均非 0 的直线段（不再是纯 4 方向 zigzag）
local has_anyangle = false
for i = 2, #plan.waypoints do
    local dx = plan.waypoints[i].x - plan.waypoints[i - 1].x
    local dz = plan.waypoints[i].z - plan.waypoints[i - 1].z
    if math.abs(dx) > 0.5 and math.abs(dz) > 0.5 then
        has_anyangle = true
        break
    end
end
assert(has_anyangle, "path has no diagonal/any-angle segment (stays grid-aligned)")

-- 目标可达（最后一个路径点在终点附近）
local last = plan.waypoints[#plan.waypoints]
assert(hdist2(last, { x = 5, y = 1, z = 5 }) <= 1.0,
    "last waypoint not near goal: " .. cell_key(last))

-- ============================================================
-- 2) 绕墙：路径不穿墙，从缺口绕过
-- ============================================================

print("== 2. wall with gap: route around ==")
clear_world()
ground_plane(12)
-- 高 3 格的墙，x=2, z in [-12,1] 封死，z>=2 留缺口
wall(2, -12, 1, 1, 3)

local plan2 = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 5, y = 1, z = 5 })
assert(plan2.success, "wall plan failed")
local cells = path_cells(plan2)
for _, c in ipairs(cells) do
    assert(not (c.x == 2 and c.z <= 1 and c.y >= 1 and c.y <= 3),
        "path goes through wall at " .. cell_key(c))
end

-- ============================================================
-- 3) 目标是方块（树）：落点校正到相邻可行走格
-- ============================================================

print("== 3. target is solid block (tree): goal snapped ==")
clear_world()
ground_plane(12)
set_block(5, 1, 5)   -- 树干 y=1
set_block(5, 2, 5)   -- 树干 y=2

local plan3 = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 5, y = 1, z = 5 })
assert(plan3.success, "tree-target plan failed")
local last3 = plan3.waypoints[#plan3.waypoints]
assert(hdist2(last3, { x = 5, y = 1, z = 5 }) <= 1.6,
    "goal snap too far from tree: " .. cell_key(last3))
-- 路径不穿树干
for _, c in ipairs(path_cells(plan3)) do
    assert(not (c.x == 5 and c.z == 5 and c.y <= 2), "path goes through tree trunk")
end

-- ============================================================
-- 4) 1 格台阶：路径可跳上（目标在墙顶）
-- ============================================================

print("== 4. one-block step: jump up ==")
clear_world()
ground_plane(12)
set_block(5, 1, 0)   -- 台阶：地面抬到 y=1，站在其上的格为 (5,2,0)

local plan4 = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 5, y = 2, z = 0 })
assert(plan4.success, "step plan failed")
local last4 = plan4.waypoints[#plan4.waypoints]
assert(math.floor(last4.y) == 2, "path should end on top of the step, got y=" .. math.floor(last4.y))

-- ============================================================
-- 5) 全封闭：优雅失败
-- ============================================================

print("== 5. fully enclosed: graceful fail ==")
clear_world()
ground_plane(12)
-- 1x1 格围墙围住起点 (0,1,0)：x/z 四个方向 y=1..3
wall(-1, -1, 1, 1, 3)
wall(1, -1, 1, 1, 3)
wall(0, -1, -1, 1, 3)
wall(0, 1, 1, 1, 3)

local plan5 = pathfind.plan({ x = 0, y = 1, z = 0 }, { x = 5, y = 1, z = 5 })
assert(plan5.success == false, "enclosed start should fail to plan")
assert(#plan5.waypoints == 0, "failed plan should have no waypoints")

-- ============================================================
-- 6) goto 集成：真实玩家驱动，规划 + 按键注入 + 到达成功
-- ============================================================

print("== 6. goto integration (fake player) ==")
clear_world()
ground_plane(12)

local fake = mt.simulate_join("bot1")
assert(mcl2agent.players["bot1"], "session not created on join")
local sess = mcl2agent.players["bot1"]
fake:set_pos({ x = 0, y = 1, z = 0 })

local function run_globalstep(n)
    for _ = 1, n do
        for _, cb in ipairs(mt._globalsteps) do
            cb(0.05)
        end
    end
end

-- 入队 goto（目标为空气格）
mcl2agent.action.execute(sess, "goto", { pos = { x = 5, y = 1, z = 5 } })
run_globalstep(1)
local a = sess.current_action
assert(a and a.id == "goto", "goto not running")
assert(a.path and #a.path >= 2, "goto did not plan a path (straight-line fallback?)")
assert(#mt._control_calls > 0, "set_player_control not called")
local ctrl = mt._control_calls[#mt._control_calls]
assert(ctrl.player == "bot1" and ctrl.controls.up == 1.0, "goto should hold forward (up=1.0)")

-- 模拟到达最后一个路径点 -> 下一步应判定成功
local wp = a.path[#a.path]
fake:set_pos({ x = wp.x, y = wp.y, z = wp.z })
run_globalstep(1)
assert(not sess.current_action, "goto should have completed after reaching target")
local aid = a.action_id
assert(sess.action_log and sess.action_log[aid]
    and sess.action_log[aid].status == "success",
    "goto action_log status != success")

-- 清理会话
mt.simulate_leave("bot1")

-- ============================================================
-- 7) 直线兜底：规划失败时 goto 不崩溃，仍注入前进
-- ============================================================

print("== 7. goto fallback: straight line when plan fails ==")
clear_world()
ground_plane(12)
wall(-1, -1, 1, 1, 3)
wall(1, -1, 1, 1, 3)
wall(0, -1, -1, 1, 3)
wall(0, 1, 1, 1, 3)

local fake2 = mt.simulate_join("bot1")
fake2:set_pos({ x = 0, y = 1, z = 0 })
local sess2 = mcl2agent.players["bot1"]
local before = #mt._control_calls
mcl2agent.action.execute(sess2, "goto", { pos = { x = 5, y = 1, z = 5 } })
run_globalstep(2)
local a2 = sess2.current_action
assert(a2 and a2.path and #a2.path == 1, "fallback should be straight line to target")
assert(#mt._control_calls > before, "fallback goto should still inject controls")
mt.simulate_leave("bot1")

-- ============================================================
-- 8) 竹子等 pathfinder_partial 细高植物：可穿过（否则竹丛里卡死）
-- ============================================================

print("== 8. bamboo (pathfinder_partial) is passable, stone still blocks ==")
mt.register_node_stub("mcl_bamboo:bamboo_small", {
    groups = {pathfinder_partial = 2, choppy = 1}, walkable = true, diggable = true,
})
assert(pathfind.node_solid("mcl_bamboo:bamboo_small") == false,
    "bamboo should be passable (pathfinder_partial)")
assert(pathfind.node_solid("mcl_core:stone") == true,
    "stone should still block")

clear_world()
ground_plane(10)
-- 竹子墙：x=2 一列竹子（y 1..3），bot 在 (0,1,0) 目标 (5,1,5)
for z = -6, 6 do
    for y = 1, 3 do
        set_block(2, y, z, "mcl_bamboo:bamboo_small")
    end
end
local plan_b = pathfind.plan({x = 0, y = 1, z = 0}, {x = 5, y = 1, z = 5})
assert(plan_b and plan_b.success, "bamboo wall should not block path")
local digs = 0
for _, wp in ipairs(plan_b.waypoints) do
    if wp.dig and #wp.dig > 0 then digs = digs + #wp.dig end
end
assert(digs == 0, "bamboo should be walked through, not dug: " .. digs)

-- 对照：石头墙仍然挡路（应绕行且不穿墙）
clear_world()
ground_plane(10)
wall(2, -6, 6, 1, 3)
local plan_s = pathfind.plan({x = 0, y = 1, z = 0}, {x = 5, y = 1, z = 5})
assert(plan_s and plan_s.success, "stone wall plan failed")
for _, wp in ipairs(plan_s.waypoints) do
    assert(math.floor(wp.x + 0.5) ~= 2 or math.floor(wp.y + 0.5) < 1,
        "path should not go through stone wall")
end

print("")
print("ALL PATHFIND TESTS PASSED")
os.exit(0)
