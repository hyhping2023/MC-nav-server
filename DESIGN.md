# MCL2-Env：Minetest/Luanti + Mineclonia 的 VLA 数据采集与 Agent 框架设计

> 版本：0.2（设计 + 实现进度）
> 目标：以 **语言任务指令 + 第一人称视觉观测 + 玩家/世界状态 + 语义动作空间** 四要素，产出**可训练 VLA（Vision-Language-Action）的轨迹数据**，并给各种 VLA 模型提供统一的沙盒世界交互接口。
>
> **实现进度（2026-08-04）**：
> - ✅ **M0 已交付**：Luanti 5.17.0-dev 服务器 + Mineclonia 0.123.0 + `mcl2_agent` 文件 IPC，`craft_planks` 全链路打通（详见 docs/m0_protocol.md）
> - ✅ **M1 已交付**：引擎 fork（`core.set_player_control` 客户端权威移动注入 + 客户端 FBO→shm 抓帧）、玩家驱动 bot、Python 渲染器（engine_fork/voxel/composite），`random_agent.py` 端到端 PASS（详见 docs/m1_protocol.md）
> - ✅ **M2 已交付**：请求式采样帧/状态对齐、WebDataset/HF/RLDS 导出器、`collect_dataset.py`，端到端数据集验证通过（详见 docs/m2_protocol.md），已验证数据集在 `datasets/m2_c_verify/`
> - ⏳ **M3/M4**：语义动作落盘优化、脚本化成功 agent、任务课程化、VLA 模型对接（见 §11）
>
> 文档中的"设计目标"描述已实现部分以实际代码为准；协议契约见 `docs/m0_protocol.md`、`docs/m1_protocol.md`、`docs/m2_protocol.md` 与 `docs/engine_fork.md`。

---

## 1. 目标与定位

在 Luanti（原 Minetest）引擎 + Mineclonia 游戏（MineClone2 的维护分支）上构建一个 **Lua API 框架**，使：

1. **自动化 Bot**：服务器权威地控制玩家（移动、视角、挖掘、放置、合成、战斗…），无需真人操作。
2. **任务生成**：程序化 / 参数化 / LLM 驱动的语言任务指令，带自动成功判定。
3. **状态采集**：统一的、JSON 可序列化的玩家/世界/背包/任务状态观测。
4. **动作执行**：语义动作空间（高层命令）+ 原始动作空间（按键级）双层设计，语义层自动分解为原始层。
5. **视觉同步**：fork 客户端抓取第一人称 RGB 帧，与游戏 tick 时间戳对齐。
6. **数据落盘**：每 episode 一个目录，JSONL + PNG，**完整保存种子信息便于世界还原**。
7. **VLA 接口**：`gymnasium.Env` + 远程 HTTP/WebSocket API + Python SDK，供各类 VLA 模型即插即用。

### 为什么用 Luanti/Mineclonia 而不是 Minecraft？

| 维度 | Minecraft (MineDojo/MineStudio) | Luanti + Mineclonia |
|---|---|---|
| 源码 | Java/闭源协议，靠 mod/插件适配 | C++ 全开源，可 fork 改渲染与输入注入 |
| 速度 | 受 Java 客户端限制 | Craftium 实测 >2K steps/s |
| 服务器权威控制 | 需要服务端 mod 注入 | fork 后 `set_player_control` 直接注入按键 |
| 内容覆盖 | Minecraft 全内容 | Mineclonia 覆盖大部分生存内容（合成、烧炼、附魔、村庄、下界） |
| 成本 | 需购买 | 全免费 |

### 游戏选择说明

MineClone2 项目已改名/分裂为两条维护线，**框架按 `mcl2_*` 物品命名空间设计，两条线都可跑**：

- **Mineclonia**（推荐）：`ryvnf/mineclonia`，ContentDB 上的活跃维护分支，持续更新。
- **VoxeLibre**（原 MineClone2）：`Wuzzy/VoxeLibre`，另一活跃分支。

若需要 Minecraft 原版物品（`minecraft:oak_log`）的语义一致性，可加一层 `item_alias.lua` 把 Mineclonia 物品名映射到原版名，后续导出 VLA 数据时可保留原版命名。

---

## 2. 参考工作

