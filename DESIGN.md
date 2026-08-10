# MCPurpur-VLA：Purpur 服务端 + Fabric 受控客户端 + Python 控制中枢

> **Minecraft Java Edition 的 VLA（Vision-Language-Action）研究与训练环境设计**
>
> 版本：0.3（设计 + 实施计划 + 里程碑骨架）
> 日期：2026-08-06
> 更新：§13 细化为任务级实施计划；§13.10 新增里程碑骨架（M0-M12：依赖 DAG、Exit 标准、状态跟踪约定）
>
> 本文档取代此前基于 Luanti/Mineclonia 的 `MCL2-Env` 设计（`mcl2_agent/`、`mcl2_env/`、引擎 fork 方案整体退役）。
> 新方案采用工业界与学术界 Minecraft 具身智能研究的标准三段式架构：**Purpur 世界引擎（Server）＋ 受控 Fabric 客户端（Client）＋ VLA 控制中枢（Python）**。
> 参考实现：MineRL（CMU）、MineDojo（NVIDIA）、MineStudio（CraftJarvis）、OpenAI VPT、Voyager（Mineflayer）。
>
> 仓库内 `mineflayer/` 保留为语义动作命名参考库（`bot.goTo/dig/craft` 风格），本框架的语义动作层与其对齐。

---

## 1. 目标与定位

构建一套面向 **VLA 模型训练与具身智能研究** 的 Minecraft 沙盒环境，满足：

1. **受控渲染**：第一人称 RGB 帧流（224×224 / 256×256，可配置），无 HUD、无物理输入干扰，帧率与动作/状态严格对齐。
2. **世界绝对控制（God Mode）**：服务端可一键重置区域、回滚方块、清空实体、冻结时间/天气/生物生成，保证 episode 可复现。
3. **任务与奖励**：程序化/课程化/LLM 生成的语言任务指令 + 自动成功判定 + 稀疏/稠密奖励。
4. **双动作空间**：VPT/RL 风格按键级原始动作 + 可解释语义动作（goto/dig/craft…），双标签记录。
5. **双模式切换**：`API_MODE`（物理键鼠隔离，供训练）与 `HUMAN_MODE`（真人操作，供人类演示数据采集）。
6. **标准接口**：`gymnasium.Env` + gRPC/WebSocket 远程接口，可接入 OpenVLA / Pi0 / GROOT / STEVE-1 / 自定义 Transformer。
7. **数据保真**：episode 级轨迹数据（帧/状态/动作/奖励严格对齐）+ 世界种子/任务种子完整保存，导出 WebDataset / HF / RLDS。

### 1.1 为什么是"Purpur + Fabric + Python"三段式

| 维度 | MineRL/MineDojo（业界参考） | 本方案 |
|---|---|---|
| 客户端 | Forge 1.16.5 定制 mod，停更于 1.16.5 | **Fabric**（现代版本、Mixin 注入、构建简单、生态活跃） |
| 服务端 | 原生 MC Server + 位置重置 | **Purpur**（Paper 高可配分支，插件 API 提供 gRPC/事件/世界控制） |
| 帧捕获 | 渲染管线内 glReadPixels（640×360） | **FBO/PBO 异步读取**，读主帧缓冲 + `glBlitFramebuffer` 下采样，抓帧开销 <1ms |
| 通信 | 私有 WS 协议 | **gRPC（Python↔服务端）＋ WebSocket（Python↔客户端）**，协议公开可控 |
| 动作空间 | 近人类按键 + camera 121 bin | 对齐 MineRL/VPT：按键 + camera 121 bin + 语义宏动作 |

> 说明：OpenAI VPT 使用 MineRL 界面（Forge 客户端 + WebSocket 桥），Voyager 使用 Mineflayer（协议 bot，无渲染）。
> "Purpur + Fabric" 不是现成框架的复刻，而是基于同样架构模式、面向现代版本的自建实现，可控性与可扩展性更好。

---

## 2. 参考工作与选型依据

| 项目 | 借鉴点 |
|---|---|
| **MineRL**（CMU，arXiv 1907.13440） | client-mod 管线：Python↔客户端 WS 桥、像素观测（640×360）、近人类动作空间、Obtain-Diamond 任务套件 |
| **MineDojo**（NVIDIA，arXiv 2206.08853） | 多模态观测（RGB + compass + voxels）、复合动作空间、程序化任务套件、`minecraft:log_success` 成功判定 |
| **MineStudio**（CraftJarvis） | 轨迹数据组织（episode + RecordCallback）、`observation.pov / action.camera/buttons` 字段约定、VPT/GROOT/STEVE-1/ROCKET 兼容 |
| **VPT**（OpenAI，NeurIPS 2022） | 分层动作映射（buttons 组合空间 + camera 121 bin 量化）、IDM 反演、动作头设计 |
| **STEVE-1** | VPT 先验 + 指令条件化 → 语言→行为闭环 |
| **Voyager**（NVIDIA） | LLM 技能库 + 课程学习；Mineflayer 底层 → 语义动作层命名对齐 |
| **Mineflayer**（仓库内参考） | `bot.goTo / dig / craft / place` 高层 API 风格 → 本框架语义动作参数语义对齐 |
| **Malmo**（微软） | 最早 client-mod 方案，XML 任务 + TCP 桥（思路源头） |
| **Baritone** | 客户端侧高性能寻路（Fabric 可用），可作客户端路径参考实现 |
| **Craftium**（ICML 2025） | Minetest fork 接 Gymnasium（本方案已弃用此路线，但其"软重置不重启引擎""慢 agent 同步"理念保留） |
| **Open X-Embodiment / RLDS** | episode 数据字段规范（observation/action），导出对齐 |

### 2.1 版本选型（决策）

| 项 | 推荐 | 理由 |
|---|---|---|
| Minecraft | **1.20.1**（可升级 1.21.x） | mod/插件生态最成熟、Mixin 资料多、Fabric API 稳定；Purpur 1.20.1 为长期维护版 |
| 服务端 | **Purpur 1.20.1** | Paper 的 drop-in 替代，`purpur.yml` 高可配；含 Paper API（`Bukkit.getCurrentTick()` 等） |
| 客户端 | **Fabric Loader + Fabric API 1.20.1** | Mixin 标准、构建轻、与 Sodium 等渲染 mod 可共存 |
| 映射 | 开发用 **Yarn**（Fabric 生态默认），文档术语与官方混淆名对照见 §5.2 | 便于 Fabric 社区资料复用 |
| Java | JDK 17（1.20.1） | 服务端与客户端统一 |
| 协议 | Python↔服务端 **gRPC**（Protobuf）；Python↔客户端 **WebSocket** | gRPC 适合结构化 RPC；WS 适合高频动作/帧流 |

> 版本锁定为工程基线；升级 1.21.x 时改动集中在 mixin 目标类与 Fabric API 调用点（见 §14.7）。

---

## 3. 总体架构

```
┌────────────────────────────────────────────────────────────────────────────┐
│ VLA 控制中枢 (Python / PyTorch)                                              │
│  ┌──────────────┐  ┌───────────────┐  ┌─────────────────┐  ┌─────────────┐  │
│  │ gymnasium.Env│  │ gRPC Client   │  │ WS Client       │  │ Dataset/    │  │
│  │ MinecraftEnv │  │ (任务/奖励/    │  │ (动作/帧流)      │  │ Export      │  │
│  │ (Lockstep)   │  │  重置/路径/体素)│  │                 │  │ (WDS/HF/RLDS)│  │
│  └──────┬───────┘  └───────┬───────┘  └────────┬────────┘  └──────┬──────┘  │
└─────────┼──────────────────┼───────────────────┼──────────────────┼─────────┘
          │  gRPC (本地回环)  │  WebSocket (本地回环) │                │
┌─────────▼──────────────────▼───────────────────▼──────────────────▼─────────┐
│ Purpur 服务端 (purpur-vla-plugin.jar)          Fabric 客户端 (fabric-vla-client.jar) │
│  ┌──────────────────────┐                     ┌───────────────────────────┐  │
│  │ gRPC Server (独立线程) │                     │ 嵌入式 WS Server (本地端口) │  │
│  │  ↳ 主线程调度器         │                     │  ↳ ConcurrentLinkedQueue  │  │
│  ├──────────────────────┤                     ├───────────────────────────┤  │
│  │ ResetEngine(区域回滚)  │◄── Minecraft 协议 ──►│ Mixin 输入隔离(API/HUMAN)   │  │
│  │ TaskManager/奖励判定    │                     │ Mixin 抓帧(FBO+PBO)        │  │
│  │ Pathfinding(A*)        │                     │ 插件消息: vla:tick 对齐     │  │
│  │ Voxel 矩阵快照          │                     │ 动作注入(键位/视角/攻击/使用)│  │
│  │ 事件监听(挖/放/击杀)     │                     └───────────────────────────┘  │
│  └──────────────────────┘                                                     │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 职责划分

| 层 | 组件 | 职责 | 关键技术 |
|---|---|---|---|
| 世界引擎 | Purpur 插件 | 世界状态权威、重置、任务、奖励、路径、全局体素 | Bukkit/Paper API + gRPC + A* |
| 受控渲染 | Fabric mod | 输入隔离、动作注入、抓帧、帧/状态上行 | Mixin + FBO/PBO + WebSocket |
| 控制中枢 | Python 包 | Gymnasium Env、锁步调度、数据采集/导出、模型对接 | grpcio + websockets + numpy + gymnasium |

### 3.2 核心数据流（一个 step）

```
Python  ──gRPC──► Server: ResetWorld(task, seed, region)      // 重置世界与任务
Python  ──WS────► Client: action {buttons, camera, hotbar}    // 发送动作
Client  ──render──► 主线程渲染后 PBO 异步取帧 ──queue──► WS 线程 ──► Python: frame + frame_id + last_server_tick
Server  ──主线程──► 执行动作引发的世界变更 → 事件 → TaskManager 判定
Python  ──gRPC──► Server: GetStepResult()                     // reward / done / state / server_tick
Python  对齐:  (frame_i, action_i, reward_i, tick_i) 一一对应
```

---

## 4. 组件一：Purpur 服务端插件（`purpur-vla-plugin`）

### 4.1 工程骨架

- Gradle + `io.papermc.paper:paper-api:1.20.1-R0.1-SNAPSHOT`，Java 17。
- 依赖：`io.grpc:grpc-netty-shaded` + `grpc-protobuf` + `grpc-stub`，经 **shadowJar** 打入插件 jar（gRPC 体积约 5-10MB，可接受）。
- `plugin.yml`：`main: dev.vla.purpur.VlaPlugin`，`api-version: 1.20`，注册频道 `vla:tick`（出站）。

### 4.2 gRPC 服务与主线程调度（关键约束）

**Minecraft 服务端是单主线程模型**：除渲染外，所有世界读写必须在主线程执行，否则导致区块竞态、TPS 抖动甚至崩溃。

```java
// gRPC 工作线程 ──调度──► 主线程
public void resetWorld(ResetRequest req, StreamObserver<ResetReply> resp) {
    Bukkit.getScheduler().runTask(plugin, () -> {          // 主线程执行
        ResetResult r = resetEngine.apply(req);
        resp.onNext(ResetReply.newBuilder()                 // gRPC 线程可安全返回
            .setServerTick(Bukkit.getCurrentTick()).build());
        resp.onCompleted();
    });
}
```

- gRPC Server 用 `NettyServerBuilder.forAddress(new InetSocketAddress("127.0.0.1", 50051))`，仅绑定回环。
- **只读**查询（体素矩阵、路径规划）可放 gRPC 线程用 NMS 直接读区块（无写则安全），写操作一律主线程。
- 后续若要 Folia（区域并行线程）支持：`Bukkit.getScheduler().runTask` 需替换为区域调度，Phase 4 再考虑，默认**不用 Folia**。

### 4.3 世界控制与重置（God Mode）

`ResetEngine` 提供（全部主线程）：

| 能力 | Paper/Bukkit API |
|---|---|
| 传送玩家 | `player.teleportAsync(loc)` |
| 清背包/装备 | `player.getInventory().clear()` / `getEquipment().clear()` |
| 生命/饥饿 | `player.setHealth(20)` / `setFoodLevel(20)` / `setSaturation(20)` |
| 游戏模式 | `player.setGameMode(GameMode.SURVIVAL)` |
| 冻结时间 | `world.setTime(6000)` + gamerule `doDaylightCycle=false` |
| 冻结天气 | `world.setStorm(false)` + gamerule `doWeatherCycle=false` |
| 关闭刷怪 | gamerule `doMobSpawning=false`（或区域限定刷怪） |
| 防饥饿/死亡循环 | gamerule `keepInventory=true`、`naturalRegeneration=true`、死亡自动重生监听 |
| 清实体 | `world.getNearbyEntities(loc, r, r, r)` → `e.remove()`（保留玩家） |
| 固定 seed | `server.properties: level-seed=`，episode 间不重生世界 |

**确定性配置**（数据采集期强制）：`doDaylightCycle=false`、`doWeatherCycle=false`、`doMobSpawning=false`、`mobGriefing=false`、`randomTickSpeed=0`、`playersSleepingPercentage` 无关、视野内方块更新恒定。

### 4.4 区域回滚（三档策略）

需求：重置 32×32×32（或更大）区域，恢复到任务初始状态。**不要卸载区块、不要重生成地图**。

| 策略 | 实现 | 适用 |
|---|---|---|
| **L1 内存快照（默认）** | 首次 `ResetWorld` 时记录区域内每格 `BlockData`（或 `ChunkSnapshot`），存 `Map<ChunkPos, ChunkSnapshot>`；回滚时 `world.getBlockAt(x,y,z).setBlockData(data, false)`（`applyPhysics=false`），32768 格 <1s | episode 内/间快速重置 |
| **L2 异步批量（FAWE）** | 引入 FastAsyncWorldEdit API，异步区域 set 与 schematic 保存/加载，不卡主线程 | 大区域 / 复杂地形 / 逐 bit 还原 |
| **L3 Structure NBT** | 用原版 Structure（`StructureTemplate`）保存任意形状结构，NMS 快速粘贴 | 任务要求特定建筑初始态 |

> 方块回滚必须**关闭物理更新**（`setBlockData(data,false)`），否则 TNT/火把/红石 连锁更新导致 TPS 崩。

### 4.5 路径规划（Pathfinding）

服务端输出 **Waypoints**（航点列表）作为观测/指令，供 VLA 参考与 reward 判定：
**拓扑 A\* 定路 + 底层控制器执行** 分工：A* 只回答全局连通性（绕哪堵墙、避开岩浆、怎么上台阶），yaw 由航点方向由客户端 `look_at` 平滑转（不关心 360°）。

- **动作级 3D A\***（NavV2，2026-08-07 升级，`path/AStar.java`）：XZ 8 方向 × 垂直多档，
  输出**动作注解航点** `{pos, action, target}`，action ∈ walk|jump|fall|dig|dig_down|place。
  确定性：固定邻域顺序 + open set TreeSet（f 升序/g 降序/坐标字典序）；迭代上限 200k。
- **方块分类**（替代 M6 二元表，`getHardness()` 分级）：
  - `PASSABLE`：空气/草丛/花/水(water 模式) —— 可站，行走 1.0（斜走 1.414）
  - `BREAKABLE`：实心且硬度有限（树叶/泥土/石头/原木）—— 可挖穿，成本 `max(1, hardness*3)+1`
  - `UNBREAKABLE`：岩浆/火/基岩/水(默认) —— 不可通行
- **动作集**（cost_mode 门控）：
  - `WALK`（含头格挖穿合成 DIG_HEAD：目标头格 BREAKABLE 时边代价 +breakCost 并前置 dig 动作 —— 治悬浮树叶/树冠下檐挡头）
  - `STEP_UP`（跳上 1 格，2.0）/ `FALL 1..3`（`1.0+0.25*(h-1)`，>3 摔伤拒绝）
  - `DIG_FORWARD`（dig/place 模式，挖穿前方脚格再走）
  - `DIG_DOWN`（dig/place 模式，挖穿脚下后按自由落体下落至首个脚下有地面的格，总落差 ≤3）
  - `PLACE_STEP`（place 模式，垫方块爬高，成本 4.0）
- **破块距离惩罚**（考量：远离目标少破块）：含 dig 的边额外
  `+FAR_PENALTY(0.5)×max(0, distToGoal(node)−COLLECT_RADIUS(4))`（distToGoal=XZ 欧氏）——
  远处挖穿贵→倾向绕路；目标附近无惩罚→放心挖穿接近；**地下目标 XZ≈0 → dig_down 无惩罚 → 天然下挖倾向**。
- **移动模型必须严格匹配真实玩家**：
  - **头部空间**：脚格+头格双格校验；dig 模式头格为 BREAKABLE 时可挖穿（M6 视为绝对墙）
  - **斜向切角（`diagonalClear`）**：斜走时两个正交邻格必须可通行（0.6 格宽玩家无法从两实心方块间斜穿）
  - **目标可达（`adjustGoal`，考量：采集距离 ~2 格）**：目标格实心时在 3D 邻域（水平±3、垂直-8..+8）
    找"可站且脚下有地面且目标中心在 reach(4.5) 内"的格，评分偏水平 1.5~2.5 格采集距离；
    目标格本身可站（洞穴/平台）→ 直接以目标为终点；找不到退回最近可站格
- 外部接入：`ComputePath(from, to, cost_mode) → PathReply{waypoints[], found, details[]}`，
  `details` 为动作级航点（Waypoint{pos, action, target}），供执行器按动作执行；
  cost_mode = `default`（不挖穿，M6 兼容）| `dig`（+挖穿/下挖）| `place`（+垫方块爬高）；
  **A\* 必须能优雅失败**（found=false），调用方 fallback = 目标黑名单 + 游走换树。
- 执行器（`collect_wood_agent.py` approach 子状态机）：按 details 的 action 分派 ——
  walk/jump/fall 移动（look_at+forward+jump）、dig/dig_down 站原地挖穿目标块（voxel 校验完成）、
  place 选 dirt 槽+look_at 放置点+use；移动卡死沿用 wp_stuck 后退/黑名单，dig 卡死重算。
- 备选：复用原版寻路（NMS `PathFinder` + `WalkNodeEvaluator`），需 NMS 访问；或引入第三方 Bukkit 寻路库。
- 已确认实现（NavV2 验收，2026-08-07）：悬浮树叶屋顶 default 无路/dig 出 DIG 头格动作；
  2 格树叶墙 dig 出 DIG 脚+头动作；地下目标 dig 出 DIG_DOWN 下挖；5 格高墙 place 出 PLACE 垫方块。

### 4.6 全局状态（Voxel 矩阵）

VLA 辅助 state / reward 计算用，32×32×32（可配置半径）：

- 读取走 **NMS**（远快于 Bukkit Block 对象）：`((CraftWorld) world).getHandle().getBlockState(new BlockPos(x,y,z))`。
- 返回格式：`{palette: [block_id...], data: int[]}`（局部块 id 索引）或直接 `BlockData` 名称数组，Python 侧重建为 numpy 3D 矩阵。
- 附带语义信息：`aimed_block`、`nearby_entities`、`items_on_ground`（与旧版状态接口对齐）。

### 4.7 任务系统与奖励

`TaskManager`（复用旧版 DESIGN 的任务 Schema，迁移到 Java）：

```java
TaskSpec {
  String id; String instruction; String instructionZh;
  TaskType type;                  // COLLECT / CRAFT / BUILD / COMBAT / NAV / INTERACT
  int difficulty; List<String> prerequisites;
  ResetSpec reset;                // spawn, inventory give/clear, region, time...
  String successPredicate;        // 判定器名
  Map<String,Object> successArgs;
  RewardSpec reward;              // per_step, on_progress, success_bonus, timeout_penalty
  int timeoutTicks;               // 默认 6000 (5min @20tps)
}
```

**内置判定器**（监听事件驱动 + 周期校验）：

| 判定器 | 触发事件 | 说明 |
|---|---|---|
| `inventory_contains` | 物品变更 | 背包内物品 ≥ N |
| `block_placed` / `block_mined` | `BlockPlaceEvent` / `BlockBreakEvent` | 区域内方块体积/数量统计 |
| `entity_killed` | `EntityDeathEvent` | 指定实体击杀 ≥ N |
| `player_at` | `PlayerMoveEvent` | 距目标 < tol |
| `stat_reached` | `PlayerStatisticIncrementEvent` | 统计值 ≥ 阈值 |
| `custom` | — | 插件注册回调 |

奖励设计：`reward = per_step*0(默认0) + on_progress*Δprogress + success_bonus + timeout_penalty`。**奖励以服务端事件为准**，绝不用客户端观测（见 §14.2 幽灵方块）。

### 4.8 多智能体（并行采样）

- **单 Purpur 服务器承载 N 个玩家**：每个 env = 一个玩家（gRPC 按 `player_name` 寻址）+ 一个 Fabric 客户端进程（负责该玩家的渲染）。
- 任务状态按玩家隔离：`Map<UUID, EpisodeState>`；不同玩家可处于不同任务/不同难度。
- 共享服务器资源开销小（客户端才是 GPU 大头）；若需彻底隔离再退化为"每 env 一个服务器实例"（端口动态分配，见 §12）。

---

## 5. 组件二：Fabric 受控客户端（`fabric-vla-client`）

### 5.1 工程骨架

- Fabric Loom 模板，MC 1.20.1 + Yarn 映射，Java 17。
- 依赖：`fabric-api`（rendering-v1、networking-v1、lifecycle-v1）+ **Java-WebSocket**（shadow 进 mod jar）。
- `mixins.json` 注册：`KeyboardInputMixin`、`MouseMixin`、`GameRendererMixin`、`ClientPlayNetworkHandlerMixin`。

### 5.2 映射对照（Mojang 官方名 ↔ Yarn）

| 功能 | Mojang（官方映射） | Yarn（Fabric 开发常用） |
|---|---|---|
| 移动输入 | `net.minecraft.client.player.KeyboardInput#tick(boolean,float)` | `net.minecraft.client.input.KeyboardInput#tick(boolean,float)` |
| 鼠标视角 | `net.minecraft.client.MouseHandler#turnPlayer()` | `net.minecraft.client.Mouse`（相机更新方法） |
| 设置视角 | `player.setYRot(float)` / `setXRot(float)` | `player.setYaw(float)` / `setPitch(float)` |
| 主手攻击 | `Minecraft#startAttack()` | `client.interactionManager.attackBlock` / `player.swingHand` |
| 使用物品 | `Minecraft#startUseItem()` | `client.interactionManager.interactItem` |

