-- mcl2_agent / api/action.lua
-- 动作空间：语义动作注册表 + 原始动作执行器。
-- 动作空间设计见 DESIGN.md §4。
-- 骨架实现：注册表结构 + 原始动作桩（依赖 fork 的 set_player_control）+ 若干语义动作示例。

mcl2agent.action = {}
mcl2agent.session = {}

-- 会话：一个 bot 一个
function mcl2agent.session.new(name)
    return {
        name = name,
        current_action = nil,     -- 当前语义动作 {id, args, action_id, status, t0, progress}
        action_queue = {},        -- 待执行语义动作队列
        action_log = {},          -- 语义动作状态迁移快照 action_id -> {id,args,action_id,status,t0,t_end,frame}（M3-A §1）
        action_counter = 0,
        primitives = {},          -- 每 tick 原始动作（由原始模式写入）
        last_obs = nil,
        task = nil,               -- 当前任务句柄
        episode = nil,            -- EpisodeRecorder
    }
end

-- ============================================================
-- 原始动作执行器（Primitive）
-- ============================================================

-- 执行一组原始动作（每 tick 调用）。
-- 依赖引擎 fork 新增的 core.set_player_control（见 docs/m1_protocol.md §1）。
-- 未 fork 的降级模式：按键注入被跳过，仅支持 set_look_*（视角）与 set_pos（goto）。
-- @param sess table
-- @param p table 与 VPT/MineStudio 字段对齐:
--   {forward,back,left,right,jump,sneak,sprint,attack,use,drop,hotbar,camera={pitch_delta,yaw_delta}}
function mcl2agent.action.exec_primitive(sess, p)
    local subj = mcl2agent.action.get_subject(sess)
    local player = mcl2agent.action.get_player(sess)  -- 真实 ObjectRef（按键注入必需）
    if not subj or not player then return false end

    p = p or {}

    -- 视角（所有模式都支持，增量经 set_look_vertical/horizontal 应用）
    local cam = p.camera
    if cam then
        local pitch, yaw = subj:get_look_vertical(), subj:get_look_horizontal()
        local dp, dy = cam[1] or 0, cam[2] or 0
        subj:set_look_vertical(pitch + dp)
        subj:set_look_horizontal(yaw + dy)
    end

    -- 按键注入（fork 模式）：sprint -> aux1；attack/use -> dig/place
    if mcl2agent.action.control_inject then
        mcl2agent.action.control_inject(player, {
            up = p.forward and 1.0 or 0.0,
            down = p.back and 1.0 or 0.0,
            left = p.left and 1.0 or 0.0,
            right = p.right and 1.0 or 0.0,
            jump = p.jump and true or false,
            sneak = p.sneak and true or false,
            sprint = p.sprint and true or false,
            dig = p.attack and true or false,
            place = p.use and true or false,
            zoom = false,
        })
    end

    -- 快捷栏切换（无 fork 时也支持）
    if p.hotbar ~= nil then
        local inv = subj:get_inventory()
        -- TODO: 通过移槽/切换实现（Mineclonia 持有物由 wielded_item 决定，需按其 API）
    end

    -- 丢弃
    if p.drop then
        -- TODO: 从手中移出到地上
    end

    return true
end

-- fork 注入函数占位：由引擎 fork 的 C++ API 提供
-- core.set_player_control(player, controls) 注册后，此变量被赋值
mcl2agent.action.control_inject = core.set_player_control

-- ============================================================
-- 语义动作（Semantic）注册与执行
-- ============================================================

-- 注册一个语义动作
-- @param def table {
--   name, args_schema, decompose(sess,args)->seq, execute(sess,args,dtime,tick),
--   success_check(sess,args)->bool, timeout(sess,args)->tick
-- }
function mcl2agent.action.register(def)
    mcl2agent.actions[def.name] = def
end

-- 执行语义动作（异步）：入队
-- @return action_id
function mcl2agent.action.execute(sess, name, args)
    local def = mcl2agent.actions[name]
    if not def then return nil, "unknown_action:" .. tostring(name) end
    sess.action_counter = sess.action_counter + 1
    local action = {
        id = name,
        args = args or {},
        action_id = sess.action_counter,
        status = "queued",
        t0 = mcl2agent.util.tick(),
        seq = nil,
    }
    table.insert(sess.action_queue, action)
    mcl2agent.action.log_state(sess, action, "queued", action.t0)
    return action.action_id
end

