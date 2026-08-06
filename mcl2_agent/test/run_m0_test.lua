-- mcl2_agent / test / run_m0_test.lua
-- 无引擎 M1 冒烟测试（真实玩家驱动路径）：
--   加载 stub → 加载 mod → 模拟玩家 bot1 加入 → 通过文件 IPC 驱动
--   begin_episode(craft_planks) → execute(craft) → globalstep 循环 → task.success
--   → 原始动作 step（forward/jump）断言 set_player_control 被调用
--   → end_episode → 断言 episode 数据文件。
--
-- 运行（在仓库根目录）：
--   lua mcl2_agent/test/run_m0_test.lua
-- 任何断言失败会打印原因并以非 0 退出码结束。

local script_path = arg and arg[0] or "mcl2_agent/test/run_m0_test.lua"
local test_dir = script_path:match("^(.*)/[^/]+$") or "."
local mod_root = test_dir .. "/.."

-- 归一化为绝对路径，避免 cwd 依赖
local p = io.popen('cd "' .. mod_root .. '" && pwd 2>/dev/null')
if p then
    local abs = p:read("*l")
    p:close()
    if abs and abs ~= "" then mod_root = abs end
end

local worldpath = mod_root .. "/test/tmp_world"

_G.minetest_stub_modpath = mod_root
_G.minetest_stub_worldpath = worldpath

-- 清理上一轮的 IPC/episode 残留：避免 stale resp_*.json 被 wait_for 误判为已处理
os.execute("rm -rf '" .. worldpath:gsub("'", "'\\''") .. "'")

print("== load stub + mod ==")
dofile(test_dir .. "/minetest_stub.lua")
dofile(mod_root .. "/init.lua")

-- ============================================================
-- 小工具
-- ============================================================

local function file_exists(path)
    local f = io.open(path, "r")
    if f then f:close(); return true end
    return false
end

local function read_file(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local c = f:read("*a")
    f:close()
    return c
end

local function write_file(path, content)
    local f = io.open(path, "w")
    assert(f, "cannot write " .. path)
    f:write(content)
    f:close()
end

local function assert_file(path, what)
    assert(file_exists(path), "missing file: " .. (what or path) .. " -> " .. path)
end

local function run_globalstep(n)
    for _ = 1, n do
        for _, cb in ipairs(minetest._globalsteps) do
            cb(0.05)
        end
    end
end

-- 运行 globalstep 直到 pred() 为真，最多 max_tick 次；返回是否达成
local function wait_for(pred, max_tick)
    for i = 1, max_tick do
        run_globalstep(1)
        if pred() then return true end
    end
    return pred()
end

local function count_item(inv, item)
    local total = 0
    for _, s in ipairs(inv:get_list("main")) do
        if s and not s:is_empty() and s:get_name() == item then
            total = total + s:get_count()
        end
    end
    return total
end

-- ============================================================
-- 加载期断言：不再有加载期自动创建的逻辑 bot
-- ============================================================

print("== sanity: mod load (no auto bot) ==")
assert(mcl2agent, "mcl2agent namespace missing")
assert(mcl2agent.bot, "bot adapter missing")
assert(mcl2agent.bot.name == "bot1", "bot name != bot1")
assert(not mcl2agent.players["bot1"], "session should NOT exist at mod load")
assert(mcl2agent.ipc and mcl2agent.ipc.root, "ipc not loaded")
assert(mcl2agent.recipes and mcl2agent.recipes["mcl_trees:wood_oak"], "recipe table missing")
assert(type(mcl2agent.action.control_inject) == "function",
    "control_inject should be core.set_player_control in stub")

-- 未加入玩家时：observe / 适配器调用返回 nil 不报错
local obs0 = mcl2agent.state.observe("bot1")
assert(obs0 == nil, "observe before join should be nil")
assert(mcl2agent.bot:get_pos() == nil, "bot:get_pos before join should be nil")
assert(mcl2agent.bot:get_inventory() == nil, "bot:get_inventory before join should be nil")

local ipc_root = mcl2agent.ipc.root
local req_dir = ipc_root .. "/requests"
local resp_dir = ipc_root .. "/responses"
local events_dir = ipc_root .. "/events"

local function send_req(req_id, body)
    write_file(req_dir .. "/req_" .. req_id .. ".json", minetest.write_json(body))
end

assert_file(ipc_root .. "/ready.json", "ready.json")

-- 会话启动清理：上一会话残留的 IPC 文件（如未消费的 ev_*.json）被清空，
-- 防止新会话误读跨会话残留 task_done（py-data 真机复测曾见 timestamp 格式 episode）。
print("== ipc reset on (re)init (stale file cleanup) ==")
write_file(events_dir .. "/ev_999.json", minetest.write_json({
    event = "task_done", data = {episode_id = "ep-1785894302-001", success = false},
}))
write_file(req_dir .. "/req_999.json", minetest.write_json({req_id = 999, op = "ping"}))
write_file(resp_dir .. "/resp_999.json", minetest.write_json({req_id = 999, ok = true, result = {pong = true}}))
mcl2agent.ipc.init()  -- 模拟服务端重启：再次 init 触发 reset
assert(not file_exists(events_dir .. "/ev_999.json"), "stale event not cleared")
assert(not file_exists(req_dir .. "/req_999.json"), "stale request not cleared")
assert(not file_exists(resp_dir .. "/resp_999.json"), "stale response not cleared")

-- ============================================================
-- 0) 模拟玩家 bot1 加入 -> on_joinplayer 创建会话
-- ============================================================