### 5.3 输入隔离与动作注入（Mixin，核心）

设计目标：`API_MODE` 下**物理键鼠完全失效**，一切输入来自 WS 指令；`HUMAN_MODE` 下完全透明。

| 控制项 | 注入方式 |
|---|---|
| 移动（前后左右/跳/潜行/疾跑） | Mixin `KeyboardInput#tick`：`@Inject(at=@At("HEAD"), cancellable=true)` 后，若 `API_MODE` 则 **cancel** 原逻辑，把 API 的浮点/布尔写入 `input` 字段（`pressingForward`、`movementForward/movementSideways`，1.20.5+ 为 `forwardImpulse/leftImpulse`）|
| 视角（pitch/yaw） | Mixin `MouseHandler#turnPlayer`（Yarn: `Mouse`）HEAD cancel（物理鼠标失效）；WS `look_at(x,y,z)`/`reset_camera(yaw,pitch)` 只设**视角目标**，`END_CLIENT_TICK` 里 `interpolateCamera` 按 `maxTurnDeg`（默认 40°/tick，`set_turn_speed` 可调）沿最短角差平滑转向（误差 <0.05° 收敛）——**消除瞬移"闪现"，天然规避万向节死锁**；`look_at` 用客户端自身眼位算精确朝向，消除服务端 pos 滞后的瞄准偏差 |
| 攻击/使用/丢弃 | Mixin `MouseHandler#onMouseButton` 屏蔽物理按键；API 动作通过 `Minecraft#startAttack()/startUseItem()` 或直接设置对应 `KeyBinding.setPressed()` 驱动原版逻辑（挖掘进度、攻击冷却自动处理） |
| 快捷栏 0-8 | `options.hotbarKeys[i].setPressed(bool)`（选中后 release，模拟按数字键） |
| 物品栏开关 | `options.keyInventory.setPressed(bool)` |

> 推荐：**移动/视角**用 Mixin 覆盖（更精细、可给模拟量）；**攻击/使用/热键**用 `KeyBinding.setPressed()`（复用原版全部时序逻辑，改动最小）。

伪代码：

```java
@Mixin(KeyboardInput.class)
public abstract class KeyboardInputMixin {
    @Inject(method = "tick", at = @At("HEAD"), cancellable = true)
    private void onTick(boolean slowDown, float sneakSpeed, CallbackInfo ci) {
        if (!VlaClient.isApiMode()) return;              // HUMAN_MODE 透明放行
        KeyboardInput self = (KeyboardInput)(Object)this;
        Input i = (Input)(Object)self;
        ActionCmd a = VlaClient.currentAction.get();     // WS 线程写入的原子引用
        // 清零 + 注入
        self.pressingForward = false; self.pressingBack = false; /* ...全部键位清零 */
        i.movementForward  = a.forward - a.back;
        i.movementSideways = a.left - a.right;
        i.jumping = a.jump; i.sneaking = a.sneak; i.sprinting = a.sprint;
        ci.cancel();                                     // 丢弃物理键鼠
    }
}
```

### 5.4 双模式切换

- `HUMAN_MODE`：Mixin 全部放行；客户端开启**演示录制**（帧 + 键位 + 视角原始数据流上行给 Python，见 §11.3）。
- `API_MODE`：输入隔离生效；WS 指令驱动。
- 切换：WS 指令 `{cmd:"mode", mode:"api"|"human"}`，客户端同时清空按键状态（防"粘键"：`KeyBinding.unpressAll()`）。

### 5.5 高效抓帧（FBO + PBO，性能核心）

**关键认知：不需要额外渲染一个低分辨率 FBO。** Minecraft 每一帧已经把场景渲染进主帧缓冲（`Minecraft#getFramebuffer()`）。正确做法：

```
渲染线程 (GameRenderer#render 完成后、HUD 绘制前)
   │  mixin 在 world 渲染结束处
   ▼
Framebuffer 绑定为 GL_READ_FRAMEBUFFER
   │  glBlitFramebuffer(主缓冲 → 224×224 小 FBO)     // GPU 下采样，几乎零 CPU
   ▼
GL_PIXEL_PACK_BUFFER (PBO 三缓冲，环形)
   │  glReadPixels(小 FBO → PBO)                    // 异步 DMA，不阻塞
   ▼
下一次渲染时 glMapBufferRange 读取上一帧数据        // <1ms CPU
   ▼
byte[] RGBA → ConcurrentLinkedQueue<FrameData>      // 仅传引用，渲染线程零编码
   │
WS 网络线程                                         // 编码 JPEG + 发送
   ▼
Python
```

- **抓帧挂点（可切换，2026-08-06 修复）**：
  - **默认（VLA 观测）**：`WorldRenderEvents.LAST`（世界+实体渲染后、HUD 前）→ 画面纯净（无准星/血条/手），避免污染像素观测。
  - **Demo/可视化（`set_capture_ui hud=true`）**：`GameRendererMixin` 在 `GameRenderer.render` **TAIL** 抓帧 → 主 framebuffer 已含世界+手+HUD+准星（完整游戏画面）。两者互斥（LAST 加 `!captureUi` 守卫），WS `set_capture_ui` 切换。
- **目标分辨率（可运行时切换，2026-08-06 新增）**：默认 224×224（VLA 常用）；WS `set_capture` 可切到显式 WxH 或 **0=原生 framebuffer 分辨率**（demo 视频用，保留游戏原始比例、不升采样）。**显式 WxH 与游戏比例不一致时按源比例居中适配（letterbox 黑边），不拉伸**；FBO 尺寸变化自动重建；`FrameData` 携带 width/height，FrameSender 按每帧尺寸编码。
- 帧率：客户端 60/120 FPS 渲染，采集端按 `record.fps`（默认 20，与 tick 对齐）降采样；FrameSender 上限 30fps 流控。
- **线程铁律**：所有 GL 调用只在渲染线程；数据交递用无锁队列（`ConcurrentLinkedQueue` / MPSC ring buffer），编码与网络传输放后台线程。

### 5.6 帧与 tick 对齐（插件消息通道）

客户端无法直接读取服务端 tick，通过 **Plugin Messaging** 下行广播：

- 服务端每 tick（或每 N tick）向玩家发送 `vla:tick` 频道 payload（`int serverTick` + `long wallNanos`）。
- 客户端 `ClientPlayNetworking.registerGlobalReceiver` 接收，写入 `volatile long lastServerTick`。
- 每帧上行消息携带：`{frame_id, last_server_tick, render_wall_time}`，Python 据此做帧↔tick 对齐（§9.3）。

### 5.7 客户端技能：垫方块爬高（pillar-up，M11，2026-08-09）

**为什么是客户端技能而不是 Python 状态机**：一个 Python step = `ticks` 个服务端 tick + gRPC/WS
往返 ≈ 5-10 tick，而放置窗口只有跳跃的第 3~8 tick；`use` 又是电平保持字段。Python 侧对不齐这个
窗口。所以整套循环下沉到 `nav/PillarExecutor.java` 逐 tick 执行，Python 只下发 `pillar_up` 并消费
`pillar_status`。

**动作序列**（人类垫楼方式）：挖头顶 `fy+2` → 视角朝正下 → 原地跳 → 顶点放一块 → 落到块上 → 重复，
每轮净升 1 格。

**三条硬约束**（1.20.1 字节码 + 跳跃积分核实）：

1. **只需挖 `fy+2`，不必挖 `fy+3`**。玩家高 1.8，脚格 `fy` / 头格 `fy+1` 是自身碰撞箱
   （`fy+1` 恒为空气，检查它没有意义）。`fy+2` 有方块 → 天花板 `fy+2.0` → 最大跳高
   `2.0-1.8 = 0.2` 格，永远垫不上去；`fy+3` 有方块 → 最大跳高 1.2 > 1.0，仍够放置
   （撞头时 vy 被碰撞清零，正好落进放置判据）。
