-- mcl2_agent / api/reset.lua
-- Episode 软重置：不需要重启引擎。负责种子管理、初始条件应用。
-- 设计见 DESIGN.md §6.5 与 §11。

mcl2agent.reset = {}

-- 探测指定 x/z 处的实际地表高度（自高处向下扫，跳过空气/忽略/液体），返回站立 y（地表+1）。
-- 找不到实心则返回 cfg.y 原值。用于修复"出生点硬编码 y=40 致摔落死亡"（见 DESIGN.md §11）。
function mcl2agent.reset.find_ground(pos)
    local x, z = pos.x, pos.z
    for y = 150, -30, -1 do
        local node = minetest.get_node({x = x, y = y, z = z})
        local name = node and node.name or "air"
        if name ~= "air" and name ~= "ignore" then
            local def = minetest.registered_nodes and minetest.registered_nodes[name]
            local liquid = def and def.liquidtype ~= nil and def.liquidtype ~= "none"
            if not liquid then
                return y + 1
            end
            -- 液体（水/岩浆）：继续向下找实心地面
        end
    end
    return pos.y or 0
end

-- 任务相关传送：在 pos 附近 radius 内找指定方块（如树），站到其旁**地面**。
-- 解决"出生点所在生物群系没有任务所需资源"（如沙漠无树）。
-- 注意从树根向下找地面（跳过树冠 leaves），避免落到树顶上。
-- @param pos table 搜索中心
-- @param spec table {name=方块名, radius=搜索半径}
-- @return 站立位置 {x,y,z} 或 nil（附近没找到）
function mcl2agent.reset.teleport_near(pos, spec)
    if not spec or not spec.name then return nil end
    if not minetest.find_node_near then return nil end  -- 无引擎测试环境降级
    local found = minetest.find_node_near(pos, spec.radius or 64, {spec.name})
    if not found then return nil end

    local function is_leaf_or_ignore(name)
        if name == "ignore" then return true end
        local def = minetest.registered_nodes and minetest.registered_nodes[name]
        return not def or not (def.groups and def.groups.leaves)
    end
    local function is_solid(name)
        if name == "air" or name == "ignore" then return false end
        local def = minetest.registered_nodes and minetest.registered_nodes[name]
        if def and def.groups and def.groups.leaves then return false end
        return def and def.walkable ~= false
    end

    -- 树根处向下找地面实体 y（跳过树叶/树冠）
    local gy = nil
    for y = found.y, found.y - 30, -1 do
        local name = minetest.get_node({x = found.x, y = y, z = found.z}).name
        if is_solid(name) then gy = y break end
    end
    if not gy then return nil end

    -- 站到树旁地面（feet = gy+1），身体格需非实心
    local dirs = {{1,0},{-1,0},{0,1},{0,-1},{1,1},{1,-1},{-1,1},{-1,-1}}
    for _, d in ipairs(dirs) do
        local cand = {x = found.x + d[1], y = gy + 1, z = found.z + d[2]}
        local body = minetest.get_node(cand).name
        local under = minetest.get_node({x = cand.x, y = gy, z = cand.z}).name
        if not is_solid(body) and is_solid(under) then
            return cand
        end
    end
    return nil
end

-- 应用任务的 reset 配置（真实玩家，见 m1_protocol.md §3）
-- @param sess table
-- @param cfg table {pos, area_radius, inventory, timeofday, weather, time_speed, seed}
-- @param seed number 该次 reset 的采样种子（还原关键）
function mcl2agent.reset.apply(sess, cfg, seed)
    return mcl2agent.reset.apply_player(sess, cfg, seed)
end

