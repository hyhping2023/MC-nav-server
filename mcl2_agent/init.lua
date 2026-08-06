-- mcl2_agent / init.lua
-- 引导入口：定义全局命名空间 mcl2agent，按顺序加载各 API 模块。
-- 设计见 DESIGN.md §10。

mcl2agent = {
    version = "0.1.0",
    VERSION = "0.1.0",

    -- 运行时状态（骨架，后续填充）
    players = {},        -- name -> 该 bot 的会话状态
    episodes = {},       -- name -> 当前 episode 记录器句柄
    tasks = {},          -- 任务注册表，见 api/task.lua
    predicates = {},     -- 成功判定器注册表
    actions = {},        -- 语义动作注册表，见 api/action.lua
    config = {},
}

local modpath = minetest.get_modpath("mcl2_agent")

dofile(modpath .. "/config.lua")
dofile(modpath .. "/api/vision.lua")
dofile(modpath .. "/api/state.lua")
dofile(modpath .. "/api/pathfind.lua")
dofile(modpath .. "/api/action.lua")
dofile(modpath .. "/api/survival.lua")
dofile(modpath .. "/api/combat.lua")
dofile(modpath .. "/api/bot.lua")
dofile(modpath .. "/api/task.lua")
dofile(modpath .. "/api/reset.lua")
dofile(modpath .. "/api/record.lua")
dofile(modpath .. "/api/ipc.lua")
dofile(modpath .. "/api/bridge.lua")

-- 加载任务定义
dofile(modpath .. "/tasks/init.lua")
dofile(modpath .. "/tasks/collect.lua")
dofile(modpath .. "/tasks/craft.lua")
dofile(modpath .. "/tasks/build.lua")
dofile(modpath .. "/tasks/combat.lua")

-- 主循环：文件 IPC 轮询 + 驱动动作队列、任务判定
-- 数据采样不再在此定时进行——由 observe bridge handler 请求式触发（m2_protocol.md §1）。
minetest.register_globalstep(function(dtime)
    local tick = mcl2agent.util.tick()
    mcl2agent.ipc.poll(tick)

    -- 钉住 timeofday（采集期确定性：避免昼夜流转导致夜晚怪物，见 reset.lua）
    local pin = mcl2agent.config.determinism.pin_timeofday
    if pin and tick % 5 == 0 then
        minetest.set_timeofday(pin)
    end

    for name, sess in pairs(mcl2agent.players) do
        -- 只驱动"玩家已加入"的会话（bot1 未连接时跳过，见 m1_protocol.md §3）
        if mcl2agent.action.get_player(sess) then
            mcl2agent.action.step(sess, dtime, tick)      -- 推进语义动作/原始动作
            mcl2agent.task.evaluate(sess, tick)            -- 成功/超时判定
        end
    end
end)

minetest.register_on_joinplayer(function(player)
    local name = player:get_player_name()
    mcl2agent.players[name] = mcl2agent.session.new(name)
    local is_bot = (name == mcl2agent.bot.name)

    -- M3：非交互模式只作用于受管 bot（bot1）。普通玩家加入不受任何自动化干预，
    -- 可正常游玩；interaction_mode="api" 时 bot 锁定输入，="user" 时 bot 也可手动操控。
    if is_bot and core.set_player_input_locked
       and mcl2agent.config.action.interaction_mode == "api" then
        core.set_player_input_locked(player, true)
    end

    -- 守卫：仅 bot 玩家文件残留死状态时（hp<=0）回血传送（防止帧 0 录到死屏）
    if is_bot then
        minetest.after(0.1, function()
            local p = minetest.get_player_by_name(name)
            if not p then return end
            if p:get_hp() <= 0 then
                p:set_hp(20)
                p:set_breath(10)
                local safe = mcl2agent.reset.find_ground({x = 0, y = 40, z = 0})
                p:set_pos({x = 0, y = safe, z = 0})
                minetest.log("action", "[mcl2_agent] join-time revive for " .. name .. " (stale dead state)")
            end
        end)
    end
end)

minetest.register_on_leaveplayer(function(player)
    local name = player:get_player_name()
    mcl2agent.record.flush(mcl2agent.players[name])
    mcl2agent.players[name] = nil
end)

minetest.log("action", "[mcl2_agent] loaded v" .. mcl2agent.version)