2. **放置窗口 = 跳跃第 3~8 tick**。`v₀=0.42`、`v ← (v−0.08)×0.98` 积分：
   Δy = .420 / .753 / **1.001** / 1.166 / 1.249 / **1.252（顶点）** / 1.177 / 1.024 / .797。
   Δy > 1.0 时脚格上移一格，目标格 `(bx,fy,bz)` 空出且碰撞箱不与之相交 ——
   `World#canPlace → isSpaceEmpty` 是严格判交，**服务端也要过这一关**。
3. **不数 tick，测位移**：判据 `Δy ≥ 1.05 && vy ≤ 0.02`，天然抗延迟/抗撞头/抗跳跃增益。

**时序依据**（`MinecraftClient.tick()` 调用顺序）：`itemUseCooldown--` → `handleInputEvents()`
（消费 use/attack）→ `GameRenderer.tick()` → `ClientWorld.tickEntities()`（玩家跳跃/移动）→
`END_CLIENT_TICK`（执行器运行）。故本 tick 置的 `use` 在下一 tick 被消费时玩家仍在观测到的位置。
`handleInputEvents` 中 use 有 `wasPressed()` 与 `isPressed() && itemUseCooldown==0` 两条路径，
`ActionApplier.setPressed` 走后者；放置后 `itemUseCooldown=4` 保证一次跳跃只放一块。
`crosshairTarget` 在渲染线程 `GameRenderer.renderWorld` 逐帧更新 → **必须先收敛视角再跳**
（SETTLE 相位等 ≥2 tick），但正下方射线在 1 格宽列内对 Δy∈(0,2) 命中同一格，故不受陈旧性影响。

**相位机**：`CLEAR_HEAD`（含完成判定）→ `EQUIP` → `SETTLE` → `JUMP` → `AIRBORNE` → `VERIFY` → 循环。
`FAILED` 带细分 reason 供 Python 选兜底：`head_blocked`（基岩）/ `no_block_item` / `out_of_blocks` /
`in_fluid` / `no_settle` / `uneven_ground`（站半砖上，vanilla 也垫不上去）/ `place_failed` /
`dig_timeout` / `timeout`。一块都没垫上 → Python 转挖阶梯（`stair_climb`）。

**按键所有权**：`pillar_up` 先 `navExecutor.cancel()`（垫方块要求水平速度≈0，与导航的持续
forward 互斥），`END_CLIENT_TICK` 里 pillar 优先于 nav；技能活跃期间 Python 只发空动作。

**踩过的坑（老 Python 实现）**：挖 `fy+1`（头格，恒空气）而非 `fy+2`；`pitch=-90`（那是正上方，
MC 正下是 `+90`）；`look_at` 在 `h≈0` 时把 pitch 退化成 0（平视）—— 现统一收敛到
`nav/Aim.java`（客户端）与 `collect_wood_agent.look_at`（Python），同口径。

---

## 6. 组件三：Python VLA 控制中枢（`vla_env`）

### 6.1 包结构

```
vla_env/
├── pyproject.toml            # deps: grpcio, websockets, numpy, gymnasium, pillow
├── vla_env/
│   ├── env.py                # MinecraftEnv(gymnasium.Env) + wrappers
│   ├── server_grpc.py        # 服务端 gRPC client（Reset/Step/Path/Voxels/Task）
│   ├── client_ws.py          # 客户端 WebSocket client（动作/帧/状态）
│   ├── lockstep.py           # 时序调度器（tick/frame 对齐）
│   ├── action_space.py       # 原始/语义/VPT-token 动作映射
│   ├── obs.py                # 观测拼装（pov + state + voxels + task）
│   ├── dataset/              # episode_writer, export(WDS/HF/RLDS), interactor
│   └── scripts/              # random_agent.py, collect_demo.py, train_loop.py
└── proto/vla.proto           # 与插件共享的 protobuf 定义
```

### 6.2 通信客户端

- **gRPC**（Python↔服务端，`grpcio`）：`ResetWorld / GetStepResult / GetState / ComputePath / GetVoxels / SetTask / GenerateTask / ClearRegion / Teleport / SpawnEntity / SetBlock`。
- **WebSocket**（Python↔客户端，`websockets`）：动作下行 + 帧/状态上行。

### 6.3 Gymnasium Env（Lockstep 同步模式）

采用业界验证的**同步（lockstep）模式**保证严格 MDP 对应：

```python
class MinecraftEnv(gym.Env):
    def __init__(self, player="agent0", task="collect_wood",
                 ticks_per_step=4, res=224, seed=None):
        self.grpc = ServerGrpc("127.0.0.1:50051", player)
        self.ws = ClientWs("127.0.0.1:30001")          # 每 env 独立端口
        self.ticks_per_step = ticks_per_step           # 每步推进的游戏刻

    def reset(self, task=None, seed=None):
        self.grpc.reset_world(task=task or self.task, seed=seed)
        self.ws.send({"cmd": "mode", "mode": "api"})
        self.ws.send({"cmd": "reset_camera", "yaw": 0, "pitch": 0})
        return self._observe()

    def step(self, action):
        # 1. 动作 → 客户端（按键+相机）
        self.ws.send(self.action_space.to_ws(action))
        # 2. 等待客户端返回本步帧（含 frame_id / last_server_tick）
        frame = self.ws.recv_frame(timeout=2.0)
        # 3. 等待服务端结算（server-authoritative reward/done）
        step = self.grpc.get_step_result(await_ticks=self.ticks_per_step)
        # 4. 本地状态 + 全局状态（可选 voxels）
        state = self.grpc.get_state()
        obs = self.obs.build(frame, state, task_info=step.info)
        return obs, step.reward, step.terminated, step.truncated, step.info
```

`step` 阻塞等待 gRPC 确认后，才请求下一帧 → **杜绝"动作已执行但 reward 读 0"的网络错位**（§14.2）。

### 6.4 向量化（SubprocVecEnv）

- 每 env 进程 = 1 个客户端 WS 连接 + 1 个玩家身份（共享同一 Purpur 服务器）。
- 端口规划：gRPC 每服务器一个（默认 50051）；客户端 WS 每 env 一个（30001+n）。
- 多服务器模式：动态分配服务端端口（25565+n）与 WS 端口，`SubprocVecEnv` 拉起 N 个 env worker。
- **看门狗**：`step()` 超 10s 未返回 → 判定 Java 进程卡死 → kill + 重启该 env（不 Debug，直接重启）。

---

## 7. 动作空间（Action Space）

### 7.1 原始动作（tick 级，MineRL/VPT 对齐）

| 字段 | 类型 | 说明 |
|---|---|---|
| `forward/back/left/right` | bool | 移动 |
| `jump` / `sneak` / `sprint` | bool | 跳跃/潜行/疾跑 |
| `attack` | bool | 挖掘/攻击（长按驱动原版挖掘进度） |
| `use` | bool | 使用/放置 |
| `drop` | bool | 丢弃 |
| `inventory` | bool | 打开物品栏 |
| `hotbar` | int 0-8 | 快捷栏槽位 |
| `camera` | `[pitch_delta, yaw_delta]` 或离散 bin | 视角增量 |

- **离散模式（默认，对齐 MineRL 1.16.5 / MineDojo / MineStudio）**：`camera` 为 **121 bin（11×11）**，buttons 每个 `Discrete(2)`，`hotbar` 为 10 选 1 → 直接喂分类模型。
- **连续模式**：`camera` 为 Box（rad/s 增量），适合 DDPG/连续 VLA。

### 7.2 VPT 离散 token（可选）

启用 `action_space_mode="vpt_token"`：buttons 按 VPT 分层映射（`fore_back` 三选一、`left_right` 三选一、`sprint_sneak` 三选一、`hotbar` 十选一 + 独立 attack/use/drop/inventory/jump…）组合成单个离散 token，camera 121 bin 独立头 → 可加载 STEVE-1 / VPT 系预训练。

### 7.3 语义动作（宏动作，双标签记录）

服务端/客户端协作分解（命名与 Mineflayer 对齐）：

| 类别 | 动作 | 参数 | 分解 |
|---|---|---|---|
| 导航 | `goto` | `{pos, tolerance}` | `ComputePath` → 逐航点移动+转向 |
| 导航 | `look_at` | `{pos}` | 客户端视角插值 |
| 采集 | `dig` | `{pos}` | 朝向+装备+attack 长按+拾取 |
| 建造 | `place` | `{item, pos}` | 选中物品→朝向→use |
| 背包 | `equip` / `select_slot` | `{item}` | 物品栏逻辑 |
| 合成 | `craft` | `{item, count}` | 打开合成 UI→放料→取回 |
| 战斗 | `attack_entity` | `{target}` | 朝向+间隔 attack |
| 交互 | `use_block` / `eat` | `{pos}` / `{item}` | use 按键 |

语义动作以 `action_id` 异步执行，Python 轮询 `state.task.action_progress` 或 push 完成事件；轨迹中同时记录 semantic + primitive 双标签。

---

## 8. 观测空间（Observation Space）

```jsonc
{
  "pov":  { "shape": [3, 224, 224], "dtype": "uint8" },     // 第一人称帧（无 HUD）
  "compass": { "yaw": 0.0, "pitch": 0.0 },                   // 可选
  "inventory": { "main": [...], "selected_slot": 0, "held_item": "minecraft:oak_log" },
  "player": { "pos": [x,y,z], "hp": 20, "hunger": 20, "effects": [], "dimension": "overworld",
              "velocity": [vx,vy,vz], "on_ground": true, "relative_pos": [0,0,0] },
  "voxels": { "palette": [...], "data": [...] },             // 32×32×32 服务端体素（可选）
  "task": { "id": "collect_wood", "instruction": "Collect 4 wood logs.",
            "difficulty": 1, "progress": 0.5, "success": false, "steps": 120 },
  "stats": { "xp": 12, "kills": 0, "playtime": 42.5 },
  "agent": { "episode_id": "ep-000001", "server_tick": 48213, "wall_time": 1722760000.123,
             "frame_id": 1024 }
}
```

- 关键字段与 MineStudio 对齐（`observation.pov / action.camera/buttons`），方便复用既有数据管线与模型。
- `relative_pos` 以 episode 出生点为原点 → 平移不变性（泛化性关键，见 §14.6）。

---

## 9. 通信协议与数据对齐

### 9.1 gRPC 服务定义（`vla.proto` 节选）

```proto
service VlaServer {
  rpc ResetWorld(ResetRequest) returns (ResetReply);
  rpc GetStepResult(StepRequest) returns (StepReply);      // 阻塞等待 k ticks 后结算
  rpc GetState(StateRequest) returns (StateReply);
  rpc GetVoxels(VoxelRequest) returns (VoxelReply);       // 32³ 体素
  rpc ComputePath(PathRequest) returns (PathReply);       // 航点
  rpc SetTask(TaskRequest) returns (TaskReply);
  rpc GenerateTask(GenerateRequest) returns (TaskReply);  // 课程/LLM 生成
  rpc ClearRegion(ClearRequest) returns (Void);
  rpc Teleport(TeleportRequest) returns (Void);
  rpc SpawnEntity(SpawnRequest) returns (Void);
  rpc SetBlock(SetBlockRequest) returns (Void);            // God Mode 单方块设置
  rpc ShowPath(ShowPathRequest) returns (Void);            // 寻路粒子可视化（demo）
}
message StepReply {
  float reward = 1;
  bool terminated = 2;
  bool truncated = 3;
  int32 server_tick = 4;
  float progress = 5;
  map<string, string> info = 6;
}
```

### 9.2 WS 帧协议（Python ↔ 客户端）

- 下行（P→C）：`{"cmd":"mode|action|reset_camera|disconnect", ...}`；`action` 为 §7.1 的原始动作 dict。
- 上行（C→P）：
  - 帧：二进制消息 `[4B frame_id][4B server_tick][8B wall_nanos][JPEG bytes]`（或 Base64 JSON，调试用）。
  - 状态：`{"frame_id":.., "last_server_tick":.., "aimed_block":.., "held_item":.., "fps":..}`。

### 9.3 Tick-Frame 对齐（Phase 3 核心）

Minecraft 服务端 20 TPS，客户端可 60-120 FPS，二者各自运行。对齐策略：

```
t 对齐方程：
  server_tick(client_frame) ≈ server_tick 权威（GetStepResult 返回）
  要求：|server_tick_client_frame - server_tick_reward| ≤ tolerance(默认 2 tick)

lockstep 保证：
  1. Python 发出 action_i 后，先等客户端 frame_i（含 last_server_tick）
  2. 再等服务端 GetStepResult（阻塞至 action_i 的 k ticks 结算，含权威 server_tick）
  3. 断言两者 tick 差在容差内 → 记录 (frame_i, action_i, reward_i, tick_i)
  4. 任一步超时/错位 → 该 step 标记 invalid 并可选丢弃
```

- `ticks_per_step` 默认 4（0.2s/决策，VLA 典型）；human 数据采集可 1。
- 服务端 tick 权威源：`Bukkit.getCurrentTick()`；客户端侧经 `vla:tick` 插件消息获得（§5.6）。
- 客户端渲染延迟补偿：`render_wall_nanos` 与 `server_wall_nanos`（gRPC 返回）配合时钟漂移校正（本地回环可忽略，集群部署时需 NTP）。

---

## 10. 任务系统与课程

- **Registry（静态）**：手工定义基础任务（当前已实现 `collect_wood`、`collect_stone`、`kill_animal`；`craft_planks` 为占位；后续可扩展 `place_torch`…）。`collect_stone`/`kill_animal` reset 时由服务端发放全套钻石工具。
- **Procedural（参数化）**：遍历注册物品/方块表，参数化生成 `collect_<item>`、`craft_<item>`。
- **Curriculum（课程）**：按难度/前置关系 DAG 动态推进（木头→木板→木棍→工具→石镐→采矿→熔炉…）。
- **LLM 生成（Voyager 式）**：`GenerateTask(prompt)` 返回 `TaskSpec`，由 `custom` 判定器动态注册。
- **自动课程**：服务端按该玩家当前任务成功率动态调整 `difficulty` 与下一任务（Phase 4）。

---

## 11. 数据管线（Data Pipeline）

### 11.1 Episode 落盘（canonical 格式，与旧版一致）

```
<run_dir>/runs.jsonl
  episodes/ep-000001/
    meta.json                 # 全部种子 + 版本 + 配置（§11.2）
    instructions.jsonl
    observations/000000.jpg   # JPEG（或 PNG）
    states.jsonl              # 与帧对齐的玩家/任务状态
    actions.jsonl             # semantic + primitive 双标签
    rewards.jsonl
    episode_summary.json
```

### 11.2 种子与还原

`meta.json` 记录：`world_seed`（`level-seed`）、`mapgen`（MC 1.20 为默认世界类型+结构选项）、`task_seed`、`reset_seed`、env 版本（MC/Purpur/Fabric/插件/mod/Python）、渲染配置（res/fov/fps）、action_space 版本、起止 `server_tick`。

**还原路径**：`level-seed` + 世界类型直接重建地形 → 播放 `ResetWorld` 恢复初始区域 → 逐 bit 还原（可选 L2 FAWE schematic）。