-- 语义动作状态迁移落盘（M3-A §1）：每次 queued/running/success/timeout/error 迁移时
-- 更新 action_log[action_id] 快照。t0=开始tick；t_end 仅在终止态（success/timeout/error）置为
-- 结束 tick，运行中为 nil（开放区间，供 record.current_action_row 判断该帧时刻是否 running）。
-- frame 取当时 rec.frame（无 episode 时为 nil）。
function mcl2agent.action.log_state(sess, action, status, tick)
    if not sess or not action then return end
    sess.action_log = sess.action_log or {}
    local frame = nil
    if sess.episode then
        frame = sess.episode.frame
    end
    sess.action_log[action.action_id] = {
        id = action.id,
        args = action.args,
        action_id = action.action_id,
        status = status,
        t0 = action.t0,
        -- 终止态 t_end 归一化：execute 的 t0 用 util.tick()（自增时钟）可能比 step 的
        -- 全局 tick 大 1，用 max 保证 t0 <= t_end 的数据一致。
        t_end = (status == "queued" or status == "running") and nil
            or math.max(tick, action.t0 or 0),
        frame = frame,
    }
end

-- ============================================================
-- 执行主体与背包辅助（bot / 玩家通用）
-- ============================================================

-- 取某会话对应的执行主体：bot（玩家适配层）或玩家 ObjectRef
function mcl2agent.action.get_subject(sess)
    if not sess then return nil end
    if mcl2agent.bot and sess.name == mcl2agent.bot.name then
        return mcl2agent.bot
    end
    return minetest.get_player_by_name(sess.name)
end

-- 取某会话对应的真实玩家 ObjectRef（set_player_control / dig_node / place_node 必需）。
-- bot1 未加入时返回 nil。
function mcl2agent.action.get_player(sess)
    if not sess then return nil end
    if mcl2agent.bot and sess.name == mcl2agent.bot.name then
        return mcl2agent.bot.player()
    end
    return minetest.get_player_by_name(sess.name)
end

-- M3：清空该会话玩家被注入的按键（回静止）。仅语义动作结束/异常时调用，
-- 不影响 primitive 模式（random_agent 靠每步 set_player_control 维持按键）。
function mcl2agent.action.reset_controls(sess)
    local p = mcl2agent.action.get_player(sess)
    if p then
        if core.reset_player_control then
            pcall(core.reset_player_control, p)
        elseif core.set_player_control then
            -- Partial API fallback: set_player_control fields are persistent.
            pcall(core.set_player_control, p, {dig = false})
        end
        if core.clear_player_look_target then pcall(core.clear_player_look_target, p) end
    end
end

-- 取会话的背包（bot 用 detached inventory，玩家用其自身背包）
function mcl2agent.action.get_inv(sess)
    local subj = mcl2agent.action.get_subject(sess)
    return subj and subj:get_inventory() or nil
end

-- 统计背包列表中某物品数量
function mcl2agent.action.count_item(inv, listname, item)
    local total = 0
    for _, stack in ipairs(inv:get_list(listname) or {}) do
        if stack and not stack:is_empty() and stack:get_name() == item then
            total = total + stack:get_count()
        end
    end
    return total
end

-- ============================================================
-- 工具选择（挖矿/挖穿用）
-- ============================================================

-- 空手（mcl_meshhand）的 dig 组等级：axey/shovely/handy/hoey=1，
-- pickaxey/swordy=0（石头/矿石类空手挖不动）。
local HAND_DIG_LEVELS = {
    handy = 1, axey = 1, shovely = 1, hoey = 1,
    pickaxey = 0, swordy = 0, swordy_cobweb = 0,
    shearsy = 0, shearsy_wool = 0, shearsy_cobweb = 0,
}

-- 节点要求的 dig group（pickaxey/shovely/axey/swordy/hoey/handy 中首个非 0 值）
-- @return group string|nil, level number|nil（nil 表示无 dig 组，空手即可挖）
function mcl2agent.action.node_requires_tool(node_def)
    local groups = node_def and node_def.groups
    if not groups then return nil end
    for _, g in ipairs({"pickaxey", "shovely", "axey", "swordy", "hoey", "handy"}) do
        local v = groups[g]
        if v and v > 0 then return g, v end
    end
    return nil
end

-- 空手能否开采该节点（dig 组等级 <= 空手等级；如木头 axey=1 可空手，石头 pickaxey=1 不行）
function mcl2agent.action.hand_can_harvest(node_def)
    local group, level = mcl2agent.action.node_requires_tool(node_def)
    if not group then return true end
    return level <= (HAND_DIG_LEVELS[group] or 0)
end

