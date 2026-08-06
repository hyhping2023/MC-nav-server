-- mcl2_agent / api/record.lua
-- 数据记录：canonical episode 目录 + JSONL/PNG 落盘。
-- 磁盘布局与字段见 DESIGN.md §7。
-- 骨架实现：目录创建、文件句柄管理、JSONL 写入、meta.json 生成、种子保存。

mcl2agent.record = {}
mcl2agent.record.out_root = minetest.get_worldpath() .. "/" .. mcl2agent.config.record.out_dir
local out_root = mcl2agent.record.out_root

-- 打开一个 episode 记录器
-- @param sess table
-- @param info table {run_id, episode_id, world_seed, task_seed, reset_seed, task_id, ...}
-- @return recorder table | nil
function mcl2agent.record.begin(sess, info)
    local cfg = mcl2agent.config.record
    if not cfg.enabled then return nil end

    local ep_dir = out_root .. "/episodes/" .. info.episode_id
    local obs_dir = ep_dir .. "/observations"
    local ok, err = mcl2agent.record.mkdirs({ep_dir, obs_dir})
    if not ok then
        minetest.log("error", "[mcl2_agent] record dir failed: " .. tostring(err))
        return nil
    end

    local recorder = {
        sess = sess,
        ep_dir = ep_dir,
        obs_dir = obs_dir,
        info = info,
        frame = -1,            -- 帧号，从 0 开始
        handles = {},
        stats = {samples = 0, bytes = 0},
    }

    -- 打开各 JSONL 文件句柄
    recorder.handles.states = io.open(ep_dir .. "/states.jsonl", "w")
    recorder.handles.actions = io.open(ep_dir .. "/actions.jsonl", "w")
    recorder.handles.rewards = io.open(ep_dir .. "/rewards.jsonl", "w")
    recorder.handles.instructions = io.open(ep_dir .. "/instructions.jsonl", "w")

    -- 写初始指令（每步一行，通常恒定）
    local tid = info.task and info.task.id
    local tdef = tid and mcl2agent.tasks[tid]
    recorder.handles.instructions:write(minetest.write_json({
        tick = mcl2agent.util.tick(),
        instruction = tdef and tdef.instruction or tid or "",
        instruction_zh = tdef and tdef.instruction_zh or nil,
    }) .. "\n")

    -- 写 meta.json（含全部种子，见 DESIGN.md §7.2）
    mcl2agent.record.write_meta(recorder)

    return recorder
end

-- 目录批量创建
function mcl2agent.record.mkdirs(dirs)
    for _, d in ipairs(dirs) do
        local ok, err = minetest.mkdir(d)
        if not ok and not err then return false, "mkdir failed: " .. d end
    end
    return true
end

-- meta.json：种子 + 版本 + 配置，保证世界可还原
function mcl2agent.record.write_meta(rec)
    local meta = {
        schema_version = "1.0.0",
        episode_id = rec.info.episode_id,
        run_id = rec.info.run_id,
        world_seed = rec.info.world_seed,
        mapgen = rec.info.mapgen,
        task_seed = rec.info.task_seed,
        task = rec.info.task,           -- {id, params}
        reset_seed = rec.info.reset_seed,
        env = {
            engine = rec.info.engine,
            game = rec.info.game,
            mod = {name = "mcl2_agent", version = mcl2agent.version},
            python = rec.info.python,
        },
        renderer = mcl2agent.config.vision,
        action_space = {mode = mcl2agent.config.action.mode, version = "1.0"},
        physics = rec.info.physics,
        start = {wall_time = os.time(), server_tick = mcl2agent.util.tick()},
    }
    local f = io.open(rec.ep_dir .. "/meta.json", "w")
    if f then
        f:write(minetest.write_json(meta))
        f:close()
    end
end

-- 请求式采样（由 observe bridge handler 调用，见 m2_protocol.md §1）：
-- 一行 observe 写一行 states/actions/rewards，frame 与行号一一对应。
function mcl2agent.record.sample(sess, tick)
    if not sess.episode then return end
    local rec = sess.episode

    -- 观测：bot 与玩家通用（bot 由 state.observe 路由到 detached inventory）
    local obs = mcl2agent.state.observe(sess.name)
    if not obs then return end

    rec.frame = rec.frame + 1

    -- 1) 图像：框架下图像由 Python 侧渲染器写入 obs_dir；Lua 侧仅记录路径
    local image_file = string.format("observations/%06d.png", rec.frame)
    -- TODO(engine_fork): 若 Lua 侧收到客户端帧（经 bridge 回传），也直接写盘

    -- 2) states.jsonl
    local state_row = obs
    state_row.tick = tick
    state_row.server_tick = tick
    state_row.wall_us = minetest.get_us_time()   -- 微秒，用于与帧时间戳核对
    state_row.frame = rec.frame
    state_row.image = image_file
    -- obs 里的 episode 段在采样前取值（frame 落后一行），此处同步保持一致
    if type(state_row.episode) == "table" then
        state_row.episode.frame = rec.frame
    end
    rec.handles.states:write(minetest.write_json(state_row) .. "\n")

    -- 3) actions.jsonl（双标签）
    local act = mcl2agent.record.current_action_row(sess, tick, rec.frame)
    rec.handles.actions:write(minetest.write_json(act) .. "\n")

    -- 4) rewards.jsonl
    rec.handles.rewards:write(minetest.write_json({
        tick = tick, frame = rec.frame,
        reward = sess.last_reward or 0.0,
        terminated = sess.task and sess.task.success or false,
        truncated = sess.truncated or false,
        info = {progress = sess.task and sess.task.progress or nil},
    }) .. "\n")

    -- 落盘：Python 侧靠 states/actions/rewards 行数对齐 frame 与 PNG，
    -- io.open 默认全缓冲，不 flush 的话 Python 会读到缓冲外的旧内容（M1 根因之一）。
    rec.handles.states:flush()
    rec.handles.actions:flush()
    rec.handles.rewards:flush()

    rec.stats.samples = rec.stats.samples + 1