### 11.3 人类演示采集

- 客户端 `HUMAN_MODE` + 真人操作：帧 + 原始键位/视角流上行（2-20Hz 降采样）。
- 场景同步：人类玩家通过 `minerl.interactor` 式的独立客户端加入同一 Purpur 世界，观看 agent 或自主演示。
- 与自动 Rollout 共用同一 canonical 格式 → 模仿学习/IDM 训练数据可直接混合。

### 11.5 Oracle 轨迹生成器（2026-08-08，去全局 A* 改造）

**决策背景**：VLA 数据生成不再追求最优轨迹（服务端全局 A* 退役为直线占位，
§4.5 更新），改为 Python Oracle 策略生成「合理但非最优」的多样轨迹，逐帧语义标签。

**架构**：

```
collect_wood_agent.py::oracle_wood_policy（Oracle V1）
  ├─ 目标选择：复用 select_target 启发式（簇/最矮/cover/黑名单）+ 目标噪声
  │    （最近 K 候选加权随机，--target-noise）+ --budget-per-target（同目标挖块上限）
  ├─ 导航：双航点 send_goto_path([玩家脚格, 目标块格]) —— 中间 30 米交给客户端
  │    NavExecutor/LocalPathfinder 局部绕障/挖穿/跳台；**不调用 compute_path**
  ├─ 兜底链：blocked_breakable → Python 挖穿；blocked_wall/stuck → 侧移绕行
  │    （LOCAL_DETOUR_RADIUS=3，≤detour_retries 次）→ 游走换目标
  ├─ 语义标签：每分支发动作前 schema.label()（intent/subgoal/strategy/reason/
  │    target/params/mode，11 类 subgoal 见 vla_env/dataset/schema.py）
  └─ 落盘：StepRecorder（canonical 单层目录：meta.json + trajectory.jsonl +
       frames/*.jpg + align_assertions.jsonl + episode_summary.json），
       复用 M8 lockstep.Aligner 对齐断言 + 四者计数

行为三档（P1，BEHAVIOR_PRESETS）：efficient=基线（与旧策略常量一致）、
cautious（更快放弃/更多失败）、aggressive（更耐心/更敢冲）——只放大既有
非最优行为频率，不改策略逻辑。场景池（世界坐标池 + reset 基线固化）为
P1 场景多样性注入点。

反事实数据：黑名单/平凡路径/卡死重试/挖不动放弃/失败轨迹（max_progress>0.3）
全部保留并打 suboptimal/retry/abandoned/stuck_recovery 标签；同
(world_seed, task_seed, reset_seed) 组内配对 → preferences.jsonl（P2）。

验收：`generate_oracle_dataset.py --episodes N` → `ORACLE_GEN_OK episodes=N
success=M align_rate=1.00`（四者计数一致 + 对齐率 100%，M8 口径）。


### 11.4 导出工具

| 目标 | 说明 |
|---|---|
| WebDataset | `{key}.jpg + .json` tar 分片，流式加载 |
| HuggingFace Dataset | Parquet + image 列，`push_to_hub` |
| RLDS / TFRecords | 对齐 Open X-Embodiment `observation/action` 字段 |
| MineStudio 兼容 | `pov/camera/buttons/hotbar` 字段别名 |

---

## 12. 性能与部署

| 项 | 目标 | 手段 |
|---|---|---|
| 抓帧开销 | <1ms | PBO 异步读取 + glBlit 下采样 + 网络线程编码 |
| step 时延 | ~50-100ms | lockstep 20Hz、`ticks_per_step=4` |
| 重置 | <1-2s | L1 内存快照回滚（不重启进程） |
| 并行度 | 8-32 env/机 | SubprocVecEnv、单服务器多玩家、端口动态分配 |
| 无头渲染 | Linux 必配 | Xvfb + llvmpipe（软件 GL）或容器内 GPU；`-Dorg.lwjgl.opengl.Display.allowSoftwareOpenGL=true` |
| 稳定性 | 7×24 训练 | 看门狗 kill/重启 Java 进程；日志分级；指标上报（Prometheus 可选） |
| 确定性 | seed 复现 | 冻结 gamerules + 固定 seed + 关闭随机 tick |

---

## 13. 完整实施计划（Implementation Plan）

> 本节把 Roadmap 细化为**任务级可执行清单**（依赖、交付物、实现要点、验收标准）。每个 Phase 内部按依赖排序，并标注可并行项。
>
> **开发原则**：
> - **先通后优**：P1.4 抓帧先同步 `glReadPixels` 求通，P3.2 再换 PBO 异步。
> - **Server-Authoritative**：reward/done 永远只信服务端事件，客户端观测仅作视觉输入。
> - **锁版本**：以 §13.1 依赖表为基线，任何升级先重跑 P3 对齐断言。
> - **每 Phase 有 exit criteria**，不达标不进入下一阶段。

### 13.0 里程碑依赖图

```
Phase 0（环境/脚手架）
   ├──► Phase 1（受控客户端）─────┐
   │                             ├──► Phase 3（对齐/性能）──► Phase 4（VLA/数据）
   └──► Phase 2（服务端引擎）─────┘
```

- **P0 为一切前置**。
- **P1 与 P2 可并行**（两个独立工程、不同仓库目录），在 P3 汇合。
- **P4 依赖 P3 的数据质量**；其中 4.1 人类演示采集依赖 P1 的 HUMAN_MODE。

### 13.1 版本与依赖锁定

**服务端（`purpur-vla-plugin`）**

| 依赖 | 版本 | 用途 |
|---|---|---|
| Purpur | 1.20.1（最新 build） | 服务端运行时 |
| paper-api | 1.20.1-R0.1-SNAPSHOT | 插件 API |
| grpc-netty-shaded / grpc-protobuf / grpc-stub | 1.62.x | gRPC（shadowJar 打入） |
| protobuf-java | 3.25.x | proto 运行时 |
| Gradle Shadow | 8.x | fat jar + relocate `io.grpc` |
| JDK | 17 | 编译/运行 |

**客户端（`fabric-vla-client`）**

| 依赖 | 版本 | 用途 |
|---|---|---|
| Minecraft | 1.20.1 | 客户端 |
| Yarn mappings | 1.20.1+build.10 | 开发映射 |
| Fabric Loader | 0.15.x | 加载器 |
| Fabric API | 0.92.x+1.20.1 | 事件/网络/生命周期 |
| Loom | 1.5.x | 构建 |
| Java-WebSocket | 1.5.6 | 嵌入式 WS 服务器 |

**Python（`vla_env`）**

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.10+ | 运行时 |
| grpcio + grpcio-tools | 1.62.x | gRPC client + proto 生成 |
| websockets | 12.x | WS client（二进制帧） |
| gymnasium | 0.29.x | Env 基类 |
| numpy / pillow / opencv-python | 最新 | 数组/图像编解码 |
| pyyaml | 最新 | 配置 |

### 13.2 端口与频道规划

| 通道 | 端口/标识 | 说明 |
|---|---|---|
| gRPC（Python↔服务端） | `127.0.0.1:50051`（每服务器一个，`VLA_GRPC_PORT`） | 结构化 RPC |
| WS（Python↔客户端） | `127.0.0.1:30001 + env_idx` | 动作下行 / 帧上行 |
| MC 服务端 | `25565 + server_idx`（多服务器模式） | 客户端进服 |
| 插件消息 channel | `vla:tick` | 服务端 tick 广播（§5.6） |

### 13.3 Phase 0：环境与工程脚手架

**目标**：三端独立可跑，为 P1/P2 铺路。

| # | 任务 | 交付物 | 实现要点 |
|---|---|---|---|
| 0.1 | 服务端环境 | `server/` 目录 + 启动脚本 | 下载 Purpur 1.20.1；接受 `eula.txt`；`server.properties`：`online-mode=false`、`level-seed=<固定种子>`、`spawn-protection=0`、`view-distance=8`、`simulation-distance=8`；启动后记录 `/seed` |
| 0.2 | gamerule 冻结脚本 | `scripts/freeze_gamerules.sh` | `doDaylightCycle/doWeatherCycle/doMobSpawning/mobGriefing/randomTickSpeed` 关闭，`keepInventory=true`，`time set 6000` |
| 0.3 | 客户端环境 | Loom 空 mod 可连服 | 装 Fabric Loader 1.20.1；`loom genSources`；空 mod join 服务端验证 |
| 0.4 | Python 环境 | `vla_env/` 可 import | venv + `pyproject.toml`；`grpc_tools.protoc` 从 `proto/vla.proto` 生成 pb2/grpc 代码 |
| 0.5 | 协议文档骨架 | `docs/p1_protocol.md` / `p2_protocol.md` / `p3_alignment.md` | 随实现同步维护契约，实现即文档 |

**Exit（P0）**：客户端可手动进服并移动；`import vla_env` 无错；proto 代码生成通过。

**M0 完成记录（2026-08-06，全部验收通过）**：
- **环境实际**：Purpur 1.20.1（latest build，45MB）；JDK 21（Homebrew `openjdk@21`，`/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home`，本机默认 Java 26 不可用于 1.20.1）；seed `123456789`；端口 25565。
- **服务端（`server/`）**：`download.sh`（幂等拉取）、`server.properties`（offline/seed/视距）、`eula.txt`、`start.sh`（JDK21 + FIFO 控制台，管道 `/tmp/vla_server_console`，`VLA_SERVER_PIPE` 可覆盖）、`freeze_gamerules.sh`。实测启动 `Done (2.7s)`、`tps 20`、6 条 gamerule 冻结 + `time set 6000` 生效。
- **客户端（`fabric-vla-client/`）**：Loom 1.5.8 + Gradle wrapper 8.7 + Fabric API 0.92.11+1.20.1 + loader 0.15.11 + Java-WebSocket 1.5.6（`include` 打包）；Java 17 toolchain 缺失 → 用 `options.release=17` + JDK 21 运行 Gradle；`./gradlew build` 成功产出 `vla-client-0.1.0.jar`。
- **Python（`vla_env/`）**：venv（Python 3.14.5）+ `pip install -e .` 全包成功（grpcio/grpcio-tools 1.83、gymnasium 1.3、numpy 2.5、websockets 17…）；`import vla_env` → `0.1.0`（纯惰性导入）；proto 已生成（19 messages + 11 RPC，`vla_pb2.py`/`vla_pb2_grpc.py` 可导入）。
- **连通性冒烟（`tools/agents/`）**：mineflayer（Node 22）2 个无头 bot 连服 → `SMOKE_OK: 2/2`，服务端 `list` 显示 `2 of a max of 16 players online`，验证多玩家连接能力。
- **遗留**：完整 Fabric 客户端进服（带渲染）属 P1（M2/M3）；gRPC 服务端实现属 P2（M4）；proto 仅代码生成，未接线。从仓库根运行 venv 会被 `vla_env/` namespace 遮蔽，需在 `vla_env/` 目录内运行（Agent C 记录）。

### 13.4 Phase 1：受控客户端（地基）

**目标**：API_MODE 下物理键鼠隔离、动作注入、抓帧上行全通。**可与 P2 并行。**

| # | 任务 | 交付物 | 实现要点 |
|---|---|---|---|
| 1.1 | 工程骨架 + WS 服务器 | `net/WsServer.java`、`VlaClient.java` | Java-WebSocket 监听 `30001`；消息 schema（§9.2）；`AtomicReference<ActionCmd> currentAction`、`volatile boolean apiMode`；`ping/pong` 心跳与断线清理 |
| 1.2 | 输入隔离 Mixin | `mixin/KeyboardInputMixin.java`、`mixin/MouseMixin.java` | 按 §5.3 伪代码；`mode` 切换时 `KeyBinding.unpressAll()` 防粘键；HUMAN_MODE 完全放行 |
| 1.3 | 动作注入 | `input/ActionApplier.java` | attack/use/hotbar/drop/inventory 经 `KeyBinding.setPressed`；视角 `setYaw/setPitch` 插值（天然规避万向节死锁）；字段名差异集中到常量表 |
| 1.4 | 抓帧 MVP（同步版） | `gfx/FrameGrabber.java`、`mixin/GameRendererMixin.java` | world 渲染后、HUD 前：主 framebuffer → `glBlitFramebuffer` 到 224² → 同步 `glReadPixels`（先求通） |
| 1.5 | 帧通道上行 | `net/FrameSender.java` | `ConcurrentLinkedQueue<FrameData>` 交递；网络线程 JPEG 编码（ImageIO）+ `send(ByteBuffer)`；帧头 `[4B frame_id][4B last_server_tick][8B wall_nanos][JPEG]` |
| 1.6 | Python 连通 | `client_ws.py`、`action_space.py`、`scripts/random_agent.py` | WS 同步封装（recv_frame/parse）；随机动作 100 步 + 帧解码断言 |

**Exit（P1 验收）**：
- API_MODE 下物理键鼠完全失效；HUMAN_MODE 透明。
- 随机策略可驱动移动/转向/攻击，`random_agent.py` 输出帧 `(3,224,224)` 与动作 1:1。
- 帧上行 ≥20fps，无粘键、无崩溃。

**M2 完成记录（2026-08-06，客户端控制 ✅）**：
- **`input/ActionCmd.java`**（纯 Java，`fromJson` 无 MC 依赖）、**`input/ActionApplier.java`**（视角 `setYaw/setPitch` 增量 + attack/use/drop/inventory 经 KeyBinding + hotbar 按下-释放）、**`mixin/KeyboardInputMixin.java`**（API 模式 cancel 原 tick，写 movement 字段）、**`mixin/MouseMixin.java`**（API 模式 cancel `Mouse#updateMouse`）。
- **Yarn 1.20.1 核实**（genSources）：`KeyboardInput#tick(boolean,float)`；`Input` 有 `pressingForward/Back/Left/Right`、`movementForward/Sideways`、`jumping/sneaking`，**无 `sprinting`**（疾跑经 `Entity#setSprinting`）；`Mouse#updateMouse()`；GameOptions 字段是 `dropKey/inventoryKey`（非 keyDrop）；hotbar/drop/inventory 走 `wasPressed()` 需补 `KeyBinding.onKeyPressed`。
- **WS 扩展**：`action`（→`action_ok`，harness 打印解析回显）、`reset_camera`（→`camera_ok`）；模式切换时 `client.options.allKeys` 全 `unpressAll` 防粘键。
- **验证**：build ✅；harness 三项全过（action 字段回显一致 / reset_camera / mode）；**`./gradlew runClient` 实机跑通**（loader 0.15.11→0.16.10 以兼容 fabric-api 0.92.11；mixin 零报错、渲染至标题屏、WS 30001 游戏内监听、实机 WS 指令通过）。
- **遗留**：hotbar/drop/inventory 用 `getDefaultKey()`（用户改键不匹配）；attack 未补 timesPressed → 仅按住挖掘、单发 doAttack 不触发；camera 与 movement 至多 1 tick 相位差（M3 对齐时处理）。物理键鼠隔离为代码路径 + mixin 加载验证，真人目视检查留待 M3。

