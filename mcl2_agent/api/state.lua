-- mcl2_agent / api/state.lua
-- 状态接口：统一的、JSON 可序列化的观测产出。
-- 输出结构见 DESIGN.md §5。
-- 骨架实现：完整字段结构 + 核心字段填充，标注 TODO 的部分为 Mineclonia 特定数据。

local V3 = vector

mcl2agent.state = {}

local function item_count(inv, listname, item_name)
    -- 统计背包列表中某物品总数量
    local list = inv:get_list(listname) or {}
    local total = 0
    for _, stack in ipairs(list) do
        local name = stack:get_name()
        if name == item_name or (name ~= "" and item_name ~= "") then
            if name == item_name then
                total = total + stack:get_count()
            end
        end
    end
    return total
end

local function serialize_list(inv, listname, limit)
    local list = inv:get_list(listname) or {}
    local out = {}
    limit = limit or #list
    for i = 1, math.min(#list, limit) do
        local stack = list[i]
        if stack and not stack:is_empty() then
            table.insert(out, {
                item = mcl2agent.util.alias_item(stack:get_name()),
                count = stack:get_count(),
                meta = nil,  -- TODO: stack:get_meta() 关键字段（如附魔）按需序列化
            })
        end
    end
    return out
end

-- 主入口：产出完整观测
-- @param player ObjectRef
-- @param sess table 会话（含 episode/task 上下文）
-- @return table 可 JSON 序列化
function mcl2agent.state.get_observation(player, sess)
    if not player then return nil end
    local name = player:get_player_name()
    local pos = player:get_pos()
    if not pos then return nil end  -- 玩家未加入 / 位置不可得
    local inv = player:get_inventory()
    if not inv then return nil end
    local hp = player:get_hp()
    local max_hp = 20  -- TODO: 从 Mineclonia 配置/属性读取
    local look_h = player:get_look_horizontal()
    local look_v = player:get_look_vertical()
    local dir = player:get_look_dir()

    local obs = {
        player = {
            pos = {x = pos.x, y = pos.y, z = pos.z},
            look = {
                yaw = look_h,
                pitch = look_v,
                dir = {x = dir.x, y = dir.y, z = dir.z},
            },
            velocity = mcl2agent.state.get_velocity(player),
            on_ground = false,                   -- 由速度/引擎探测
            hp = hp,
            max_hp = max_hp,
            breath = player:get_breath(),
            saturation = mcl2agent.survival.get_saturation(player),
            hunger = mcl2agent.survival.get_hunger(player),
            armor = 0,
            selected_slot = 0,                   -- TODO: 引擎暂未暴露，需客户端/CSM 或 fork
            held_item = "",
            dimension = "overworld",
            effects = {},
        },
        inventory = {
            main = serialize_list(inv, "main", 36),
            armor = serialize_list(inv, "armor", 4),
            offhand = serialize_list(inv, "offhand", 1),
            cursor = nil,
            slots_total = 36,
        },
        world = {
            timeofday = minetest.get_timeofday(),
            day_count = minetest.get_day_count(),
            biome = mcl2agent.state.get_biome(pos),
            weather = mcl2agent.config.determinism.weather or "clear",
            seed = mcl2agent.state.get_world_seed(),
            nearby_blocks = mcl2agent.state.get_nearby_blocks(player, mcl2agent.config.state.nearby_radius or 8),
            aimed_block = mcl2agent.state.get_aimed_block(player),
            entities = mcl2agent.state.get_entities(pos, mcl2agent.config.state.nearby_radius),
            items_on_ground = mcl2agent.state.get_items(pos, mcl2agent.config.state.nearby_radius),
            voxels = nil,  -- config.state.voxels 为 true 时填充
        },
        stats = {
            xp = 0, level = 0, kills = 0, deaths = 0,
            playtime = minetest.get_player_privs(name) and 0 or 0,  -- TODO: 累计
        },
        task = sess and mcl2agent.task.get_observation(sess) or nil,
        episode = sess and mcl2agent.record.get_episode_info(sess) or nil,
        actions = sess and {
            current = sess.current_action and sess.current_action.id or nil,
            queue = #(sess.action_queue or {}),
        } or nil,
    }

    -- 手持物品（引擎 ObjectRef 直接可读）
    local held = player:get_wielded_item()
    obs.player.held_item = mcl2agent.util.alias_item(held and held:get_name() or "")
    -- 速度 y 分量近似着地判定
    obs.player.on_ground = math.abs((obs.player.velocity or {}).y or 0) < 0.05

    -- 局部体素网格（可选）
    if mcl2agent.config.state.voxels then
        obs.world.voxels = mcl2agent.state.get_voxel_grid(pos, mcl2agent.config.state.voxel_half)
    end

    return obs
end

-- 玩家速度（引擎 get_velocity，pcall 兜底为 {0,0,0}）
function mcl2agent.state.get_velocity(player)
    if player and player.get_velocity then
        local ok, v = pcall(player.get_velocity, player)
        if ok and v then return {x = v.x or 0, y = v.y or 0, z = v.z or 0} end
    end
    return {x = 0, y = 0, z = 0}
end

-- 当前 biome 名称
function mcl2agent.state.get_biome(pos)
    local data = minetest.get_biome_data(pos)
    local def = data and data.biome and minetest.registered_biomes[data.biome]
    return def and def.name or "unknown"
end

-- 世界种子：从 world.mt 读取（还原关键）
function mcl2agent.state.get_world_seed()
    local wm = minetest.get_worldpath() .. "/world.mt"
    local f = io.open(wm, "r")
    if not f then return nil end
    local seed = nil
    for line in f:lines() do
        local k, v = line:match("^([^=]+)=(.*)$")
        if k and k:match("seed") then seed = tonumber(v) or v end
    end
    f:close()
    return seed
end

-- 玩家朝向 raycast 命中的方块
function mcl2agent.state.get_aimed_block(player)
    -- observe 的 bot 是 Lua 代理表，look-target C API 只接受真实 ObjectRef。
    -- 解析真实玩家后再读取 target，避免静默回退到旧的服务器 look/raycast。
    local aim_player = player
    local name = player and player.get_player_name and player:get_player_name()
    local real_player = name and minetest.get_player_by_name(name)
    if real_player then aim_player = real_player end
    local pos = aim_player:get_pos()
    -- 准星从眼睛射出（客户端相机在脚 + 眼高），raycast 必须从眼睛发射，
    -- 否则上报的 aimed_block 比准星实际命中的低约 1.62 格。
    local eye = {x = pos.x, y = pos.y + mcl2agent.config.player.eye_height, z = pos.z}
    local dir
    local target
    if core.get_player_look_target then
        local ok, t = pcall(core.get_player_look_target, aim_player)
        if ok then target = t end
    end
    if target then
        -- 客户端以自己预测眼位瞄向该目标点，服务器用同一方向近似
        dir = vector.direction(eye, target)
    else
        dir = aim_player:get_look_dir()
    end
    local p2 = {
        x = eye.x + dir.x * 4.5,
        y = eye.y + dir.y * 4.5,
        z = eye.z + dir.z * 4.5,
    }
    local hit = minetest.raycast(eye, p2, false, false)
    local pointed = hit:next()
    if pointed and pointed.type == "node" then
        local node = minetest.get_node(pointed.under)
        return {
            pos = {x = pointed.under.x, y = pointed.under.y, z = pointed.under.z},
            name = mcl2agent.util.alias_item(node.name),
            param2 = node.param2,
        }
    end
    return nil
end

-- 玩家周围指定半径内的方块列表（用于视野/场景）
function mcl2agent.state.get_nearby_blocks(player, radius)
    local out = {}
    local pos = player:get_pos()
    local px, py, pz = math.floor(pos.x), math.floor(pos.y), math.floor(pos.z)
    local offset = 1  -- 全量扫描（step=1），保证树干/目标方块不因采样跳点漏掉
    for x = px - radius, px + radius, offset do
        for z = pz - radius, pz + radius, offset do
            for y = py - 3, py + 6, 1 do
                local node = minetest.get_node({x = x, y = y, z = z})
                if node.name ~= "air" and node.name ~= "ignore" then
                    table.insert(out, {
                        pos = {x = x, y = y, z = z},
                        name = mcl2agent.util.alias_item(node.name),
                        param2 = node.param2,
                    })
                end
            end
        end
    end
    return out
end

-- 半径内实体（生物/玩家）
function mcl2agent.state.get_entities(pos, radius)
    local out = {}
    for _, obj in ipairs(minetest.get_objects_inside_radius(pos, radius)) do
        local ent = obj:get_luaentity()
        local opos = obj:get_pos()
        local is_player = obj:is_player()
        local item = {
            id = tostring(obj) --[[TODO: 稳定 object id]],
            name = ent and ent.name or (is_player and obj:get_player_name()) or "",
            pos = {x = opos.x, y = opos.y, z = opos.z},
            hp = obj:get_hp(),
            is_player = is_player or false,
        }
        if ent then
            -- mcl_mobs：type=monster/animal/npc；真实血条在 luaentity.health（引擎 get_hp 被冻结）
            item.type = ent.type or nil
            item.health = ent.health or nil
            item.hostile = (ent.type == "monster") or false
            item.is_mob = ent.is_mob or false
            item.targets_player = false
            if ent.attack ~= nil then
                local ok, isp = pcall(function()
                    return ent.attack.is_player and ent.attack:is_player()
                end)
                if ok and isp then
                    item.targets_player = true
                end
            end
            item.name = ent.name
        end
        table.insert(out, item)
    end
    return out
end

-- 半径内掉落物
function mcl2agent.state.get_items(pos, radius)
    local out = {}
    for _, obj in ipairs(minetest.get_objects_inside_radius(pos, radius)) do
        local ent = obj:get_luaentity()
        if ent and ent.name == "__builtin:item" then
            local opos = obj:get_pos()
            table.insert(out, {
                item = mcl2agent.util.alias_item(ent.itemstring),
                pos = {x = opos.x, y = opos.y, z = opos.z},
            })
        end
    end
    return out
end

-- 局部体素网格（MineDojo 风格，可选）
function mcl2agent.state.get_voxel_grid(pos, half)
    local grid = {}
    for dx = -half, half do
        local row = {}
        for dy = -half, half do
            local col = {}
            for dz = -half, half do
                local node = minetest.get_node({
                    x = pos.x + dx, y = pos.y + dy, z = pos.z + dz,
                })
                table.insert(col, mcl2agent.util.alias_item(node.name))
            end
            table.insert(row, col)
        end
        table.insert(grid, row)
    end
    return grid
end

-- 便捷：直接取某主体（bot 或玩家）的完整观测（供 bridge/外部查询）
function mcl2agent.state.observe(name)
    name = name or "bot1"
    local subj
    if mcl2agent.bot and name == mcl2agent.bot.name then
        subj = mcl2agent.bot
    else
        subj = minetest.get_player_by_name(name)
    end
    if not subj then return nil end
    local sess = mcl2agent.players[name]
    return mcl2agent.state.get_observation(subj, sess)
end