print("== simulate join bot1 ==")
local fake = minetest.simulate_join("bot1")
assert(fake, "simulate_join returned nil")
assert(mcl2agent.players["bot1"], "session not created on join")

-- ============================================================
-- 1) begin_episode(craft_planks, seeds) via IPC
-- ============================================================

print("== begin_episode ==")
local ep_id = "ep-000001"
send_req(1, {
    req_id = 1,
    op = "begin_episode",
    player = "bot1",
    task_id = "craft_planks",
    run_id = "test-run",
    episode_id = ep_id,
    world_seed = 42,
    task_seed = 7,
    reset_seed = 13,
    mapgen = {name = "singlenode"},
    engine = {name = "luanti", version = "5.12.0", fork = "stub"},
    game = {name = "mineclonia", version = "0.92"},
    python = {package = "mcl2_env", version = "0.1.0"},
})

assert(wait_for(function() return file_exists(resp_dir .. "/resp_1.json") end, 30),
    "no response for begin_episode")

local r1 = json.decode(read_file(resp_dir .. "/resp_1.json"))
assert(r1 and r1.ok == true, "begin_episode failed: " .. tostring(r1 and r1.result and r1.result.error))
assert(r1.result.episode == ep_id, "begin_episode returned wrong episode")

-- reset 已生效：fake 玩家背包有 3 个 mcl_trees:tree_oak
local bot_inv = mcl2agent.bot:get_inventory()
assert(bot_inv, "bot inventory nil after join")
assert(count_item(bot_inv, "mcl_trees:tree_oak") == 3, "reset did not give 3 mcl_trees:tree_oak")
assert(count_item(bot_inv, "mcl_trees:wood_oak") == 0, "should start with 0 planks")

-- observe：读真实玩家（经适配层），结构与 state 接口同构
local obs = mcl2agent.state.observe("bot1")
assert(obs and obs.player and obs.player.pos, "observe returned bad structure")
assert(obs.player.pos.x == 0 and obs.player.pos.y == 40, "observe pos mismatch")
assert(obs.inventory and type(obs.inventory.main) == "table", "observe inventory missing")
assert(obs.task and obs.task.id == "craft_planks", "observe task section missing")
assert(obs.episode and obs.episode.episode_id == ep_id, "observe episode section missing")

-- ============================================================
-- 1b) 请求式采样（M2 §1）：observe via IPC -> 一行 states
--     不再由全局 step 定时采样；states 行数必须 == observe 次数。
-- ============================================================

print("== observe via IPC (request-driven sampling) ==")
local observe_count = 0
local function send_observe(req_id)
    send_req(req_id, {req_id = req_id, op = "observe", player = "bot1"})
    assert(wait_for(function() return file_exists(resp_dir .. "/resp_" .. req_id .. ".json") end, 30),
        "no response for observe req_" .. req_id)
    local resp = json.decode(read_file(resp_dir .. "/resp_" .. req_id .. ".json"))
    assert(resp and resp.ok == true, "observe failed: " .. tostring(resp and resp.result and resp.result.error))
    return resp.result