**M3 完成记录（2026-08-06，客户端视觉 ✅，端到端跑通）**：
- **`gfx/FrameGrabber.java`**：渲染线程抓帧——主 framebuffer `glBlitFramebuffer` 下采样到 224×224 小 FBO → `glReadPixels` RGBA（每帧分配，PBO 三缓冲属 M9 优化）；FrameData 入 `ConcurrentLinkedQueue`。
- **`net/FrameSender.java`**：守护线程 JPEG（`TYPE_3BYTE_BGR`，ARGB 带 alpha 会抛 "Bogus input colorspace"）+ 二进制帧头 `[4B frame_id][4B last_server_tick=0 占位][8B wall_nanos][JPEG]` 经 WS 上行。
- **抓帧钩子**：`WorldRenderEvents.LAST`（世界+实体渲染后、HUD 前；AFTER_TRANSLUCENT 在实体之前不可用）。
- **autojoin**：`run/autojoin.txt`（`host:port`）→ `ConnectScreen.connect(...)`（1.20.1 标准入口，genSources 核实；非 getServerConnection）。
- **Python**：`client_ws.recv_frame()`（解析二进制头 + JPEG→numpy，跳过 JSON 夹杂）；`scripts/random_agent.py`（100 步随机动作 + 收帧断言）。
- **验收**：Purpur + runClient（autojoin 自动入服，离线名 Player249）→ WS 30001 → `random_agent --steps 100` → **`M3_OK frames=100`**，全部 `shape=(224,224,3)`，零超时，帧内容非空（mean≈160/std≈76）。
- **遗留**：PBO 异步抓帧留 M9；`last_server_tick` 占位（M8 对齐）；run/autojoin.txt 已保留（下次 runClient 自动入服）。

### 13.5 Phase 2：Purpur 服务端世界引擎（可与 P1 并行）

**目标**：gRPC + 世界重置 + 任务/奖励 + 体素 + 寻路，Python 端 `reset/step` 闭环。

| # | 任务 | 交付物 | 实现要点 |
|---|---|---|---|
| 2.1 | 插件骨架 | `VlaPlugin.java`、`build.gradle.kts` | paper-api + grpc + shadowJar（relocate `io.grpc`）；`/vla status` 命令（TPS + gRPC 端口）；onEnable 起 gRPC |
| 2.2 | gRPC 服务 + 主线程调度 | `grpc/VlaGrpcService.java`、`grpc/MainThreadDispatcher.java` | 按 §4.2：只读（体素/路径）gRPC 线程直跑，写操作 `runTask`；优雅关闭（onDisable shutdown） |
| 2.3 | ResetEngine（L1） | `reset/RegionSnapshot.java`、`reset/ResetEngine.java` | 首次记录 `BlockData[]`（按 ChunkPos 分片）+ 实体清单；apply：清实体→`setBlockData(data,false)`→清掉落→玩家态重置（传送/背包/HP/饥饿）→gamerule 冻结→`setTime(6000)`；reset 前 `chunk.isLoaded()` 检查 |
| 2.4 | 玩家管理 | `player/AgentManager.java` | 离线 UUID 稳定映射（`UUID.nameUUIDFromBytes("OfflinePlayer:"+name)`）；死亡自动重生监听；断线清理 |
| 2.5 | TaskManager + 判定器 | `task/TaskManager.java`、`task/Predicates.java` | §4.7 TaskSpec 解析；事件监听 `BlockBreakEvent/BlockPlaceEvent/EntityDeathEvent/PlayerMoveEvent` + 周期轮询 inventory；`EpisodeState` 进度/超时/结算 |
| 2.6 | 体素 + A* | `world/VoxelReader.java`、`path/AStar.java` | NMS `Level#getBlockState` + palette 编码；3D 8 方向 A*（方块成本表、跳跃支持）→ 拐点航点 |
| 2.7 | Python 集成 | `server_grpc.py`、`env.py`（初版） | 全 RPC 封装；`reset/step` 打通（先同步抓帧）；`GetStepResult` 阻塞 k ticks |

**Exit（P2 验收）**：
- `env.reset(task="collect_wood", seed=123)` 两次重置体素一致（可复现）。
- 随机/脚本策略完成 `collect_wood`：判定器 success=true、reward 来自服务端事件。
- `GetVoxels` 与实景一致；`ComputePath` 输出可达航点。

**M1 完成记录（2026-08-06，双端 ping 打通 ✅）**：
- **客户端 WS（任务 1.1）**：`net/WsServer.java`（纯 Java 无 MC 依赖，含独立 `main` 测试入口）+ `VlaClient` 接线（守护线程启动 WS 端口 30001，`vla.ws.port` 系统属性可覆盖，apiMode 回调）；`tools/ws_harness.sh` 一键独立 harness。实测 4 项协议：`pong` / `mode_ok`（api_mode 翻转）/ `bye` / `error` 全过。
- **服务端 gRPC（任务 2.1-2.2）**：`purpur-vla-plugin/`（paper-api + grpc-java 1.62.2 + protobuf 3.25.3 + shadow relocate `dev.vla.shadow.*`）；`/vla status` 命令（tick/tps/端口）；gRPC `127.0.0.1:50051` + ProtoReflectionService；`MainThreadDispatcher.runSync` 骨架。实测插件加载、tick=135、tps=20.0、端口监听。
- **Python（任务 2.7 前置）**：proto 重生成（新增 Ping RPC）；`ServerGrpc.ping()` / `ClientWs.ping()` 实现；`scripts/ping_both.py` 双端验收 → `M1_PING_BOTH_OK`（WS pong + gRPC `server_tick=104`、`tps=20.21`、`version=git-Purpur-2062`）。
- **遗留**：`action`/`reset_camera` 指令、**游戏内** WS 接入（需运行完整客户端，harness 已验证协议本身）属 M2；端口占用探测待补。

**M4 完成记录（2026-08-06，世界引擎 ✅）**：
- **`reset/RegionSnapshot.java`**：BlockData[] 扁平区域快照（capture/restore/CRC32 checksum/blockCount），`World#getBlockState(x,y,z)` 快速读取。
- **`reset/ResetEngine.java`**：`ResetSpec`（center/halfExtent=16/spawn/clearInventory/initialItems/time=6000）+ **基线缓存**（首次 capture 缓存，后续从缓存恢复保证确定性）；reset 全流程：force-load chunk → 清非玩家实体（含掉落物）→ `Block#setBlockData(data,false)` 回滚 → 玩家态重置（teleport/清背包/HP/饥饿/药水/生存模式）→ gamerule 冻结 → `setTime(6000)+setStorm(false)`；`verify()` 实时校验（checksum/实体数/时间/玩家摘要）。
- **`player/AgentManager.java`**：离线 UUID 稳定映射（`nameUUIDFromBytes("OfflinePlayer:"+name)`）、join/quit 事件、死亡 2s 自动重生；**跨重连持久区域映射**（mineflayer dig 会踢掉原连接 → session 重建，需持久化 region 否则 C2≠C1）。
- **`VlaGrpcService`**：实现 `ResetWorld` RPC（经 MainThreadDispatcher 调度，玩家不存在回 FAILED_PRECONDITION）。
- **验收**（mineflayer bot + `/summon` 僵尸 + dig 挖 grass）：
  - C1=`fdd0b795` → summon+dig 后 verify `ef349e33`（≠C1）+ `entities=2` → 二次 reset C2=`fdd0b795` **==C1 ✓**、`entities=0`、`pos==spawn`、`time=6000`、`hp=20/food=20/sat=20`
  - gRPC `ResetWorld ok=true server_tick=1560`；不存在玩家 → FAILED_PRECONDITION
- **踩坑记录**：① 本仓库 paper-api 无 `World#setBlockState(...,applyPhysics)`，用 `Block#setBlockData(data,false)`（语义相同）；② **FIFO 控制台命令不带 `/` 前缀**（`vla status` 有效、`/vla status` 报 Unknown command）；③ dig 踢连接→session 重建，region 持久化解决。
- **遗留**：`vla_env/server_grpc.py::reset_world()` 仍占位（属 M7 Python 集成）；世界留有 dig 痕迹（基线即捕获态，不影响确定性）。

**M5+M6 完成记录（2026-08-06，任务系统 + 体素/寻路 ✅）**：
- **任务系统（M5）**：`task/TaskSpec.java`、`task/TaskRegistry.java`（内置 `collect_wood`=block_mined×4 oak_log、`craft_planks` 占位）、`task/Predicates.java`（block_mined / inventory_contains / entity_killed / player_at）、`task/TaskManager.java`（EpisodeState + `step(awaitTicks)` 经 runTaskLater **主线程回调**结算，server-authoritative）；RPC `setTask/getStepResult/getState`（+`generateTask` 占位）；命令 `vla task/taskinfo`。
- **状态与寻路（M6）**：`world/VoxelReader.java`（paper-api `getBlockData(x,y,z)` 快速读 + palette 编码，**不用 NMS**）；`path/AStar.java`（3D 8 方向 + 跳跃，成本表，open set 确定性排序，拐点压缩，**目标不可站自动上移至首个可站格**）；RPC `getVoxels/computePath`；命令 `vla voxels/path`。
- **验收**：`collect_wood` 挖 4 原木 → `taskinfo success=true counters={block_mined:oak_log=4}`，`GetStepResult reward=10.0 terminated=True`；`GetVoxels(half_extent=8)` palette=25/data=4913/origin=(56,60,-56)/size=17；`ComputePath(+30格)` found=True waypoints=11（翻越山丘）。
- **踩坑**：① AStar 目标在山体内 → 不可站时上移；② paper 快速读块是 `getBlockData`（非 getBlockState）；③ 服务端启动必须 `run_in_background=true`，否则工具在前台命令结束时 SIGTERM 掉；④ craft_planks 未验收（占位）。

**M7 完成记录（2026-08-06，Env 闭环 ✅ 端到端跑通）**：
- **Python**：`server_grpc.py` 全 RPC 封装（reset_world/set_task/get_step_result/get_state/get_voxels/compute_path…）；`obs.py`（pov+state+task 拼装）、`action_space.py`、`env.py`（`MinecraftEnv`：reset=ResetWorld→SetTask→mode api→收首帧→GetState；step=WS 动作→收帧→GetStepResult 结算→GetState，lockstep server-authoritative）；`scripts/collect_wood_agent.py` 脚本策略。
- **客户端**：`MinecraftClientAccessor`（session 用户名覆盖 agent0）、`MinecraftClientMixin`（API 模式**阻止 GameMenuScreen 被 setScreen 安装**）、`FrameSender` 30fps 流控、autojoin.txt 两行（host + 用户名）。
- **M7.1 修复（用户反馈：API 模式仍抓鼠标 + 失焦弹菜单；也是挖掘失效根因）**：
  - 根因：窗口失焦 → 原版打开 GameMenuScreen → `handleInputEvents` 被跳过 → 挖掘（handleBlockBreaking）完全不执行（实测 screen=GameMenuScreen 41/42 采样）；移动经 KeyboardInputMixin 直接写 input 不受影响 → 表现为"能走但挖不动"。
  - 修复① `MouseMixin` 新增 `lockCursor` HEAD cancel（API 模式**不物理抓取鼠标**，光标自由）；`isCursorLocked` 谎报 true 保留（handleBlockBreaking 依赖）。
  - 修复② `VlaClient.onModeChange`：进入 API 置 `options.pauseOnLostFocus=false` + `unlockCursor`，退出恢复原值；配合 MinecraftClientMixin 双保险。
  - 修复③ **动作电平保持**：END_CLIENT_TICK 不再 `getAndSet(null)` 消费动作，`currentAction` 持续持有直到被新动作替换（env.step 跨 5-10 ticks，原按 tick 消费使 forward/attack 占空比极低 → 蠕动/挖不动）；一次性字段（camera/hotbar/drop/inventory）在 `ActionApplier.apply` 应用后清零，避免每 tick 重复触发。
- **验收**：`M7_COLLECT_WOOD_OK steps=202 progress=1.00`（env.reset/step 端到端 + 脚本策略挖 4 原木，reward=10.0 来自服务端）；obs pov(224,224,3)+player.pos；step 返回类型断言通过。
- **踩坑**：① 旧客户端进程 cmdline 含 `fabric.dli.config`（非 `net.minecraft.client.main.Main`），pkill 模式不匹配 → 僵尸占 30001 → 新客户端 WS BindException，需按 fabric 特征杀进程；② 修复后需重建 jar 并**重启整个客户端**才生效。

### 13.6 Phase 3：数据对齐与性能优化（最关键）

**目标**：tick/frame 严格对齐、抓帧 <1ms、多 env 并行稳定。

| # | 任务 | 交付物 | 实现要点 |
|---|---|---|---|
| 3.1 | tick 对齐链路 | 插件 `vla:tick` 广播、`mixin/PlayNetworkMixin.java`、`lockstep.py` | 服务端每 tick 广播 `[4B tick][8B wall_nanos]`；客户端写入 `volatile long lastServerTick`；帧元数据携带；lockstep 断言（§9.3） |
| 3.2 | PBO 异步抓帧 | `gfx/FrameGrabber.java`（重构） | FBO blit + PBO 三缓冲（`GL_PIXEL_PACK_BUFFER` + `glMapBufferRange`）；GL 版本检测 + 同步 fallback；byte[] 池化降 GC |
| 3.3 | 并行采样 | `env/MinecraftVecEnv.py`、端口分配、看门狗 | SubprocVecEnv；WS 端口 30001+n；`step()` 超 10s → kill/restart（不 Debug） |
| 3.4 | 采集脚本 | `scripts/collect_dataset.py`、`dataset/episode_writer.py` | N episode → canonical 目录（§11.1）+ 四者计数对齐断言 + `meta.json` 全字段（§11.2） |

**Exit（P3 验收）**：
- 10 episode 连续采集，帧/状态/动作/奖励数量一致，tick 对齐断言 100%。
- 抓帧耗时 <1ms（开关对比实验，FPS 无显著下降）。
- 4 env 并行稳定运行 ≥1h，无死锁/卡死。
- 产出首份可验证数据集。

**M8 完成记录（2026-08-06，数据对齐 ✅）**：
- **服务端 `vla:tick` 广播**（`VlaPlugin.java`）：`registerOutgoingPluginChannel("vla:tick")` + `runTaskTimer(20L, 1L)` 每 tick `sendPluginMessage` 12B `[4B serverTick][8B wallNanos]`（ByteBuffer 大端，无玩家不广播）。
- **客户端接收**（`VlaClient.java`）：`ClientPlayNetworking.registerGlobalReceiver("vla:tick")` 读 12B → `volatile long lastServerTick`（静态 `getLastServerTick()`；`catch Throwable` 防网络线程崩）；`FrameGrabber.FrameData` 增 `lastServerTick` 字段、`FrameSender` 帧头写真实值。
- **`lockstep.py`**：`assert_step_alignment`（窗口 `0<=server_tick-frame_tick<=ticks_per_step+tol` + frame_id/tick 单调不减）+ `Aligner`（累 mismatch/max_diff，`report()` 对齐率）。
- **`scripts/collect_episodes.py`**：N episode 全栈采集（随机动作，`/tmp` JSONL）+ 四者计数断言 + 对齐率汇总。
- **验收**：10ep×60steps → `frames=600 actions=600 rewards=600 states=600 counts_consistent=True`；`align steps=600 mismatch=0 align_rate=1.00 max_diff=3`（tol=2，窗口 ticks+2=4）；`M8_ALIGN_OK episodes=10 align_rate=1.00`。
- **踩坑**：① WS 二进制帧撞 `_recv_json` 的 utf-32-be BOM 解析（帧头 frame_id=0x0000FEFF 时），修 `_recv_json` 跳过 bytes/乱码仅收 dict，episode 间切换从 ~10s 降到即时；② 客户端被遮挡时 MC 静默断连（Throwable catch 防御，未根因）；③ 多屏截图看不到被遮窗口，用 jstack + 抓帧计数确认渲染。

