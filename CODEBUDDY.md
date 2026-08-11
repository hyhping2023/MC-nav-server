# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

## 项目概览

MCPurpur-VLA：面向 VLA 模型训练与具身智能研究的 Minecraft 1.20.1 沙盒环境，三段式架构：

- **Purpur 服务端插件**（`purpur-vla-plugin/`）：世界权威引擎，gRPC（`127.0.0.1:50051`）+ `/vla` 命令 + `vla:tick` tick 广播。
- **Fabric 受控客户端**（`fabric-vla-client/`）：输入隔离 Mixin + 动作注入 + 抓帧（FBO/PBO）+ 内嵌 WS 服务器（`127.0.0.1:30001`）。
- **Python 控制中枢**（`vla_env/`）：`MinecraftEnv(gymnasium.Env)` + lockstep 对齐 + 数据采集脚本。

里程碑 M0–M8 全部验收通过（各阶段记录见 DESIGN.md §13.3–13.6），demo 录制（`demo_task.py`/`demo_record.py`，路径粒子可视化）与 **M11.5 架构重构（DESIGN.md §17，客户端为手/Python 为脑/服务端为世界与裁判，2026-08-09 全验收：`HUMAN_DEMO_OK` dig_stone/dig_dirt/place_dirt/kill_animal）**均已完成；剩 **M9**（PBO 异步抓帧 + 并行采样）、**M10**（数据管线/导出）、**M12**（VLA 闭环）。

**单材质元世界 + 录制状态机**：每个 player 对每种地表材质维护独立持久化存档
`server/vla_surface_<surface>__<player>/`，元数据位于
`server/plugins/vla-purpur/surface-worlds/<player>/<surface>/world-meta.json`；
gRPC `SelectSurfaceWorld` / Python `surface=` 选择世界。并发录制使用
`record_coordinator.py` 分配 JSONL、`record_worker.py` 持久复用一个 Fabric client；
`map_seed` 只创建地图时使用，episode `seed` 控制目标和环境。采集采用
`MOVE_TO_STAND(move_only) -> DIG_BATCH -> PILLAR`：导航不攻击、不跳、不挖穿；到合法站位后
通过客户端准星距离门控批量挖可达目标；只在高处剩余目标不可从地面触及时脚底垫方块。完整
启动、并发、断点续录和回放说明见 `docs/demo_recording.md`。

## 权威文档（实现即文档，改协议/行为时同步更新）

- `DESIGN.md` — 总设计 + 协议 + 实施计划 + 里程碑状态 + **§14 避坑指南（必读）**
- `docs/p1_protocol.md` — WS 协议契约（动作/帧/模式）
- `docs/p2_protocol.md` — gRPC 契约（vla.proto + 主线程约定）
- `docs/p3_alignment.md` — tick/frame 对齐契约

## 构建与运行

### Purpur 服务端（`server/`）

```bash
bash server/download.sh          # 幂等下载 purpur.jar（Purpur 1.20.1）
bash server/start.sh             # 启动（JDK21 + FIFO 控制台 /tmp/vla_server_console）
bash server/freeze_gamerules.sh  # 冻结 gamerule（doDaylightCycle/doMobSpawning 等 + time set 6000）
```

- **常驻进程（服务端 / runClient / mineflayer bot）一律用 Bash 工具的 `run_in_background=true` 启动**。前台命令结束时工具会清理后台 job → Java 收到 SIGTERM 优雅关闭，表现为"服务端几秒后自动退出"，勿误判为 FIFO 幽灵 stop。
- FIFO 控制台命令**不带 `/` 前缀**：`echo "vla status" > /tmp/vla_server_console`（`/vla status` 报 Unknown command）。管道路径可用 `VLA_SERVER_PIPE` 覆盖。
- 插件部署：`cp purpur-vla-plugin/build/libs/vla-purpur.jar server/plugins/`，FIFO 发 `reload`。

### Purpur 插件（`purpur-vla-plugin/`）

```bash
cd purpur-vla-plugin && ./gradlew build   # 产出 build/libs/vla-purpur.jar（shadowJar，gRPC 已 relocate）
```

Java toolchain 21；paper-api 1.20.1 为 `compileOnly`；protobuf 插件从 `src/main/proto/vla.proto` 生成 Java 代码。

### Fabric 客户端（`fabric-vla-client/`）