end

for i = 1, 5 do
    local obsr = send_observe(100 + i)
    assert(obsr and obsr.player and obsr.player.pos, "observe response bad structure")
    assert(obsr.episode and obsr.episode.episode_id == ep_id, "observe episode section missing")
    observe_count = observe_count + 1
end

-- ============================================================
-- 2) execute(craft {item=planks, count=4}) + globalstep 循环
-- ============================================================

print("== execute craft ==")
send_req(2, {
    req_id = 2,
    op = "execute",
    player = "bot1",
    action = "craft",
    args = {item = "mcl_trees:wood_oak", count = 4},
})

assert(wait_for(function()
    return file_exists(resp_dir .. "/resp_2.json") and
           file_exists(worldpath .. "/mcl2_agent/data/episodes/" .. ep_id .. "/episode_summary.json")
end, 40), "craft flow did not complete (no resp_2 or episode_summary)")

local r2 = json.decode(read_file(resp_dir .. "/resp_2.json"))
assert(r2 and r2.ok == true, "execute failed: " .. tostring(r2 and r2.result))
assert(tonumber(r2.result.action_id) >= 1, "execute did not return action_id")

-- 任务判定成功
local sess = mcl2agent.players["bot1"]
assert(sess.task and sess.task.success == true, "task.success != true")

-- 背包已有 >=4 木板、消耗了原木
assert(count_item(bot_inv, "mcl_trees:wood_oak") >= 4, "no planks after craft")
assert(count_item(bot_inv, "mcl_trees:tree_oak") == 2, "craft should consume 1 tree (3 -> 2)")

-- observe 反映 task.success
obs = mcl2agent.state.observe("bot1")
assert(obs.task.success == true, "observe task.success != true")

-- M3-A：action_log 记录了 craft 的状态迁移，最终状态为 success
assert(sess.action_log and sess.action_log[1], "action_log missing craft entry")
assert(sess.action_log[1].id == "craft", "action_log[1].id != craft")
assert(sess.action_log[1].status == "success", "action_log[1] final status != success: "
    .. tostring(sess.action_log[1].status))
assert(sess.action_log[1].t0 ~= nil and sess.action_log[1].t_end ~= nil,
    "action_log craft t0/t_end missing")

-- ============================================================
-- 3) 原始动作 step：forward/jump -> set_player_control + camera
-- ============================================================

print("== primitive step (forward/jump/camera) ==")
local before_calls = #minetest._control_calls
send_req(3, {
    req_id = 3,
    op = "step",
    player = "bot1",
    primitive = {forward = 1, jump = 1, camera = {0.1, 0.2}},
})

assert(wait_for(function() return file_exists(resp_dir .. "/resp_3.json") end, 30),
    "no response for step")
local r3 = json.decode(read_file(resp_dir .. "/resp_3.json"))
assert(r3 and r3.ok == true, "step failed: " .. tostring(r3 and r3.result))