-- 玩家版软重置（真实玩家 ObjectRef）
function mcl2agent.reset.apply_player(sess, cfg, seed)
    local player = minetest.get_player_by_name(sess.name)
    if not player then return false end

    -- 1) 位置（默认地面感知出生点，避免传送进高空摔死；cfg.pos.surface=false 时用原 y）
    if cfg.pos then
        local y = (cfg.pos.surface ~= false) and mcl2agent.reset.find_ground(cfg.pos) or cfg.pos.y
        player:set_pos({x = cfg.pos.x, y = y, z = cfg.pos.z})
    end

    -- 1.2) 任务相关传送：出生点附近无任务资源（如树）时，挪到目标方块旁
    if cfg.teleport_to_block then
        local t = mcl2agent.reset.teleport_near(cfg.pos or player:get_pos(), cfg.teleport_to_block)
        if t then
            player:set_pos(t)
        end
    end

    -- 1.5) episode 开始静止：清掉上一段残留的注入按键/瞄准目标
    if core.reset_player_control then
        pcall(core.reset_player_control, player)
    end
    if core.clear_player_look_target then
        pcall(core.clear_player_look_target, player)
    end

    -- 2) 清空并设置背包
    if cfg.inventory then
        local inv = player:get_inventory()
        if cfg.inventory.clear then
            inv:set_list("main", {})
            inv:set_list("armor", {})
            inv:set_list("offhand", {})
        end
        for _, give in ipairs(cfg.inventory.give or {}) do
            inv:add_item("main", give)
        end
    end

    -- 3) 生命值/呼吸
    player:set_hp(20)
    player:set_breath(10)

    -- 4) 时间与天气（确定性：钉住 timeofday，避免夜晚怪物与亮度变化）
    if cfg.timeofday then
        minetest.set_timeofday(cfg.timeofday)
        mcl2agent.config.determinism.pin_timeofday = cfg.timeofday  -- 全局 step 持续钉住
    else
        mcl2agent.config.determinism.pin_timeofday = nil
    end
    if cfg.time_speed ~= nil then
        -- TODO: 设置 time_speed 环境键
    end
    if cfg.weather then
        -- TODO: Mineclonia weather API 固定天气
    end

    -- 5) 清理周边区域（重建干净场地，清掉掉落物/生物）；以最终位置（可能已任务传送）为中心
    if cfg.area_radius then
        mcl2agent.reset.clear_area(player:get_pos(), cfg.area_radius)
    end

    -- 6) 任务生物生成（如 kill_animal：在玩家附近生成被动动物供狩猎）
    if cfg.spawn_mobs then
        mcl2agent.reset.spawn_mobs(player:get_pos(), cfg.spawn_mobs)
    end

    -- 记录种子到会话（供 record 写入 meta.json）
    sess.last_reset = {
        seed = seed or 0,
        cfg = cfg,
        applied_tick = mcl2agent.util.tick(),
    }

    return true
end

-- 在 pos 附近生成任务生物（列表项 {name=实体id, count=数量, radius=生成半径}）
-- 落点跳过树冠树叶，生成在地面（实体 y=地面+1）
function mcl2agent.reset.spawn_mobs(pos, list)
    local add_entity = core.add_entity
    for _, spec in ipairs(list or {}) do
        for _ = 1, (spec.count or 1) do
            local r = spec.radius or 4
            local p = {x = pos.x + math.random(-r, r), y = pos.y, z = pos.z + math.random(-r, r)}
            local gy = mcl2agent.reset.ground_below(p, true)
            if gy then
                p.y = gy + 1
            end
            if add_entity then
                add_entity(p, spec.name)
            end
        end
    end
end

-- 从 pos 向下找第一个可站实体面 y（skip_leaves=true 时跳过树冠树叶，防止落到树顶）
function mcl2agent.reset.ground_below(pos, skip_leaves)
    for y = pos.y, pos.y - 30, -1 do
        local name = minetest.get_node({x = pos.x, y = y, z = pos.z}).name
        if name == "ignore" then return nil end
        if name ~= "air" then
            local def = minetest.registered_nodes and minetest.registered_nodes[name]
            local is_leaves = def and def.groups and def.groups.leaves
            local liquid = def and def.liquidtype ~= nil and def.liquidtype ~= "none"
            if not (skip_leaves and is_leaves) and not liquid
               and (def and def.walkable ~= false) then
                return y
            end
        end
    end
    return nil
end

-- 清理指定半径区域：移除掉落物 + 生物（含 mcl_mobs:*），保留地形
function mcl2agent.reset.clear_area(pos, radius)
    for _, obj in ipairs(minetest.get_objects_inside_radius(pos, radius)) do
        local ent = obj:get_luaentity()
        if ent then
            local n = ent.name
            if n == "__builtin:item" or (n and n:find("^mcl_mobs:")) then
                obj:remove()
            end
        end
    end
end

-- 自动重生：数据采集时无人点击 Respawn，死亡后画面会一直停在死亡界面，
-- 导致后续帧全是 "You died" UI（M2-C 发现，见 DESIGN.md §11）。这里在死亡
-- 2 秒后回血并传送回任务安全出生点，客户端 hp>0 时死亡界面自动消失。
minetest.register_on_dieplayer(function(player)
    local name = player:get_player_name()
    -- 仅受管 bot 自动重生；普通玩家走正常死亡/重生流程
    if name ~= mcl2agent.bot.name then return end
    local sess = mcl2agent.players[name]
    if not sess then return end
    minetest.after(2.0, function()
        local p = minetest.get_player_by_name(name)
        if not p then return end
        p:set_hp(20)
        p:set_breath(10)
        local tid = sess.task and sess.task.id
        local tdef = tid and mcl2agent.tasks[tid]
        if tdef and tdef.reset and tdef.reset.pos then
            local cfg = tdef.reset.pos
            local y = (cfg.surface ~= false) and mcl2agent.reset.find_ground(cfg) or cfg.y
            p:set_pos({x = cfg.x, y = y, z = cfg.z})
        end
    end)
end)

-- 完全重置世界（按 seed 重建，仅用于研究/测试）
function mcl2agent.reset.recreate_world(seed)
    -- TODO: 需要改 world.mt + mapgen 参数，通常通过外部进程控制
    return false, "not_implemented"
end