| 项目 | 借鉴点 |
|---|---|
| **MineDojo** (arXiv 2206.08853) | 多模态观测（RGB + compass + voxels）、复合动作空间（movement/camera/attack/craft/place）、程序化任务套件、`minecraft:log_success` 成功判定 |
| **Voyager** | LLM 驱动的技能库 + 任务课程（curriculum），可迁移为任务生成器 + 语义动作组合 |
| **VPT / STEVE-1** | 按键级动作 token（camera bin × buttons 的组合离散空间），文本→行为的先验 + 反演层 |
| **MR-STEVE / ROCKET-2** | 指令跟随导航策略、跨游戏零样本迁移（验证"语义动作 + 第一人称观测"范式） |
| **Craftium** (ICML 2025) | **fork Luanti 引擎**接 Gymnasium/PettingZoo：Python↔引擎通信、动作→键盘鼠标注入、软重置（不重启引擎）、慢 agent 同步 |
| **MineStudio** (CraftJarvis) | 轨迹数据组织（episode + modal kernel callbacks）、`RecordCallback`、VPT/GROOT/STEVE-1/ROCKET 模型与数据字段兼容 |
| **Mineflayer**（仓库内参考） | `bot.goTo / dig / craft / place` 高层 bot API 风格 → 本框架语义动作命名与参数语义对齐，便于迁移既有 Mineflayer 脚本 |
| **Open X-Embodiment / RLDS** | episode 数据格式规范、字段命名（observation/action），便于跨平台对齐 |
| **OpenVLA** | 7-DoF 动作 + 指令微调范式（本框架的语义动作空间按可序列化 dict 输出，兼容 OpenVLA 的 action 字段） |

---

## 3. 总体架构

```
┌────────────────────────────────────────────────────────────────────┐
│ Python 层 (mcl2_env/)                                              │
│  ┌──────────────┐ ┌───────────────────┐ ┌───────────────────────┐  │
│  │ Env          │ │ VLA Server        │ │ Dataset               │  │
│  │ (gymnasium)  │ │ (FastAPI + WS)    │ │ EpisodeWriter/Export  │  │
│  │ RemoteEnv    │ │ /reset /step ...  │ │ (WebDataset/HF/RLDS)  │  │
│  └──────┬───────┘ └────────┬──────────┘ └───────────┬───────────┘  │
└─────────┼──────────────────┼────────────────────────┼──────────────┘
          │ JSON 控制/状态    │ 帧流 (共享内存 / TCP)   │ 磁盘 JSONL+PNG
┌─────────▼──────────────────▼────────────────────────▼──────────────┐
│ 桥接层 (mcl2_agent/api/bridge.lua + 引擎 fork)                      │
│  • TCP server / mod channel 协议解析 (帧头 + JSON body)             │
│  • core.set_player_control(player, controls)   ← fork 新增 C++ API  │
│  • 客户端抓帧: RenderingEngine 绘制后读 FBO → 环形缓冲 → Python     │
└─────────┬──────────────────────────────────────────────────────────┘
┌─────────▼──────────────────────────────────────────────────────────┐
│ Luanti 引擎 (fork) + Mineclonia 游戏 + mcl2_agent 模组              │
│  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌────────┐ ┌──────────────┐   │
│  │ action  │ │ state    │ │ task   │ │ record │ │ reset (软重置)│   │
│  │ 动作执行 │ │ 状态采集  │ │ 任务生成│ │ 数据落盘│ │ episode 重置  │   │
│  └─────────┘ └──────────┘ └────────┘ └────────┘ └──────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

### 分层职责

| 层 | 位置 | 职责 | 关键技术 |
|---|---|---|---|
| 游戏引擎层 | `luanti/`（fork） | 渲染、物理、服务器权威 | C++ fork：`set_player_control`、FBO 抓帧 |
| 游戏内容层 | `games/mineclonia/` | 方块/物品/合成/生物 | Mineclonia 自带 |
| 框架模组层 | `mods/mcl2_agent/` | 状态/动作/任务/记录/reset | 纯 Lua，全部可替换 |
| 桥接层 | `mcl2_agent/api/bridge.lua` | 与 Python 进程通信 | TCP + JSON（长连接，按消息长度分包） |
| Python 环境层 | `mcl2_env/` | gymnasium Env、渲染器、数据集、服务器 | Python ≥3.10 |
| 模型接口层 | `mcl2_env/server.py` + `client.py` | VLA 模型接入 | HTTP/WebSocket |

### 通信协议（bridge）

- 传输：TCP 长连接（本地回环，默认 `127.0.0.1:25585`），可加 TLS。
- 编码：**长度前缀帧** `[4B big-endian length][1B type][JSON body]`；type ∈ `{request, response, push, frame}`。
- 帧流通道独立：图像走共享内存环形缓冲（mmap）+ 事件通知，避免大图像走 TCP 阻塞控制面。
- 时序：每条请求带 `req_id` 与 `server_tick`，响应带回 `server_tick`，客户端用 tick 对齐帧与状态。

> **实现现状（M0）**：当前实际传输为**文件 IPC**（`<world>/mcl2_agent/ipc/`，`req_<seq>/resp_<req_id>/ev_<seq>` JSON 文件 + 原子写），
> 消息体 schema 与 TCP 版同构，后续可无缝换回 TCP。协议细节见 docs/m0_protocol.md。
> 关键约定：**Lua handler 失败必须返回 `{error=...}`**，`ipc.process_request` 会把含 `error` 字段的 result 置 `ok=false`，
> Python 侧据此抛 `BridgeError`（M1-E 期间修复的错误传播缺陷，勿回退）。

---

## 4. 动作空间（Action Space）

### 4.1 双层设计

```
语义动作 SemanticAction  (高层、可解释、适合 LLM/VLA 输出)
   │  decompose
   ▼
