-- mcl2_agent / config.lua
-- 默认配置。所有字段可被 Python 侧 bridge 下发覆盖。

mcl2agent.config = {
    -- 数据记录
    record = {
        enabled = true,
        fps = 5,                 -- 降采样记录帧率（与图像一致）
        max_steps = 6000,        -- 单 episode 最大步数（超时判定）
        out_dir = "mcl2_agent/data",  -- 相对 worldpath（见 docs/m0_protocol.md §5）
        snapshot_every = 50,     -- 每 N 个 episode 对世界存档打 tar.zst 快照，-1 关闭
    },

    -- 动作
    action = {
        mode = "semantic+primitive",  -- "semantic" | "primitive" | "vpt_token" | "semantic+primitive"
        interaction_mode = "api",     -- "api"：bot1 锁定输入（纯 API 驱动）；"user"：bot1 也可手动操控
        vpt_token = true,             -- 同时编码 vpt_token 字段
        default_timeout = 300,        -- 语义动作默认超时（tick）
    },

    -- 观测
    state = {
        nearby_radius = 8,       -- entities / items 扫描半径（格）
        voxels = false,          -- 是否输出局部体素网格（MineDojo 风格）
        voxel_half = 2,          -- voxels 半边长（格）
        item_alias = true,       -- 把 mcl_* 物品名映射到 minecraft:*（需要 alias 表，骨架留空）
    },

    -- 玩家
    player = {
        eye_height = 1.62,       -- 眼睛高度（Mineclonia 玩家 1.8 高、眼 1.62），瞄准基准
    },

    -- 视觉
    vision = {
        renderer = "engine_fork", -- "engine_fork" | "voxel"（由 Python 侧协商）
        width = 224,
        height = 224,
        fov = 72,
    },

    -- 桥接
    bridge = {
        enabled = true,
        host = "127.0.0.1",
        port = 25585,
        timeout = 10,            -- 秒
        ipc_poll_ticks = 10,     -- 文件 IPC 轮询间隔（tick），见 docs/m0_protocol.md §3
    },

    -- 确定性
    determinism = {
        freeze_time = true,      -- time_speed = 0（TODO）
        freeze_timeofday = true,
        pin_timeofday = nil,     -- 数值时全局 step 每 5 tick 钉住该 timeofday（reset 时设置）
        weather = "clear",       -- "clear" | "rain" | nil
    },
}

-- 工具函数：全局步进计数（服务器 tick 单调递增）
mcl2agent.util = mcl2agent.util or {}
mcl2agent.util.tick = (function()
    local n = 0
    return function()
        n = n + 1
        return n
    end
end)()

-- 物品名映射（骨架）：mcl2 系 -> minecraft 系
mcl2agent.item_alias = mcl2agent.item_alias or {}
function mcl2agent.util.alias_item(name)
    if mcl2agent.config.state.item_alias then
        return mcl2agent.item_alias[name] or name
    end
    return name
end

-- 原子写文件：临时名 + os.rename，避免读端读到半截 JSON（见 docs/m0_protocol.md §3）
function mcl2agent.util.atomic_write(path, content)
    local tmp = path .. ".tmp." .. tostring(os.time()) .. tostring(math.random(1000, 9999))
    local f = io.open(tmp, "w")
    if not f then return false end
    f:write(content)
    f:close()
    local ok = os.rename(tmp, path)
    if not ok then
        os.remove(tmp)
        return false
    end
    return true
end

-- 列目录：优先用 minetest.get_dir_list，否则退回 io.popen（无引擎测试用）
function mcl2agent.util.list_dir(path)
    if minetest.get_dir_list then
        return minetest.get_dir_list(path, "file") or {}
    end
    local out = {}
    local p = io.popen('ls -1 "' .. tostring(path) .. '" 2>/dev/null')
    if p then
        for line in p:lines() do
            if line ~= "" then table.insert(out, line) end
        end
        p:close()
    end
    return out
end

-- 位置归一化：{x,y,z} 或 [x,y,z] -> {x,y,z}
function mcl2agent.util.to_pos(p)
    if type(p) ~= "table" then return p end
    if p.x ~= nil and p.y ~= nil and p.z ~= nil then
        return {x = p.x, y = p.y, z = p.z}
    end
    if p[1] ~= nil and p[2] ~= nil and p[3] ~= nil then
        return {x = p[1], y = p[2], z = p[3]}
    end
    return p
end

-- 跨 Lua 版本 atan2：Lua 5.4+ 移除 math.atan2，改用 math.atan(y, x)
mcl2agent.util.atan2 = math.atan2 or (function(y, x)
    return math.atan(y, x)
end)
