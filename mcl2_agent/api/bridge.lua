-- mcl2_agent / api/bridge.lua
-- 与 Python 进程的桥接：长度前缀帧 JSON 协议。
-- 协议见 DESIGN.md §3。传输层两种实现：
--   A) socket（推荐）：引擎 fork 暴露 core.bridge_listen()，C++ 侧持有 TCP 连接
--   B) file 轮询（降级）：命令/响应目录，无 fork 也能跑，延迟高
-- 骨架实现：协议层（帧编码/解码、handler 注册）+ 两种传输的占位。

local framebuf = {}

mcl2agent.bridge = {
    transport = "file",   -- "socket" | "file"（启动时按 fork 能力探测）
    pending = {},         -- req_id -> {handler, t0}
    handlers = {},
}

-- ============================================================
-- 帧协议（与 Python 侧 bridge.py 保持一致）
--   帧: [4B big-endian length][1B type][JSON body]
--   type: 'r'=request, 'p'=response, 'e'=event
-- ============================================================

function mcl2agent.bridge.encode(msg_type, payload)
    local body = minetest.write_json(payload)
    local len = #body + 1
    local header = string.char(
        math.floor(len / 0x1000000) % 256,
        math.floor(len / 0x10000) % 256,
        math.floor(len / 0x100) % 256,
        len % 256
    )
    return header .. msg_type .. body
end

function mcl2agent.bridge.decode_frame(buf)
    if #buf < 5 then return nil, buf end
    local len = buf:byte(1) * 0x1000000 + buf:byte(2) * 0x10000 +
                buf:byte(3) * 0x100 + buf:byte(4)
    local typ = buf:sub(5, 5)
    if #buf < 4 + len then return nil, buf end
    local body = buf:sub(6, 4 + len)
    return {type = typ, body = minetest.parse_json(body)}, buf:sub(5 + len)
end

-- 处理一帧（由传输层喂入）
function mcl2agent.bridge.on_frame(frame)
    if not frame then return end
    if frame.type == "r" then
        local req = frame.body
        local handler = mcl2agent.bridge.handlers[req.op]
        if handler then
            local ok, result = pcall(handler, req)
            mcl2agent.bridge.send_response(req.req_id, ok, result)
        else
            mcl2agent.bridge.send_response(req.req_id, false, {error = "unknown_op:" .. tostring(req.op)})
        end
    elseif frame.type == "p" then
        local resp = frame.body
        local entry = mcl2agent.bridge.pending[resp.req_id]
        if entry then
            entry.handler(resp)
            mcl2agent.bridge.pending[resp.req_id] = nil
        end
    elseif frame.type == "e" then
        minetest.log("action", "[mcl2_agent] event: " .. minetest.write_json(frame.body))
    end
end

function mcl2agent.bridge.send_response(req_id, ok, result)
    local payload = {req_id = req_id, ok = ok, result = result}
    if mcl2agent.bridge.transport == "socket" then
        -- TODO(engine_fork): core.bridge_send(encoded)
    else
        mcl2agent.ipc.write_response(req_id, payload)
    end
end

-- 主动推送事件给 Python
function mcl2agent.bridge.push_event(name, data)
    local payload = {event = name, data = data}
    if mcl2agent.bridge.transport == "socket" then
        -- TODO(engine_fork): core.bridge_send(encode('e', payload))
    else
        mcl2agent.ipc.emit(name, data)
    end
end

-- 注册请求处理器
function mcl2agent.bridge.handle(op, fn)
    mcl2agent.bridge.handlers[op] = fn
end

-- ============================================================
-- 传输：文件 IPC（M0，见 docs/m0_protocol.md §3）
-- ============================================================

-- 启动（由 init 在最后调用）
function mcl2agent.bridge.start()
    -- 探测 fork 传输能力
    if core.bridge_listen then
        mcl2agent.bridge.transport = "socket"
        core.bridge_listen(mcl2agent.config.bridge.port)
        minetest.log("action", "[mcl2_agent] bridge (socket) on "
            .. mcl2agent.config.bridge.host .. ":" .. mcl2agent.config.bridge.port)
    else
        mcl2agent.bridge.transport = "file"
        mcl2agent.ipc.init()
        minetest.log("action", "[mcl2_agent] bridge (file ipc) -> " .. mcl2agent.ipc.root)
    end
end

-- ============================================================
-- 内置 handlers（供 Python 调用的核心操作）
-- ============================================================

-- 请求格式: {req_id, op, ...args}

mcl2agent.bridge.handle("ping", function(req)
    return {pong = true, version = mcl2agent.version, tick = mcl2agent.util.tick()}
end)