原始动作 PrimitiveAction (按键级、VPT 兼容、适合 VLA/RL 输出)
```

两者都记录进轨迹数据（双标签），供不同训练范式使用：
- VLA 语义范式：用 `SemanticAction`（如 `craft("planks", 4)`）。
- VPT/RL 范式：用 `PrimitiveAction` token（如 `action["forward"]=1, action["camera"]=[0,0]`）。

### 4.2 原始动作空间（Primitive）

与 VPT / MineStudio 字段对齐（`action.forward/back/left/right/jump/sneak/sprint/attack/use/drop/hotbar/camera`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `forward/back/left/right` | bool | 移动按键（由 fork 的 `set_player_control` 注入） |
| `jump` | bool | 跳跃 |
| `sneak` | bool | 潜行 |
| `sprint` | bool | 疾跑 |
| `attack` | bool | 挖掘/攻击（鼠标左键） |
| `use` | bool | 使用/放置（鼠标右键） |
| `drop` | bool | 丢弃手中物品 |
| `hotbar` | int 0-8 | 选中快捷栏槽位 |
| `camera` | [pitch_delta, yaw_delta] 或 int bin | 视角增量（连续 float 或离散 bin） |

**VPT 兼容 token**：可选启用 `action_space_mode = "vpt_token"`，将 camera 分为 7×7 bins、按钮取有效组合，编码为 0..8639 的离散 token（与 VPT 论文一致），便于直接加载 VPT/STEVE-1 系模型。

### 4.3 语义动作空间（Semantic）

动作名使用 snake_case，参数用 JSON 兼容的 dict。所有语义动作都可被记录并在轨迹中还原为原始动作序列。

| 类别 | 动作 | 参数示例 | 分解方式 |
|---|---|---|---|
| 导航 | `goto` | `{pos, tolerance=1.0}` | `core.find_path` → 分段移动 + 朝向 |
| 导航 | `look_at` | `{pos}` / `{entity}` | `set_look_horizontal/vertical` 插值 |
| 导航 | `follow` | `{entity_id}` | 循环 goto |
| 采集 | `dig` | `{pos}` / `{target="aimed"}` | 朝向 + 装备合适工具 + attack 按键 + 拾取掉落物 |
| 采集 | `collect_nearby` | `{radius=4}` | 扫描物品实体 → goto → 接触拾取 |
| 建造 | `place` | `{item, pos}` | 选中物品 → 朝向 → use 按键 |
| 建造 | `build_up` / `build_wall` | `{item, n}` | 多点 place 序列 |
| 背包 | `equip` | `{item}` | 从背包移动到手/快捷栏 |
| 背包 | `select_slot` | `{item}` | 查找物品所在槽 → hotbar 选择 |
| 背包 | `drop` | `{item, count}` | 移出背包到地上 |
| 合成 | `craft` | `{item, count, where="crafting_table"}` | 打开合成 UI（含工作台）→ 放料 → 取回 |
| 烧炼 | `smelt` | `{item, fuel}` | 熔炉放料放燃料 → 等待 → 取回 |
| 战斗 | `attack_entity` | `{target="nearest_animal"}` | 朝向 + 间隔 attack |
| 交互 | `use_block` | `{pos}` | 箱子/门/拉杆/熔炉的 use |
| 交互 | `eat` | `{item}` | 选中食物 → use |
| 交易 | `trade` | `{villager_entity, index}` | 打开交易表单 → 选择 |

> 每个语义动作在 Lua 侧注册为 `{name, args_schema, decomposer, success_check, timeout}`，见 §7。

### 4.4 动作执行协议

```
Python 动作 ──> bridge ──> action.execute(action_id, args)
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
            semantic registry     primitive executor
              (decomposer)        (set_player_control / set_look_* /
                                   dig_node / place_node / 背包操作)
                    │
                    ▼
              current_action 队列 + progress
