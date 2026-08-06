-- mcl2_agent / test / minetest_stub.lua
-- 最小 Luanti（Minetest）引擎 API 桩，供无引擎 Lua 测试（Lua 5.4+，自带 JSON 编解码）。
--
-- 覆盖 mcl2_agent 模组加载与 M0 流程所需的最小 API 面：
--   - 全局步进回调收集、节点内存表、detached inventory、玩家查询（返回 nil）
--   - write_json/parse_json、get_worldpath/mkdir/get_dir_list、时间/天气
--   - core = minetest、vector 数学表、ItemStack
--
-- 需要预先设置两个全局变量：
--   minetest_stub_modpath   -- mcl2_agent 模组根目录
--   minetest_stub_worldpath -- 模拟 world 目录
--
-- 注意：Lua 标准库没有 JSON，这里内置一个极简 encoder/decoder。
-- 若环境有 cjson / dkjson，可直接替换 json.encode / json.decode 以增强兼容性。

-- ============================================================
-- JSON 编解码（极简，够用即可）
-- ============================================================

local json = {}

local function jenc(v)
    local t = type(v)
    if t == "nil" then return "null" end
    if t == "boolean" then return tostring(v) end
    if t == "number" then
        if v ~= v then return "null" end
        return string.format("%.14g", v)
    end
    if t == "string" then
        local s = v:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n")
            :gsub("\r", "\\r"):gsub("\t", "\\t")
        return '"' .. s .. '"'
    end
    if t == "table" then
        local is_arr, max, n = true, 0, 0
        for k in pairs(v) do
            if type(k) ~= "number" then
                is_arr = false
                break
            end
            if k > max then max = k end
            n = n + 1
        end
        if is_arr and n > 0 and n ~= max then is_arr = false end
        local parts = {}
        if is_arr then
            for i = 1, max do
                parts[#parts + 1] = jenc(v[i])
            end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            for k, val in pairs(v) do
                if val ~= nil then
                    parts[#parts + 1] = jenc(tostring(k)) .. ":" .. jenc(val)
                end
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    end
    return "null"
end

json.encode = function(v) return jenc(v) end

local function skip_ws(s, i)
    while s:sub(i, i):match("%s") do i = i + 1 end
    return i
end

local function jdec(s, i)
    i = skip_ws(s, i)
    local c = s:sub(i, i)
    if c == "{" then
        i = i + 1
        local out = {}
        i = skip_ws(s, i)
        if s:sub(i, i) == "}" then return out, i + 1 end
        while true do
            i = skip_ws(s, i)
            assert(s:sub(i, i) == '"', "json: expected key string")
            local key, ni = jdec(s, i)
            i = skip_ws(s, ni)
            assert(s:sub(i, i) == ":", "json: expected ':'")
            i = skip_ws(s, i + 1)
            local val, ni2 = jdec(s, i)
            out[key] = val
            i = skip_ws(s, ni2)
            local sep = s:sub(i, i)
            if sep == "}" then return out, i + 1 end
            assert(sep == ",", "json: expected ',' or '}'")
            i = i + 1
        end
    elseif c == "[" then
        i = i + 1
        local out = {}
        i = skip_ws(s, i)
        if s:sub(i, i) == "]" then return out, i + 1 end
        local idx = 1
        while true do
            local val, ni = jdec(s, i)
            out[idx] = val
            idx = idx + 1
            i = skip_ws(s, ni)
            local sep = s:sub(i, i)
            if sep == "]" then return out, i + 1 end
            assert(sep == ",", "json: expected ',' or ']'")
            i = i + 1
        end
    elseif c == '"' then
        i = i + 1
        local out = {}
        while true do
            local ch = s:sub(i, i)
            if ch == '"' then return table.concat(out), i + 1 end
            if ch == "\\" then
                local esc = s:sub(i + 1, i + 1)
                if esc == "n" then table.insert(out, "\n")
                elseif esc == "r" then table.insert(out, "\r")
                elseif esc == "t" then table.insert(out, "\t")
                elseif esc == "u" then
                    local code = tonumber(s:sub(i + 2, i + 5), 16)
                    table.insert(out, utf8.char(code or 63))
                    i = i + 4
                else
                    table.insert(out, esc)
                end
                i = i + 2
            else
                table.insert(out, ch)
                i = i + 1
            end
        end
    elseif c == "t" then
        assert(s:sub(i, i + 3) == "true", "json: bad literal")
        return true, i + 4
    elseif c == "f" then
        assert(s:sub(i, i + 4) == "false", "json: bad literal")
        return false, i + 5
    elseif c == "n" then
        assert(s:sub(i, i + 3) == "null", "json: bad literal")
        return nil, i + 4
    else
        local num = s:match("^-?%d+%.?%d*[eE]?[+-]?%d*", i)
        assert(num and num ~= "", "json: expected value at " .. i)
        local val = tonumber(num)
        return val, i + #num
    end
end

json.decode = function(s)
    local ok, res = pcall(jdec, s, 1)
    if ok then return res end
    return nil
end

-- ============================================================
-- ItemStack（模拟引擎全局）
-- ============================================================

local ItemStack = {}
ItemStack.__index = ItemStack

function ItemStack.new(s)
    local o = {name = "", count = 1}
    if type(s) == "table" then
        o.name = s.name or ""
        o.count = tonumber(s.count) or 1
    elseif type(s) == "string" then
        local parts = {}
        for w in s:gmatch("%S+") do
            parts[#parts + 1] = w
        end
        o.name = parts[1] or ""
        o.count = tonumber(parts[2]) or 1
    end
    setmetatable(o, ItemStack)
    return o
end

function ItemStack:get_name() return self.name end
function ItemStack:get_count() return self.count end
function ItemStack:get_free_space() return 99 end
function ItemStack:is_empty() return self.name == "" or self.count <= 0 end
function ItemStack:set_count(c) self.count = c end
function ItemStack:take_item(n)
    n = n or 1
    local t = math.min(self.count, n)
    self.count = self.count - t
    return ItemStack.new(self.name .. " " .. t)
end
function ItemStack:add_item(s)
    local other = type(s) == "table" and s or ItemStack.new(s)
    if self:is_empty() then
        self.name = other.name
        self.count = other.count
    elseif self.name == other.name then
        self.count = self.count + other.count
    else
        return other  -- 槽位不同，放不下
    end
    return ItemStack.new("")
end
function ItemStack:to_string()
    if self:is_empty() then return "" end
    return self.name .. " " .. self.count
end
function ItemStack:get_meta()
    return {to_table = function() return {} end}
end

setmetatable(ItemStack, {__call = function(_, s) return ItemStack.new(s) end})
_G.ItemStack = ItemStack

-- ============================================================
-- DetachedInventory（内存实现）
-- ============================================================

local DetachedInv = {}
DetachedInv.__index = DetachedInv

function DetachedInv.new(size)
    return setmetatable({lists = {main = {}}, size = size or 32}, DetachedInv)
end

function DetachedInv:get_list(listname)
    return self.lists[listname] or {}
end

function DetachedInv:get_stack(listname, i)
    local s = self.lists[listname] and self.lists[listname][i]
    if not s then return ItemStack.new("") end
    return s
end

function DetachedInv:set_list(listname, list)
    local out = {}
    for _, s in ipairs(list or {}) do
        local st = type(s) == "table" and (getmetatable(s) == ItemStack and s or ItemStack.new(s)) or ItemStack.new(s)
        if not st:is_empty() then
            table.insert(out, st)
        end
    end
    self.lists[listname] = out
end

function DetachedInv:set_size(listname, size)
    self.size = size
    self.lists[listname] = self.lists[listname] or {}
    return true
end

function DetachedInv:add_item(listname, s)
    local list = self.lists[listname] or {}
    local stack = type(s) == "table" and (getmetatable(s) == ItemStack and s or ItemStack.new(s)) or ItemStack.new(s)
    -- 合并进已有同名槽
    for _, slot in ipairs(list) do
        if slot.name == stack.name then
            slot.count = slot.count + stack.count
            stack = ItemStack.new("")
            break
        end
    end
    if not stack:is_empty() then
        table.insert(list, stack)
    end
    self.lists[listname] = list
    return ItemStack.new("")   -- 剩余未放入的部分（桩里恒空）
end

function DetachedInv:remove_item(listname, s)
    local list = self.lists[listname] or {}
    local stack = type(s) == "table" and (getmetatable(s) == ItemStack and s or ItemStack.new(s)) or ItemStack.new(s)
    for i = 1, #list do
        local slot = list[i]
        if slot and slot.name == stack.name then
            local rem = math.min(slot.count, stack.count)
            slot.count = slot.count - rem
            stack.count = stack.count - rem
            if slot.count <= 0 then
                list[i] = nil
            end
            if stack.count <= 0 then break end
        end
    end
    -- 重排，保持紧凑数组
    local tmp = {}
    for _, v in ipairs(list) do
        if v then table.insert(tmp, v) end
    end
    self.lists[listname] = tmp
    return stack
end

function DetachedInv:contains_item(listname, s)
    local stack = type(s) == "table" and s or ItemStack.new(s)
    return self:count_item(listname, stack.name) >= stack.count
end

function DetachedInv:count_item(listname, item)
    local total = 0
    for _, slot in ipairs(self.lists[listname] or {}) do
        if slot.name == item then total = total + slot.count end
    end
    return total
end

-- ============================================================
-- FakePlayer（模拟真实玩家 ObjectRef）
-- 供 register_on_joinplayer 触发的"玩家加入"测试使用。
-- 方法与真实 ObjectRef 玩家对齐：get_pos/set_pos/look/inventory/hp/breath。
-- ============================================================

local FakePlayer = {}
FakePlayer.__index = FakePlayer

function FakePlayer.new(name)
    local inv = DetachedInv.new(36)
    inv:set_size("main", 36)
    local o = {
        name = name,
        pos = {x = 0, y = 40, z = 0},
        yaw = 0,
        pitch = 0,
        hp = 20,
        breath = 10,
        inv = inv,
        wielded = ItemStack.new(""),
    }
    setmetatable(o, FakePlayer)
    return o
end

function FakePlayer:get_player_name() return self.name end
function FakePlayer:get_pos() return self.pos end
function FakePlayer:set_pos(p) self.pos = {x = p.x, y = p.y, z = p.z} end
function FakePlayer:get_hp() return self.hp end
function FakePlayer:set_hp(h) self.hp = h end
function FakePlayer:get_breath() return self.breath end
function FakePlayer:set_breath(b) self.breath = b end
function FakePlayer:get_velocity() return {x = 0, y = 0, z = 0} end
function FakePlayer:get_wielded_item() return self.wielded end
function FakePlayer:set_wielded_item(s)
    self.wielded = type(s) == "table" and s or ItemStack.new(s)
end
function FakePlayer:is_player() return true end
function FakePlayer:get_luaentity() return nil end
function FakePlayer:get_look_horizontal() return self.yaw end
function FakePlayer:get_look_vertical() return self.pitch end
function FakePlayer:set_look_horizontal(yaw) self.yaw = yaw end
function FakePlayer:set_look_vertical(pitch) self.pitch = pitch end
function FakePlayer:get_look_dir()
    -- 与真实引擎一致：set_look_horizontal(yaw) 后 get_look_dir().x = -sin(yaw)
    local cp = math.cos(self.pitch)
    return {x = -math.sin(self.yaw) * cp, y = -math.sin(self.pitch), z = math.cos(self.yaw) * cp}
end
function FakePlayer:set_player_look_target(pos) self.look_target = {x = pos.x, y = pos.y, z = pos.z} end
function FakePlayer:clear_player_look_target() self.look_target = nil end
function FakePlayer:get_player_look_target() return self.look_target end
function FakePlayer:get_inventory() return self.inv end
function FakePlayer:set_physics_override() return true end

-- ============================================================
-- FakeEntity（模拟怪物/动物实体 ObjectRef）
-- ============================================================

local FakeEntity = {}
FakeEntity.__index = FakeEntity

function FakeEntity.new(name, pos, ent)
    ent = ent or {name = name, type = "monster", health = 20}
    ent.name = ent.name or name
    ent.health = ent.health or 20
    ent.is_mob = true   -- mcl_mobs 实体标记（combat.resolve_target 过滤非生物用）
    local o = {
        pos = {x = pos.x, y = pos.y, z = pos.z},
        ent = ent,
        dead = false,
        removed = false,
        _punches = 0,
    }
    setmetatable(o, FakeEntity)
    return o
end

function FakeEntity:get_luaentity() return self.ent end
function FakeEntity:get_pos() return self.pos end
function FakeEntity:get_hp() return self.ent.health or 0 end
function FakeEntity:set_hp(h) self.ent.health = h end
function FakeEntity:is_player() return false end
function FakeEntity:remove() self.dead = true self.removed = true end
function FakeEntity:punch(hitter, tflp, caps, dir)
    self._punches = self._punches + 1
    local dmg = 1
    if caps and caps.damage_groups and caps.damage_groups.fleshy then
        dmg = caps.damage_groups.fleshy
    end
    self.ent.health = (self.ent.health or 0) - dmg
    if self.ent.health <= 0 then
        self.dead = true
    end
    return true
end

-- 暴露给测试脚本
_G.FakeEntity = FakeEntity

-- ============================================================
-- minetest 全局
-- ============================================================

minetest = {
    _globalsteps = {},
    _join = {},
    _leave = {},
    _dignode = {},
    _node_map = {},      -- "x,y,z" -> node
    _detached = {},
    _players = {},       -- name -> fake player ObjectRef（simulate_join 注册）
    _control_calls = {}, -- core.set_player_control 调用记录 {player, controls}
    _entities = {},      -- FakeEntity 注册表（get_objects_inside_radius 扫描）
    _hunger = {},        -- name -> 饥饿值（mcl_hunger 桩）
    _saturation = {},    -- name -> 饱和值（mcl_hunger 桩）
    _item_eat_calls = {},-- core.do_item_eat 调用记录
    _worldpath = minetest_stub_worldpath or (os.tmpname()),
    _timeofday = 0.5,
    registered_biomes = {},
    registered_nodes = {},  -- name -> 节点定义（{groups, _mcl_hardness, walkable, diggable}）
    registered_items = {},  -- name -> 物品定义（{tool_capabilities, groups, _mcl_saturation, _mcl_eat_replace_with}）
}

local mt = minetest

-- 节点表
local function node_key(pos)
    return math.floor(pos.x) .. "," .. math.floor(pos.y) .. "," .. math.floor(pos.z)
end

-- ---- 生命周期 / 日志 ----

function mt.get_modpath(name)
    return minetest_stub_modpath
end

function mt.log(level, msg)
    print("[minetest:" .. tostring(level) .. "] " .. tostring(msg))
end

function mt.register_globalstep(cb)
    table.insert(mt._globalsteps, cb)
end

function mt.register_on_joinplayer(cb)
    table.insert(mt._join, cb)
end

function mt.register_on_leaveplayer(cb)
    table.insert(mt._leave, cb)
end

function mt.register_on_dignode(cb)
    table.insert(mt._dignode, cb)
end

-- ---- 世界路径 / 时间 / 天气 ----

function mt.get_worldpath()
    return mt._worldpath
end

function mt.get_timeofday()
    return mt._timeofday
end

function mt.set_timeofday(t)
    mt._timeofday = t
end

function mt.get_day_count()
    return 0
end

-- 定时器（stub：立即执行；死亡自动重生在测试中同步触发）
function mt.after(delay, func)
    if func then pcall(func) end
end

-- 死亡回调注册（reset.lua 自动重生用）
mt._ondie = {}
function mt.register_on_dieplayer(cb)
    table.insert(mt._ondie, cb)
end

-- 节点注册表（reset.find_ground 的液体判断用；stub 默认空表，def 为 nil 视为实心）
mt.registered_nodes = {}

-- 单调递增微秒时钟（对齐引擎 minetest.get_us_time，供 wall_us 字段）
local _us_clock = 0
function mt.get_us_time()
    _us_clock = _us_clock + 1000
    return _us_clock
end

function mt.get_biome_data()
    return nil
end

-- ---- 玩家 ----

function mt.get_player_by_name(name)
    return mt._players[name] or nil
end

-- 模拟玩家加入：创建 fake player ObjectRef，触发 register_on_joinplayer 回调
function mt.simulate_join(name)
    local fp = FakePlayer.new(name)
    mt._players[name] = fp
    for _, cb in ipairs(mt._join) do
        pcall(cb, fp)
    end
    return fp
end

-- 模拟玩家离开：触发 register_on_leaveplayer 回调并移除
function mt.simulate_leave(name)
    local fp = mt._players[name]
    if not fp then return end
    for _, cb in ipairs(mt._leave) do
        pcall(cb, fp)
    end
    mt._players[name] = nil
end

function mt.get_player_privs(name)
    return {}
end

function mt.chat_send_player(name, msg)
end

-- ---- M1 fork API：core.set_player_control（服务器按键注入）----
-- 真实实现见 docs/m1_protocol.md §1。桩里把调用记录到 minetest._control_calls。
-- 同时模拟 fork 客户端的攻击链路：注入 dig=true 时对附近实体发 INTERACT 造成伤害
-- （真实流程：set_player_control → 0x65 → 客户端 dig_pressed → INTERACT → 服务器 on_punch）。
function mt.set_player_control(player, controls)
    local pname = player and player.get_player_name and player:get_player_name() or "?"
    table.insert(mt._control_calls, {player = pname, controls = controls})
    if controls and controls.dig and player and player.get_pos then
        local ppos = player:get_pos()
        if ppos then
            local held
            if player.get_wielded_item then
                held = player:get_wielded_item()
            end
            local def = held and mt.registered_items and mt.registered_items[held:get_name()]
            local caps = def and def.tool_capabilities
            for _, obj in ipairs(mt._entities) do
                if not obj.dead and obj.punch then
                    local opos = obj:get_pos()
                    if opos and vector.distance(ppos, opos) <= 5 then
                        obj:punch(player, caps and caps.full_punch_interval or 0.625, caps,
                                  {x = 0, y = 0, z = 1})
                    end
                end
            end
        end
    end
end

-- ---- 世界查询 ----

-- M3.5 fork API 桩：客户端准星瞄准目标点（服务器下发、客户端算 look）
function mt.set_player_look_target(player, pos)
    if player and player.set_player_look_target then player:set_player_look_target(pos) end
end
function mt.clear_player_look_target(player)
    if player and player.clear_player_look_target then player:clear_player_look_target() end
end
function mt.get_player_look_target(player)
    if player and player.get_player_look_target then return player:get_player_look_target() end
    return nil
end

function mt.raycast(p1, p2, ...)
    return {next = function() return nil end}
end

-- 实体注册/清理（测试用）：get_objects_inside_radius 从这里扫描
function mt.register_entity_stub(obj)
    table.insert(mt._entities, obj)
end

function mt.clear_entity_stubs()
    mt._entities = {}
end

function mt.get_objects_inside_radius(pos, radius)
    local out = {}
    for _, obj in ipairs(mt._entities) do
        local opos = obj and obj.get_pos and obj:get_pos()
        if opos then
            local d = vector.distance(pos, opos)
            if d <= radius then
                table.insert(out, obj)
            end
        end
    end
    return out
end

function mt.get_node(pos)
    return mt._node_map[node_key(pos)] or {name = "air", param2 = 0}
end

function mt.set_node(pos, node)
    local key = node_key(pos)
    if node.name == "air" or node.name == nil then
        mt._node_map[key] = nil
    else
        mt._node_map[key] = {name = node.name, param2 = node.param2 or 0}
    end
end

function mt.dig_node(pos, digger)
    local old = mt.get_node(pos)
    mt.set_node(pos, {name = "air"})
    for _, cb in ipairs(mt._dignode) do
        pcall(cb, pos, old, digger)
    end
    return true
end

function mt.place_node(pos, node, placer)
    mt.set_node(pos, node)
    return true
end

function mt.find_nodes_in_area(p1, p2, name)
    local out = {}
    for x = math.floor(p1.x), math.floor(p2.x) do
        for y = math.floor(p1.y), math.floor(p2.y) do
            for z = math.floor(p1.z), math.floor(p2.z) do
                if mt.get_node({x = x, y = y, z = z}).name == name then
                    table.insert(out, {x = x, y = y, z = z})
                end
            end
        end
    end
    return out
end

-- ---- 文件系统 ----

function mt.mkdir(d)
    local ok = os.execute("mkdir -p '" .. d:gsub("'", "'\\''") .. "'")
    return ok == true or ok == 0
end

function mt.get_dir_list(path, mode)
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

-- ---- 背包 ----

function mt.create_detached_inventory(name, callbacks)
    local inv = DetachedInv.new(32)
    mt._detached[name] = inv
    return inv
end

-- ---- 杂项 ----

function mt.get_translator(lang)
    return function(s) return s end
end

-- 便捷：注册一个节点定义（供 block_diggable / 挖穿寻路测试）
-- @param name string
-- @param def table {groups={...}, _mcl_hardness=, walkable=, diggable=}
function mt.register_node_stub(name, def)
    mt.registered_nodes[name] = def or {groups = {}, _mcl_hardness = 1, walkable = true}
end

-- 便捷：注册一个物品定义（供 tool_capabilities / eatable 测试）
-- @param name string
-- @param def table {tool_capabilities={groupcaps=, damage_groups=, full_punch_interval=},
--                   groups={eatable=}, _mcl_saturation=, _mcl_eat_replace_with=}
function mt.register_item_stub(name, def)
    mt.registered_items[name] = def or {}
end

-- core.do_item_eat 桩（对齐真实 mcl_hunger 行为：回血/回饥饿 + 消耗物品）
function mt.do_item_eat(hunger_points, replace_with_item, itemstack, user, pointed_thing)
    table.insert(mt._item_eat_calls, {
        hunger = hunger_points,
        replace = replace_with_item,
        item = itemstack and itemstack:get_name() or nil,
        user = user and user.get_player_name and user:get_player_name() or nil,
    })
    if user and user.get_player_name then
        local name = user:get_player_name()
        mt._hunger[name] = math.min(20, (mt._hunger[name] or 20) + (hunger_points or 0))
        user:set_hp(math.min(20, user:get_hp() + (hunger_points or 0)))
    end
    if itemstack and itemstack.take_item then
        itemstack:take_item()
        if replace_with_item then
            itemstack:add_item(replace_with_item)
        end
    end
    return itemstack
end

-- mcl_hunger 桩
mcl_hunger = {
    active = true,
}
function mcl_hunger.get_hunger(player)
    local name = player and player.get_player_name and player:get_player_name()
    return mt._hunger[name] or 20
end
function mcl_hunger.set_hunger(player, v)
    local name = player and player.get_player_name and player:get_player_name()
    mt._hunger[name] = v
end
function mcl_hunger.get_saturation(player)
    local name = player and player.get_player_name and player:get_player_name()
    return mt._saturation[name] or 5
end
function mcl_hunger.is_player_full(player)
    return mcl_hunger.get_hunger(player) >= 20
end

mt.write_json = function(t) return json.encode(t) end
mt.parse_json = function(s) return json.decode(s) end

-- ============================================================
-- core 别名 + vector 数学表
-- ============================================================

core = mt

vector = {}

function vector.new(x, y, z)
    if type(x) == "table" then
        return {x = x.x or 0, y = x.y or 0, z = x.z or 0}
    end
    return {x = x or 0, y = y or 0, z = z or 0}
end

function vector.add(a, b)
    return {x = a.x + b.x, y = a.y + b.y, z = a.z + b.z}
end

function vector.subtract(a, b)
    return {x = a.x - b.x, y = a.y - b.y, z = a.z - b.z}
end

function vector.multiply(v, s)
    if type(s) == "table" then
        return {x = v.x * s.x, y = v.y * s.y, z = v.z * s.z}
    end
    return {x = v.x * s, y = v.y * s, z = v.z * s}
end

function vector.divide(v, s)
    return {x = v.x / s, y = v.y / s, z = v.z / s}
end

function vector.length(v)
    return math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
end

function vector.distance(a, b)
    return vector.length(vector.subtract(b, a))
end

function vector.direction(a, b)
    local d = vector.subtract(b, a)
    local l = vector.length(d)
    if l < 1e-9 then return {x = 0, y = 0, z = 1} end
    return {x = d.x / l, y = d.y / l, z = d.z / l}
end

function vector.normalize(v)
    return vector.direction({x = 0, y = 0, z = 0}, v)
end

function vector.floor(v)
    return {x = math.floor(v.x), y = math.floor(v.y), z = math.floor(v.z)}
end

function vector.round(v)
    return {x = math.floor(v.x + 0.5), y = math.floor(v.y + 0.5), z = math.floor(v.z + 0.5)}
end

function vector.dot(a, b)
    return a.x * b.x + a.y * b.y + a.z * b.z
end

function vector.cross(a, b)
    return {
        x = a.y * b.z - a.z * b.y,
        y = a.z * b.x - a.x * b.z,
        z = a.x * b.y - a.y * b.x,
    }
end

function vector.offset(v, x, y, z)
    return {x = v.x + x, y = v.y + y, z = v.z + z}
end

function vector.equals(a, b)
    return a.x == b.x and a.y == b.y and a.z == b.z
end

function vector.angle_to(a, b)
    return math.acos(vector.dot(a, b) / (vector.length(a) * vector.length(b) + 1e-9))
end

-- 暴露 json 供测试脚本使用
_G.json = json