mcl2agent.bridge.handle("observe", function(req)
    -- 请求式采样（m2_protocol.md §1）：成功观测 + episode 激活 -> 写一行 states。
    -- begin_episode 之前（探测玩家会话）sess.episode 为 nil -> 不采样。
    -- 先采样后取观测：state.get_observation 返回的 episode 段带 frame，
    -- 需在 rec.frame 递增之后再取，保证响应里的 frame 就是刚写入的这行。
    local name = req.player or "bot1"
    local sess = mcl2agent.players[name]
    if sess and sess.episode then
        mcl2agent.record.sample(sess, mcl2agent.util.tick())
    end
    return mcl2agent.state.observe(name)
end)

mcl2agent.bridge.handle("tasks", function(req)
    return {tasks = mcl2agent.task.list()}
end)

mcl2agent.bridge.handle("task_generate", function(req)
    -- M3-C §3 任务生成 op（py-data 调用）：
    --   {kind="procedural", item, count, difficulty} -> 注册单条 craft_<item>
    --   {kind="curriculum", max_difficulty}          -> 难度升序任务列表
    --   {kind="llm", prompt}                         -> 本地 mock 注册 canned 任务（不接真实 LLM）
    -- 响应 result：{tasks=[当前任务列表，含 id/instruction/type/difficulty],
    --              registered=[本次新注册任务 id 列表]}（与 py-data taskgen.py 契约一致）
    local kind = req.kind
    if kind == "procedural" then
        if not req.item then return {error = "task_generate: missing item"} end
        local id, is_new = mcl2agent.task.register_craft_task(req.item, req.count or 1, req.difficulty or 1)
        return {tasks = mcl2agent.task.list(), registered = is_new and {id} or {}}
    elseif kind == "curriculum" then
        return {tasks = mcl2agent.task.curriculum(req.max_difficulty), registered = {}}
    elseif kind == "llm" then
        local res = mcl2agent.task.generate_llm(req.prompt or "")
        if type(res) ~= "table" or res.error then
            return {error = "task_generate: llm_generate_failed"}
        end
        return {tasks = mcl2agent.task.list(), registered = {res.task.id}}
    else
        return {error = "unknown_task_generate_kind:" .. tostring(kind)}
    end
end)

mcl2agent.bridge.handle("begin_episode", function(req)
    -- req: {player, task_id, run_id, episode_id, world_seed, task_seed, reset_seed}
    local sess = mcl2agent.players[req.player or "bot1"]
    if not sess then return {error = "player not connected"} end

    -- M3-A：action_log 按 episode 隔离（避免跨 episode 的"最近 success"回退污染）
    sess.action_log = sess.action_log or {}
    for k in pairs(sess.action_log) do sess.action_log[k] = nil end

    local info = {
        run_id = req.run_id or "local",
        episode_id = req.episode_id or ("ep-" .. string.format("%06d", math.random(0, 999999))),
        world_seed = req.world_seed or mcl2agent.state.get_world_seed(),
        mapgen = req.mapgen,
        task_seed = req.task_seed or 0,
        task = {id = req.task_id, params = req.task_params or {}},
        reset_seed = req.reset_seed or 0,
        engine = req.engine,
        game = req.game,
        python = req.python,
        physics = req.physics,
    }

    local t, err = mcl2agent.task.begin(sess, req.task_id, req.task_seed)
    if not t then return {error = err} end

    sess.episode = mcl2agent.record.begin(sess, info)
    if not sess.episode then return {error = "record disabled"} end

    return {episode = info.episode_id}
end)

mcl2agent.bridge.handle("end_episode", function(req)
    local sess = mcl2agent.players[req.player or "bot1"]
    if sess then
        mcl2agent.record.on_task_done(sess, req.success or false)
    end
    return {ok = true}
end)

mcl2agent.bridge.handle("execute", function(req)
    -- req: {player, action, args}
    local sess = mcl2agent.players[req.player or "bot1"]
    if not sess then return {error = "player not connected"} end
    local aid, err = mcl2agent.action.execute(sess, req.action, req.args or {})
    if not aid then return {error = err} end
    return {action_id = aid}
end)

mcl2agent.bridge.handle("step", function(req)
    -- req: {player, primitive:{...}}  原始动作单步
    local sess = mcl2agent.players[req.player or "bot1"]
    if not sess then return {error = "player not connected"} end
    local p = req.primitive or {}
    mcl2agent.action.exec_primitive(sess, p)
    sess.last_primitives = p
    -- TODO: 编码 vpt_token
    return {ok = true, tick = mcl2agent.util.tick()}
end)

mcl2agent.bridge.handle("set_config", function(req)
    -- req: {path, value}  浅合并
    for k, v in pairs(req.value or {}) do
        mcl2agent.config[k] = v
    end
    return {ok = true}
end)

-- init.lua 末尾调用
mcl2agent.bridge.start()