-- 工具 tool_capabilities 能否挖某 group（含等级要求）
function mcl2agent.action.tool_can_dig(caps, group, level)
    if not caps or not caps.groupcaps then return false end
    local gc = caps.groupcaps[group]
    if not gc then return false end
    if gc.maxlevel and level and gc.maxlevel < level then return false end
    return true
end

-- 从背包找能挖 node_def 的最优工具（第一个可用即返回；后续可加挖速比较）
function mcl2agent.action.best_tool_for(inv, node_def)
    local group, level = mcl2agent.action.node_requires_tool(node_def)
    if not group then return nil end  -- 无 dig 组，无需换工具
    if not inv then return nil end
    for _, slot in ipairs(inv:get_list("main") or {}) do
        if slot and not slot:is_empty() then
            local iname = slot:get_name()
            local idef = minetest.registered_items and minetest.registered_items[iname]
            local caps = idef and idef.tool_capabilities
            if caps and mcl2agent.action.tool_can_dig(caps, group, level) then
                return iname
            end
        end
    end
    return nil
end

-- 确保手持工具能挖 node_name：当前可挖则不动，否则从背包换最优工具；
-- 换不到但空手能挖则维持空手（慢挖）；空手也挖不动则返回空手（调用方决定）。
-- @return 最终手持物品名
function mcl2agent.action.ensure_tool_for_node(sess, node_name)
    local player = mcl2agent.action.get_player(sess)
    if not player or not node_name then return nil end
    local node_def = minetest.registered_nodes and minetest.registered_nodes[node_name]
    if not node_def then return nil end

    local held = player:get_wielded_item()
    local held_def = held and held:get_name()
        and minetest.registered_items and minetest.registered_items[held:get_name()]
    local held_caps = held_def and held_def.tool_capabilities
    local group, level = mcl2agent.action.node_requires_tool(node_def)
    if group then
        if held_caps and mcl2agent.action.tool_can_dig(held_caps, group, level) then
            return held:get_name()
        end
        local best = mcl2agent.action.best_tool_for(mcl2agent.action.get_inv(sess), node_def)
        if best then
            player:set_wielded_item(best .. " 1")
            return best
        end
        return held and held:get_name() or ""
    end
    return held and held:get_name() or ""
end

-- 主循环步进：推进当前语义动作
-- M3-A：状态迁移发生处——running/success/timeout/error 均写 action_log。
function mcl2agent.action.step(sess, dtime, tick)
    -- 从队列取下一个
    if not sess.current_action and #sess.action_queue > 0 then
        sess.current_action = table.remove(sess.action_queue, 1)
        sess.current_action.status = "running"
        mcl2agent.action.log_state(sess, sess.current_action, "running", tick)
        local def = mcl2agent.actions[sess.current_action.id]
        if def and def.decompose then
            sess.current_action.seq = def.decompose(sess, sess.current_action.args) or {}
        end
    end

    local a = sess.current_action
    if not a then return end

    local def = mcl2agent.actions[a.id]
    if not def then
        a.status = "error"
        mcl2agent.action.log_state(sess, a, "error", tick)
        mcl2agent.action.reset_controls(sess)
        sess.current_action = nil
        return
    end

    -- 执行器
    if def.execute then
        def.execute(sess, a, dtime, tick)
    end

    -- execute 内部可能置错（如 craft 配方缺失）：按状态迁移落盘并结束
    if a.status ~= "running" and a.status ~= "success" then
        mcl2agent.action.log_state(sess, a, a.status or "error", tick)
        mcl2agent.action.reset_controls(sess)
        sess.current_action = nil
        return
    end

    -- 成功检查
    if def.success_check and def.success_check(sess, a.args) then
        a.status = "success"
        mcl2agent.action.log_state(sess, a, "success", tick)
        mcl2agent.action.reset_controls(sess)
        sess.current_action = nil
        return
    end

    -- 超时
    local limit = def.timeout and def.timeout(sess, a.args) or mcl2agent.config.action.default_timeout
    if tick - a.t0 > limit then
        a.status = "timeout"
        mcl2agent.action.log_state(sess, a, "timeout", tick)
        mcl2agent.action.reset_controls(sess)
        sess.current_action = nil
    end
end

-- ============================================================
-- 内置语义动作（M0 简化实现，非物理）
-- ============================================================

