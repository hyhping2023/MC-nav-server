-- mcl2_agent / api/task.lua
-- 任务生成器：任务注册表 + 成功判定器 + 课程/程序化生成骨架。
-- 任务 schema 见 DESIGN.md §6。

mcl2agent.task = {}

-- ============================================================
-- 成功判定器（Predicates）
-- ============================================================

-- 注册判定器
-- @param name string
-- @param fn function(sess, args) -> bool
function mcl2agent.predicates.register(name, fn)
    mcl2agent.predicates[name] = fn
end

mcl2agent.predicates.register("inventory_contains", function(sess, args)
    local inv = mcl2agent.action.get_inv(sess)
    if not inv then return false end
    local total = 0
    for _, listname in ipairs({"main"}) do
        total = total + mcl2agent.action.count_item(inv, listname, args.item)
    end
    return total >= (args.count or 1)
end)

mcl2agent.predicates.register("block_placed", function(sess, args)
    -- args: {pos1, pos2, name, count}
    local p1 = mcl2agent.util.to_pos(args.pos1)
    local p2 = mcl2agent.util.to_pos(args.pos2)
    if not p1 or not p2 then return false end
    local nodes = minetest.find_nodes_in_area(p1, p2, args.name)
    return #nodes >= (args.count or 1)
end)

mcl2agent.predicates.register("entity_killed", function(sess, args)
    -- TODO: 通过 on_die 统计或会话计数器
    return (sess.kills or 0) >= (args.count or 1)
end)

mcl2agent.predicates.register("player_at", function(sess, args)
    local subj = mcl2agent.action.get_subject(sess)
    if not subj then return false end
    local t = mcl2agent.util.to_pos(args.pos)
    if not t then return false end
    return vector.distance(subj:get_pos(), t) <= (args.tolerance or 1.5)
end)

mcl2agent.predicates.register("block_mined", function(sess, args)
    -- 会话内 on_dignode 累计计数（见 init.lua register_on_dignode）
    local n = (sess.digged or {})[args.name] or 0
    return n >= (args.count or 1)
end)

-- ============================================================
-- 任务注册表
-- ============================================================

-- 注册任务
-- @param def table（见 DESIGN.md §6.1）
function mcl2agent.task.register(def)
    assert(def.id and def.instruction, "task needs id + instruction")
    mcl2agent.tasks[def.id] = def
    if def.tags then
        mcl2agent.task.tags[def.id] = def.tags
    end
end

mcl2agent.task.tags = {}

function mcl2agent.task.get(id)
    return mcl2agent.tasks[id]
end

function mcl2agent.task.list()
    local out = {}
    for id, def in pairs(mcl2agent.tasks) do
        table.insert(out, {
            id = id,
            instruction = def.instruction,
            type = def.type,
            difficulty = def.difficulty,
        })
    end
    return out
end

-- ============================================================
-- 会话内任务状态
-- ============================================================

-- 任务观测片段（并入状态接口）
function mcl2agent.task.get_observation(sess)
    local t = sess.task
    if not t then return nil end
    local def = mcl2agent.tasks[t.id]
    return {
        id = t.id,
        instruction = def and def.instruction or t.id,
        instruction_zh = def and def.instruction_zh or nil,
        type = def and def.type,
        difficulty = def and def.difficulty,
        progress = t.progress,
        success = t.success,
        steps = t.steps or 0,
    }
end

-- 开始一个任务（由 bridge / rollouter 调用）
-- @param sess table
-- @param task_id string
-- @param seed number|nil 任务参数采样种子
function mcl2agent.task.begin(sess, task_id, seed)
    local def = mcl2agent.tasks[task_id]
    if not def then return nil, "unknown_task:" .. tostring(task_id) end

    sess.task = {
        id = task_id,
        seed = seed or 0,
        progress = {},
        success = false,
        steps = 0,
        started_tick = mcl2agent.util.tick(),
    }

    -- 应用 reset 配置
    if def.reset then
        mcl2agent.reset.apply(sess, def.reset, sess.task.seed)
    end

    -- 初始化 progress 结构（由 success_predicate 需要的计数）
    sess.task.progress = {
        collected = {},
        placed = {},
    }

    -- 指令写入聊天/HUD
    minetest.chat_send_player(sess.name, "[mcl2_agent] task: " .. tostring(def.instruction))
    -- TODO: HUD 显示 instruction

    return sess.task