```

- **服务器权威**：除相机移动外的所有物理由服务器注入按键实现，保证多 bot 并行与确定性。
- **阻塞语义**：语义动作是异步任务队列；`execute` 立即返回 `action_id`，Python 侧轮询 `state.action_progress` 或 push 完成事件。
- **原子化**：原始动作是单 tick 语义，`step()` 每个 tick 消费一组原始动作。

---

## 5. 状态接口（State Interface）

所有状态由 `mcl2_agent/api/state.lua` 的 `agent.get_observation()` 产出，**纯 JSON 可序列化**。Python 侧用 `schemas.py` 校验。

```jsonc
{
  "player": {
    "pos": [x, y, z],
    "look": {"yaw": 0.0, "pitch": 0.0, "dir": [dx, dy, dz]},
    "velocity": [vx, vy, vz],          // fork 后由引擎提供
    "on_ground": true,
    "hp": 20, "max_hp": 20,
    "breath": 10,
    "saturation": 20.0, "hunger": 20,   // Mineclonia 提供
    "armor": 0.0,
    "selected_slot": 0,
    "held_item": "mcl_core:wood",       // 物品名，alias 到 minecraft:* 可选
    "dimension": "overworld",
    "effects": []                        // 药水等
  },
  "inventory": {
    "main": [ {"item": "mcl_core:wood", "count": 10, "meta": null}, ... ],
    "armor": [], "offhand": null,
    "cursor": null,
    "slots_total": 36
  },
  "world": {
    "timeofday": 0.5,
    "day_count": 3,
    "biome": "plains",
    "weather": "clear",
    "seed": 123456789,                   // mapgen 种子（world.mt）
    "nearby_blocks": [ {"pos": [...], "name": "mcl_core:tree", "facedir": 0}, ... ],  // 当前视野 raycast 结果
    "aimed_block": {"pos": [...], "name": "mcl_core:tree"},
    "entities": [ {"id": 5, "name": "mcl_mobs:chicken", "pos": [...], "hp": 8}, ... ],  // 半径内
    "items_on_ground": [ {"item": "mcl_core:stick", "pos": [...]}, ... ],
    "voxels": [[[node_id, ...], ...], ...]   // 可选：玩家周围局部体素网格（MineDojo 风格）
  },
  "stats": {"xp": 12, "level": 0, "kills": 0, "deaths": 0, "playtime": 42.5},
  "task": {
    "id": "craft_planks",
    "instruction": "Craft 4 planks from wood.",
    "instruction_zh": "用木头合成 4 个木板。",
    "type": "craft",
    "difficulty": 1,
    "progress": {"collected_wood": 3, "needed": 4},
    "success": false,
    "steps": 120
  },
  "episode": {
    "episode_id": "ep-000042",
    "run_id": "run_20260804_ab12",
    "world_seed": 123456789,
    "task_seed": 987654321,
    "server_tick": 48213,
    "wall_time": 1722760000.123
  }
}
```

### 观测频率与采样

- 服务器 `register_globalstep`（默认 0.05s = 20Hz tick）驱动状态采样。
- 记录时可按 `record.fps`（默认 2~5Hz，与图像一致）降采样，图像与状态同帧号。
- VLA 训练典型输入：`image 224×224 + player.state + task.instruction`。

> **实现现状（M2）**：采样改为**请求式**——`record.sample` 由 `observe` 触发（全局 step 不再定时采样），
> **一次 observe 恰好写一行 states/actions/rewards**，帧号 `episode.frame` 在 observe 响应中返回，
> Python 用该帧号写 PNG，实现帧/状态严格一一对应（M2-C 验证：states==actions==rewards==PNG）。
> states 行含 `server_tick` 与 `wall_us`（`minetest.get_us_time()`）。

---

## 6. 任务生成器（Task Generator）

### 6.1 任务 Schema

```lua
{
  id = "craft_planks",
  name = "Craft Planks",
  instruction = "Craft 4 planks from wood.",      -- 英文（VLA 主流）
  instruction_zh = "用木头合成 4 个木板。",        -- 中文备用
  type = "craft",                                   -- collect / craft / build / combat / nav / interact
  tags = {"early_game", "wood"},
  difficulty = 1,                                   -- 0..10，课程用
  reset = {                                        -- 见 §6.3
    pos = {x=0, y=40, z=0}, area_radius = 8,
    inventory = {give = {"mcl_core:wood 3"}, clear = true},
    timeofday = 0.5, weather = "clear", time_speed = 0,
  },
  success_predicate = "inventory_contains",        -- 判定器名
  success_args = {item = "mcl_core:planks", count = 4},
  reward_shaping = {per_step = 0.0, on_progress = 0.1},
  timeout_ticks = 6000,
  prerequisites = {"collect_wood"},                 -- 课程依赖
}
```

### 6.2 四类生成器

| 生成器 | 说明 | 例子 |
|---|---|---|
| **Registry（静态）** | 手工定义的任务表，覆盖基础教学 | `collect_wood`, `craft_planks`, `place_torch` |
| **Procedural（参数化）** | 在注册表基础上按参数实例化 | 遍历 Mineclonia 可合成物品表 → `craft_<item>`，参数化数量/位置 |
| **Curriculum（课程）** | 按难度/前置关系排序，动态调整 | 木头 → 木板 → 木棍 → 工具 → 石镐 → 采矿 → 熔炉… |
| **LLM 生成** | 调外部 LLM 生成任务+判定器（Voyager 式） | "造一座 3×3 木屋" → 生成放置区域判定 |

### 6.3 成功判定器（Predicates）

内置判定器注册表，`success_predicate` 指向它：

| 判定器 | 含义 |
|---|---|
| `inventory_contains` | 背包内物品数量 ≥ N（可含 meta 匹配） |
| `block_placed` | 指定区域内存在某方块（体积统计） |
| `entity_killed` | 击杀指定类型实体 ≥ N |
| `player_at` | 玩家到达目标点（距离 < tol） |
| `block_mined` | 指定方块被挖掉（on_dignode 统计） |
| `stat_reached` | 某项 stat ≥ 阈值 |
| `custom` | 注册的 Lua 回调 |

判定器在 `register_globalstep` 中评估，命中后：写记录 → 结算 reward → push 成功事件。

### 6.4 与 LLM / 外部规划器的接口

- 暴露 `agent.task.list() / get(id) / generate(prompt)`。
- `generate` 返回候选任务，由 `success_predicate="custom"` + `agent.task.register` 动态注册。
- 支持将"语言指令 → 语义动作序列"的规划外包给外部 LLM（Voyager 模式），Lua 只负责执行与验证。

### 6.5 Episode 生命周期

```
rollout.begin(task_id)  ──►  reset.apply(task.reset)        (软重置, 见 reset.lua)
                          ►  task.seed 采样（种子由 Python 传入或从 run 配置派生）
                          ►  instruction 写入 HUD + 记录
                          ►  采集开始
