-- mcl2_agent / api/survival.lua
-- 生存辅助：饥饿/饱和读取、背包食物选择、进食。
-- 依赖 mcl_hunger 与食物节点 groups.eatable；缺省优雅降级。

mcl2agent.survival = {}

local survival = mcl2agent.survival

-- 读取玩家饥饿值（0-20）。mcl_hunger 不可用时返回 20（视为不饿）。
function survival.get_hunger(player)
    if not player then return 20 end
    if mcl_hunger and mcl_hunger.get_hunger then
        local ok, v = pcall(mcl_hunger.get_hunger, player)
        if ok and v then return v end
    end
    return 20
end

-- 读取玩家饱和值。不可用时返回 0。
function survival.get_saturation(player)
    if not player then return 0 end
    if mcl_hunger and mcl_hunger.get_saturation then
        local ok, v = pcall(mcl_hunger.get_saturation, player)
        if ok and v then return v end
    end
    return 0
end

-- 从背包选"最优食物"：优先高饥饿恢复（eatable），再比饱和。
-- @return {item=名称, eatable=恢复, saturation=饱和} 或 nil
function survival.find_food(inv)
    if not inv then return nil end
    local best = nil
    for _, slot in ipairs(inv:get_list("main") or {}) do
        if slot and not slot:is_empty() then
            local name = slot:get_name()
            local def = minetest.registered_items and minetest.registered_items[name]
            local eatable = def and def.groups and def.groups.eatable
            if eatable and eatable > 0 then
                local sat = def._mcl_saturation or 0
                if not best or eatable > best.eatable
                   or (eatable == best.eatable and sat > best.saturation) then
                    best = { item = name, eatable = eatable, saturation = sat }
                end
            end
        end
    end
    return best
end

-- 进食是否还有收益（防止饱食满值/满血时浪费食物）
local function can_benefit(player)
    if not player then return false end
    local hunger = survival.get_hunger(player)
    local hp = player:get_hp()
    if hp >= 20 and hunger >= 20 then return false end
    -- mcl_hunger 激活时：饥饿满值进食只可能回饥饿，不回血，无收益
    if mcl_hunger and mcl_hunger.active ~= false and hunger >= 20 then return false end
    return true
end

-- 吃掉最优食物（走真实进食管线 core.do_item_eat：持物→饱食/回血→消耗物品）。
-- @return true 吃到了，false 无可吃/不需要吃/进食失败
function survival.eat(sess)
    local player = mcl2agent.action.get_player(sess)
    local inv = mcl2agent.action.get_inv(sess)
    if not player or not inv then return false end
    if not can_benefit(player) then return false end
    local food = survival.find_food(inv)
    if not food then return false end
    local def = minetest.registered_items and minetest.registered_items[food.item]
    local replace = def and def._mcl_eat_replace_with

    if core.do_item_eat then
        player:set_wielded_item(food.item .. " 1")
        local stack = player:get_wielded_item()
        local ok, res = pcall(core.do_item_eat, food.eatable, replace, stack, player, nil)
        if ok then
            if res then
                player:set_wielded_item(res)
            end
            return true
        end
        return false
    end

    -- 降级：无 mcl_hunger 时直接回血并扣物品（供测试/无游戏环境）
    player:set_hp(math.min(player:get_hp() + food.eatable, 20))
    inv:remove_item("main", food.item .. " 1")
    return true
end
