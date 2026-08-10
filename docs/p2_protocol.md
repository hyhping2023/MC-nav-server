# P2 协议契约：Phase 2 gRPC（Python ↔ Purpur 服务端）

> 来源：DESIGN.md §4.2-4.7 / §9.1 / §13.2 / §13.5（P2）
> 本文件只收录 DESIGN.md 已定义的内容，作为 Phase 2 服务端世界引擎的实现契约（实现即文档）。
> 适用通道：**Python（P）↔ Purpur 服务端（S）**，结构化 RPC 专用。

---

## 1. 连接与端口

| 项 | 约定 | 来源 |
|---|---|---|
| 监听地址 | `127.0.0.1:50051`（`NettyServerBuilder.forAddress(new InetSocketAddress("127.0.0.1", 50051))`，仅绑回环） | §4.2 / §13.2 |
| 每服务器一个 | 环境变量 `VLA_GRPC_PORT` | §13.2 |
| 多服务器模式 | 动态分配服务端端口（`25565+n`）与 WS 端口，`SubprocVecEnv` 拉起 N 个 env worker | §6.4 |
| Python 依赖 | `grpcio` + `grpc-tools`（1.62.x）；proto 由 `vla_env/proto/vla.proto` 用 `grpc_tools.protoc` 生成 | §6.1 / §13.1 |

---

## 2. vla.proto 服务方法清单（§9.1）

> 完整定义见 `vla_env/proto/vla.proto`（与插件共享的 protobuf 定义，§6.1）。

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
  rpc SetBlock(SetBlockRequest) returns (Void);
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

| RPC | 语义 | 对应组件 |
|---|---|---|
| `ResetWorld` | 重置世界与任务（`ResetWorld(task, seed, region)`） | ResetEngine |
| `GetStepResult` | 阻塞等待 k ticks 结算，返回 reward/done/权威 `server_tick` | TaskManager |
| `GetState` | 玩家/全局状态查询 | 状态接口 |
| `GetVoxels` | 32³ 局部体素矩阵 | VoxelReader |
| `ComputePath` | A* 寻路输出航点 | AStar |
| `SetTask` | 下发任务 | TaskManager |
| `GenerateTask` | 课程/LLM 生成任务 | TaskManager / Curriculum |
| `ClearRegion` / `Teleport` / `SpawnEntity` / `SetBlock` | God Mode 世界控制 | ResetEngine |

> Python 侧完整封装见 `server_grpc.py`（全 RPC 封装，P2.7）。

---

## 3. 主线程调度约定（§4.2，关键约束）

**Minecraft 服务端是单主线程模型**：除渲染外，所有世界读写必须在主线程执行，否则导致区块竞态、TPS 抖动甚至崩溃。

- gRPC Server 用独立线程运行；**只读**查询（体素矩阵、路径规划）可放 gRPC 线程用 NMS 直接读区块（无写则安全），写操作一律主线程。
- 写操作经 `Bukkit.getScheduler().runTask(plugin, ...)` 调度回主线程，gRPC 线程可安全返回结果。
- 后续若要 Folia（区域并行线程）支持：`runTask` 需替换为区域调度，Phase 4 再考虑，默认**不用 Folia**。
- 服务端 tick 权威源：`Bukkit.getCurrentTick()`（§9.3 / §16）。

示例（§4.2）：

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

---

## 4. ResetEngine 对外接口（§4.3-4.4，God Mode）

全部主线程。能力与 Paper/Bukkit API 映射：

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

确定性配置（数据采集期强制）：`doDaylightCycle=false`、`doWeatherCycle=false`、`doMobSpawning=false`、`mobGriefing=false`、`randomTickSpeed=0`、`playersSleepingPercentage` 无关、视野内方块更新恒定。

区域回滚三档策略（重置 32×32×32 或更大区域，**不卸载区块、不重生成地图**）：

