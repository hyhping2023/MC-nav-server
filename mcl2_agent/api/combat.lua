-- mcl2_agent / api/combat.lua
-- 战斗辅助：敌对实体解析、打击（punch 冷却节流）、手持工具攻击属性。
-- 依赖 mcl_mobs 的 luaentity 字段（ent.type / ent.health）；缺省优雅降级。

mcl2agent.combat = {}

local combat = mcl2agent.combat

-- 敌对判定：mcl_mobs 实体 type == "monster"。玩家/未知实体不算。
function combat.is_hostile(ent)
    return ent ~= nil and ent.type == "monster"
end

-- 解析攻击目标：
--   target == "auto"  -> 最近敌对实体
--   target == 实体名  -> 按 luaentity name 找最近的该类实体（含被动/中立）
-- 返回 {obj=ObjectRef, ent=luaentity, dist=number} 或 nil
function combat.resolve_target(sess, target, max_dist)
    local player = mcl2agent.action.get_player(sess)
    if not player then return nil end
    local ppos = player:get_pos()
    if not ppos then return nil end
    local want_name = (target and target ~= "auto") and tostring(target) or nil
    local best = nil
    for _, obj in ipairs(minetest.get_objects_inside_radius(ppos, max_dist or 16)) do
        local ent = obj:get_luaentity()
        local opos = obj:get_pos()
        -- 跳过非 mcl_mobs 实体（is_mob=false：wieldview/掉落物/载具等）
        if opos and ent and ent.name and ent.is_mob ~= false then
            local is_target
            if want_name then
                is_target = ent.name == want_name
            else
                is_target = combat.is_hostile(ent)
            end
            if is_target then
                local d = vector.distance(ppos, opos)
                if not best or d < best.dist then
                    best = { obj = obj, ent = ent, dist = d }
                end
            end
        end
    end
    return best
end

-- 实体实时 HP：mcl_mobs 把真实血条放 luaentity.health（引擎 get_hp 被 immortal 冻结为 20）。
-- 有 luaentity.health 的实体（生物）优先读它；其余回退引擎 get_hp。死亡返回 0。
function combat.entity_hp(obj)
    if not obj then return nil end
    local ent = obj:get_luaentity()
    if ent and (ent.is_mob or ent.health ~= nil) then
        if ent.dead then return 0 end
        return ent.health or 0
    end
    local ok, hp = pcall(function() return obj:get_hp() end)
    if ok and hp and hp > 0 then return hp end
    if ent and ent.dead then return 0 end
    return ent and ent.health
end

-- 当前手持工具的攻击冷却（full_punch_interval，秒）；缺省 0.625（木剑）。
function combat.attack_cooldown(player)
    local item = player and player:get_wielded_item()
    local def = item and item:get_name()
        and minetest.registered_items and minetest.registered_items[item:get_name()]
    local fpi = def and def.tool_capabilities and def.tool_capabilities.full_punch_interval
    return fpi and fpi > 0 and fpi or 0.625
end

-- 当前手持工具的近战伤害（fleshy 伤害组）；缺省 1（空手）。
function combat.melee_damage(player)
    local item = player and player:get_wielded_item()
    local def = item and item:get_name()
        and minetest.registered_items and minetest.registered_items[item:get_name()]
    local dg = def and def.tool_capabilities and def.tool_capabilities.damage_groups
    return (dg and dg.fleshy) or 1
end

-- 对目标实体打一拳（满冷却伤害）。调用方负责冷却节流。
-- @return true（punch 已发出）
function combat.punch(player, obj)
    if not player or not obj then return false end
    local ppos = player:get_pos()
    local opos = obj:get_pos()
    if not ppos or not opos then return false end
    local dir = vector.direction(ppos, opos)
    local item = player:get_wielded_item()
    local def = item and item:get_name()
        and minetest.registered_items and minetest.registered_items[item:get_name()]
    local caps = def and def.tool_capabilities
    local fpi = combat.attack_cooldown(player)
    local ok = pcall(function()
        obj:punch(player, fpi, caps, dir)
    end)
    return ok == true
end
