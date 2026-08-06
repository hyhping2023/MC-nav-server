-- mcl2_agent / api/ipc.lua
-- 文件 IPC（M0 传输层）：<world>/mcl2_agent/ipc/
--   目录/命名/原子写约定见 docs/m0_protocol.md §3。
--   协议层（bridge.handlers）与传输层解耦：handler 不变，仅换传输实现。
--
--   ready.json       Lua → Python：mod 加载完成后写一次
--   requests/        Python → Lua：req_<seq>.json，Lua 处理后删除
--   responses/       Lua → Python：resp_<req_id>.json（原子写）
--   events/          Lua → Python：ev_<seq>.json（原子写）

mcl2agent.ipc = {
    root = minetest.get_worldpath() .. "/mcl2_agent/ipc",
    ready_written = false,
    event_seq = 0,
}

local root = mcl2agent.ipc.root

-- 初始化：建目录 + 清空上会话残留 + 写 ready.json（仅首次）
function mcl2agent.ipc.init()
    for _, d in ipairs({
        root,
        root .. "/requests",
        root .. "/responses",
        root .. "/events",
    }) do
        minetest.mkdir(d)
    end
    -- 先删掉上一会话残留的 ready.json：否则新服务器还在启动加载时，
    -- Python 的 wait_ready 会读到旧文件提前放行，随后本函数的 reset()
    -- 会把它刚写入的第一个请求清掉（首请求丢失/超时的根因）。
    pcall(os.remove, root .. "/ready.json")
    mcl2agent.ipc.reset()
    if not mcl2agent.ipc.ready_written then
        mcl2agent.ipc.ready_written = true
        mcl2agent.util.atomic_write(root .. "/ready.json",
            minetest.write_json({ready = true, version = mcl2agent.version}))
    end
end

-- 清空 requests/responses/events 中的残留文件。
-- 每次会话（服务端启动）调用：上一会话若 Python 未消费完就退出，残留的
-- ev_*.json 会在新会话被 Python 误读（曾导致跨 episode 读到旧 task_done，
-- 如 timestamp 格式的 ep-1785894302-001）。新会话从空队列开始。
-- 注：event_seq 每次启动从 0 计数，不清理的话文件名还会与新事件冲突覆盖。
function mcl2agent.ipc.reset()
    for _, dir in ipairs({root .. "/requests", root .. "/responses", root .. "/events"}) do
        minetest.mkdir(dir)
        for _, fn in ipairs(mcl2agent.util.list_dir(dir) or {}) do
            pcall(os.remove, dir .. "/" .. fn)
        end
    end
end

-- 写响应：responses/resp_<req_id>.json（原子写）
function mcl2agent.ipc.write_response(req_id, payload)
    local dir = root .. "/responses"
    minetest.mkdir(dir)
    mcl2agent.util.atomic_write(
        dir .. "/resp_" .. tostring(req_id) .. ".json",
        minetest.write_json(payload)
    )
end

-- 推送事件：events/ev_<seq>.json（原子写）
function mcl2agent.ipc.emit(name, data)
    mcl2agent.ipc.event_seq = mcl2agent.ipc.event_seq + 1
    local dir = root .. "/events"
    minetest.mkdir(dir)
    mcl2agent.util.atomic_write(
        dir .. "/ev_" .. tostring(mcl2agent.ipc.event_seq) .. ".json",
        minetest.write_json({event = name, data = data})
    )
end

-- 处理单个请求文件：解析 → 分发（pcall）→ 写响应 → 删除请求文件
function mcl2agent.ipc.process_request(path)
    local f = io.open(path, "r")
    if not f then return end
    local content = f:read("*a")
    f:close()
    os.remove(path)

    local req = minetest.parse_json(content)
    if not req then
        minetest.log("error", "[mcl2_agent] ipc: bad json in " .. tostring(path) .. " content=" .. tostring(content))
        return
    end

    local handler = mcl2agent.bridge.handlers[req.op]
    local ok, result
    if handler then
        ok, result = pcall(handler, req)
        if not ok then
            result = {error = tostring(result)}
        elseif type(result) == "table" and result.error ~= nil then
            -- handler 以 {error=...} 表示的失败：统一转为 ok=false，让客户端能识别
            ok = false
        end
    else
        ok, result = false, {error = "unknown_op:" .. tostring(req.op)}
    end

    if req.req_id ~= nil then
        mcl2agent.ipc.write_response(req.req_id, {
            req_id = req.req_id,
            ok = ok,
            result = result,
        })
    end
end

-- 轮询 requests/（由 init.lua 的 globalstep 每 tick 调用，内部按间隔节流）
function mcl2agent.ipc.poll(tick)
    local interval = (mcl2agent.config.bridge and mcl2agent.config.bridge.ipc_poll_ticks) or 10
    if tick % interval ~= 0 then return end

    local req_dir = root .. "/requests"
    local files = mcl2agent.util.list_dir(req_dir)
    table.sort(files)
    for _, fn in ipairs(files) do
        if fn:match("^req_.+%.json$") then
            mcl2agent.ipc.process_request(req_dir .. "/" .. fn)
        end
    end
end