| 策略 | 实现 | 适用 |
|---|---|---|
| **L1 内存快照（默认）** | 首次 `ResetWorld` 记录区域内每格 `BlockData`（或 `ChunkSnapshot`），存 `Map<ChunkPos, ChunkSnapshot>`；回滚 `world.getBlockAt(x,y,z).setBlockData(data, false)`（`applyPhysics=false`），32768 格 <1s | episode 内/间快速重置 |
| **L2 异步批量（FAWE）** | FastAsyncWorldEdit API，异步区域 set 与 schematic 保存/加载，不卡主线程 | 大区域/复杂地形/逐 bit 还原 |
| **L3 Structure NBT** | 原版 Structure（`StructureTemplate`）保存任意形状结构，NMS 快速粘贴 | 任务要求特定建筑初始态 |

> 方块回滚必须**关闭物理更新**（`setBlockData(data,false)`），否则 TNT/火把/红石连锁更新导致 TPS 崩。

---

## 5. TaskManager 对外接口（§4.7）

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

内置判定器（监听事件驱动 + 周期校验）：

| 判定器 | 触发事件 | 说明 |
|---|---|---|
| `inventory_contains` | 物品变更 | 背包内物品 ≥ N |
| `block_placed` / `block_mined` | `BlockPlaceEvent` / `BlockBreakEvent` | 区域内方块体积/数量统计 |
| `entity_killed` | `EntityDeathEvent` | 指定实体击杀 ≥ N |
| `player_at` | `PlayerMoveEvent` | 距目标 < tol |
| `stat_reached` | `PlayerStatisticIncrementEvent` | 统计值 ≥ 阈值 |
| `custom` | — | 插件注册回调 |

奖励设计：`reward = per_step*0(默认0) + on_progress*Δprogress + success_bonus + timeout_penalty`。**奖励以服务端事件为准**，绝不用客户端观测（幽灵方块避坑，§14.6）。

---

## 6. Voxel 矩阵接口（§4.6）

VLA 辅助 state / reward 计算用，32×32×32（可配置半径）：

- 读取走 **NMS**（远快于 Bukkit Block 对象）：`((CraftWorld) world).getHandle().getBlockState(new BlockPos(x,y,z))`。
- 返回格式：`{palette: [block_id...], data: int[]}`（局部块 id 索引）或直接 `BlockData` 名称数组，Python 侧重建为 numpy 3D 矩阵。
- 附带语义信息：`aimed_block`、`nearby_entities`、`items_on_ground`。

---

## 7. A* 寻路对外接口（§4.5）

服务端输出 **Waypoints**（航点列表）作为观测/指令，供 VLA 参考与 reward 判定：

- **动作级 3D A\***（NavV2）：XZ 8 方向 × 垂直多档，方块分类 PASSABLE/BREAKABLE/UNBREAKABLE
  （`Material.getHardness()` 分级），动作集 walk/jump/fall/dig/dig_down/place（cost_mode 门控）；
  破块距离惩罚（远离目标挖穿贵→绕路）；reach-aware 目标微调（采集距离 ~2 格）。
- 备选：复用原版寻路（NMS `PathFinder` + `WalkNodeEvaluator`），需 NMS 访问；或引入第三方 Bukkit 寻路库。
- 外部接入：`ComputePath(from, to, cost_mode) → PathReply{waypoints[], found, details[]}`，
  `details` = 动作级航点 `{pos, action, target}`（action ∈ walk|jump|fall|dig|dig_down|place），
  Python 执行器按动作分派执行（移动/挖穿/下挖/垫方块），可再喂给 VLA 作为语言/航点指令。

### 7.1 M11.5 粗航点语义（`CoarsePathPlanner`，两层导航，DESIGN.md §17.3 难点⑤）

全局 A* 退役后 `ComputePath` 的现行语义：