-- 朝向某位置（bot 直接改 look；玩家用 set_look_*）
mcl2agent.action.register({
    name = "look_at",
    args_schema = {pos = "vec3"},
    execute = function(sess, a, dtime, tick)
        local subj = mcl2agent.action.get_subject(sess)
        if not subj then return end
        local p = subj:get_pos()
        local t = mcl2agent.util.to_pos(a.args.pos)
        if not t then return end
        -- 准星从眼睛射出（客户端相机在脚 + 眼高），必须从眼睛算俯仰角
        local eye = {x = p.x, y = p.y + mcl2agent.config.player.eye_height, z = p.z}
        local dir = vector.direction(eye, t)
        local yaw = mcl2agent.util.atan2(dir.x, dir.z)
        local pitch = -math.asin(dir.y / (vector.length(dir) + 1e-9))
        subj:set_look_horizontal(yaw)
        subj:set_look_vertical(pitch)
        -- 目标点必须发给真实玩家 ObjectRef（bot 的 subj 是代理表，C API 不认）
        local player = mcl2agent.action.get_player(sess)
        if player and core.set_player_look_target then
            core.set_player_look_target(player, t)
        end
    end,
    success_check = function(sess, args)
        local subj = mcl2agent.action.get_subject(sess)
        if not subj then return false end
        local p = subj:get_pos()
        local t = mcl2agent.util.to_pos(args.pos)
        if not t then return false end
        local eye = {x = p.x, y = p.y + mcl2agent.config.player.eye_height, z = p.z}
        local dir = vector.direction(eye, t)
        local look = subj:get_look_dir()
        local dot = (dir.x * look.x + dir.y * look.y + dir.z * look.z) /
            ((vector.length(dir) or 1) + 1e-9)
        return dot > 0.99
    end,
    timeout = function() return 60 end,
})