```bash
cd fabric-vla-client && ./gradlew build       # 产出 build/libs/vla-client-0.1.0.jar
cd fabric-vla-client && ./gradlew runClient   # 启动 MC 客户端（autojoin 进服）
cd fabric-vla-client && ./gradlew genSources  # 生成 Yarn 源码，改 Mixin 前核实方法/字段名
```

- 版本锁定在 `gradle.properties`：MC 1.20.1 / Yarn `1.20.1+build.10` / Fabric Loader `0.16.10` / Fabric API `0.92.11+1.20.1`；`org.gradle.java.home` 指向 JDK21，编译 `options.release=17`。
- autojoin：`run/autojoin.txt` 两行 = `主机:端口` + 离线用户名（默认 `agent0`），通过 `ConnectScreen.connect` 编程式入服。
- **改 Mixin 后必须重建 jar 并重启整个客户端进程才生效**。

### Python（`vla_env/`）

```bash
cd vla_env && .venv/bin/python -c "import vla_env; print(vla_env.__version__)"
cd vla_env && bash scripts/gen_proto.sh       # 改 proto 后重新生成 gRPC stub
```

- venv 在 `vla_env/.venv`（可编辑安装）。**脚本必须在 `vla_env/` 目录内运行**：仓库根目录的 `vla_env/` 目录会遮蔽同名包命名空间，根目录运行 import 会失败或错包。
- 示例：`cd vla_env && .venv/bin/python scripts/collect_wood_agent.py`。

### 验收脚本（各打印 `M*_OK` 并 exit 0）

| 脚本 | 里程碑 | 输出 |
|---|---|---|
| `vla_env/scripts/ping_both.py` | M1 | `M1_PING_BOTH_OK` |
| `vla_env/scripts/random_agent.py` | M3 | `M3_OK frames=N` |
| `vla_env/scripts/collect_wood_agent.py` | M7 | `M7_COLLECT_WOOD_OK steps=N progress=1.00` |
| `vla_env/scripts/collect_episodes.py` | M8 | `M8_ALIGN_OK episodes=N align_rate=1.00` |
| `vla_env/scripts/demo_record.py` | demo | `DEMO_OK`（产出帧目录，ffmpeg 合成 mp4） |
| `vla_env/scripts/demo_task.py` | demo | `DEMO_OK`（按 `--task` 生成 collect_wood/collect_stone/kill_animal 三任务 demo，真实世界资源 + A* 路径粒子可视化，合成 mp4；`demo_dig_tree.py` 为 collect_wood 别名） |
| `vla_env/scripts/generate_oracle_dataset.py` | M11.5/Oracle | `ORACLE_GEN_OK episodes=N success=M align_rate=1.00`（批量 oracle 轨迹，DESIGN.md §11.5） |
| `vla_env/scripts/demo_human.py` | M11.5 | `HUMAN_DEMO_OK`（人类式按键录制：`--task dig_stone/dig_dirt/kill_animal/place_dirt/collect_wood`、`--surface` 选择 worker 专属单材质世界、`--ws-url/--grpc-port` 选择 endpoint、`--replay <ep>` 种子回放；输出 frame↔按键对齐 canonical 数据 + mp4，DESIGN.md §17.4） |
| `tools/ws_harness.sh [port]` | M1 | WS 协议独立 harness（不需 MC） |

其余实用脚本（无验收标记）：`task_runner.py`（规划-执行分层任务驱动）、`interact_demo.py`/`read_state.py`/`replay_check.py`/`spawn_check.py`/`unstick_test.py`（调试/回放校验）。

完整端到端流程：server（run_in_background）→ runClient（run_in_background）→ 运行验收脚本。

## 架构

### 一个 step 的数据流（lockstep，§9.3）

```
Python ──WS──► 客户端: action
客户端 ──渲染线程──► 帧入队 ──WS 线程 JPEG 编码──► Python: 23B 帧头 [4B frame_id][4B server_tick][8B wall_nanos][2B buttons][1B hotbar][2B yawΔ][2B pitchΔ][JPEG]
Python ──gRPC──► 服务端: GetStepResult（阻塞 await_ticks，权威 reward/done/progress/tick）
Python ──gRPC──► 服务端: GetState → build_obs
```

**核心不变式：reward/done 只信服务端事件（Server-Authoritative），客户端观测仅作视觉输入**（防幽灵方块错位，§14.2）。`step()` 阻塞等 gRPC 结算后才请求下一帧。