**Demo 三问题修复记录（2026-08-06，`5b26aa6`）**：
用户反馈 demo 视频三个问题，全部修复并端到端验收：
1. **无 HUD（物品栏/手/准星缺失）**：抓帧默认在 `WorldRenderEvents.LAST`（HUD 前，VLA 观测需要干净画面）。新增 WS `set_capture_ui hud=true` 切换抓帧挂点到 `GameRenderer.render` **TAIL**（含世界+手+HUD+准星），demo 录制默认开启；VLA 观测默认保持无 HUD 不变。
2. **突然闪现**：`look_at`/`reset_camera` 原为 `setYaw/setPitch` 瞬移。改为**平滑转向**——只设视角目标，`END_CLIENT_TICK` 里按 `maxTurnDeg`（默认 40°/tick）沿最短角差插值收敛（误差 <0.05° 停用）；`look_at` 用客户端自身眼位算精确朝向，消除服务端 pos 滞后瞄准偏差。
3. **被障碍挡住（3D 路径规划）**：根因有两层——(a) A* 目标微调只向上找，树顶 log 抬到树冠之上不可达 → 全路径失败（`ComputePath` 实测全空）；(b) 玩家撞墙后"重算路径"不改变方向，永远卡死。修复：`adjustGoal` 在目标周围 3D 邻域（水平±2、垂直 -8..+2）找最近可站格（7/8 目标有路径，原全空）；策略 approach 卡死先 `back` 3 步解卡，同目标二次卡死黑名单+游走换树。
- **验收**：`DEMO_OK steps=219 progress=1.00`；视频 `datasets/demo/collect_wood_agent_view.mp4` 1708×960 原生 16:9、44.5s、含完整 HUD，帧间平滑无闪现。移动模型细节（canStand/diagonalClear/adjustGoal）已并入 §4.5。
- **附带发现**：本机 MC 1.20.1 + fabric-api 0.92.11 中 `WorldRenderEvents.LAST` 实际已含 HUD（环境差异）；"VLA 观测默认干净"若需严格保证，抓帧挂点应改到 `inGameHud.render` 之前（待 M9 处理）。

**寻路可视化 Demo（2026-08-08，`demo_dig_tree.py`）**：
- **路径粒子特效（ShowPath RPC）**：插件新增 `debug/PathVisualizer`——在 A* 航点上周期（10 tick）刷 END_ROD 粒子（航点方块中心 y+0.5，目标树额外加亮：END_ROD 簇 + 红色 Dust 大颗粒），每玩家一条活动路径，新路径覆盖/clear 清除/60s 超时消失；gRPC `ShowPath(waypoints, goal, clear, lifetime_ticks)` 主线程调度。第一视角录制时路径是一条"发光路径"，直观展示 A* 走向（绕障碍/穿树叶/跳台阶）。
- **`scripts/demo_task.py`**：三任务通用 demo 录制——**真实世界**：默认不人工放置方块，树/石头取自种子生成的自然世界（模拟真实伐木/采矿；`--setup` 可 opt-in 人工放置）；kill_animal 因 reset 冻结 doMobSpawning 必须 gRPC spawn 2 头猪（实体）。驱动 `collect_wood_policy`（传 `on_path` 回调，每次 ComputePath 后调 ShowPath）→ 录帧 → ffmpeg 合成 mp4 → `DEMO_OK`。`demo_dig_tree.py` 为 collect_wood 的兼容别名。
- **真实世界探索修复（2026-08-08）**：agent 砍几棵树后可能走到树冠/山坡高位，其余原木 |dy|>2.6 被 `_select_block_target` 过滤 → 随机游走找不回树（demo 600 步 fail）。修复：① 候选 dy 过滤只限"比玩家高 ≤2.6"，"比玩家低"放宽到 `APPROACH_DY_DOWN=6`（山坡上可走下去接近）；② explore 分支扫到任务块但无可选目标时，朝最近块 A* 接近（走近后低层块可达）替代随机游走。
- 三任务真实世界验收：collect_wood `steps=166`（砍真实树）、collect_stone `steps=184`（挖穿草皮暴露自然石头）、kill_animal `steps=87`（追猎 spawn 的猪）。世界种子 `20260808`，`setworldspawn` 到最近森林（真实种子生成）。
- **`setBlock` RPC 实现**：God Mode 单方块设置（`Block#setBlockData(data, applyPhysics)`），demo 放置树用（apply_physics=false）。
- **验收**：`DEMO_OK steps=N progress=1.00`；视频 `datasets/demo/dig_tree_*.mp4`，可见发光路径 + 红色目标标记 + 逐树挖原木。

### 13.7 Phase 4：VLA 接入与数据管线

**目标**：人类演示采集、导出器、VLA 闭环、自动课程。

| # | 任务 | 交付物 | 实现要点 |
|---|---|---|---|
| 4.1 | 人类演示采集 | `scripts/collect_demo.py`、`dataset/interactor.py` | HUMAN_MODE 第二客户端同服；帧+按键+视角 20Hz 录制；与自动 rollout 同 canonical 格式（模仿学习/IDM 数据可混合） |
| 4.2 | 导出器 | `dataset/export.py` | WDS / HF / RLDS / MineStudio 兼容（§11.4） |
| 4.3 | VLA 模型闭环 | `scripts/train_loop.py` | obs 预处理（resize/normalize）；接入 OpenVLA / Pi0 / STEVE-1；训练 loss 下降 + 闭环评估 |
| 4.4 | 自动课程 | `task/Curriculum.java` | 按成功率动态调 difficulty/下一任务（§10） |

**Exit（P4 验收）**：
- 数据集可被外部 VLA 框架加载训练；训练策略能在 env 中闭环执行简单任务。
- 自动课程随成功率提升逐步解锁更难任务。

### 13.8 实施期风险与预案

| 风险 | 触发 | 预案 |
|---|---|---|
| Mixin 目标类改名 | 升级 MC/映射版本 | 锁 1.20.1 基线；字段名集中常量表；mapping 工具核对（§14.11） |
| gRPC 包冲突 | 与其他插件共存 | shadow relocate `io.grpc`；仅绑回环端口 |
| 抓帧掉帧 | PBO 未生效 | GL 版本检测 fallback 同步版；编码下放网络线程；byte[] 池化 |
| 客户端连不上服 | 离线模式/UUID | `online-mode=false`；核对 `server.properties` 与插件消息 channel 注册 |
| reset 后方块残留 | 区块未加载/物理更新 | reset 前 `chunk.isLoaded()`；`setBlockData(data,false)`；实体与掉落物全清 |
| 训练卡死 | Java 进程僵死 | 看门狗 kill/restart；日志分级；指标上报 |
| reward 错位 | 网络延迟 | 强制 server-authoritative step；lockstep 断言丢弃 invalid（§14.2） |

### 13.9 验收总表（项目级）

| 能力 | 验收方式 | 里程碑 |
|---|---|---|
| 输入隔离 | API_MODE 物理键鼠失效 | P1 |
| 帧/动作 1:1 | `random_agent.py` 100 步断言 | P1 |
| 世界可复现 | 两次 reset 体素一致 | P2 |
| 任务判定 | `collect_wood` success=true | P2 |
| 数据对齐 | 10 episode 四者计数一致 + tick 断言 100% | P3 |
| 性能 | 抓帧 <1ms；4 env 并行 ≥1h | P3 |
| 模型闭环 | 外部 VLA 加载训练 + 闭环执行 | P4 |
| 数据可导出 | WDS/HF/RLDS 冒烟通过 | P4 |

### 13.10 里程碑骨架（Milestone Skeleton）

> 里程碑 = 项目**跟踪主干**（比 Phase 细、比任务粗）。每个里程碑有明确 **Exit 标准**，全部达标才标记完成。
> 状态约定：⬜ 待开始 · 🔄 进行中 · ✅ 完成 · ⚠️ 阻塞
> "任务"列指向 §13.4-13.7 的任务编号，建立 骨架 ↔ 任务表 ↔ 验收 的对应链。

**依赖 DAG**：

```
M0 ─► M1 ─┬─► M2 ─► M3 ──┬──────────────────────────┐
          │              │                          ▼
          └─► M4 ─┬─► M5 ─┴─► M7 ─► M8 ─► M9 ─► M10 ─► M12
                  └─► M6 ──────────────────┘        ▲
                                       M3 ─► M11 ────┘
```

- **客户端线（M2/M3）与服务端线（M4/M5/M6）可并行**，M7 为两线汇合点。
- M11（人类演示）只依赖 M3，可与 M8-M10 并行。

**骨架表**：

| ID | 里程碑 | 阶段 | 依赖 | 任务 | Exit 标准 | 状态 |
|---|---|---|---|---|---|---|
| M0 | 环境脚手架 | P0 | — | 0.1-0.5 | 三端可跑：客户端可进服 / `vla_env` import / proto 代码生成 | ✅ |
| M1 | 通信底座 | P1+P2 | M0 | 1.1, 2.1-2.2 | Python 可 ping 两端：WS `pong` + gRPC 返回 `server_tick` | ✅ |
| M2 | 客户端控制 | P1 | M1 | 1.2-1.3 | API_MODE 物理键鼠完全失效；HUMAN_MODE 透明；无粘键 | ✅ |
| M3 | 客户端视觉 | P1 | M1, M2 | 1.4-1.6 | 随机策略驱动移动/转向/攻击；224² 帧 ≥20fps 上行且与动作 1:1 | ✅ |
| M4 | 世界引擎 | P2 | M1 | 2.3-2.4 | 两次 reset 体素一致；玩家态/背包/时间/实体正确重置 | ✅ |
| M5 | 任务系统 | P2 | M4 | 2.5 | `collect_wood` 判定 success=true；reward 来自服务端事件 | ✅ |
| M6 | 状态与寻路 | P2 | M4 | 2.6 | `GetVoxels` 与实景一致；`ComputePath` 输出可达航点 | ✅ |
| M7 | Env 闭环 | P2 | M2, M3, M5, M6 | 2.7 | `env.reset/step` 端到端跑通 `collect_wood`（随机/脚本策略） | ✅ |
| M8 | 数据对齐 | P3 | M7 | 3.1 | 10 episode 帧/状态/动作/奖励计数一致 + tick 断言 100% | ✅ |
| M9 | 性能与并行 | P3 | M8 | 3.2-3.3 | 抓帧 <1ms；4 env 并行稳定 ≥1h | ⬜ |
| M10 | 数据管线 | P3+P4 | M8, M9 | 3.4, 4.2 | 首份可验证数据集 + WDS/HF/RLDS 导出冒烟 | ⬜ |
| M11 | 人类演示 | P4 | M3 | 4.1 | 人类演示数据与自动 rollout 同 canonical 格式 | 🔄（M11.5 重构后 dig_stone 全绿，§17.7；真人 HUMAN_MODE 采集待做） |
| M12 | VLA 闭环 | P4 | M10, M11 | 4.3-4.4 | 外部 VLA 加载训练 + 闭环执行 + 自动课程解锁 | ⬜ |

**推进规则**：
1. 里程碑开工置 🔄，Exit 全过后置 ✅；依赖未达 ⬜ 的不启动。
2. 遇阻置 ⚠️，在文档/issue 记录原因与阻断项。
3. M7 之前两条线独立推进、互不阻塞；M3 与 M4 无依赖关系，可同时开工。

---

## 14. 避坑指南（Blood & Tears）

1. **OpenGL 线程**：抓帧/GL 调用只许在渲染线程（`GameRenderer.render` 回调内）；WS 线程里碰 GL → JVM 崩溃。用 `ConcurrentLinkedQueue` 交递数据，网络线程只做编码与发送。
2. **网络延迟导致的状态错位**：客户端执行了攻击，但服务端事件晚 50ms 才触发，此时读 reward 是 0。**Server-Authoritative Step**：Python 发动作后**阻塞等 gRPC 结算**（含 reward/done/tick），再请求客户端下一帧。
3. **视角万向节死锁**：别手动累加 pitch/yaw 后再 set。直接用 `setYaw/setPitch`（原版内部处理 pitch∈[-90,90] 与 yaw 环绕），或经 `changeLookDirection` 增量。
4. **区块缓存/重置泄漏**：不要频繁卸载/重载区块。L1 内存快照回滚（`setBlockData(data,false)`）是最快路径；避免 `Chunk#load/unload` 反复。
5. **主线程纪律**：gRPC 回调查世界 = 崩。所有写操作经 `Bukkit.getScheduler().runTask` 回主线程；只读查询可 NMS 直读。
6. **幽灵方块（Ghost Blocks）**：客户端预测 vs 服务端权威不一致（距离/碰撞/权限拒绝）。奖励与任务判定**只用服务端事件**，客户端观测仅作视觉输入。
7. **饥饿/死亡循环**：非生存任务必须关 `doMobSpawning`、关饥饿或给饱和/回血；监听死亡自动重生并传送回安全点（旧 Luanti 项目的血泪教训，移植）。
8. **无头渲染**：Linux 上必须 Xvfb/llvmpipe 或 GPU 容器；否则 LWJGL 初始化失败。Windows/mac 本地开发窗口需保持（或虚拟显示器）。
9. **端口冲突**：并行 env 各自绑定唯一 WS 端口（30001+n）/服务端端口（25565+n）；动态分配 + 启动探测。
10. **坐标轴约定**：Minecraft Y 向上、XZ 水平；3D 视觉库常 Z 向上。第一行注释写清，转换函数集中一处。
11. **版本/Mapping 漂移**：Mixin 目标类与方法名随版本变化（1.20.5 `Input` 字段改名、Fabric networking API 1.20.5 改 CustomPayload）。升级版本时用 mapping 工具（Yarn→Mojang 对照）核对；锁版本基线（§2.1）。
12. **EULA 与账号**：服务端首次运行需接受 `eula.txt`；离线模式（`online-mode=false`）下客户端可离线登录进服，训练环境建议离线。
13. **Sodium 兼容**：若装 Sodium，其替换了部分渲染管线但 `GameRenderer` 仍可达；抓帧以**最终帧缓冲**为准，关掉分辨率缩放类 mod 或按其实际缓冲读取。
14. **Reset 时区块未加载**：回滚/清实体前先 `chunk.isLoaded()`/强制加载目标区块，避免 NPE 或静默漏格。
15. **观测量纲漂移**：`relative_pos`、`camera` 增量、体素坐标必须统一基准（以 episode 出生点/当前帧为准），否则模型过拟合绝对坐标。

---

## 15. 目录结构与交付物