end

-- 主循环判定：成功 / 超时
function mcl2agent.task.evaluate(sess, tick)
    local t = sess.task
    if not t or t.success or t.finished then return end
    t.steps = (t.steps or 0) + 1

    local def = mcl2agent.tasks[t.id]
    if not def then return end

    local pred = mcl2agent.predicates[def.success_predicate]
    if pred and pred(sess, def.success_args) then
        t.success = true
        mcl2agent.record.on_task_done(sess, true)
        minetest.chat_send_player(sess.name, "[mcl2_agent] task succeeded: " .. t.id)
        return
    end

    if def.timeout_ticks and t.steps >= def.timeout_ticks then
        t.finished = true
        t.success = false
        mcl2agent.record.on_task_done(sess, false)
        minetest.chat_send_player(sess.name, "[mcl2_agent] task timed out: " .. t.id)
    end
end

-- ============================================================
-- 生成器（骨架）
-- ============================================================

-- 程序化生成：按可合成物品表批量生成 craft 类任务
-- @param item_map table {["mcl_trees:wood_oak"] = {count=4, difficulty=1}, ...}
function mcl2agent.task.generate_craft_tasks(item_map)
    for item, cfg in pairs(item_map or {}) do
        mcl2agent.task.register_craft_task(item, cfg.count or 1, cfg.difficulty or 1,
            cfg.timeout_ticks or 1200)
    end
end

-- 单条注册 craft 任务（供 task_generate procedural 调用，M3-C §3）
-- @param item string 可合成物品（如 mcl_trees:wood_oak）
-- @param count number|nil 目标数量（默认 1）
-- @param difficulty number|nil（默认 1）
-- @param timeout_ticks number|nil（默认 1200）
-- @return task_id string 既有或新注册的任务 id（幂等）
-- @return is_new boolean 本次是否新注册（false 表示已存在，未重复注册）
function mcl2agent.task.register_craft_task(item, count, difficulty, timeout_ticks)
    local id = "craft_" .. tostring(item):gsub("[^%w]", "_")
    if not mcl2agent.tasks[id] then
        mcl2agent.task.register({
            id = id,
            name = "Craft " .. item,
            instruction = "Craft " .. (count or 1) .. " of " .. item .. ".",
            type = "craft",
            difficulty = difficulty or 1,
            success_predicate = "inventory_contains",
            success_args = {item = item, count = count or 1},
            timeout_ticks = timeout_ticks or 1200,
        })
        return id, true
    end
    return id, false
end

-- 课程生成：按 difficulty 升序返回任务列表（含 id/instruction/type/difficulty，
-- 与 task.list() 条目同构，供 task_generate curriculum 直接回给 Python）
function mcl2agent.task.curriculum(max_difficulty)
    local out = {}
    for id, def in pairs(mcl2agent.tasks) do
        if not max_difficulty or (def.difficulty or 0) <= max_difficulty then
            table.insert(out, {
                id = id,
                instruction = def.instruction,
                type = def.type,
                difficulty = def.difficulty or 0,
            })
        end
    end
    table.sort(out, function(a, b) return a.difficulty < b.difficulty end)
    return out
end

-- LLM 生成入口（M3-C §3 本地 mock，不接真实 LLM）：
-- 注册一条 canned 任务并返回 {mock=true, task={...}}。
-- 真实路径：Python 侧 /generate_task → LLM → 返回 task def → 调 task_generate 注册。
-- @param prompt string（mock 忽略内容）
function mcl2agent.task.generate_llm(prompt)
    local def = {
        id = "collect_dirt",
        name = "Collect Dirt",
        instruction = "Collect 3 dirt blocks.",
        instruction_zh = "收集 3 个泥土方块。",
        type = "collect",
        tags = {"llm_generated", "mock"},
        difficulty = 0,
        reset = {
            pos = {x = 0, y = 40, z = 0},
            area_radius = 6,
            inventory = {clear = true, give = {}},
            timeofday = 0.5,
        },
        success_predicate = "inventory_contains",
        success_args = {item = "mcl_core:dirt", count = 3},
        timeout_ticks = 1200,
    }
    mcl2agent.task.register(def)
    return {
        mock = true,
        prompt = prompt,
        task = {
            id = def.id,
            instruction = def.instruction,
            type = def.type,
            difficulty = def.difficulty,
        },
    }
end