-- set_player_control 被调用（fake player 收到按键注入）
assert(#minetest._control_calls == before_calls + 1,
    "set_player_control not called on step (before=" .. before_calls .. ", after="
    .. tostring(#minetest._control_calls) .. ")")
local ctrl = minetest._control_calls[#minetest._control_calls]
assert(ctrl.player == "bot1", "set_player_control wrong player: " .. tostring(ctrl.player))
assert(ctrl.controls.up == 1.0, "forward -> up != 1.0")
assert(ctrl.controls.jump == true, "jump != true")
assert(ctrl.controls.down == 0.0, "back -> down != 0.0")
assert(ctrl.controls.sprint == false, "sprint should be false")

-- 字段名与 m1_protocol.md §1 完全对齐：
--   {up,down,left,right}(float) + {jump,sneak,sprint,dig,place,zoom}(bool)
for _, f in ipairs({"up","down","left","right","jump","sneak","sprint","dig","place","zoom"}) do
    assert(ctrl.controls[f] ~= nil, "set_player_control missing field: " .. f)
end
assert(ctrl.controls.left == 0.0, "left should be 0.0")
assert(ctrl.controls.right == 0.0, "right should be 0.0")
assert(ctrl.controls.sneak == false, "sneak should be false")
assert(ctrl.controls.dig == false, "dig should be false")
assert(ctrl.controls.place == false, "place should be false")

-- 第二组原始动作：sprint/attack/use -> sprint=true/aux1、dig、place
local before_calls2 = #minetest._control_calls
send_req(5, {
    req_id = 5,
    op = "step",
    player = "bot1",
    primitive = {back = 1, left = 1, sprint = 1, attack = 1, use = 1},
})
assert(wait_for(function() return file_exists(resp_dir .. "/resp_5.json") end, 30),
    "no response for step2")
local r3b = json.decode(read_file(resp_dir .. "/resp_5.json"))
assert(r3b and r3b.ok == true, "step2 failed: " .. tostring(r3b and r3b.result))
assert(#minetest._control_calls == before_calls2 + 1,
    "set_player_control not called on step2")
local ctrl2 = minetest._control_calls[#minetest._control_calls].controls
assert(ctrl2.down == 1.0, "back -> down != 1.0")
assert(ctrl2.left == 1.0, "left != 1.0")
assert(ctrl2.up == 0.0, "forward -> up should be 0.0")
assert(ctrl2.sprint == true, "sprint -> aux1 should be true")
assert(ctrl2.dig == true, "attack -> dig should be true")
assert(ctrl2.place == true, "use -> place should be true")

-- camera 增量经 set_look_vertical/horizontal 应用
assert(math.abs(fake:get_look_vertical() - 0.1) < 1e-9, "pitch not applied via set_look_vertical")
assert(math.abs(fake:get_look_horizontal() - 0.2) < 1e-9, "yaw not applied via set_look_horizontal")

-- ============================================================
-- 4) 数据落盘断言
-- ============================================================

print("== record assertions ==")
local data_root = mcl2agent.record.out_root
assert(worldpath .. "/mcl2_agent/data" == data_root, "record.out_root mismatch")

local ep_dir = data_root .. "/episodes/" .. ep_id
assert_file(ep_dir .. "/meta.json", "meta.json")
assert_file(ep_dir .. "/states.jsonl", "states.jsonl")
assert_file(ep_dir .. "/actions.jsonl", "actions.jsonl")
assert_file(ep_dir .. "/rewards.jsonl", "rewards.jsonl")
assert_file(ep_dir .. "/episode_summary.json", "episode_summary.json")

local meta = json.decode(read_file(ep_dir .. "/meta.json"))
assert(meta, "meta.json not parseable")
assert(meta.world_seed == 42, "meta.world_seed missing")
assert(meta.task_seed == 7, "meta.task_seed missing")
assert(meta.reset_seed == 13, "meta.reset_seed missing")
assert(meta.mapgen and meta.env, "meta.mapgen / meta.env missing")
assert(meta.env.mod and meta.env.python, "meta.env content missing")

assert(read_file(ep_dir .. "/actions.jsonl"):match("%S"), "actions.jsonl empty")
assert(read_file(ep_dir .. "/rewards.jsonl"):match("%S"), "rewards.jsonl empty")

-- M3-A §1：每行 actions.jsonl 都有 semantic 键（ep-000001 在 craft 前采样的 5 行全为 idle）。
-- （craft 完成后任务立即 success、episode 随之关闭，本 episode 不会再产生 craft 行；
--   craft 语义行在下方 ep-000002 验证，见 section 7。）
local actions = read_file(ep_dir .. "/actions.jsonl")
local n_actions = 0
for line in actions:gmatch("[^\n]+") do
    local arow = json.decode(line)
    assert(arow, "actions line not parseable")
    assert(arow.semantic ~= nil, "actions row missing semantic key: " .. tostring(line))
    assert(arow.semantic.status ~= nil, "actions row semantic.status missing: " .. tostring(line))
    n_actions = n_actions + 1
end
assert(n_actions == observe_count,
    "actions rows (" .. n_actions .. ") != observe count (" .. observe_count .. ")")

-- M2 §1 请求式采样：states 行数 == observe 次数（不再是全局 step 定时采样）
local states = read_file(ep_dir .. "/states.jsonl")
assert(states and states:match("%S"), "states.jsonl empty")
local n_states = 0
for _ in states:gmatch("[^\n]+") do n_states = n_states + 1 end
assert(n_states == observe_count,
    "states rows (" .. n_states .. ") != observe count (" .. observe_count .. ")")

-- states 行含 server_tick / wall_us；image 与行号一一对应
local row_i = 0
local prev_wall = nil
for line in states:gmatch("[^\n]+") do
    local row = json.decode(line)
    assert(row, "states line not parseable")
    assert(row.tick ~= nil, "states row missing tick")
    assert(row.server_tick == row.tick, "server_tick != tick")
    assert(type(row.wall_us) == "number", "wall_us not a number")
    if prev_wall then
        assert(row.wall_us >= prev_wall, "wall_us not monotonic")
    end
    prev_wall = row.wall_us
    assert(row.image == string.format("observations/%06d.png", row_i),
        "image not aligned with row index: " .. tostring(row.image))
    row_i = row_i + 1
end
assert(row_i == observe_count, "states row iteration mismatch")

local summary = json.decode(read_file(ep_dir .. "/episode_summary.json"))
assert(summary and summary.success == true, "episode_summary success != true")
assert(summary.steps and summary.steps >= 1, "episode_summary steps < 1")
assert(summary.seeds and summary.seeds.world_seed == 42, "episode_summary seeds missing")
assert(summary.frames == observe_count, "summary.frames != observe count")
assert(summary.samples == observe_count, "summary.samples != observe count")

-- task_done 事件已推送
assert(wait_for(function() return file_exists(events_dir .. "/ev_1.json") end, 5),
    "task_done event not emitted")
local ev = json.decode(read_file(events_dir .. "/ev_1.json"))
assert(ev and ev.event == "task_done", "event payload wrong")
assert(ev.data and ev.data.episode_id == ep_id and ev.data.success == true, "event data wrong")

-- ============================================================
-- 5) end_episode via IPC
-- ============================================================

print("== end_episode ==")
send_req(4, {
    req_id = 4,
    op = "end_episode",
    player = "bot1",
    success = true,
})

assert(wait_for(function() return file_exists(resp_dir .. "/resp_4.json") end, 30),
    "no response for end_episode")
local r4 = json.decode(read_file(resp_dir .. "/resp_4.json"))
assert(r4 and r4.ok == true, "end_episode failed: " .. tostring(r4 and r4.result))

-- ============================================================
-- 6) task_generate（M3-C §3）：procedural / curriculum / llm mock
-- ============================================================

print("== task_generate procedural ==")
send_req(6, {
    req_id = 6,
    op = "task_generate",
    kind = "procedural",
    item = "mcl_core:coalblock",
    count = 2,
    difficulty = 2,
})
assert(wait_for(function() return file_exists(resp_dir .. "/resp_6.json") end, 30),
    "no response for task_generate procedural")
local r6 = json.decode(read_file(resp_dir .. "/resp_6.json"))
assert(r6 and r6.ok == true, "task_generate procedural failed: " .. tostring(r6 and r6.result and r6.result.error))
-- M3-C 契约：registered 为本次新注册任务 id 数组
assert(type(r6.result.registered) == "table", "procedural registered missing")
local registered_proc = false
for _, rid in ipairs(r6.result.registered) do
    if rid == "craft_mcl_core_coalblock" then registered_proc = true end
end
assert(registered_proc, "procedural registered id wrong: "
    .. tostring(minetest.write_json(r6.result.registered)))
assert(type(r6.result.tasks) == "table" and #r6.result.tasks >= 1, "procedural tasks list missing")

print("== tasks lists registered task ==")
send_req(7, {req_id = 7, op = "tasks"})
assert(wait_for(function() return file_exists(resp_dir .. "/resp_7.json") end, 30),
    "no response for tasks")
local r7 = json.decode(read_file(resp_dir .. "/resp_7.json"))
assert(r7 and r7.ok == true, "tasks failed: " .. tostring(r7 and r7.result and r7.result.error))
local listed_craft = false
for _, t in ipairs(r7.result.tasks or {}) do
    if t.id == "craft_mcl_core_coalblock" then listed_craft = true end
end
assert(listed_craft, "procedural task not listed by tasks")

print("== task_generate curriculum ==")
send_req(8, {req_id = 8, op = "task_generate", kind = "curriculum", max_difficulty = 1})
assert(wait_for(function() return file_exists(resp_dir .. "/resp_8.json") end, 30),
    "no response for task_generate curriculum")
local r8 = json.decode(read_file(resp_dir .. "/resp_8.json"))
assert(r8 and r8.ok == true, "task_generate curriculum failed: " .. tostring(r8 and r8.result and r8.result.error))
assert(type(r8.result.tasks) == "table" and #r8.result.tasks >= 1, "curriculum empty")
local prev_diff = -1
for _, t in ipairs(r8.result.tasks) do
    local d = t.difficulty or 0
    assert(d >= prev_diff, "curriculum not sorted by difficulty")
    assert(d <= 1, "curriculum max_difficulty not respected")
    prev_diff = d
end

print("== task_generate llm mock ==")
send_req(9, {req_id = 9, op = "task_generate", kind = "llm", prompt = "Collect 3 dirt blocks"})
assert(wait_for(function() return file_exists(resp_dir .. "/resp_9.json") end, 30),
    "no response for task_generate llm")
local r9 = json.decode(read_file(resp_dir .. "/resp_9.json"))
assert(r9 and r9.ok == true, "task_generate llm failed: " .. tostring(r9 and r9.result and r9.result.error))
-- registered 为本次新注册任务 id 数组（llm mock 注册 collect_dirt）
assert(type(r9.result.registered) == "table", "llm registered missing")
local registered_llm = false
for _, rid in ipairs(r9.result.registered) do
    if rid == "collect_dirt" then registered_llm = true end
end
assert(registered_llm, "llm mock task id wrong: "
    .. tostring(minetest.write_json(r9.result.registered)))

-- ============================================================
-- 7) M3-A：craft 语义行落盘验证（ep-000002）
--    craft_planks 下 craft 完成后任务立即 success、episode 随之关闭，无法再采样；
--    故用 collect_wood（需 tree_oak，craft 木板不会满足它）保持 episode 存活，
--    使 craft 完成后的一次 observe 能写出 completed_recently 语义行。
-- ============================================================

print("== ep-000002: craft semantic row (M3-A) ==")
local ep2 = "ep-000002"

-- 注册一个需要 10 个木板的任务：craft 一次只产 4 个 -> 动作成功但任务永不满足，
-- 从而保持 episode 存活，让 craft 完成后的 observe 能写出 completed_recently 语义行。
local ep2_task_id = mcl2agent.task.register_craft_task("mcl_trees:wood_oak", 10, 1)
assert(mcl2agent.tasks[ep2_task_id], "ep2 task not registered")

send_req(10, {
    req_id = 10,
    op = "begin_episode",
    player = "bot1",
    task_id = ep2_task_id,
    run_id = "test-run-2",
    episode_id = ep2,
    world_seed = 42,
    task_seed = 8,
    reset_seed = 14,
    mapgen = {name = "singlenode"},
    engine = {name = "luanti", version = "5.12.0", fork = "stub"},
    game = {name = "mineclonia", version = "0.92"},
    python = {package = "mcl2_env", version = "0.1.0"},
})
assert(wait_for(function() return file_exists(resp_dir .. "/resp_10.json") end, 30),
    "no response for begin_episode ep2")
local r10 = json.decode(read_file(resp_dir .. "/resp_10.json"))
assert(r10 and r10.ok == true, "begin_episode ep2 failed: " .. tostring(r10 and r10.result and r10.result.error))
assert(r10.result.episode == ep2, "begin_episode ep2 returned wrong episode")

-- 该任务无 reset：显式清空背包并给 3 个原木供 craft 消耗
bot_inv:set_list("main", {})
bot_inv:set_list("armor", {})
bot_inv:set_list("offhand", {})
bot_inv:add_item("main", "mcl_trees:tree_oak 3")
assert(count_item(bot_inv, "mcl_trees:tree_oak") == 3, "ep2: tree give failed")

-- craft wood_oak×4 成功（消耗 1 原木 -> 剩 2），但任务要 3 原木 -> 任务未满足，episode 保持存活
send_req(11, {
    req_id = 11,
    op = "execute",
    player = "bot1",
    action = "craft",
    args = {item = "mcl_trees:wood_oak", count = 4},
})
assert(wait_for(function() return file_exists(resp_dir .. "/resp_11.json") end, 30),
    "no response for ep2 craft")
local r11 = json.decode(read_file(resp_dir .. "/resp_11.json"))
assert(r11 and r11.ok == true, "ep2 craft execute failed: " .. tostring(r11 and r11.result and r11.result.error))

-- craft 完成后 observe：该行 semantic 应回退为最近 success（status="completed_recently"）
send_req(107, {req_id = 107, op = "observe", player = "bot1"})
assert(wait_for(function() return file_exists(resp_dir .. "/resp_107.json") end, 30),
    "no response for ep2 observe")
local r107 = json.decode(read_file(resp_dir .. "/resp_107.json"))
assert(r107 and r107.ok == true, "ep2 observe failed: " .. tostring(r107 and r107.result and r107.result.error))
assert(r107.result.task and r107.result.task.success == false,
    "ep2: task should NOT be success yet")

local ep2_dir = data_root .. "/episodes/" .. ep2
local a2 = read_file(ep2_dir .. "/actions.jsonl")
assert(a2 and a2:match("%S"), "ep2 actions.jsonl empty")
local craft_row_found = false
for line in a2:gmatch("[^\n]+") do
    local arow = json.decode(line)
    assert(arow and arow.semantic ~= nil, "ep2 actions row missing semantic key: " .. tostring(line))
    if arow.semantic.id == "craft" then
        craft_row_found = true
        local st = arow.semantic.status
        assert(st == "success" or st == "completed_recently",
            "ep2 craft semantic status should be success/completed_recently, got: " .. tostring(st))
        assert(arow.semantic.args and arow.semantic.args.item == "mcl_trees:wood_oak",
            "ep2 craft semantic args missing")
        assert(arow.semantic.t0 ~= nil and arow.semantic.t_end ~= nil,
            "ep2 craft semantic t0/t_end missing")
    end
end
assert(craft_row_found, "ep2: no actions row with semantic.id=='craft'")

-- M3-A：queued 捕获——execute 后（全局 step 未提升为 running）立即采样，
-- current_action_row 应命中"队列首个 queued"，而不是回退 idle。
-- 直接调用（中间不跑全局 step），复现集成验证里 ep-895341 只采到 idle 的场景。
print("== M3-A: queued semantic capture ==")
mcl2agent.action.execute(sess, "craft", {item = "mcl_trees:wood_oak", count = 4})
local qrow = mcl2agent.record.current_action_row(sess, 9999, 99)
assert(qrow.semantic and qrow.semantic.id == "craft",
    "queued row semantic.id != craft: " .. tostring(minetest.write_json(qrow.semantic)))
assert(qrow.semantic.status == "queued",
    "queued row semantic.status != queued: " .. tostring(qrow.semantic.status))
assert(qrow.semantic.args and qrow.semantic.args.item == "mcl_trees:wood_oak",
    "queued row semantic args missing")
assert(qrow.semantic.action_id ~= nil, "queued row semantic.action_id missing")
-- 清掉测试入队的动作，避免污染后续 end_episode
sess.action_queue = {}
sess.action_log = {}

-- 收尾：关闭 ep-000002
send_req(12, {req_id = 12, op = "end_episode", player = "bot1", success = false})
assert(wait_for(function() return file_exists(resp_dir .. "/resp_12.json") end, 30),
    "no response for end_episode ep2")
local r12 = json.decode(read_file(resp_dir .. "/resp_12.json"))
assert(r12 and r12.ok == true, "end_episode ep2 failed: " .. tostring(r12 and r12.result))

-- ============================================================
-- 请求文件已被 Lua 侧删除
-- ============================================================
assert(not file_exists(req_dir .. "/req_1.json"), "req_1.json not deleted")
assert(not file_exists(req_dir .. "/req_2.json"), "req_2.json not deleted")
assert(not file_exists(req_dir .. "/req_3.json"), "req_3.json not deleted")
assert(not file_exists(req_dir .. "/req_4.json"), "req_4.json not deleted")
assert(not file_exists(req_dir .. "/req_5.json"), "req_5.json not deleted")
for i = 6, 12 do
    assert(not file_exists(req_dir .. "/req_" .. i .. ".json"), "req_" .. i .. " not deleted")
end
for i = 1, 7 do
    assert(not file_exists(req_dir .. "/req_" .. (100 + i) .. ".json"), "req_" .. (100 + i) .. " not deleted")
end

print("")
print("ALL M1 STUB TESTS PASSED")
print("  worldpath : " .. worldpath)
print("  ipc root  : " .. ipc_root)
print("  episode   : " .. ep_dir)
os.exit(0)