step 循环                  ►  每 tick: 执行动作 → 采样状态 → 判定成功/超时 → 记录
rollout.end(success)      ►  汇总 episode_summary.json
```

---

## 7. 数据记录格式（Data Recording）

### 7.1 磁盘布局（Canonical）

```
<run_dir>/                                   # 如 datasets/run_20260804_ab12/
  runs.jsonl                                 # run 级：每个 epoch 一行
  episodes/
    ep-000000/
      meta.json                              # 全部种子 + 版本 + 配置
      instructions.jsonl                     # 每步指令文本（通常恒定，但可动态改）
      observations/
        000000.png                           # 第一人称 RGB
        000001.png
        ...
      states.jsonl                           # 与帧对齐的玩家/世界状态
      actions.jsonl                          # 语义动作 + 原始动作双标签
      rewards.jsonl                          # reward / terminated / truncated / info
      episode_summary.json                   # 成功、步数、时长、指标
```

### 7.2 种子保存（还原世界的关键，用户重点要求）

`meta.json` 完整记录所有可还原信息：

```json
{
  "schema_version": "1.0.0",
  "episode_id": "ep-000042",
  "run_id": "run_20260804_ab12",
  "world_seed": 123456789,
  "mapgen": {"name": "v7", "flags": {"water_level": 1, "cave_width": 3}},
  "task_seed": 987654321,
  "task": {"id": "craft_planks", "params": {"count": 4}},
  "reset_seed": 555666777,
  "env": {
    "engine": {"name": "luanti", "version": "5.12.0", "fork": "mcl2-agent-fork-v0.1"},
    "game": {"name": "mineclonia", "version": "0.92"},
    "mod": {"name": "mcl2_agent", "version": "0.1.0"},
    "python": {"package": "mcl2_env", "version": "0.1.0"}
  },
  "renderer": {"impl": "engine_fork", "width": 224, "height": 224, "fov": 72, "fps": 5},
  "action_space": {"mode": "semantic+primitive", "version": "1.0"},
  "physics": {"set_physics_override": null, "time_speed": 0},
  "world_files": ["../worlds/world_123456789.tar.zst"],   // 可选：世界存档快照
  "start": {"wall_time": 1722760000.0, "server_tick": 1000}
}
```

**还原路径**：`world_seed` + `mapgen` 可直接重建地形；`reset_seed` 还原初始位置/背包/时间；极端情况下可启用 `world_files` 对世界存档打 tar 快照（每 N 个 episode 一次），实现**逐 bit 还原**。

### 7.3 文件行格式

`states.jsonl`（每行一个 JSON，`tick` 与图像文件名对齐）：

```json
{"tick": 1000, "frame": 0, "image": "observations/000000.png", "player": {...}, "world": {...}, "episode": {...}}
```

`actions.jsonl`（双标签）：

```json
{"tick": 1000, "frame": 0,
 "semantic": {"id": "craft_planks", "args": {"item": "mcl_core:planks", "count": 4}, "action_id": 7, "status": "running"},
 "primitive": {"forward": 1, "jump": 0, "attack": 0, "use": 0, "hotbar": 2, "camera": [0, 0]},
 "vpt_token": 1234}
