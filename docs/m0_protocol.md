# M0 协议规范：Luanti ↔ Python 文件 IPC

> M0 目标（DESIGN.md §11）：**不 fork 引擎**，打通全链路：
> `luantiserver + Mineclonia + mcl2_agent` ↔ 文件 IPC ↔ `Python 驱动`，
> 跑通首个任务 `craft_planks`，产出 canonical 数据（meta.json 含种子）。
>
> 本规范是 Lua 侧与 Python 侧的**共同契约**，双方严格按此实现。

## 1. 消息体（JSON，与 TCP 版本同构）

所有消息是单行 JSON。传输层可插拔（M0 用文件，M1 换 TCP），**消息体 schema 不变**。

### 请求（Python → Lua）

```json
{"req_id": 1, "op": "begin_episode", "...参数...": "..."}
```

### 响应（Lua → Python）

```json
{"req_id": 1, "ok": true, "result": {...}}
{"req_id": 2, "ok": false, "result": {"error": "unknown_task:xxx"}}
```

### 事件（Lua → Python，异步推送）

```json
{"event": "task_done", "data": {"episode_id": "ep-000001", "success": true}}
```

## 2. 操作清单（op → 请求参数 → 响应 result）

| op | 请求参数 | 响应 result |
|---|---|---|
| `ping` | — | `{"pong": true, "version": "...", "tick": N}` |
| `observe` | `player` | 完整观测（DESIGN.md §5 结构，含 task/episode 段） |
| `tasks` | — | `{"tasks": [{"id","instruction","type","difficulty"}, ...]}` |
| `begin_episode` | `player, task_id, run_id, episode_id, world_seed, task_seed, reset_seed` | `{"episode": "ep-000001"}` |
| `end_episode` | `player, success` | `{"ok": true}` |
| `execute` | `player, action, args` | `{"action_id": N}` |
| `step` | `player, primitive{...}` | `{"ok": true, "tick": N}` |
| `set_config` | `value{...}`（浅合并进 config） | `{"ok": true}` |

## 3. 文件传输（M0 实现）

IPC 根目录：`<world>/mcl2_agent/ipc/`

| 目录 | 方向 | 说明 |
|---|---|---|
| `ready.json` | Lua → Python | mod 加载完成后写入 `{"ready": true, "version": "..."}`；Lua 只在首次写 |
| `requests/` | Python → Lua | 每文件一个请求 `req_<seq>.json`；**Lua 处理后删除** |
| `responses/` | Lua → Python | 每文件一个响应 `resp_<req_id>.json`；**Python 读取后删除** |
| `events/` | Lua → Python | 每文件一个事件 `ev_<seq>.json`；Python 读取后删除 |

### 原子写约定

写文件用"临时名 + 重命名"（`os.rename`），避免读端读到半截 JSON。
Lua 用 `io.open(tmp)` → 写 → `close` → `os.rename(tmp, final)`。
Python 用 `os.replace()` 同理。

### 轮询频率

- Lua 侧：`register_globalstep` 每 10 tick（0.5s）扫一次 `requests/`。
- Python 侧：每 0.1s 扫 `responses/` 与 `events/`；等待 `ready.json` 最多 30s。

### 编解码

Lua: `minetest.write_json` / `minetest.parse_json`。
Python: `json`。文件名编码一律 UTF-8，不转义非 ASCII（`ensure_ascii=False`）。

## 4. M0 Bot 模型（服务器端逻辑 bot，无客户端）

不 fork 引擎、不接客户端，bot 由 mod 管理：

- bot 名：`bot1`，mod 加载时自动创建（无需玩家加入）。
- 位置/朝向：mod 内状态表 `pos`, `look{yaw,pitch}`。
- 背包：`minetest.create_detached_inventory("mcl2_agent_bot_"..name)`。
- 血量等：mod 状态表。
- **动作语义**（M0 简化实现，非物理）：
  - `goto`/`look_at`：直接改 bot 状态。
  - `dig`/`place`：`core.dig_node` / `core.place_node`（无 placer，掉落物实体进世界）。
  - `craft`：**模拟合成**——查 mod 内建配方表，扣 input 加 output 到背包。
  - 配方表（M0 内建，后续对接 Mineclonia `mcl_crafting`）：
    `{["mcl_core:planks"] = {input = {["mcl_core:tree"] = 1}, output = 4}}`

## 5. 记录与数据路径（M0）

- `record.out_dir` 默认改为 `mcl2_agent/data`（在 worldpath 下）。
- episode 目录：`<world>/mcl2_agent/data/episodes/ep-XXXXXX/`
  - `meta.json`（含 `world_seed/task_seed/reset_seed/mapgen/env`）
  - `instructions.jsonl`、`states.jsonl`、`actions.jsonl`、`rewards.jsonl`
  - `observations/`（M0 无渲染器，可为空）
  - `episode_summary.json`（成功/步数/帧数/种子）

## 6. M0 验收标准

1. `luantiserver` 以 Mineclonia + `mcl2_agent` worldmod 启动，写出 `ready.json`。
2. Python 驱动跑通：
   `begin_episode(craft_planks)` → 循环 `execute(craft)` / `observe` →
   `task.success == true` → `end_episode(true)`。
3. 断言：episode 目录存在，`meta.json` 含三个种子字段；
   `states.jsonl/actions.jsonl/rewards.jsonl` 非空；`episode_summary.json` `success=true`。

## 7. 服务器启动参数（供驱动复用）

```bash
luanti/bin/luantiserver \
  --world <repo>/luanti/worlds/m0world \
  --config <repo>/luanti/worlds/m0world/server.conf \
  --logfile <repo>/luanti/worlds/m0world/server.log
```

- `world.mt`：`gameid = mineclonia`，`enable_damage = true`
- `server.conf`：`name = m0admin`、`bind_address = 127.0.0.1`、`server_announce = false`、
  `enable_damage = true`、`debug_log_level = action`
- mod 放 `<world>/worldmods/mcl2_agent/`（worldmods 自动加载，无需游戏依赖）
