-- mcl2_agent / api/pathfind.lua
-- Lua 寻路：8 方向 A* + 跳跃/下落建模 + 挖穿/下挖 + 路径平滑（string-pulling）。
-- 参考 mcl_mobs/pathfinding.lua（怪物 8 方向 A* + 跳跃/下落建模）与
-- mineflayer-pathfinder（Movement 遍历判定 + dig 代价 + 路径平滑）。
--
-- 相比引擎 core.find_path（4 方向、Manhattan、贪心 closed-list 非最优）：
--   - 8 方向（含对角，防切角）
--   - octile 一致启发式，路径更短
--   - 目标为方块（如要挖的树）时自动校正落点到相邻可行走格
--   - 挖穿：墙可挖时按 dig 代价进入 A*，自动权衡"绕远 vs 挖穿"
--   - 下挖：脚下实心可挖时挖一格下行，生成阶梯式下矿道
--   - 输出可被平滑成任意角度直线段，玩家路径不再只沿正东正西
--
-- 坐标约定：格子 = floor(脚位置)；格子 y 即脚所在空气格，地面节点在其 y-1。
-- 路径点输出为脚部位置 {x=cx+0.5, y=cy, z=cz+0.5}，可带 dig={节点列表}（进入该点前需挖）。

mcl2agent.pathfind = {}

local pathfind = mcl2agent.pathfind

-- 默认配置（goto 可通过 opts 覆盖）
pathfind.defaults = {
    max_jump = 1,             -- 最多可跳上的格数（墙高）
    max_drop = 4,             -- 最多可下落格数（>4 会摔伤，见 mcl_serverplayer 摔落伤害）
    search_radius = 32,       -- A* 搜索半径（格）
    max_expansions = 5000,    -- A* 最大扩展节点数（防不可达目标全展开卡死主线程）
    repath_interval = 100,    -- 定期重规划间隔（tick），供 goto 使用
    cost_card = 10,           -- 基数步代价
    cost_diag = 14,           -- 对角步代价（与 octile 启发式一致）
    cost_jump_penalty = 8,    -- 跳跃附加代价
    cost_drop_penalty = 4,    -- 下落附加代价
    cost_dig = 10,            -- 挖穿单位代价（与基数步可比；mineflayer-pathfinder digCost）
    dig_time_scale = 3,       -- dig 时间权重：(1 + dig_time_scale*digTime)*cost_dig
    allow_dig = true,         -- 是否允许 A* 规划挖穿（墙）/下挖（地面）
}

local defaults = pathfind.defaults

-- 供 goto 等外部直接引用的常用配置
pathfind.repath_interval = defaults.repath_interval
pathfind.max_jump = defaults.max_jump
pathfind.max_drop = defaults.max_drop

-- ============================================================
-- 节点查询
-- ============================================================

-- 节点名是否实心（可阻挡）。air/液体(def.walkable=false) 不实心；未知节点默认实心；
-- ignore（未加载区块）视为实心（不可进入）。
function pathfind.node_solid(name)
    if name == "ignore" then return true end
    if name == "air" then return false end
    local def = minetest.registered_nodes and minetest.registered_nodes[name]
    if def then
        -- 细高植物（竹子 pathfinder_partial=2 等）：引擎 minetest.find_path 允许
        -- 穿过（沿格子中心走可避开细杆碰撞），Lua A* 保持一致。否则出生在竹丛里
        -- 8 方向全被判实心 → 无路可走直接卡死。
        local g = def.groups or {}
        if (g.pathfinder_partial or 0) > 0 then return false end
        return def.walkable ~= false
    end
    return true
end

local function is_walkable(pos)
    return pathfind.node_solid(minetest.get_node(pos).name)
end

-- 方块是否可被挖穿（供 A* 决策"绕远 vs 挖穿"）。
-- 不可挖：air/ignore/未知节点/显式 diggable=false/基岩与不可破坏组。
function pathfind.block_diggable(name)
    if name == nil or name == "air" or name == "ignore" then return false end
    local def = minetest.registered_nodes and minetest.registered_nodes[name]
    if not def then return false end
    if def.diggable == false then return false end
    local g = def.groups or {}
    if g.bedrock or g.unbreakable or g.unbreakable_by_hand then return false end
    return true
end

-- 挖穿某节点耗时估计（秒，规划用）。用 _mcl_hardness 近似，缺省 1s。
function pathfind.dig_time(name)
    local def = minetest.registered_nodes and minetest.registered_nodes[name]
    if not def then return 1 end
    local hardness = def._mcl_hardness or 1
    return math.max(0.1, hardness * 1.5)