```

`rewards.jsonl`：

```json
{"tick": 1000, "frame": 0, "reward": 0.0, "terminated": false, "truncated": false, "info": {"progress": 0.3}}
```

### 7.4 图像编码

- 编码：PNG（有损采集时可用 JPEG q90）。命名 `%06d.png`，与帧号一致。
- 帧头写入 EXIF 不可靠，改为在 `states.jsonl` 中记录 `server_tick` + `wall_time`，保证**时间对齐**（渲染延迟补偿见 §9）。

### 7.5 导出工具（Python）

`mcl2_env/dataset/export.py` 提供从 canonical 格式导出的转换器：

| 目标 | 说明 | 状态 |
|---|---|---|
| **WebDataset** | `{__key__}.jpg + .json` 的 tar 分片，流式加载 | ✅ 已实现并验证（42 样本加载成功） |
| **HuggingFace Dataset** | Parquet + image 列，可直接 `push_to_hub` | ✅ 已实现（data.parquet，12 行验证） |
| **RLDS / TFRecords** | 对齐 Open X-Embodiment episode 结构（`observation/action` 字段名） | ⚠️ 已实现；本机 py3.14 无 TF wheel，走"缺包提示"路径 |
| **MineStudio 兼容** | 字段名对齐 `observation.pov / action.camera / action.buttons` | ✅ action 已映射为 `buttons/camera/hotbar` |

> **M2 现状**：`collect_dataset.py` 采集 N episode → 对齐断言 → `export_webdataset` 一站式。
> 已验证数据集产物：`datasets/m2_c_verify/`（3 episode 42 帧，含成功轨迹）。
> WebDataset 1.0.2 gotcha：`wds.WebDataset()` 传目录不 glob、需显式 shard 文件列表；`.json` 样本需 `.decode()`。
> datasets 5.x gotcha：`Sequence(dict_feature)` 是列式语义；写 parquet 用 `Dataset.to_parquet`。

---

## 8. VLA 模型接口

### 8.1 gymnasium.Env（本地）

```python
env = mcl2_env.make("MCL2/Mineclonia-v0", task="craft_planks", headless=True)
obs, info = env.reset(seed=42)          # obs = {image, player_state, instruction, ...}
obs, reward, terminated, truncated, info = env.step(action)
# action 可选两种：
#   semantic:  {"type": "semantic", "id": "goto", "args": {"pos": [...]}}
#   primitive: {"type": "primitive", "forward": 1, "jump": 0, ...}
```

### 8.2 远程接口（供任意 VLA 模型 / 集群接入）

`mcl2_env/server.py` 暴露 REST + WebSocket：

| 方法 | 端点 | 说明 |
|---|---|---|
| POST | `/reset` | 启动新 episode，可选 `task` / `seed` |
| POST | `/step` | 执行一个动作，返回 `{obs, reward, terminated, truncated, info}` |
| POST | `/execute` | 执行语义动作（非阻塞，返回 `action_id`） |
| GET | `/observe` | 拉取最新观测（不推进） |
| GET | `/state` | 拉取原始状态 JSON |
| GET | `/tasks` | 列出可用任务 |
| POST | `/generate_task` | 触发任务生成 |
| POST | `/record/start|stop` | 控制数据落盘 |
| POST | `/visualize` | 返回当前帧 PNG（给模型/人看） |

### 8.3 Python SDK（`mcl2_env/client.py`）

```python
from mcl2_env.client import AgentClient

client = AgentClient("http://127.0.0.1:8000")
obs, info = client.reset(task="craft_planks")
for _ in range(6000):
    action = model.predict(obs)          # 任意 VLA：OpenVLA / Pi0 / GROOT / ROCKET / STEVE-1 ...
    obs, reward, done, truncated, info = client.step(action)
    if done:
        break
```

### 8.4 与现有模型的对接说明

| 模型 | 接入方式 |
|---|---|
| **OpenVLA** | semantic action 输出为 dict 序列化；obs 用 `image + state` |
| **Pi0 / GR00T** | 输出离散/连续动作 → 映射到 primitive 字段 |
| **VPT / STEVE-1 / GROOT (MineStudio)** | 启用 `vpt_token` 动作模式 + `observation.pov` 字段别名 |
| **LLM（Voyager 式）** | 直接输出语义动作字符串，经 `client.execute()` |

---

## 9. 视觉同步（引擎 fork 方案）

### 9.1 抓帧链路

```
Luanti 客户端 (fork)
  RenderingEngine::drawAllScenes()   // 每帧渲染完成后
      └── 读 FBO (GL_RGBA) → 下采样到 224×224
          └── 写入共享内存环形缓冲 (mmap, 32 帧深)
              └── 写事件通知 (pipe/eventfd)
Python Renderer.engine_fork
  └── 环形缓冲消费 → RGB → obs["image"]
