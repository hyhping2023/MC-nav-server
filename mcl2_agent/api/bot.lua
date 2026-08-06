-- mcl2_agent / api/bot.lua
-- 玩家适配层（M1）：把真实玩家 ObjectRef 包装成 bot 接口。
-- 见 docs/m1_protocol.md §3：bot 改为真实玩家驱动，移除加载期自动创建的逻辑 bot。
--
-- 适配器提供与 ObjectRef 兼容的方法子集（get_pos/set_pos/look/inventory/hp/breath 等），
-- 使 state.lua / action.lua / reset.lua / task.lua 可以无差别地把它当作玩家 ObjectRef 使用。
-- 当玩家 bot1 未加入时，各方法返回 nil / 无操作，不报错。

mcl2agent.bot = {
    name = "bot1",
    is_bot = true,
}

local bot = mcl2agent.bot

-- 取 bot1 的真实玩家 ObjectRef；未加入返回 nil
function bot.player()
    return minetest.get_player_by_name(bot.name)
end

-- ============================================================
-- 与 ObjectRef 兼容的方法子集（全部转发给真实玩家）
-- ============================================================

function bot:get_player_name()
    return bot.name
end

function bot:get_pos()
    local p = bot.player()
    if p then return p:get_pos() end
    return nil
end

function bot:set_pos(pos)
    local p = bot.player()
    if p then p:set_pos(pos) end
end

function bot:get_hp()
    local p = bot.player()
    if p then return p:get_hp() end
    return nil
end

function bot:set_hp(h)
    local p = bot.player()
    if p then p:set_hp(h) end
end

function bot:get_breath()
    local p = bot.player()
    if p then return p:get_breath() end
    return nil
end

function bot:set_breath(b)
    local p = bot.player()
    if p then p:set_breath(b) end
end

function bot:get_look_horizontal()
    local p = bot.player()
    if p then return p:get_look_horizontal() end
    return nil
end

function bot:get_look_vertical()
    local p = bot.player()
    if p then return p:get_look_vertical() end
    return nil
end

function bot:set_look_horizontal(yaw)
    local p = bot.player()
    if p then p:set_look_horizontal(yaw) end
end

function bot:set_look_vertical(pitch)
    local p = bot.player()
    if p then p:set_look_vertical(pitch) end
end

-- 由引擎或 yaw/pitch 计算朝向向量
function bot:get_look_dir()
    local p = bot.player()
    if p then return p:get_look_dir() end
    return nil
end

function bot:get_inventory()
    local p = bot.player()
    if p then return p:get_inventory() end
    return nil
end

function bot:get_wielded_item()
    local p = bot.player()
    if p then return p:get_wielded_item() end
    return nil
end

function bot:set_wielded_item(s)
    local p = bot.player()
    if p then p:set_wielded_item(s) end
end

function bot:get_velocity()
    local p = bot.player()
    if p then return p:get_velocity() end
    return nil
end

function bot:is_player()
    return true
end

function bot:set_physics_override(...)
    local p = bot.player()
    if p then return p:set_physics_override(...) end
    return true
end

-- ============================================================
-- 会话与观测
-- ============================================================

-- 会话在 on_joinplayer 中创建（见 init.lua）；本文件不再创建任何会话。

-- 观测：与 state 接口同构；需要玩家已加入
function mcl2agent.bot.observe(sess)
    return mcl2agent.state.observe(bot.name)
end