```
fake-mc/
├── DESIGN.md                        # 本文档
├── docs/
│   ├── p1_protocol.md               # Phase 1 WS 协议契约（动作/帧/模式）
│   ├── p2_protocol.md               # Phase 2 gRPC 契约（vla.proto + 主线程约定）
│   └── p3_alignment.md              # Phase 3 tick/frame 对齐与性能基线
├── purpur-vla-plugin/               # Gradle 插件工程 → purpur-vla-plugin.jar
│   ├── build.gradle.kts             # paper-api + grpc(shadow) + shadowJar
│   └── src/main/java/dev/vla/purpur/
│       ├── VlaPlugin.java           # 主类 + 频道注册 + gRPC 启动/关闭
│       ├── grpc/VlaGrpcService.java          # gRPC 服务实现（§9.1 proto）
│       ├── grpc/MainThreadDispatcher.java    # gRPC→主线程调度器
│       ├── reset/RegionSnapshot.java         # L1 区域 BlockData 快照
│       ├── reset/ResetEngine.java            # L1 回滚 / L2 FAWE / L3 Structure
│       ├── player/AgentManager.java          # 离线 UUID 玩家管理 + 死亡重生
│       ├── task/TaskManager.java             # 任务注册表 + EpisodeState
│       ├── task/Predicates.java              # 判定器（inventory/block/entity/player_at）
│       ├── task/Curriculum.java              # 自动课程（P4）
│       ├── path/AStar.java                   # 3D A* 寻路
│       └── world/VoxelReader.java            # NMS 体素读取
├── fabric-vla-client/               # Fabric Loom 工程 → fabric-vla-client.jar
│   ├── build.gradle.kts             # loom + fabric-api + java-websocket
│   └── src/main/java/dev/vla/client/
│       ├── VlaClient.java           # 模式状态 + 原子动作缓冲
│       ├── net/WsServer.java        # 嵌入式 WS 服务器
│       ├── net/FrameSender.java     # 队列消费 + JPEG 编码 + 发送
│       ├── input/ActionApplier.java # KeyBinding 注入 + 视角插值
│       ├── mixin/KeyboardInputMixin.java / MouseMixin.java /
│       │        GameRendererMixin.java / PlayNetworkMixin.java
│       └── gfx/FrameGrabber.java    # FBO blit + PBO 异步读取
├── vla_env/                         # Python 包 → vla_env.py 入口 + 完整 SDK
│   ├── proto/vla.proto
│   └── vla_env/
│       ├── env.py / env/MinecraftVecEnv.py   # 单/并行 Gymnasium Env
│       ├── lockstep.py              # tick/frame 对齐断言
│       ├── server_grpc.py           # gRPC client
│       ├── client_ws.py             # WS client（动作/帧）
│       ├── action_space.py / obs.py # 动作/观测映射
│       ├── dataset/                 # episode_writer / export / interactor
│       └── scripts/                 # random_agent / collect_dataset /
│                                    #   collect_demo / train_loop
├── server/                          # Purpur 运行时目录（§13.3）✅ M0
│   ├── download.sh / eula.txt / server.properties / purpur.yml
│   ├── start.sh（JDK21 + FIFO 控制台）/ freeze_gamerules.sh
│   ├── plugins/purpur-vla-plugin.jar（P2 加入）
│   └── world/ / logs/（gitignore）
├── tools/agents/                    # 多 agent 连通性冒烟（M0）
│   ├── package.json / bot.mjs       # mineflayer 无头 bot：`node bot.mjs --count N --prefix agent`
└── data_pipeline/                   # 采集/导出/演示录制脚本
```

**最终交付物**：
1. `purpur-vla-plugin.jar` —— 世界控制、任务、奖励、路径、体素后端。
2. `fabric-vla-client.jar` —— 输入隔离、抓帧、动作执行前端。
3. `vla_env`（含 `vla_env.py` 入口）—— 标准 Gymnasium 封装，支持 `SubprocVecEnv`。
4. `data_pipeline/` —— 人类演示录制 + VLA 自动 Rollout 数据存储与导出。

---

## 16. 关键工程决策与风险

| 决策 | 理由 | 风险与缓解 |
|---|---|---|
| Purpur（而非原版/Spigot） | Paper API（`getCurrentTick`、异步区块）+ 高可配 + 现代版本 | 与 Fabric 客户端共享 MC 版本基线；插件 API 随版本演进 |
| Fabric（而非 Forge） | Mixin 标准、构建轻、1.20.1 生态成熟 | 部分类名随版本漂移 → 锁版本 + mapping 工具核对 |
| 双通道（gRPC + WS） | 结构化 RPC 与高频帧流各司其职 | 双通道时序对齐成本 → lockstep 单一驱动（§9.3） |
| server-authoritative step | 杜绝网络错位读 0 奖励 | step 阻塞等待 → 吞吐受限，用并行 env 补偿 |
| PBO 异步抓帧 | 抓帧 <1ms，不掉 FPS | GL 版本要求（1.20 环境满足）；失败 fallback 同步读取 |
| L1 内存快照回滚 | 重置快、不卸载区块 | 大区域内存占用 → L2 FAWE 升级路径 |
| 单服务器多玩家 | 资源复用，共享世界 | 玩家间干扰 → 任务区域隔离 + 实体清理 |
| 离线模式 | 本地训练无需正版账号 | 集群部署注意账号策略 |

### 里程碑

> 里程碑骨架（M0-M12：ID/阶段/依赖/任务/Exit/状态 + DAG）见 **§13.10**。
> 当前状态（2026-08-06）：**M0-M8 全部 ✅**（记录见 §13.3-13.6）；剩 **M9（性能与并行）**、**M10（数据管线）**、**M11（人类演示）**、**M12（VLA 闭环）**。

---

## 17. M11.5 架构重构：客户端为手、Python 为脑、服务端为世界与裁判（2026-08-09）

> **触发**：`demo_human.py --task dig_stone --seed 42`（2026-08-09 14:31）失败复盘——
> 400 步 progress=0.0、玩家原地卡死 ~1 格位移、`actions.jsonl` 400 行全空动作、
> 对齐断言 78 次 `key_event_future_frame` 违规。
> 需求（M11 主线）：**无人演示条件下**用世界真实坐标规划路径、模拟人类真实按键执行，
> 录制 frame↔action↔state 严格对齐的数据；同一接口供未来 VLA 推理闭环（act→frame）。

### 17.1 失败根因（四层叠加）

| # | 根因 | 证据 |
|---|---|---|
| 1 | **双手互搏**：`SimHuman`（Python 逐 tick 遥控）与客户端 `NavExecutor` 都在写 `currentAction`——SimHuman 走 `_nav_fallback` 后只发空动作，真实按键全部来自客户端注入 | `actions.jsonl` 全空（attack=0/forward=0），`keys.jsonl` 却有攻击按下/抬起循环 |
| 2 | **Python 逐 tick 遥控在物理上不可行**：一个 step = 2 tick + gRPC/WS RTT ≈ 5-10 tick，瞄准/步态/跳跃窗口永远差半拍（与 §5.7 pillar 下沉客户端的论证同构） | 卡死 400 步；§5.7 已论证过一次 |
| 3 | **两条上行通道乱序**：key_event（文本）可先于其归属帧（二进制）到达录制线程 | 78 次 `key_event_future_frame`，且 fid 只超前 1-2 帧 |
| 4 | **服务端直线规划失败即放弃**：`DirectPathPlanner` LOS 不通 → found=false → Python 全程退化 `goto_path` 双航点，客户端 24 格半径外无全局引导；卡死后上层无技能决策（该垫方块还是该挖阶梯） | compute_path 无粗航点输出 |

### 17.2 重构原则（职责三分）

```
Python 编排层（脑）      选目标 / 建挖块计划(带工具) / 请求粗航点 / 技能决策(goto|pillar|近身挖|换目标)
                        / 录制与对齐 / episode 编排。不逐 tick 遥控。
   │ gRPC(任务/奖励/航点/体素/重置)         │ WS(goto_path+dig / pillar_up / action / 事件)
Purpur 服务端（世界+裁判） 世界权威 / 任务判定 / 奖励(含过度挖掘惩罚) / 粗航点(直线采样+落地吸附)
                        / 确定性重置(含自定义出生点)
Fabric 客户端（手）      唯一的 per-tick 按键合成者：NavExecutor(跟航点/局部A*/按计划挖/自动选工具)
                        + PillarExecutor(垫方块) + Humanizer(人类化整形滤波) + 帧头按键采样
```

- **每 tick 的按键合成只发生在客户端**。人类化（步态微松/挖掘节奏/瞄准限速/镜头微漂）
  是客户端执行器输出上的**整形滤波器**（`nav/Humanizer.java`），seed 确定可复现
  （WS `set_humanize {enabled, seed}`）。外部 VLA 直发的 `action` **不整形**（模型输出必须原样执行）。
- **数据对齐唯一真值 = 帧头按键状态**（23B 头，帧采集时刻采样 → 帧↔按键按构造对齐）；
  `key_event` 给精确按下/抬起时刻；`actions.jsonl` 记录 Python 语义编排指令（goto/pillar/dig_at…）。
  三层各司其职，训练对 (frame, keys) 取自 `trajectory.jsonl`。
- **奖励/进度仍只信服务端**（§14.2 不变）。

### 17.3 用户五难点 → 设计对照

| 难点 | 方案 |
|---|---|
| ① VLA 输出空间↔按键（长短按/左右键/鼠标滑动） | tick 级**电平流**：11 键 bool + camera Δ(度) + hotbar，长短按 = 电平流中连续 1 的长度（VPT/MineRL 同构，无需显式时长 token）；鼠标滑动 = camera Δ 序列（训练可 121-bin 量化，`action_space.encode/decode`）；左右键长短按 = attack/use 电平。推理入口 `SeedReplayApi.step()/step_discrete()`。事件流 `key_event` 记录精确按/抬时刻供 IDM/分析 |
| ② 框架兼容/插件式扩展 | 服务端任务 data-driven：`plugins/VlaPlugin/tasks/*.json` 定义 TaskSpec（`vla reloadtasks` 热载）；Python `tasks.py` 统一任务 profile（目标块/工具表/供给器）；客户端技能是可组合原语（goto_path/pillar_up/…新技能=新 WS cmd+Executor），VLA 可只输出按键也可混用语义技能 |
| ③ 出生点/定位/过度挖掘惩罚/避障避坑/脱困/预判跳跃 | ResetRequest 新增 spawn 字段（服务端 `ResetSpec.spawn` 已有，本次打通 gRPC）；定位 = `GetState.pos` + `relative_pos` + 体素；**过度挖掘惩罚** = TaskSpec.digPenalty，服务端对每块非目标挖掘扣 reward 并在 info 报 `mined_offtarget`（行为端 LocalPathfinder DIG_COST=20 已抑制）；避坑 = BlockTraits 四态（HAZARD 岩浆/火/仙人掌拒绝、水加价 3.0、MAX_FALL=3）；**脱困决策树**（Python）：客户端两级自愈（本地重规划≤3 → 原地挖前方≤2）→ 上报 STUCK → 目标高差 ≥2 且有泥土 → `pillar_up`，否则黑名单换目标；**阶梯式挖通道** = LocalPathfinder 新增 `dig_step_up` 边（挖台阶脚/头格再跳上，A* 自动出上行阶梯）；**预判跳跃** = 路径里的 step_up 边 + `shouldJumpNow` 台阶探测（不等卡住才跳） |
| ④ 按方块选工具 | 客户端 `BlockTraits.toolFor(BlockState)`（材质→pickaxe/axe/shovel/sword）+ NavExecutor 对**一切**挖掘自动选工具（规划器显式 tool 优先，未标注/卡死挖掘走自动）；Python 侧 `tasks.py TOOL_FOR_BLOCK` 单一来源 |
| ⑤ 服务端/客户端分层导航 | **服务端粗航点**：`CoarsePathPlanner`——LOS 通 → `[start,goal]`；不通 → 沿直线每 8 格采样、垂直吸附最近可站地面列（±8 搜索、邻列 ±2 兜底），输出途径点序列；**客户端**在相邻途径点间 LocalPathfinder（半径 24 A*：walk/step_up/fall≤3/dig-through/dig_step_up）局部实测绕障 + 逐 tick 执行。搜索效率：客户端数组块缓存每格一读 + 二叉堆 + 加权启发（1.1）；服务端粗航点 O(距离/8) 次列扫描，无全局 A* |

### 17.4 数据契约（M11.5 canonical，落盘于 `datasets/demo_human/<ep>/`）

| 文件 | 内容 | 对齐键 |
|---|---|---|
| `trajectory.jsonl` | 逐帧 `{frame_id, server_tick, wall_nanos, keys{11键+hotbar+camera Δ}}`（**训练对**） | frame_id（帧头自带，按构造对齐） |
| `frames/f_%06d.jpg` | 帧图像，与 trajectory 行序一一对应 | 行号 |
| `keys.jsonl` | 离散按/抬事件 `{key, down, tick, wall_nanos, frame_id}` | tick + frame_id |
| `actions.jsonl` | Python 语义编排流 `{step, kind: goto_path\|pillar_up\|dig_at\|attack_burst\|place_use\|idle, args…}` | step + server_tick |
| `state.jsonl` | 每 step 服务端状态快照 + progress | server_tick |
| `align_assertions.jsonl` | 违规记录（帧号单调 / 事件归属帧超窗），乱序事件先入 pending 缓冲、帧到即冲销 | — |
| `meta.json` / `episode_summary.json` | 种子/kit/版本/渲染 + 统计 | — |

回放：`demo_human.py --replay <ep>` 从 `keys.jsonl`（tick 级按/抬）+ `trajectory.jsonl`
（camera Δ 按 tick 聚合）重建 tick 级动作序列，同 seed reset 后逐 tick 重放。

### 17.5 实施清单

- 服务端：`path/CoarsePathPlanner`（替换 ComputePath 内直线占位）；`TaskSpec+digPenalty`、
  `TaskManager` 全量挖掘计数+离目标惩罚+info 计数；`TaskRegistry.loadFromDir`（JSON 任务）+
  `vla reloadtasks`；proto `ResetRequest.spawn_*` 打通 `ResetSpec.spawn`。
- 客户端：`nav/Humanizer`（整形滤波，WS `set_humanize`）；`BlockTraits.toolFor` +
  NavExecutor 全路径自动选工具；`LocalPathfinder` 新增 `dig_step_up` 边。
- Python：`tasks.py`（统一 profile）；`orchestrator.py KitAgent`（编排层，替代 SimHuman 的
  遥控职责；SimHuman 退役）；`human_recorder` 修乱序竞争 + 语义动作流；`demo_human.py`
  切换编排器 + keys 重放；`interact.SeedReplayApi.reset(spawn=…)`。

### 17.6 附带修复的四个存量 bug（复盘/回放校验/追击失败排查时发现）

1. **LocalPathfinder step_up 边恒不可行**：边条件同时要求 `hasGround(nx,cy,nz)`（台阶实心）
   与 `isPassable(nx,cy,nz)`（同格非实心），恒矛盾 → 局部 A* 从未规划出「跳上台阶」，
   丘陵地形只能绕/挖/摔。修复：第三条件改查**当前列**起跳净空 `isPassable(cx,cy+2,cz)`。