```

- 客户端以 `video_fps=30` 渲染，采集端按 `record.fps`（2~5）降采样并记录 `server_tick`。
- 相机位姿来源：服务器 `player:get_look_*` 与 `get_pos`，在 `states.jsonl` 中已含，帧与状态用 `server_tick` 对齐。
- **确定性**：数据采集期间 `time_speed=0`、固定 `timeofday`、关闭天气随机、`renderer` 关闭昼夜亮度抖动，保证同一 seed 两次采集视觉一致。

### 9.2 服务器输入注入（fork 最小改动）

| 改动 | 位置（C++） | 说明 |
|---|---|---|
| `core.set_player_control(player, {forward=, jump=, ...})` | `src/script/lua_api/l_object.cpp` | 把按键表写入 `RemotePlayer::controls`，走真实输入管线 |
| `player:get_velocity()` / `set_velocity()` | 同上 | 观测速度 + 可选直接设速 |
| 可选 `core.freeze_environment()` | `src/serverenvironment.cpp` | 数据采集时暂停生物 AI / 掉落物物理（保确定性） |

> Craftium 已实现等价功能，可直接参考其 `src/` 下 patch 的写法（GPL-3.0，注意许可证兼容）。

> **实现现状（M1）**：
> - `core.set_player_control` 已实现（`l_object.cpp`，两种形式：`player:set_player_control(...)` 与 `core.set_player_control(player, ...)`）。
> - **关键发现**：本 Luanti 5.17 fork 是**客户端权威移动**——纯服务器赋值不移动连入客户端。已实现完整客户端注入：
>   新协议包 `TOCLIENT_MCL2_PLAYER_CONTROL = 0x65` + `RemotePlayer::control_injected` 标记 +
>   客户端 `updatePlayerControl` 用注入控件覆盖键盘。自验：bot1 60 tick 前进 2.2 格。
> - 抓帧：`src/client/mcl2_framegrab.{h,cpp}` 挂在 `RenderingEngine::draw_scene` 尾部，写 `/tmp/mcl2_frames`
>   （44 字节头 + 32 槽 × stride×height BGRA）。**实测本环境 GL 窗口创建失败但仍能出真实像素帧**（内容静态，
>   真实 GL 环境会随相机变化）。
> - 详细改动点见 docs/engine_fork.md 与 docs/m1_protocol.md。

---

## 10. 目录结构与代码骨架

```
fake-mc/
├── DESIGN.md                        # 本文档（设计 + 实现进度）
├── docs/
│   ├── m0_protocol.md               # M0 文件 IPC 契约
│   ├── m1_protocol.md               # M1 引擎 fork 契约（set_player_control + 抓帧）
│   ├── m2_protocol.md               # M2 数据管道契约（请求式采样对齐 + 导出）
│   └── engine_fork.md               # 引擎 fork 改动指南
├── mcl2_agent/                      # Lua 模组（部署到 worldmods/mcl2_agent，符号链接）
│   ├── mod.conf
│   ├── init.lua                     # 引导 + 会话 + 全局 step（动作/任务/事件）
│   ├── config.lua                   # 默认配置 + util（tick/atomic_write/list_dir/atan2）
│   ├── api/
│   │   ├── action.lua               # 语义/原始动作注册与执行器 + recipes 模拟合成
│   │   ├── state.lua                # get_observation()（玩家 ObjectRef）
│   │   ├── task.lua                 # 任务注册表 + 判定器 + 生成器
│   │   ├── record.lua               # 请求式采样 + JSONL/PNG 落盘 + meta.json
│   │   ├── reset.lua                # 软重置（apply_player）
│   │   ├── bridge.lua               # op handlers（ping/observe/tasks/begin/end/execute/step）
│   │   ├── ipc.lua                  # 文件 IPC（requests 轮询 / responses / events / ready）
│   │   ├── bot.lua                  # 玩家适配层（bot1 → 真实 ObjectRef）
│   │   └── vision.lua               # 相机上报 + 确定性渲染配置
│   ├── tasks/                       # collect/craft/build/combat 任务定义
│   └── test/                        # minetest_stub.lua + run_m0_test.lua（无引擎测试）
├── mcl2_env/                        # Python 包
│   ├── pyproject.toml
│   └── mcl2_env/
│       ├── __init__.py              # 可选导入 pydantic/gymnasium
│       ├── schemas.py               # 状态/动作类型
│       ├── env.py                   # GymnasiumEnv + RemoteEnv
│       ├── bridge.py                # BridgeClient(TCP) + FileBridgeClient(文件 IPC)
│       ├── server.py                # FastAPI + WS VLA 接口
│       ├── client.py                # 模型 SDK
│       ├── renderer/
│       │   ├── base.py              # Renderer ABC + Frame
│       │   ├── engine_fork.py       # /tmp/mcl2_frames 环形缓冲读取
│       │   ├── voxel.py             # DDA 射线投射合成（fallback）
│       │   └── composite.py         # engine_fork 主 + voxel 回退
│       ├── dataset/
│       │   ├── episode_writer.py    # canonical JSONL+PNG 写入（images_only 模式）
│       │   └── export.py            # WebDataset / HF / RLDS 导出
│       └── scripts/
│           ├── _common.py           # 服务器/渲染器/begin_episode/帧号/对齐断言共用
│           ├── random_agent.py      # 单 episode 随机探索 + 对齐断言
│           ├── collect_dataset.py   # 多 episode 采集 + 导出一站式
│           └── m0_demo.py           # M0 craft_planks 演示
└── datasets/m2_c_verify/            # 已验证数据集（canonical + webdataset shard）
```

> 实际实现与设计差异：桥接用**文件 IPC**（非 TCP）；bot 为**真实玩家驱动**（非逻辑 bot）；采样为**请求式**（非全局 step 定时）。

---

## 11. 关键工程决策与风险

| 决策 | 理由 | 风险与缓解 |
|---|---|---|
| 引擎 fork（客户端抓帧 + 服务器按键注入） | 服务器权威、速度快、画面真实（Craftium 验证） | 需维护 fork；许可证 GPL-3.0；把改动收拢到少量 C++ 文件便于 rebase |
| 游戏选 Mineclonia | 活跃维护、内容全 | 物品命名与 Minecraft 不同 → `item_alias.lua` 映射 |
| 语义+原始双标签记录 | 同时支持 VLA 与 VPT 范式 | 存储翻倍 → 可选仅记录 semantic |
| 种子全量保存 | 世界/任务可还原（用户重点） | 世界存档大 → tar.zst 按需快照 |
| 软重置不重启引擎 | 吞吐量（Craftium 的 reset 设计） | 需要 `remove_player` + 重连/teleport + 清空区域，`core.delete_area` 谨慎使用 |
| 20Hz tick / 2-5Hz 记录 | 状态精度与存储/训练成本平衡 | 可配置 |
| 无 `set_player_control` 原生 API | 必须 fork；不 fork 时 fallback：`set_pos` 瞬移 + 直接 `dig_node/place_node`（非物理，仅语义任务可用） | 文档标注能力差异 |

### 里程碑进度

1. **M0 桥接通** ✅（2026-08-04）：Lua bridge + Python env + `set_pos`/`dig_node`/`place_node` 语义动作 → 首个 `craft_planks` 任务全链路（无渲染）。产物：文件 IPC + 逻辑 bot + 模拟合成 + 记录落盘。
2. **M1 引擎 fork** ✅（2026-08-04）：`set_player_control`（含客户端权威移动注入包 0x65）+ 客户端抓帧 → 视觉观测上线，`random_agent.py` 跑通。产物：`/tmp/mcl2_frames` 真实第一人称帧 + 玩家驱动 bot + engine_fork/voxel/composite 渲染器。
3. **M2 数据管道** ✅（2026-08-04）：请求式采样帧/状态严格对齐 + meta.json 种子/env 字段 + WebDataset/HF/RLDS 导出 → 产出第一份已验证数据集 `datasets/m2_c_verify/`（3 episode 42 帧，含成功轨迹）。
4. **M3 任务与课程** ⏳：语义动作落盘优化（actions.jsonl 的 semantic 在瞬时动作时缺失）、脚本化成功 agent（goto→dig→craft 确定性策略产出成功轨迹，模仿学习正样本）、procedural/curriculum 生成器 + LLM 生成、任务套件规模化。
5. **M4 模型对接** ⏳：VLA Server + SDK，接入 OpenVLA / GROOT / STEVE-1 验证闭环；GPU 环境验证帧随相机变化。

### M0-M2 集成中发现并已修复的问题（勿回退）

- **协议错误传播缺陷**（M1-E）：Lua handler 返回 `{error=...}` 时 pcall 成功 → `ok=true`，Python 静默吞错误。修复：`ipc.process_request` 对含 `error` 字段的 result 置 `ok=false`。约定：**Lua handler 失败必须返回 `{error=...}`**。
- **begin_episode 时序竞态**（M1-E）：需等 bot1 会话建立后再 begin_episode（random_agent 加玩家会话等待）。
- **帧/状态数量不一致**（M1-E→M2）：Lua 全局 step 采样（302 行）与 Python 帧（60）错位 → M2 改为请求式采样根治。
- **符号链接二进制启动失败**（M0）：`build/bin/luantiserver` 符号链接导致 RUN_IN_PLACE 找不到 games/，需用真实 `bin/luantiserver`。
- **采集帧全为死亡界面**（M2 数据质量，2026-08-04）：reset 硬编码 `pos={0,40,0}` 但 m0world 地表约 y=3~9，传送进高空 → 单次摔落伤害 23hp 致命；且无人点击 Respawn → 死亡界面持续录制。修复：
  1. `mcl2agent.reset.find_ground(pos)` 自高处向下扫实心（跳过空气/液体）→ 出生点取地表+1（`cfg.pos.surface ~= false` 时默认生效）；
  2. `register_on_dieplayer` 自动重生（2s 后 `set_hp(20)` + 传回任务安全点）；
  3. 采集期 `pin_timeofday` 每 5 tick 钉住 timeofday（无 `set_time_speed` API），`clear_area` 同时移除 `mcl_mobs:*`；
  4. **死状态守卫**：`on_joinplayer` 检测 hp<=0 时回血传送（玩家文件残留死状态会让客户端首帧显示死屏），采集前删除残留 `<world>/players/bot1`。

---

## 12. 附录：与 Mineclonia 内容注册的对接

Mineclonia 物品/方块/配方均走 `minetest.register_*`，`mcl2_agent` 可在运行时枚举：

- 方块列表：遍历 `core.registered_nodes`（排除 `air/ignore`）。
- 物品列表：`core.registered_items`。
- 配方：Mineclonia 用自定义 `mcl_crafting` 注册表，需按其 API 读取（`crafting.register_recipe`）→ 供 `craft` 任务生成器使用。
- 生物列表：`core.registered_entities` 中 `mcl_mobs:*` 前缀。

生成任务时按这些注册表参数化，保证"所有可合成物品都能成为任务目标"。