### 服务端插件（`dev.vla.purpur.*`）

- `VlaPlugin` — 主类：gRPC server（50051）+ `/vla status|reset|verify|task|taskinfo|voxels|path` 命令 + `vla:tick` 每 tick 广播（12B `[4B tick][8B wallNanos]`）。
- `grpc/MainThreadDispatcher` — **主线程纪律**：服务端单主线程模型，一切世界写操作经 `Bukkit.getScheduler().runTask` 回主线程；只读（体素/路径）可在 gRPC 线程直跑。
- `grpc/VlaGrpcService` — vla.proto 实现（player 不存在回 `FAILED_PRECONDITION`）。
- `reset/RegionSnapshot` + `reset/ResetEngine` — L1 内存快照回滚；回滚用 `Block#setBlockData(data,false)`（**关闭物理更新**，否则连锁更新崩 TPS）；首次 capture 缓存为基线保证确定性。
- `player/AgentManager` — 离线 UUID 稳定映射（`nameUUIDFromBytes("OfflinePlayer:"+name)`）、死亡自动重生、**跨重连持久化区域**（bot dig 会踢连接 → session 重建）。
- `task/TaskSpec|TaskRegistry|TaskManager|Predicates` — 任务 schema + 事件驱动判定器（`collect_wood`=block_mined×4 oak_log）。**M11.5**：`plugins/vla-purpur/tasks/*.json` data-driven 任务（`vla reloadtasks` 热载，同 id 覆盖内置，示例见 `purpur-vla-plugin/examples/tasks/`）；`TaskSpec.digPenalty` 过度挖掘惩罚（每挖一块非目标方块扣 reward，采集类内置默认 0.05）；`StepReply.info` 带 `mined_total/mined_offtarget`。
- `world/VoxelReader` — palette 编码体素（paper-api `getBlockData`，**非 NMS**）。
- `world/ControlledPlainsGenerator` — 首次创建 player 专属 surface world 时生成单材质受控平原：地表 `Y=63`，`Y>=64` 为空气；世界按 `surface + player` 隔离并持久复用。
- `world/ObjectPlacer` — 在玩家周围 `[6,12]` 格环带按 episode seed 放置树、石头或泥土目标；服务端 `SetTask` 每个 episode 只生成当前任务目标。
- **M11.7 地面保护**（`VlaPlugin.groundProtected`/`protectedSeaLevel`）：`FlattenWorld` 削平后海平面及以下方块不可破坏（BlockBreakEvent 拦截，只有生成的柱/树可挖），`KitAgent(protected_ground_y=…)` 在 Python 侧跳过这些格选目标。
- `path/DirectPathPlanner` + `path/CoarsePathPlanner` — **M11.5 两层导航**：直线可达 → `[start,goal]`；被挡 → 沿线每 8 格采样+落地吸附出途径点（垂直 ±8 吸附、邻列 ±2 借位）；目标邻域无可站格才 found=false。客户端在途径点间做局部 A*。

### Fabric 客户端（`dev.vla.client.*`）