-- 导航到某位置：Lua 寻路（pathfind.lua，8 方向 A* + 平滑 + 挖穿 + 定期重规划）。
-- 目标可为方块（如要挖的树），plan 会自动校正落点到相邻可行走格。
-- 路径中带 dig 的路径点会先挖穿（装备最优工具）再继续移动。
mcl2agent.action.register({
    name = "goto",
    args_schema = {pos = "vec3", tolerance = "number"},
    execute = function(sess, a, dtime, tick)
        local player = mcl2agent.action.get_player(sess)
        if not player then return end
        local target = mcl2agent.util.to_pos(a.args.pos)
        if not target then return end
        a.args.tolerance = a.args.tolerance or 1.0
        local p = player:get_pos()
        if not p then return end

        -- 首次执行：规划路径；失败则直线兜底
        if not a.path then
            local plan
            local t0 = os.clock()
            plan = mcl2agent.pathfind.plan(p, target)
            local dt = os.clock() - t0
            if dt > 3.0 then
                -- 规划超时（A* 全展开/引擎回退慢）：直接失败，不阻塞主线程
                minetest.log("warning", "[mcl2_agent] goto plan too slow (" .. dt .. "s), abort")
                a.status = "error"
                return
            end
            a.path = (plan and plan.success) and plan.waypoints or { target }
            a.wp_idx = 1
            a.stuck_ticks = 0
            a.last_pos = p
            a.repath_ticks = 0
            a.planned_pos = p
            a.dig_queue = nil
            a.dig_stuck = 0
        end

        -- 定期重规划：仅在自规划以来有位移或卡住时进行，避免原地抖动
        a.repath_ticks = (a.repath_ticks or 0) + 1
        local moved_since_plan = vector.distance(p, a.planned_pos or p)
        if a.repath_ticks >= mcl2agent.pathfind.repath_interval
           and (moved_since_plan > 1.0 or (a.stuck_ticks or 0) > 10)
           and #a.path > 1 then
            a.path = nil  -- 下一 tick 重新规划
            a.repath_ticks = 0
            return
        end

        -- 挖穿相位：进入下一路径点前先挖掉其 dig 节点（挖完自动继续）
        if a.dig_queue and #a.dig_queue > 0 then
            mcl2agent.action.do_dig_phase(sess, a)
            return
        end

        local wp = a.path[a.wp_idx] or target
        local dx, dz = wp.x - p.x, wp.z - p.z
        local hdist = math.sqrt(dx * dx + dz * dz)

        -- 到达当前路径点，前进到下一个
        if hdist <= (a.args.tolerance or 1.0) then
            if a.wp_idx < #a.path then
                a.wp_idx = a.wp_idx + 1
                wp = a.path[a.wp_idx]
                a.stuck_ticks = 0
                a.last_pos = p
                -- 排队下一路径点所需的挖穿节点
                if wp.dig and #wp.dig > 0 then
                    a.dig_queue = {}
                    for _, d in ipairs(wp.dig) do
                        table.insert(a.dig_queue, mcl2agent.util.to_pos(d))
                    end
                    a.dig_stuck = 0
                end
            else
                -- 最终到达：停止（success_check 判定成功）
                mcl2agent.action.reset_controls(sess)
                return
            end
        end

        -- 朝向当前路径点；接近目标且下一段是长段（>2 格，平滑路径）时预转向，
        -- 平滑转弯避免到点急转（相邻格路径不启用，防切角）
        local look_wp = wp
        if a.wp_idx < #a.path then
            local nxt = a.path[a.wp_idx + 1]
            local wdx, wdz = wp.x - p.x, wp.z - p.z
            local wd = math.sqrt(wdx * wdx + wdz * wdz)
            local ndx, ndz = nxt.x - p.x, nxt.z - p.z
            local nd = math.sqrt(ndx * ndx + ndz * ndz)
            if wd <= 1.5 and nd - wd >= 2.0 then
                look_wp = nxt
            end
        end
        -- 平视前方路径点（移动只关心 yaw；pitch 保持水平避免低头看脚）
        mcl2agent.action.look_at_pos(player, {
            x = look_wp.x,
            y = p.y + mcl2agent.config.player.eye_height,
            z = look_wp.z,
        })

        -- 卡住检测：只在真正产生位移时清零（跳一下不算解除卡住）
        a.last_pos = a.last_pos or p
        local moved = vector.distance(p, a.last_pos)
        if moved < 0.04 then
            a.stuck_ticks = (a.stuck_ticks or 0) + 1
        else
            a.stuck_ticks = 0
            a.last_pos = p
        end

        -- 注入前进；下一格更高（约 1 格台阶/墙顶）时按住跳
        local needs_jump = (wp.y - p.y) >= 0.8 and (wp.y - p.y) <= 2.2
        if core.set_player_control then
            if a.stuck_ticks > 25 then
                -- 长时间卡住：带跳强冲 + 下 tick 强制重规划
                a.path = nil
                core.set_player_control(player, {up = 1.0, jump = true})
            elseif a.stuck_ticks > 8 then
                -- 短暂卡住：带跳前冲（可迈 1 格台阶/跳出小坑）
                core.set_player_control(player, {up = 1.0, jump = true})
            elseif needs_jump then
                core.set_player_control(player, {up = 1.0, jump = true})
            else
                core.set_player_control(player, {up = 1.0})
            end
        end

        -- 长时间无法前进：报错结束（不再死等 timeout）
        if a.stuck_ticks > 120 then
            a.status = "error"
        end
    end,
    success_check = function(sess, args)
        local player = mcl2agent.action.get_player(sess)
        if not player then return false end
        local p = player:get_pos()
        -- 以最终路径点为准（dig-down 后 y 已下降；目标方块被 snap 到相邻格）
        local a = sess.current_action
        local t
        if a and a.path and #a.path > 0 then
            local last = a.path[#a.path]
            t = { x = last.x, y = last.y, z = last.z }
        else
            t = mcl2agent.util.to_pos(args.pos)
        end
        if not p or not t then return false end
        local dx, dz = t.x - p.x, t.z - p.z
        local hd = math.sqrt(dx * dx + dz * dz)
        local dy = math.abs(t.y - p.y)
        return hd <= math.max(args.tolerance or 1.2, 2.0) and dy <= 3.0
    end,
    timeout = function() return 900 end,
})

-- 挖穿相位：按队列逐个挖掉阻挡节点（装备最优工具、面向目标、按住挖掘）。
-- 队列清空后由 goto 继续移动；长时间挖不动升级为 error。
function mcl2agent.action.do_dig_phase(sess, a)
    local player = mcl2agent.action.get_player(sess)
    if not player then return end
    local node_pos = a.dig_queue[1]
    if not node_pos then
        a.dig_queue = nil
        a.dig_stuck = 0
        a.dig_target_key = nil
        a.dig_aim_ticks = nil
        mcl2agent.action.reset_controls(sess)
        return
    end
    local node = minetest.get_node(mcl2agent.util.to_pos(node_pos))
    if node.name == "air" or node.name == "ignore" then
        -- 队头已被其它动作挖空：先停止旧 dig/target，不能让旧射线
        -- 延续到下一 server tick，否则会复用上一个方块的瞄准点。
        mcl2agent.action.reset_controls(sess)
        table.remove(a.dig_queue, 1)
        a.dig_tool_checked = nil  -- 下一节点重新选工具
        a.dig_stuck = 0
        a.dig_target_key = nil
        a.dig_aim_ticks = nil
        return
    end
    -- 装备合适工具（每节点首次）
    if not a.dig_tool_checked then
        a.dig_tool_checked = true
        mcl2agent.action.ensure_tool_for_node(sess, node.name)
    end
    -- 每个挖穿节点都先稳定瞄准三维中心，再开始按住挖掘。
    local np = mcl2agent.util.to_pos(node_pos)
    mcl2agent.action.aim_then_dig(player, a, np)
    a.dig_stuck = (a.dig_stuck or 0) + 1
    if a.dig_stuck > 400 then
        a.dig_queue = nil
        a.status = "error"
    end
end

-- 计算挖掘的瞄准点：始终使用目标方块的三维体积中心。
-- 客户端会从实际 camera origin 直接指向该点，保证画面准星定位在方块中心。
function mcl2agent.action.dig_aim_point(player, block_pos)
    return {
        x = block_pos.x + 0.5,
        y = block_pos.y + 0.5,
        z = block_pos.z + 0.5,
    }
end

-- 目标切换后先连续发送若干 tick 的 look + dig=false，给客户端一个完整
-- 的网络/相机更新周期，再开始真实交互，避免第一帧沿用上一个方块的射线。
local DIG_AIM_SETTLE_TICKS = 2

local function dig_target_key(block_pos)
    return string.format("%s,%s,%s", block_pos.x, block_pos.y, block_pos.z)
end

function mcl2agent.action.aim_then_dig(player, state, block_pos)
    local key = dig_target_key(block_pos)
    if state.dig_target_key ~= key then
        state.dig_target_key = key
        state.dig_aim_ticks = DIG_AIM_SETTLE_TICKS
    end

    mcl2agent.action.look_at_pos(player, mcl2agent.action.dig_aim_point(player, block_pos))
    if (state.dig_aim_ticks or 0) > 0 then
        state.dig_aim_ticks = state.dig_aim_ticks - 1
        if core.set_player_control then
            core.set_player_control(player, {dig = false})
        end
        return false
    end

    if core.set_player_control then
        core.set_player_control(player, {dig = true})
    end
    return true
end

-- 挖掘指定位置方块（core.dig_node(pos, player)，玩家作为 digger 触发掉落物/统计）
-- 朝向某位置（供 dig/place 等动作复用）。
-- 关键：准星从**眼睛**发射（客户端相机在脚+眼高），必须从眼睛位置算俯仰角，
-- 否则纵向目标（高处原木/低头挖地）角度系统性偏斜、交叉线打不中。
function mcl2agent.action.look_at_pos(player, pos)
    local p = player:get_pos()
    if not p or not pos then return end
    local eye = { x = p.x, y = p.y + mcl2agent.config.player.eye_height, z = p.z }
    local dir = vector.direction(eye, pos)
    local len = vector.length(dir)
    if len < 1e-9 then return end
    local yaw = mcl2agent.util.atan2(dir.x, dir.z)
    local pitch = -math.asin(dir.y / len)
    player:set_look_horizontal(yaw)
    player:set_look_vertical(pitch)
    -- M3.5: client-authoritative look——服务器下发目标点，客户端用自己的预测
    -- 眼位每帧计算 look，消除服务器 pos 滞后导致的准星偏移。
    if core.set_player_look_target then
        pcall(core.set_player_look_target, player, pos)
    end
end

mcl2agent.action.register({
    name = "dig",
    args_schema = {pos = "vec3"},
    execute = function(sess, a, dtime, tick)
        local p = mcl2agent.util.to_pos(a.args.pos)
        local player = mcl2agent.action.get_player(sess)
        if not p or not player then return end
        if not a.phase then
            a.phase = "digging"
        end
        -- 目标已被提前挖空时，先清理旧 target/dig，不要让上一块的
        -- 交互状态延续到这个 action。
        local node = minetest.get_node(p)
        if node.name == "air" or node.name == "ignore" then
            mcl2agent.action.reset_controls(sess)
            a.dig_target_key = nil
            a.dig_aim_ticks = nil
            return
        end
        -- 自动装备合适工具（首 tick）：当前工具挖不动时从背包换最优工具；
        -- 换不到且空手也挖不动（如石头无镐）-> 直接 error（比 300 tick 超时更符合预期）
        if not a.tool_checked then
            a.tool_checked = true
            if node and node.name ~= "air" and node.name ~= "ignore" then
                local ndef = minetest.registered_nodes and minetest.registered_nodes[node.name]
                local group, level = mcl2agent.action.node_requires_tool(ndef)
                if group then
                    local inv = mcl2agent.action.get_inv(sess)
                    local held = player:get_wielded_item()
                    local held_def = held and held:get_name()
                        and minetest.registered_items and minetest.registered_items[held:get_name()]
                    local held_caps = held_def and held_def.tool_capabilities
                    local ok_held = held_caps and mcl2agent.action.tool_can_dig(held_caps, group, level)
                    if not ok_held then
                        local best = mcl2agent.action.best_tool_for(inv, ndef)
                        if best then
                            player:set_wielded_item(best .. " 1")
                        elseif not mcl2agent.action.hand_can_harvest(ndef) then
                            a.status = "error"  -- 需要工具但没有且空手挖不动（如石头无镐）
                            return
                        end
                    end
                end
            end
        end
        -- 每个目标先稳定瞄准三维中心，再开始按住挖掘键；目标切换时
        -- 不复用上一方块的 dig/target 状态。
        mcl2agent.action.aim_then_dig(player, a, p)
    end,
    success_check = function(sess, args)
        local p = mcl2agent.util.to_pos(args.pos)
        if not p then return false end
        local node = minetest.get_node(p)
        return node.name == "air"
    end,
    timeout = function() return 300 end,
})

-- 放置方块（core.place_node(pos, {name=item}, player)，并扣背包 1 个）
mcl2agent.action.register({
    name = "place",
    args_schema = {item = "string", pos = "vec3"},
    execute = function(sess, a, dtime, tick)
        local p = mcl2agent.util.to_pos(a.args.pos)
        if not p or not a.args.item then return end
        local inv = mcl2agent.action.get_inv(sess)
        if inv then
            inv:remove_item("main", a.args.item .. " 1")
        end
        core.place_node(p, {name = a.args.item}, mcl2agent.action.get_player(sess))
    end,
    success_check = function(sess, args)
        local p = mcl2agent.util.to_pos(args.pos)
        if not p then return false end
        local node = minetest.get_node(p)
        return node.name == args.item or node.name == mcl2agent.util.alias_item(args.item)
    end,
    timeout = function() return 200 end,
})

-- ============================================================
-- 合成（M0：模拟合成，查 mod 内建配方表）
-- 配方表后续对接 Mineclonia mcl_crafting（见 docs/m0_protocol.md §4）
-- ============================================================

mcl2agent.recipes = mcl2agent.recipes or {
    ["mcl_trees:wood_oak"] = {input = {["mcl_trees:tree_oak"] = 1}, output = 4},
}

local function can_craft(inv, recipe)
    for input_item, need in pairs(recipe.input or {}) do
        if mcl2agent.action.count_item(inv, "main", input_item) < need then
            return false
        end
    end
    return true
end

mcl2agent.action.register({
    name = "craft",
    args_schema = {item = "string", count = "number", where = "string"},
    execute = function(sess, a, dtime, tick)
        local inv = mcl2agent.action.get_inv(sess)
        if not inv then return end
        local item = a.args.item
        local recipe = mcl2agent.recipes[item]
        if not recipe then
            a.status = "error"
            return
        end
        local count = a.args.count or 1
        local have = mcl2agent.action.count_item(inv, "main", item)
        local needed = math.max(0, count - have)
        local crafts = math.ceil(needed / recipe.output)
        for _ = 1, crafts do
            if not can_craft(inv, recipe) then break end
            for input_item, need in pairs(recipe.input) do
                inv:remove_item("main", input_item .. " " .. need)
            end
            inv:add_item("main", item .. " " .. recipe.output)
        end
    end,
    success_check = function(sess, args)
        local inv = mcl2agent.action.get_inv(sess)
        if not inv then return false end
        return mcl2agent.action.count_item(inv, "main", args.item) >= (args.count or 1)
    end,
    timeout = function() return 400 end,
})

-- ============================================================
-- 战斗 / 生存语义动作
-- ============================================================

-- 切换手持物品
mcl2agent.action.register({
    name = "equip",
    args_schema = {item = "string"},
    execute = function(sess, a, dtime, tick)
        local player = mcl2agent.action.get_player(sess)
        if not player or not a.args.item then return end
        player:set_wielded_item(a.args.item .. " 1")
    end,
    success_check = function(sess, args)
        local player = mcl2agent.action.get_player(sess)
        if not player then return false end
        local held = player:get_wielded_item()
        return held ~= nil and held:get_name() == args.item
    end,
    timeout = function() return 60 end,
})

-- 攻击/战斗：target="auto" 打最近敌对实体，target=<实体名> 打指定实体。
-- mode="melee" 靠近后站桩输出（按 full_punch_interval 节流拿满伤）；
-- mode="kite" 目标贴身时后退保持距离（拉扯）。success = 目标死亡/消失。
mcl2agent.action.register({
    name = "attack",
    args_schema = {target = "string", mode = "string"},
    execute = function(sess, a, dtime, tick)
        local player = mcl2agent.action.get_player(sess)
        if not player then return end

        -- 首 tick 解析目标
        if not a.target_obj then
            local t = mcl2agent.combat.resolve_target(sess, a.args.target or "auto", 16)
            if not t then
                a.status = "error"   -- 附近无目标（Python 侧应避免下发）
                return
            end
            a.target_obj = t.obj
        end
        local obj = a.target_obj

        -- 目标死亡/消失：计一次击杀，成功结束
        local hp = mcl2agent.combat.entity_hp(obj)
        if hp == nil or hp <= 0 then
            if not a.killed then
                a.killed = true
                sess.kills = (sess.kills or 0) + 1
            end
            mcl2agent.action.reset_controls(sess)
            return
        end

        local ppos = player:get_pos()
        local opos = obj:get_pos()
        if not ppos or not opos then return end
        local dist = vector.distance(ppos, opos)
        local reach = 4.0
        local mode = a.args.mode or "melee"

        -- 面向目标身体中心（实体 pos 是脚部，准星对准躯干；+0.7 压身体正中）
        mcl2agent.action.look_at_pos(player, {x = opos.x, y = opos.y + 0.7, z = opos.z})

        if dist > reach then
            -- 追击：疾跑追赶（被动动物逃跑速度≈行走，需疾跑才能追上）
            core.set_player_control(player, { up = 1.0, sprint = true })
            a.last_dist = a.last_dist or dist
            if math.abs(a.last_dist - dist) < 0.05 then
                a.stuck_ticks = (a.stuck_ticks or 0) + 1
            else
                a.stuck_ticks = 0
                a.last_dist = dist
            end
            if a.stuck_ticks > 40 then
                a.status = "error"
            end
        elseif mode == "kite" and dist < 2.5 then
            -- 拉扯：目标贴身时后退保持距离（面向目标 + 后退）
            core.set_player_control(player, { back = 1.0 })
            a.stuck_ticks = 0
        else
            -- 站桩输出：按冷却脉冲注入 dig（真实用户左键挥刀）。
            -- fork 客户端把注入 dig 当真实挖掘键（边沿检测）→ 发 INTERACT →
            -- 服务器真实结算伤害；服务器 globalstep 播 mine 动画 + 客户端第一人称挥臂。
            a.stuck_ticks = 0
            a.cooldown = (a.cooldown or 0) - (dtime or 0.05)
            local dig = false
            if a.cooldown <= 0 then
                a.cooldown = mcl2agent.combat.attack_cooldown(player)
                a.dig_hold = 4   -- 按住 ~4 tick 让客户端捕获按下边沿并播完挥刀
            end
            if (a.dig_hold or 0) > 0 then
                a.dig_hold = a.dig_hold - 1
                dig = true
            end
            core.set_player_control(player, { dig = dig })
        end
    end,
    success_check = function(sess, args)
        local a = sess.current_action
        if a and a.killed then return true end
        -- 目标已消失（死亡/消失）视为成功；也没有就结束，避免死循环
        local t = mcl2agent.combat.resolve_target(sess, args.target or "auto", 16)
        return t == nil
    end,
    timeout = function() return 600 end,
})

-- 进食：吃背包最优食物（或指定 item）。success = 吃到了（食物消耗/饥饿上升）。
mcl2agent.action.register({
    name = "eat",
    args_schema = {item = "string"},
    execute = function(sess, a, dtime, tick)
        -- 指定物品时先手持（食物选择默认走 survival 最优策略）
        if a.args.item then
            local player = mcl2agent.action.get_player(sess)
            if player then
                player:set_wielded_item(a.args.item .. " 1")
            end
        end
        local ok = mcl2agent.survival.eat(sess)
        if not ok then
            a.status = "error"   -- 无可吃食物（Python 侧应避免下发）
            return
        end
        a.ate = true
    end,
    success_check = function(sess, args)
        local a = sess.current_action
        return a and a.ate == true
    end,
    timeout = function() return 100 end,
})

-- 便捷：同步执行一个语义动作直到结束（阻塞，供调试/单步）
function mcl2agent.action.run_until_done(sess, name, args, max_tick)
    mcl2agent.action.execute(sess, name, args)
    max_tick = max_tick or mcl2agent.config.action.default_timeout
    local tick0 = mcl2agent.util.tick()
    while mcl2agent.util.tick() - tick0 < max_tick do
        mcl2agent.action.step(sess, 0.05, mcl2agent.util.tick())
        if not sess.current_action then break end
        -- 注意：真实环境应依赖全局 step；此函数仅用于测试
    end
    return sess.current_action == nil
end