end

-- 找 cell 脚下的地面节点 y；无地面（悬空/深渊/未加载）返回 nil。
-- dy=1 -> 直接站在 cell.y-1 的地面上（不跳不落）；下落格数 = dy-1。
local function ground_level(cell, max_drop)
    local n = minetest.get_node(cell)
    if n.name == "ignore" then return nil end
    for dy = 1, max_drop + 1 do
        local g = { x = cell.x, y = cell.y - dy, z = cell.z }
        local gn = minetest.get_node(g)
        if gn.name == "ignore" then return nil end
        if pathfind.node_solid(gn.name) then return g.y end
    end
    return nil
end

-- 格子是否可作为站立位（身体格空气 + 脚下有支撑）
local function cell_valid(cell, cfg)
    if is_walkable(cell) then return false end
    return ground_level(cell, cfg.max_drop) ~= nil
end

-- ============================================================
-- 挖穿移动（dig-through / dig-down）
-- ============================================================

-- 一组挖穿节点的总代价（mineflayer-pathfinder laborCost 公式）：
--   cost = sum( (1 + dig_time_scale*digTime(n)) * cost_dig )
local function dig_cost(cfg, nodes)
    local total = 0
    for _, n in ipairs(nodes) do
        local name = minetest.get_node(n).name
        total = total + (1 + cfg.dig_time_scale * pathfind.dig_time(name)) * cfg.cost_dig
    end
    return total
end

-- 正方向挖穿墙：目标格 t 为可挖墙（墙脚下有支撑、头顶空或可挖），
-- 挖开后落位 t（若墙顶更高则连挖 t+1 腾出头部空间）。
-- 返回 {pos=落位, cost, dig={节点列表}} 或 nil。仅限正方向（对角不挖穿）。
local function dig_through_move(cell, dx, dz, base, cfg)
    if not cfg.allow_dig then return nil end
    if dx ~= 0 and dz ~= 0 then return nil end
    local t = { x = cell.x + dx, y = cell.y, z = cell.z + dz }
    local tname = minetest.get_node(t).name
    if not pathfind.block_diggable(tname) then return nil end

    -- 进入 t 需头部（t.y+1）空或可挖
    local nodes = { t }
    local head = { x = t.x, y = t.y + 1, z = t.z }
    local hname = minetest.get_node(head).name
    if pathfind.node_solid(hname) then
        if not pathfind.block_diggable(hname) then return nil end
        table.insert(nodes, head)
    end

    -- 挖后落位：墙必须直接建于地面（t.y-1 实心），否则视为悬浮障碍不走挖穿
    local below = { x = t.x, y = t.y - 1, z = t.z }
    if not pathfind.node_solid(minetest.get_node(below).name) then return nil end

    -- 起跳位置头部（cell.y+2）不能挡（否则挖穿进不去）
    local start_head = { x = cell.x, y = cell.y + 2, z = cell.z }
    if is_walkable(start_head) then return nil end

    return { pos = t, cost = base + dig_cost(cfg, nodes), dig = nodes }
end

-- 下挖一格：脚下实心可挖时挖掉落到其下可站立格（支持挖穿薄地面落进下方空间）。
-- 生成"阶梯式"下行路径，供 goto 下矿洞/下矿道。
local function dig_down_move(cell, base, cfg)
    if not cfg.allow_dig then return nil end
    local below = { x = cell.x, y = cell.y - 1, z = cell.z }
    if not pathfind.block_diggable(minetest.get_node(below).name) then return nil end
    -- 挖掉 below 后落位其下支撑格（ground_level 允许落进已挖开/天然的空间）
    local g = ground_level(below, cfg.max_drop)
    if not g then return nil end
    local dest = { x = below.x, y = g + 1, z = below.z }
    if dest.y >= cell.y then return nil end  -- 必须下行
    local nodes = { below }
    return {
        pos = dest,
        cost = base + dig_cost(cfg, nodes) + cfg.cost_drop_penalty,
        dig = nodes,
    }
end

-- ============================================================
-- 邻域移动（8 方向，含跳跃/下落建模）
-- ============================================================

local DIRS = {
    { 1, 0 }, { -1, 0 }, { 0, 1 }, { 0, -1 },
    { 1, 1 }, { 1, -1 }, { -1, 1 }, { -1, -1 },
}

