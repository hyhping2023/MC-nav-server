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

- **首选：自研 3D A\***（确定性、可控成本表）：8 方向、每格成本由方块类型表决定（空气=1、可跳跃=2、岩浆/水=∞），支持跳跃/潜行标记。输出 `List<BlockPos>` 压缩为拐点序列。
- 备选：复用原版寻路（NMS `PathFinder` + `WalkNodeEvaluator`，Mojang 映射类名），需 NMS 访问；或引入第三方 Bukkit 寻路库。
- 外部接入：`ComputePath(from, to, cost_mode) → PathReply{waypoints[]}`，Python 可再喂给 VLA 作为语言/航点指令。

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
| 视角（pitch/yaw） | Mixin `MouseHandler#turnPlayer`（Yarn: `Mouse`）HEAD cancel；每 tick 直接 `player.setYRot/setXRot` 平滑插值——**天然规避万向节死锁**（原版方法已限制 pitch∈[-90,90]、yaw 360° 环绕） |
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

- 抓帧挂在 `GameRendererMixin`（world 渲染之后、HUD 之前）→ 画面纯净（无准星/血条，避免污染像素观测；需要 HUD 信息的模型可用本地状态补）。
- 目标分辨率：默认 224×224（VLA 常用），窗口比例设为 1:1（或中心裁切）以**避免非等比拉伸导致 FOV 畸变**；`fov` 可配置（默认 70）。
- 帧率：客户端 60/120 FPS 渲染，采集端按 `record.fps`（默认 20，与 tick 对齐）降采样。
- **线程铁律**：所有 GL 调用只在渲染线程；数据交递用无锁队列（`ConcurrentLinkedQueue` / MPSC ring buffer），编码与网络传输放后台线程。

### 5.6 帧与 tick 对齐（插件消息通道）

客户端无法直接读取服务端 tick，通过 **Plugin Messaging** 下行广播：

- 服务端每 tick（或每 N tick）向玩家发送 `vla:tick` 频道 payload（`int serverTick` + `long wallNanos`）。
- 客户端 `ClientPlayNetworking.registerGlobalReceiver` 接收，写入 `volatile long lastServerTick`。
- 每帧上行消息携带：`{frame_id, last_server_tick, render_wall_time}`，Python 据此做帧↔tick 对齐（§9.3）。

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

- **Registry（静态）**：手工定义基础任务（`collect_wood` / `craft_planks` / `place_torch`…）。
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
| M3 | 客户端视觉 | P1 | M1, M2 | 1.4-1.6 | 随机策略驱动移动/转向/攻击；224² 帧 ≥20fps 上行且与动作 1:1 | ⬜ |
| M4 | 世界引擎 | P2 | M1 | 2.3-2.4 | 两次 reset 体素一致；玩家态/背包/时间/实体正确重置 | ✅ |
| M5 | 任务系统 | P2 | M4 | 2.5 | `collect_wood` 判定 success=true；reward 来自服务端事件 | ✅ |
| M6 | 状态与寻路 | P2 | M4 | 2.6 | `GetVoxels` 与实景一致；`ComputePath` 输出可达航点 | ✅ |
| M7 | Env 闭环 | P2 | M2, M3, M5, M6 | 2.7 | `env.reset/step` 端到端跑通 `collect_wood`（随机/脚本策略） | ⬜ |
| M8 | 数据对齐 | P3 | M7 | 3.1 | 10 episode 帧/状态/动作/奖励计数一致 + tick 断言 100% | ⬜ |
| M9 | 性能与并行 | P3 | M8 | 3.2-3.3 | 抓帧 <1ms；4 env 并行稳定 ≥1h | ⬜ |
| M10 | 数据管线 | P3+P4 | M8, M9 | 3.4, 4.2 | 首份可验证数据集 + WDS/HF/RLDS 导出冒烟 | ⬜ |
| M11 | 人类演示 | P4 | M3 | 4.1 | 人类演示数据与自动 rollout 同 canonical 格式 | ⬜ |
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
> 当前状态（2026-08-06）：**M0/M1/M2/M4/M5/M6 全部 ✅**（记录见 §13.3-13.5）；剩 **M3（客户端视觉）**、**M7（Env 闭环）**、**M8（数据对齐）**。

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