2. **起点站在草丛/花里被判「不可站」**：起点校验用裸 `isAir()/isSolid()`（非 BlockTraits
   口径），草原地图玩家脚格常含 grass → 返回空路径 → 局部规划整体失效。修复：改用与
   搜索一致的 `canStandAt`（BlockTraits 四态），且不再要求脚下有地面（起跳/下落中可规划）。
3. **帧头相机增量被流控丢帧连带丢失**：FrameGrabber 按**渲染帧**差分，FrameSender 30fps
   流控丢帧时转角一起丢——实测 look_at 下压 54° 只有 0.3° 进数据（yaw 慢转损失小、pitch
   短促快转损失最重），录出的数据训练视角头会系统性学不到下压。修复：抓帧记绝对角，
   FrameSender 在发送时对上一个**实际发出**帧差分（yaw 最短角差）→ ∑Δ 严格 = 终态−初态。
   回放校验：修复后 ∑pitchΔ=69.0° 与终态 69.0° 精确闭合。
4. **NavExecutor 航点推进对起点航点错位零容忍 → 掉头振荡**：推进判据只有「3D 距航点
   中心 <0.8」；调用方用 Python `int()` 截断坐标（负坐标错一格，`int(-29.7)=-29` 应为
   floor `-30`）时玩家永远进不了起点航点的 0.8 圈 → idx 卡 0，1.5 格 lookahead 一失效
   `swp` 就回落到**起点航点** → 客户端算出一条从目标折回起点的本地路径（活体探针复现：
   走到目标附近被拖回起点，8.5s 才蹭到 arrived）——kill 追击永远差一步、上午 400 步
   原地卡死均源于此。修复：① NavExecutor 增加**越段推进**判据（到下一航点的距离已小于
   两航点段长 → 当前航点视为已越过，不回踩）；② `task_runner.py` 的 `int()` 全部改
   `math.floor`（orchestrator 一开始就用 floor）。回归探针：同样的截断起点 2.4s 直达。

### 17.7 验收记录（2026-08-09，全部在本机全栈实测）

- 构建：`purpur-vla-plugin` / `fabric-vla-client` gradle build ✅；Python 全模块 py_compile ✅；
  proto spawn 字段双端生成 ✅。
- data-driven 任务：`plugins/vla-purpur/tasks/collect_sand.json` → `vla reloadtasks` →
  `loaded 1 JSON task(s), 7 total` ✅（示例存 `purpur-vla-plugin/examples/tasks/`）。
- 端到端四任务（kit：镐/剑/铲/泥土；最终构建回归，全部 progress=1.00、align_violations=0）：
  - `dig_stone seed=42` → `HUMAN_DEMO_OK steps=310 frames=1217 key_events=140`
    （重构前同一验收：progress=0.0、78 次对齐违规、玩家原地卡死）
  - `dig_dirt seed=7` → `HUMAN_DEMO_OK steps=70`（铲，自然泥土）
  - `place_dirt seed=7` → `HUMAN_DEMO_OK steps=39`（use 脉冲放置）
  - `kill_animal seed=11` → `HUMAN_DEMO_OK steps=489 progress=1.00`（sprint 追击 +
    边追边打 + 冷却间隙追身；§17.6-4 修复前恒 0.5——受惊猪 1.25× 速度追不上/掉头振荡。
    另：编排器带目标补给兜底——惊逃坠亡的猪不给击杀计数，episode 会不可完成）
- 相机对齐（§17.6-3 修复后）：`∑pitchΔ=69.0° == 终态 69.0°`；`∑yawΔ=258.8° ≡ -101.3°
  (mod 360)` —— 帧头增量流积分严格闭合。
- **种子回放**：`--replay`（keys.jsonl 按/抬事件展开 tick 电平 + trajectory 相机增量按
  tick 聚合）机械重放 100% 可靠（860/860 动作全部注入、帧全程返回）；**语义级复现是
  尽力而为**——同一 episode 两次回放一次 progress=1.00（8 块石头全部重挖到）、一次
  0.38：开环重放对物理模拟是刀尖平衡（渲染帧时序影响准星更新与挖掘判定），任何一次
  分叉后不再收敛。世界级确定性仍由 `verify_determinism`（同 seed 区域 checksum +
  体素指纹一致）保证；训练数据的正确性不依赖回放（训练对来自帧头按构造对齐）。
- 踩坑：`fabric-vla-client/run/mods/` 残留旧构建 jar 会**静默顶掉** dev classpath 新代码
  （表现为新 WS 命令 unknown cmd）——run/mods 只放第三方 mod（已记入 CODEBUDDY.md）。

### 17.8 用户反馈修复轮（2026-08-09 晚）：工具竞争 + 出剑门控 + 寻路重试强化

**反馈①「杀猪不用剑」**（数据实锤：kill 攻击帧大多持铲/镐，最好一轮仅 24/200 帧持剑；
dig/place 类经帧头 hotbar 校验本来就正确）。根因是**选槽竞争**：Python 发 `goto_cancel`
后紧跟 `action{hotbar=剑}`——cancel 的按键清空经 `client.execute` 调度执行，晚于 WS 线程
写入的 hotbar 动作，老代码 `currentAction.set(new ActionCmd())` 把选槽整个吞掉。修复：

- 客户端全部 cancel/结束路径改 `releaseLevels()`——释放电平键（移动/attack/use）但
  **保留未消费的一次性字段**（hotbar/camera/drop/inventory）；
- 编排器 kill 流程 cancel 后先泵一步再选剑（保序双保险）。

**反馈②「超距乱挥剑 / 剑砍到方块」**。根因：出剑只看服务端坐标距离，不看准星实际套住
什么——猪一侧移准星就落在它身前的方块上。修复：**出剑门控**——客户端 `state` 上行新增
`aimed_entity/aimed_entity_dist`（crosshairTarget 实体命中），编排器只在准星实际套住目标
实体且 ≤3.0 米时才挥击，否则只贴身。验收（kill seed=11）：**209 步完成（修复前全绿轮
489 步，2.3×）；全程仅 4 次出剑、帧头校验 100% 持剑、每剑都带准星实锤距离 1.1-1.7m**
——满充能一剑一确认，无空挥、无剑刨方块。

**反馈③「路径重试多次应换路/补落脚点」**。三层强化：

| 层 | 机制 |
|---|---|
| LocalPathfinder | ① `avoid` 避让集软加价（AVOID_COST=15）：重规划时绕开失败走廊（撞墙块+卡死脚位由 NavExecutor 累积传入）——重试真正「换一条路」而非原路重算；② 新增 `place_step` 边（PLACE_COST=12）：前方可站但脚下缺失（空气/水）且有可放置支撑（1 格深坑=下方实心，瞄顶面；1 格宽沟=对侧同层实心，瞄其朝沟侧面偏下点）→ 垫一块再走，`LocalResult.placeTargets` 输出 |
| NavExecutor | place 子模式（站定→选泥土→瞄支撑→settle 后 use 脉冲→校验实心，80 tick 放弃）；挖/放子模式加 4.0m 触达门控（超出先沿路走近，防站远处空挥到弃挖阈值）；避让集跨重规划累积、setPath/cancel 清空 |
| KitAgent | 脱困决策树头部加「横向绕行」：每目标一次经垂直中线偏移 ±6 格途径点的重走（`_goto(via=…)`），仍卡才进 pillar/黑名单梯队 |

place_step 实测注记：MC 中 1 格宽沟走路惯性即可跨越、浅水加价可涉——A* 作为成本最优
规划会先选这些**更便宜的合法路径**（合成沟壑测试均以绕行/惯性跨越到达），place_step
在它确实最便宜时（长深水面、宽平坑场）才被选中，属预期行为。

### 17.9 M11.6 操作丝滑度修复（2026-08-10）：冲刺滞回 + 视线工具策略

**反馈①「疾跑频繁开关」**。先澄清：疾跑键在键位层是持续长按的（`KeyboardInputMixin`
每 tick `sprintKey.setPressed` + `setSprinting`），抖动源是 **`cmd.sprint` 每 tick 被
硬阈值重算**——`NavExecutor` 旧逻辑 `h>4.0 && |yawErr|<20°` 无滞回：航点间距 8 格，
距航点 <4m 即关、下一航点 >4m 又开；局部路径点间距更小命中更频繁；kill 追击时航点是
猪脚位（在动），h 在 4.0 附近振荡 → 每几 tick 开关一次。叠加 Humanizer 步态微松
（30-60t 释放 2-3t）使闪烁更明显。

修复（`NavExecutor`）：**冲刺 latch + 死区**——`h>5 && |yawErr|<15°` 开启，
`h<3 || |yawErr|>30°` 熄灭，3-5m / 15-30° 区间保持现状。跨航点/局部路径点不再抖。
新路径 `setPath`/`cancel` 重置 latch。

**反馈②「用镐子攻击猪」**。根因：工具只在「挖穿子模式 / `_dig_at` / kill 爆发前 /
place 前」四个时刻切换，**正常行走/追击期间无策略** → 挖完石头一直持镐，追击猪时
视觉上"镐子追/打猪"（爆发前虽会选剑，但追击占 kill 绝大部分时间）。

修复三层：
1. **客户端视线工具策略 `nav/ToolPolicy`**（新类）：每 tick 按 `crosshairTarget`
   命中的第一个目标切工具，档位经 WS `set_tool_mode` 下发：
   - `auto`（dig 任务）：命中触及范围内（≤4.5m）可挖方块 → 按 `BlockTraits.toolFor`
     切对应工具；命中近战范围内（≤3m）活体实体 → 切剑；
   - `melee`（kill 任务）：无条件确保持剑——追击全程持剑，挖穿绕障时跳过（NavExecutor
     busy），挖完准星对回猪自动换回剑；
   - `none`（place 任务）：不干预（防 auto 把 dirt 槽换走）。
   防抖：只在手持不匹配时切换、切换冷却 5t、命中实体后 20t 保持窗口（防准星扫过
   草皮把剑换回铲）。挖穿/放置子模式与 pillar 进行中一律跳过（工具由技能决定）。
2. **kill 追击提前持剑**（`KitAgent._run_kill`）：确认目标后 `goto_cancel → pump →
   选剑 → goto`，不再只在爆发前选；爆发前 select 保留作保序兜底。
3. 工具选择逻辑收拢：`selectToolCategory` 从 NavExecutor 迁入 ToolPolicy（静态共享），
   NavExecutor 挖穿/放置子模式改调 ToolPolicy。

验收口径：`demo_human.py --task kill_animal` 帧头 hotbar 校验追击段 100% 持剑、
冲刺连续段无 <3m 死区抖动；`--task dig_stone` 准星命中石头自动持镐。

### 17.10 M11.6 第二轮修复（2026-08-10）：目标高亮 debug + 挖穿持对工具 + place 远目标

**反馈①「dig 目标来回跳，想可视化目标块」**。两层：
- 可视化：走 **gRPC ShowPath 服务端粒子**（红色 Dust + END_ROD 刷在目标方块，demo_task
  路径可视化同款机制）——编排器 `_run_dig`/`_run_place` 每轮选目标后
  `show_path(goal=<目标块>, lifetime=600t)`，无目标/收尾 `clear`。粒子由服务端刷进
  世界、渲染进抓帧画面，**录制视频可见**。（先试过客户端 overlay：GPU 直绘画进
  consumer 会因 LAST 早于 endBatch flush 进不了抓帧；CPU 投影又受相机矩阵/视角效果
  影响对不准——最终弃用，统一走粒子。）
- 根因修复（实测 progress=0、玩家全程在高地 y≈70、目标在谷底 y=65-67）：客户端
  LocalPathfinder **只能 fall≤3 / step_up≤1**，而 `_select_target` 对高度几乎无惩罚
  （排序 `hdist²+2|dy|` 让水平近但低 5 格的目标胜出）→ 每轮 STUCK → 横向绕行（同
  高度无效）→ 换目标，原地来回跳。修复：`_select_target` 候选分两档——「可达高度
  |dy|≤2」优先，全部不可达才放宽到 |dy|≤6；排序高度惩罚提到 4；`_on_stuck` 对目标
  低于玩家 ≥2 格（nav 下不去）直接黑名单、跳过无效的横向绕行。配套：供给计数
  `_count_reachable` 窗口收紧到 |dy|≤3。修复后 dig_stone 154 步 progress=1.00
  （失败跑 1012 步 progress=0.00）。

**反馈②「kill 追击第 2 头猪时用剑挖泥土」**。根因：`NavExecutor` 本地挖穿（section 1.5）
与卡死脱困挖掘（stuck-dig）在**设置 digTarget 的同一 tick 直接返回 attack 动作、未切工具**
——MELEE 档全程持剑，于是第一剑以剑挥在泥土上（下一 tick section 0 才切铲，但挥剑帧已
进录像）。修复：抽 `selectDigTool(player, plan)`（规划工具标注优先、否则按挖掘 tag），
在 **digTarget 首次设置的 tick 就地调用**（1.5 / stuck-dig / section 0 三处共用）——
首次挥击即用铲/镐，MELEE 只保证追击/攻击段持剑，挖穿段由方块决定工具。实测 kill 攻击
帧 hotbar 分布：sword=15（打猪）+ shovel=169（挖土全用铲），无剑挖土。

**反馈③「place_dirt 放置无逻辑，应设较远目标」**。旧 `_run_place` 永远放在面前 2 格
（"走到哪放到哪"）。重写：`_select_place_spot` 体素扫描选**偏好 ~6 格（4-8m）**的
可放置地面格（实心地面 + 上方 1 格 air + 高差 ≤3 + 非黑名单）→ debug 高亮放置格 →
`_goto` 停在其 ~2 格处的停靠格 → 选 dirt 槽 → 瞄地面顶面 use 脉冲 → 服务端权威校验，
失败（射线被挡/站位不佳）黑名单换目标。实测 3 次放置分别落在 4-8 格外的目标格。

---

## 附录 A：术语映射

| 本方案 | Luanti 旧方案 | 说明 |
|---|---|---|
| `server_tick`（Paper `getCurrentTick`） | `server_tick`（`register_globalstep`） | 20Hz 权威时钟 |
| `ResetEngine` | `reset.lua` | 软重置：区域回滚 + 玩家态 + gamerule 冻结 |
| `TaskManager` + 判定器 | `task.lua` + predicates | 任务 schema 与判定器语义一致 |
| `GetVoxels` | `state.world.voxels` | 局部体素矩阵 |
| `A* → waypoints` | `core.find_path` | 语义 `goto` 的路径来源 |
| `HUMAN_MODE` 录制 | `record.lua` 请求式采样 | 人类演示数据 |
| canonical episode 格式 | 同一格式 | 导出器复用 |

---

## 附录 B：与 MineRL/MineStudio 数据字段对齐

| 本方案字段 | MineStudio/MineDojo 字段 | 说明 |
|---|---|---|
| `obs.pov` | `observation.pov` | 第一人称 RGB |
| `action.forward/back/...` | `action.buttons` | 按键组 |
| `action.camera` | `action.camera` | 121 bin / 连续 |
| `action.hotbar` | `action.hotbar` | 快捷栏 |
| `info.progress` | — | 稠密进度 |
| episode 结构 | `episode/segment` | RLDS 导出兼容 |