1. **直线可达**（LOS，脚+头双格 0.5 格采样）→ `waypoints=[start, goal]`、`found=true`；
2. **直线被挡** → 沿 start→goal 连线每 8 格采样一列，垂直方向在插值高度 ±8 内吸附最近
   可站格（脚+头可通行、脚下实心非危险），该列不可站向邻列（±1、±2 环）借位，仍不可站
   跳过该点（空档交客户端 LocalPathfinder）→ `waypoints=[start, wp₁…, goal′]`、`found=true`；
   `goal′` = 目标 3D 邻域（水平 ±3、垂直 -4..+4）最近可站格；
3. **目标邻域无可站格** → `found=false`（调用方黑名单/游走兜底）。

`details` 恒空；`cost_mode` 保留但等价。客户端在相邻途径点间做半径 24 的局部 A*
（walk / step_up / fall≤3 / dig-through / dig_step_up），逐 tick 执行。

## 7.2 M11.5 其余 gRPC 契约变更

| 项 | 变更 |
|---|---|
| `ResetRequest` | 新增 `has_spawn/spawn_x/spawn_y/spawn_z/spawn_yaw`（字段 9-13）：自定义出生点（难点③）。`has_spawn=true` 时传送至该点；region 未显式给出时以 spawn 为中心做基线快照 |
| `StepReply.info` | 新增 `mined_total` / `mined_offtarget`（本 episode 全部/非目标挖掘计数，字符串编码） |
| 过度挖掘惩罚 | `TaskSpec.digPenalty`（内置采集任务默认 0.05）：每挖一块非目标方块 `rewardSinceLastStep -= digPenalty`（server-authoritative，难点③）；挖目标块永不惩罚 |
| data-driven 任务 | `plugins/VlaPlugin/tasks/*.json`（schema 见 `TaskRegistry` 类注释）onEnable 加载；`vla reloadtasks` 热重载；同 id 覆盖内置（难点②） |

---

## 8. 实现进度（Progress）

### 8.1 M0 当前状态

> **M0：骨架占位，尚未实现。**

- 工程目录 `server/`、`purpur-vla-plugin/` 已创建（内容为空）。
- 本文件（任务 0.5「协议文档骨架」交付物之一）为 M0 已完成的唯一产出。
- gRPC 服务端（`VlaPlugin.java`、`VlaGrpcService.java`、`MainThreadDispatcher.java`）、proto 生成、`server_grpc.py` 均**未开始**。

### 8.2 随 M4-M7 填充项

| 里程碑 | 任务 | 交付物 | 填充内容 |
|---|---|---|---|
| M4 世界引擎 | P2.3 / P2.4 | `reset/RegionSnapshot.java`、`reset/ResetEngine.java`、`player/AgentManager.java` | L1 快照回滚、玩家态/背包/时间/实体重置、离线 UUID 稳定映射、死亡重生；Exit：两次 reset 体素一致 |
| M5 任务系统 | P2.5 | `task/TaskManager.java`、`task/Predicates.java` | TaskSpec 解析、事件监听判定器、EpisodeState 进度/超时/结算；Exit：`collect_wood` 判定 success=true、reward 来自服务端事件 |
| M6 状态与寻路 | P2.6 | `world/VoxelReader.java`、`path/AStar.java` | NMS 体素 palette 编码、3D 8 方向 A* 航点；Exit：`GetVoxels` 与实景一致、`ComputePath` 输出可达航点 |
| M7 Env 闭环 | P2.7 | `server_grpc.py`、`env.py` | 全 RPC 封装、`reset/step` 打通（先同步抓帧）、`GetStepResult` 阻塞 k ticks；Exit：`env.reset/step` 端到端跑通 `collect_wood` |

> gRPC 与 WS 两线在 M7 汇合（§13.0/§13.10：M2/M3 客户端线 ∥ M4/M5/M6 服务端线，M7 汇合）。
> 状态约定沿用 §13.10：⬜ 待开始 · 🔄 进行中 · ✅ 完成 · ⚠️ 阻塞。当前 M4-M7 均为 ⬜。