end

-- 当前动作行（语义 + 原始双标签）
-- M3-A §1：semantic 字段每行必有。选择链：
--   1) 优先取当前 sess.current_action（status=="running"）；
--   2) 否则从 action_log 找该帧时刻正在 running 的动作（status=="running" 且 t0<=tick<=t_end，
--      t_end 为空表示仍在进行）；
--   3) 否则取队列中首个 queued 动作（已 execute 入队、尚未被 action.step 提升为 running）——
--      覆盖 execute 与 observe 背靠背、快成功 episode 只采到 idle 的真实缺口；
--   4) 再回退最近 success 动作（带 status="completed_recently" 标记）；
--   5) 全无则为 {id=nil, status="idle"}。
function mcl2agent.record.current_action_row(sess, tick, frame)
    local semantic
    local a = sess.current_action
    if a and a.status == "running" then
        semantic = {
            id = a.id, args = a.args, action_id = a.action_id, status = a.status,
            t0 = a.t0, t_end = a.t_end,
        }
    else
        local log = sess.action_log or {}
        -- 2) 该帧时刻正在 running 的动作
        local running = nil
        for _, entry in pairs(log) do
            if entry.status == "running" and entry.t0 and entry.t0 <= tick
               and (entry.t_end == nil or entry.t_end >= tick) then
                running = entry
                break
            end
        end
        if running then
            semantic = {
                id = running.id, args = running.args, action_id = running.action_id,
                status = running.status, t0 = running.t0, t_end = running.t_end,
                frame = running.frame,
            }
        else
            -- 3) 队列中首个 queued 动作（比"最近 success"优先：待执行动作比刚完成的更相关）
            local queued = sess.action_queue and sess.action_queue[1]
            if queued then
                semantic = {
                    id = queued.id, args = queued.args, action_id = queued.action_id,
                    status = queued.status or "queued", t0 = queued.t0, t_end = nil,
                }
            else
                -- 4) 最近 success 动作
                local recent = nil
                for _, entry in pairs(log) do
                    if entry.status == "success"
                       and (not recent or (entry.t_end or 0) > (recent.t_end or 0)) then
                        recent = entry
                    end
                end
                if recent then
                    semantic = {
                        id = recent.id, args = recent.args, action_id = recent.action_id,
                        status = "completed_recently", t0 = recent.t0, t_end = recent.t_end,
                        frame = recent.frame,
                    }
                else
                    -- 5) 无动作
                    semantic = {id = nil, status = "idle"}
                end
            end
        end
    end

    return {
        tick = tick,
        frame = frame,
        semantic = semantic,
        primitive = sess.last_primitives or {   -- M0 无原始动作时的占位
            forward = 0, jump = 0, attack = 0, use = 0, hotbar = 0,
            camera = {0, 0},
        },
        vpt_token = sess.last_vpt_token or nil,
    }
end

-- 任务结束回调：写 episode_summary.json + 关闭句柄 + 推送 task_done 事件
function mcl2agent.record.on_task_done(sess, success)
    if not sess.episode then return end
    local rec = sess.episode
    if rec.finalized then return end   -- 防止 task.evaluate 与 end_episode 重复结算
    rec.finalized = true

    local summary = {
        episode_id = rec.info.episode_id,
        success = success,
        steps = sess.task and sess.task.steps or 0,
        frames = rec.frame + 1,
        duration_s = (mcl2agent.util.tick() - (sess.task and sess.task.started_tick or 0)) * 0.05,
        samples = rec.stats.samples,
        seeds = {world_seed = rec.info.world_seed, task_seed = rec.info.task_seed, reset_seed = rec.info.reset_seed},
    }
    local f = io.open(rec.ep_dir .. "/episode_summary.json", "w")
    if f then
        f:write(minetest.write_json(summary))
        f:close()
    end

    -- 事件：{"event": "task_done", "data": {"episode_id": ..., "success": ...}}（见 m0_protocol.md §1）
    mcl2agent.bridge.push_event("task_done", {
        episode_id = rec.info.episode_id,
        success = success,
    })

    mcl2agent.record.flush(sess)
end

-- 关闭所有句柄
function mcl2agent.record.flush(sess)
    if not sess then return end
    local rec = sess.episode
    if not rec then return end
    for _, h in pairs(rec.handles) do
        if h then pcall(function() h:close() end) end
    end
    sess.episode = nil
end

-- 便捷：供 state 接口读取当前 episode 信息
function mcl2agent.record.get_episode_info(sess)
    if not sess or not sess.episode then return nil end
    local rec = sess.episode
    return {
        episode_id = rec.info.episode_id,
        run_id = rec.info.run_id,
        world_seed = rec.info.world_seed,
        task_seed = rec.info.task_seed,
        frame = rec.frame,
        server_tick = mcl2agent.util.tick(),
        wall_time = os.time(),
    }
end

-- 会话统计钩子（供判定器）：记录挖掘/击杀
function mcl2agent.record.hook_events()
    minetest.register_on_dignode(function(pos, oldnode, digger)
        if not digger then return end
        local name = digger:get_player_name()
        local sess = mcl2agent.players[name]
        if sess then
            sess.digged = sess.digged or {}
            sess.digged[oldnode.name] = (sess.digged[oldnode.name] or 0) + 1
        end
    end)
    -- TODO: on_dieplayer / on_kill 统计（Mineclonia 击杀由 mcl_mobf 事件）
end
mcl2agent.record.hook_events()