-- 从 cell 沿 (dx,dz) 移动，返回 {pos=目标格, cost} 或 nil
local function move_to(cell, dx, dz, cfg)
    local t = { x = cell.x + dx, y = cell.y, z = cell.z + dz }
    local base = (dx ~= 0 and dz ~= 0) and cfg.cost_diag or cfg.cost_card

    -- 对角防切角：相邻两个基数格在当前高度必须都是空气
    if dx ~= 0 and dz ~= 0 then
        local ax = { x = cell.x + dx, y = cell.y, z = cell.z }
        local az = { x = cell.x, y = cell.y, z = cell.z + dz }
        if is_walkable(ax) or is_walkable(az) then return nil end
    end

    if is_walkable(t) then
        -- 墙：尝试跳上去（1 格）。墙顶在 t.y+1；需墙顶与头顶皆空
        if cfg.max_jump >= 1 then
            local top = { x = t.x, y = t.y + 1, z = t.z }
            local head = { x = t.x, y = t.y + 2, z = t.z }
            if not is_walkable(top) and not is_walkable(head) then
                -- 起跳位置头顶也需空，否则撞头
                local start_head = { x = cell.x, y = cell.y + 2, z = cell.z }
                if not is_walkable(start_head) then
                    return { pos = top, cost = base + cfg.cost_jump_penalty }
                end
            end
        end
        -- 挖穿：可挖的墙（跳不上去时）——A* 自动权衡绕远 vs 挖穿
        return dig_through_move(cell, dx, dz, base, cfg)
    end

    -- 空气格：找下方支撑（平走 / 下落）
    local g = ground_level(t, cfg.max_drop)
    if not g then return nil end
    local dest = { x = t.x, y = g + 1, z = t.z }
    if dest.y == t.y then
        return { pos = dest, cost = base }
    end
    -- 下落
    return { pos = dest, cost = base + cfg.cost_drop_penalty }
end

-- ============================================================
-- A* 搜索
-- ============================================================

local function key(x, y, z)
    return x .. "," .. y .. "," .. z
end

-- 八方向 octile 启发式（与基数/对角代价一致，可采纳且一致）
local function heuristic(a, b)
    local dx = math.abs(a.x - b.x)
    local dz = math.abs(a.z - b.z)
    local diag = math.min(dx, dz)
    return diag * defaults.cost_diag + (math.max(dx, dz) - diag) * defaults.cost_card
end

-- 最小堆（按 f 排序）
local Heap = {}
Heap.__index = Heap

function Heap.new()
    return setmetatable({ arr = {} }, Heap)
end