- `VlaClient` — 入口：模式状态、`AtomicReference<ActionCmd>` 动作缓冲、`vla:tick` 接收（写 `volatile lastServerTick`）。
- `net/WsServer` — 内嵌 WS 服务器（30001，可 `vla.ws.port` 覆盖）。
- `net/FrameSender` — 后台线程 JPEG 编码（**必须 `TYPE_3BYTE_BGR`**，ARGB 带 alpha 会抛 "Bogus input colorspace"）+ 二进制帧头上行。
- `gfx/FrameGrabber` — 渲染线程抓帧：主 framebuffer `glBlitFramebuffer` 下采样 → `glReadPixels`。抓帧挂点：`WorldRenderEvents.LAST`（纯净画面，VLA 观测）或 `GameRenderer.render` TAIL（含 HUD，demo，经 `set_capture_ui` 切换）。
- `input/ActionApplier` + `ActionCmd` — 动作注入：移动/视角走 Mixin 覆盖，攻击/使用/热键走 `KeyBinding.setPressed`；攻击动作同时调用客户端原版 `doAttack`（避免瞄准后延迟一 tick）；视角为平滑插值转向（`maxTurnDeg`），`look_at` 用客户端自身眼位算精确朝向。
- `mixin/KeyboardInputMixin`（API 模式 cancel `KeyboardInput#tick` 写 movement 字段）、`MouseMixin`（cancel `Mouse#updateMouse` + lockCursor）、`MinecraftClientMixin`（API 模式阻止 GameMenuScreen 被 setScreen）、`MinecraftClientAccessor`（session 用户名覆盖 + `doAttack` invoker）、`GameRendererMixin`、`KeyboardMixin`（M11 HUMAN_MODE 透明记录真实键盘事件，`Keyboard#onKey`；API 模式跳过）。
- **M11.5 客户端为手（DESIGN.md §17）**：`nav/NavExecutor`（跟服务端航点 + `nav/LocalPathfinder` 半径 24 局部 A*：walk/step_up/fall≤3/dig-through/**dig_step_up 阶梯挖掘**；挖掘按 `nav/BlockTraits.toolFor` 挖掘 tag **自动选工具**且 **digTarget 首 tick 就地切工具**（`selectDigTool`，防 MELEE 下"剑挖土"，§17.10）；**冲刺 latch + 死区**（h>5&&yawErr<15° 开、h<3‖yawErr>30° 熄，防跨航点频繁开关，§17.9））、`nav/PillarExecutor`（垫方块爬高）、`nav/Humanizer`（人类化整形滤波：步态微松/挖掘节奏/镜头微漂，WS `set_humanize {enabled,seed}` 可复现；外部 VLA action 与 pillar 输出不整形）、`nav/ToolPolicy`（**M11.6 视线工具策略**：按 crosshair 命中切工具，WS `set_tool_mode` 三档 auto/melee/none——kill 任务 melee 全程持剑、dig 任务 auto 随准星换工具、place 任务 none；挖穿/放置子模式与 pillar 时跳过；防抖冷却 5t + 实体保持窗口 20t，§17.9）、`nav/Aim`（全客户端唯一瞄准角计算——同列目标 `h≈0` 时按 dy 符号给 ∓90°，修"挖头顶/朝下垫块永远瞄不中"）、`input/KeyRecorder`（按键事件 + 帧头 23B 按键状态采样——帧↔按键按构造对齐）。

**线程铁律**：GL 调用只在渲染线程；数据交递用 `ConcurrentLinkedQueue`；编码/网络放后台线程。

### Python（`vla_env/vla_env/`）

- `env.py` — `MinecraftEnv`：`reset`（ResetWorld→SetTask→mode api→收首帧→GetState）、`step`（WS action→收帧→GetStepResult→GetState）。
- `server_grpc.py` — 全部 RPC 封装（reset_world/set_task/get_step_result/get_state/get_voxels/compute_path/
teleport/spawn_entity/set_block/select_surface_world…）；`SelectSurfaceWorldReply` 返回
`world_name/worker_id/map_seed/metadata_path`。
- `client_ws.py` — `ClientWs` + `Frame`；`recv_frame_latest` 排空积压帧（客户端满帧率上行 vs 每 step 收一帧，TCP 缓冲堆积会阻塞客户端写）；`_recv_json` 跳过二进制帧。
- `lockstep.py` — `assert_step_alignment`（`0 <= server_tick - frame_tick <= ticks_per_step + tol` + 单调性）+ `Aligner`。
- `action_space.py` — buttons/hotbar/camera 映射；`random_action()` / `to_ws()`（**按键必须真布尔**，客户端 Gson `getAsBoolean` 把整数 1 解析为 false）。
- `obs.py` — `build_obs`。
- `keys.py` — 23B 帧头编解码（`HEADER_BYTES=23`、`decode_keys`）：帧↔按键**按构造对齐**（p1_protocol §2.3/§2.4）。
- `tasks.py` — 任务知识**单一来源**（M11.5）：`PROFILES`/`TOOL_FOR_BLOCK`（方块→工具）/`KIT_TOOL_SLOT`/`SURVIVAL_KIT`（镐/剑/铲/泥土/**M11.7 +斧**，hotbar 0-4）。新增任务 = 服务端加 tasks/*.json + 此处加 profile。
- `interact.py` — `SeedReplayApi`（M11）：`reset(seed, task, items)` 确定性重置 / `step` / `step_discrete`（VPT/STEVE-1 离散 token）/ `play_script` / `verify_determinism`。
- `orchestrator.py` — M11.5 `KitAgent` 任务编排层（§17.2「Python 为脑」）：体素选目标 → 粗航点（compute_path）+ 挖块计划（按 `tasks.TOOL_FOR_BLOCK` 选工具）→ 技能派发（goto_path/pillar_up/近战/放置）→ 脱困决策树。逐 tick 按键由客户端合成；**M11.6 按任务下发 `set_tool_mode`**（kill→melee 追击全程持剑、dig→auto、place→none）且 kill 追击前先选剑；**选目标后 `show_path(goal=…)` 服务端粒子高亮**（dig/place，录制可见，§17.10）；**`_select_target` 高度优先**（|dy|≤2 的 nav 可达目标优先，否则会选谷底石头原地来回跳，§17.10）；place 用 `_select_place_spot` 选偏好 ~6 格的远目标格；**M11.7 `protected_ground_y`**（受控扁平环境跳过受保护地面格）。
- `dataset/` — canonical 落盘（`schema.py` + `human_recorder.py`/`human_episode.py`/
`oracle_recorder.py`），`human_episode.py` 被单条 `demo_human.py` 和长期
`record_worker.py` 共用，M11.5 契约见 DESIGN.md §17.4。

### 端口规划

| 通道 | 端口 |
|---|---|
| gRPC（Python↔服务端） | `127.0.0.1:50051` |
| WS（Python↔客户端） | `127.0.0.1:30001 + env_idx` |
| MC 服务端 | `25565` |

## 关键约定与坑（踩过且已修复，勿重犯）

1. **Server-Authoritative**：reward/done 只信服务端事件；帧只作视觉输入（§14.2 幽灵方块）。
2. **主线程纪律**：gRPC 回调直接写世界 = 崩；写操作经 `runTask` 回主线程。
3. **Yarn 映射核实**：改 Mixin 前 `genSources` 查字段名——`Input` **无 `sprinting`**（疾跑经 `Entity#setSprinting`）、hotbar/drop/inventory 键是 `dropKey/inventoryKey`（非 keyDrop）、移动走 `Mouse#updateMouse()`。
4. **vla_env 脚本须在 `vla_env/` 目录内运行**（根目录 namespace 遮蔽）。
5. **FIFO 命令不带 `/` 前缀**。
6. **常驻进程必须 `run_in_background=true`**（见上文，教训见 memory：曾误判为 FIFO 幽灵 stop）。
7. **杀僵尸客户端**：旧客户端进程 cmdline 含 `fabric.dli.config`（非 `net.minecraft.client.main.Main`），pkill 模式不匹配 → 僵尸占 30001 → 新客户端 WS BindException。按 fabric 特征杀进程：`pkill -f fabric.dli.config`。
8. **改 Mixin/客户端代码后需重建 jar 并重启整个客户端进程**。
8b. **`fabric-vla-client/run/mods/` 里不要放本 mod 的 jar**：dev `runClient` 从 classpath 加载本 mod，若 run/mods 残留旧构建 jar 会**顶掉新代码**且无报错——表现为"新命令 unknown cmd / 帧头格式旧"（2026-08-09 实踩：旧 jar 静默跑了半下午）。run/mods 只放第三方 mod。
9. **重置确定性**：region 基线缓存在服务端内存；bot dig 踢连接后 session 重建，region 须持久化否则 C2≠C1。
10. **A* 目标微调**：目标实心（原木/墙）时 `adjustGoal` 在水平±2、垂直 -8..+2 的 3D 邻域找最近可站格（只向上找会把树顶 log 抬到树冠之上 → 全路径失败）。
11. **抓帧分辨率**：demo 用 `set_capture`（0 = 原生 framebuffer 分辨率，保留比例）；VLA 观测默认 224×224。`WorldRenderEvents.LAST` 在本机环境实际已含 HUD（环境差异），严格纯净画面需改到 `inGameHud.render` 之前（M9 处理）。
12. **持久并发录制**：每个 worker 必须独占 player、Fabric `runDir`、WS 端口、job JSONL 和
worker 专属 world；worker 启动时只调用一次 `SelectSurfaceWorld(map_seed)`，后续 episode
必须使用 `reset(..., select_surface=False)`。一个 worker 录制多条数据时不要重复启动客户端。

## 其他

- `mineflayer/`：**外来参考库**（gitignore，磁盘保留），用于语义动作命名对齐（`bot.goTo/dig/craft` 风格），不参与本仓库构建。
- `tools/agents/`：mineflayer 无头 bot 冒烟脚本（Node 22，`node bot.mjs --count N --prefix agent`）。
- `datasets/`：demo 视频等生成产物（gitignore）。