function Heap:push(item)
    local a = self.arr
    a[#a + 1] = item
    local i = #a
    while i > 1 do
        local p = math.floor(i / 2)
        if a[p].f <= a[i].f then break end
        a[p], a[i] = a[i], a[p]
        i = p
    end
end

function Heap:pop()
    local a = self.arr
    local top = a[1]
    local last = table.remove(a)
    if #a > 0 then
        a[1] = last
        local i = 1
        while true do
            local l, r = i * 2, i * 2 + 1
            local m = i
            if l <= #a and a[l].f < a[m].f then m = l end
            if r <= #a and a[r].f < a[m].f then m = r end
            if m == i then break end
            a[i], a[m] = a[m], a[i]
            i = m
        end
    end
    return top
end

function Heap:size()
    return #self.arr
end

-- 8 方向 A*，返回格子路径 { {x,y,z}, ... }（含起点终点）或 nil
local function a_star(start, goal, cfg)
    local radius = cfg.search_radius
    local minb = {
        x = math.min(start.x, goal.x) - radius,
        y = math.min(start.y, goal.y) - radius,
        z = math.min(start.z, goal.z) - radius,
    }
    local maxb = {
        x = math.max(start.x, goal.x) + radius,
        y = math.max(start.y, goal.y) + radius,
        z = math.max(start.z, goal.z) + radius,
    }
    local function in_bounds(c)
        return c.x >= minb.x and c.x <= maxb.x
           and c.y >= minb.y and c.y <= maxb.y
           and c.z >= minb.z and c.z <= maxb.z
    end

    local sk = key(start.x, start.y, start.z)
    local gk = key(goal.x, goal.y, goal.z)
    local gscore = { [sk] = 0 }
    local came_from = {}
    local dig_map = {}   -- 到达某格所需挖掉的节点（进格前挖）
    local closed = {}
    local open = Heap.new()
    open:push({ f = heuristic(start, goal), x = start.x, y = start.y, z = start.z })

    -- 松弛一个邻居（horizontal 或 dig-down）
    local function relax(n, parent_key, parent_g)
        if not n then return end
        local nk = key(n.pos.x, n.pos.y, n.pos.z)
        if in_bounds(n.pos) and not closed[nk] then
            local tentative = parent_g + n.cost
            if tentative < (gscore[nk] or math.huge) then
                gscore[nk] = tentative
                came_from[nk] = parent_key
                if n.dig then dig_map[nk] = n.dig end
                open:push({
                    f = tentative + heuristic(n.pos, goal),
                    x = n.pos.x, y = n.pos.y, z = n.pos.z,
                })
            end
        end
    end

    local expansions = 0
    while open:size() > 0 do
        local cur = open:pop()
        local ck = key(cur.x, cur.y, cur.z)
        if not closed[ck] then
            closed[ck] = true
            if ck == gk then
                -- 回溯：goal -> start（携带每格 dig 列表）
                local path = { { x = goal.x, y = goal.y, z = goal.z, dig = dig_map[gk] } }
                local curk = gk
                while curk ~= sk do
                    local pkey = came_from[curk]
                    if not pkey then break end
                    local px, py, pz = pkey:match("^(-?%d+),(-?%d+),(-?%d+)$")
                    table.insert(path, 1, {
                        x = tonumber(px), y = tonumber(py), z = tonumber(pz),
                        dig = dig_map[pkey],
                    })
                    curk = pkey
                end
                return path
            end

            expansions = expansions + 1
            if expansions > cfg.max_expansions then break end

            local cur_cell = { x = cur.x, y = cur.y, z = cur.z }
            local cur_g = gscore[ck] or 0
            for _, d in ipairs(DIRS) do
                relax(move_to(cur_cell, d[1], d[2], cfg), ck, cur_g)
            end
            -- 下挖一格（阶梯式下行）
            relax(dig_down_move(cur_cell, cfg.cost_card, cfg), ck, cur_g)
        end
    end
    return nil
end

-- ============================================================
-- 起点/终点校正
-- ============================================================

-- 终点若是方块（如要挖的树），在 3 格邻域内找最近的可行走空气格。
-- 按曼哈顿距离由近到远扫描，优先"身体格空气 + 脚下有支撑"。
local OFFSETS = {}
do
    for dy = -3, 3 do
        for dx = -3, 3 do
            for dz = -3, 3 do
                local d = math.abs(dx) + math.abs(dy) + math.abs(dz)
                if d >= 1 and d <= 3 then
                    table.insert(OFFSETS, { dx = dx, dy = dy, dz = dz, d = d })
                end
            end
        end
    end
    table.sort(OFFSETS, function(a, b) return a.d < b.d end)
end

local function snap_goal(cell, cfg)
    if cell_valid(cell, cfg) then return cell end
    local fallback = nil
    for _, o in ipairs(OFFSETS) do
        local c = { x = cell.x + o.dx, y = cell.y + o.dy, z = cell.z + o.dz }
        if cell_valid(c, cfg) then
            return c
        end
        -- 兜底：身体格非实心（空气/液体）即可，用于脚下无支撑的悬浮目标
        if not fallback and not is_walkable(c) then
            fallback = c
        end
    end
    return fallback or cell
end

-- 起点悬空时下落至支撑；身体格意外在方块内时上移
local function snap_start(cell, cfg)
    local c = { x = cell.x, y = cell.y, z = cell.z }
    local guard = 0
    while is_walkable(c) and guard < 8 do
        c.y = c.y + 1
        guard = guard + 1
    end
    local g = ground_level(c, cfg.max_drop)
    if g then
        c.y = g + 1
    end
    return c
end

-- ============================================================
-- 路径平滑（string-pulling，任意角度）
-- ============================================================

-- 判断从格 a 直线走到格 b 是否可行：采样检查身体格无墙、地面落差在 jump/drop 内，
-- 允许 1 格上台阶（玩家自动迈上）。
function pathfind.segment_clear(a, b, cfg)
    if a.x == b.x and a.y == b.y and a.z == b.z then return true end
    local len = math.sqrt((a.x - b.x) ^ 2 + (a.y - b.y) ^ 2 + (a.z - b.z) ^ 2)
    local steps = math.max(2, math.ceil(len * 2))
    local last_ground = a.y - 1  -- 起点地面（格 a 脚下）
    for k = 1, steps do
        local t = k / steps
        local s = {
            x = a.x + (b.x - a.x) * t,
            y = a.y + (b.y - a.y) * t,
            z = a.z + (b.z - a.z) * t,
        }
        local body = { x = math.floor(s.x), y = math.floor(s.y), z = math.floor(s.z) }
        if is_walkable(body) then
            -- 1 格上台阶：身体格为墙，但墙顶（body.y+1）可站、头顶（body.y+2）空
            local top = { x = body.x, y = body.y + 1, z = body.z }
            local head = { x = body.x, y = body.y + 2, z = body.z }
            if is_walkable(top) or is_walkable(head) then return false end
            if (body.y - last_ground) > cfg.max_jump then return false end
            last_ground = body.y  -- 墙顶地面 = 墙体所在格
        else
            local g = ground_level(body, cfg.max_drop)
            if not g then return false end
            if g - last_ground > cfg.max_jump then return false end
            if last_ground - g > cfg.max_drop then return false end
            last_ground = g
        end
    end
    return true
end

-- 贪心合并：从当前点向最远的可直接到达点跳，去掉冗余中间节点
function pathfind.smooth(path, cfg)
    local n = #path
    if n <= 2 then return path end
    local result = { path[1] }
    local i = 1
    while i < n do
        local j = n
        while j > i do
            if pathfind.segment_clear(path[i], path[j], cfg) then break end
            j = j - 1
        end
        table.insert(result, path[j])
        i = j
    end
    return result
end

-- ============================================================
-- 规划入口
-- ============================================================

-- plan(feet_pos, target_pos, opts) -> {success=bool, waypoints={{x,y,z},...}, cells={{x,y,z},...}}
-- waypoints 为脚部位置（x/z 取格子中心，y 为脚所在高度）；goto 直接沿 waypoints 走。
-- Lua A* 失败时回退引擎 core.find_path（4 方向打底）再平滑。
function pathfind.plan(feet_pos, target_pos, opts)
    local cfg = {}
    for k, v in pairs(defaults) do cfg[k] = v end
    if opts then
        for k, v in pairs(opts) do cfg[k] = v end
    end

    local sp = mcl2agent.util.to_pos(feet_pos)
    local tp = mcl2agent.util.to_pos(target_pos)
    if not sp or not tp then return { success = false, waypoints = {} } end

    local start_cell = { x = math.floor(sp.x), y = math.floor(sp.y), z = math.floor(sp.z) }
    local goal_cell = { x = math.floor(tp.x), y = math.floor(tp.y), z = math.floor(tp.z) }

    start_cell = snap_start(start_cell, cfg)
    goal_cell = snap_goal(goal_cell, cfg)

    if key(start_cell.x, start_cell.y, start_cell.z) == key(goal_cell.x, goal_cell.y, goal_cell.z) then
        return { success = true, waypoints = {} }
    end

    local path = a_star(start_cell, goal_cell, cfg)

    -- 回退：引擎 A*（4 方向）打底；引擎对"目标是方块"会失败，用校正后的终点再试一次
    if not path and core.find_path then
        local ep
        local ok1, r1 = pcall(core.find_path, sp, tp, cfg.search_radius, cfg.max_jump, cfg.max_drop, "A*_noprefetch")
        if ok1 and r1 and #r1 > 0 then
            ep = r1
        else
            local gpos = { x = goal_cell.x + 0.5, y = goal_cell.y, z = goal_cell.z + 0.5 }
            local ok2, r2 = pcall(core.find_path, sp, gpos, cfg.search_radius, cfg.max_jump, cfg.max_drop, "A*_noprefetch")
            if ok2 and r2 and #r2 > 0 then
                ep = r2
            end
        end
        if ep then
            path = {}
            for _, w in ipairs(ep) do
                table.insert(path, { x = w.x, y = w.y, z = w.z })
            end
        end
    end

    if not path or #path == 0 then
        return { success = false, waypoints = {} }
    end

    -- 含挖穿的路径不平滑（平滑会把 dig 节点信息弄丢），按 A* 格路径走；
    -- 纯走路路径走 string-pulling 平滑（任意角度）。
    local has_dig = false
    for _, c in ipairs(path) do
        if c.dig and #c.dig > 0 then has_dig = true break end
    end
    local smoothed = has_dig and path or pathfind.smooth(path, cfg)

    local waypoints = {}
    for _, c in ipairs(smoothed) do
        local wp = { x = c.x + 0.5, y = c.y, z = c.z + 0.5 }
        if c.dig and #c.dig > 0 then
            wp.dig = c.dig
        end
        table.insert(waypoints, wp)
    end
    return { success = true, waypoints = waypoints, cells = smoothed }
end

return pathfind
