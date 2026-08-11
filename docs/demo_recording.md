# 受控单材质世界与 Demo 录制

本项目使用“单材质元世界”录制受控第一视角任务数据。每个元世界只有一种完整地表方块：地表位于 `Y=63`，`Y>=64` 保证为空气；任务目标（石头、泥土、树）由服务端在玩家附近按 seed 随机放置。

## 1. 前置启动

在仓库根目录分别启动 Purpur 服务端和 Fabric 客户端：

```bash
cd server
bash start.sh
```

另开终端：

```bash
cd fabric-vla-client
./gradlew runClient
```

客户端读取 `fabric-vla-client/run/autojoin.txt`，默认会以 `agent0` 连接 `127.0.0.1:25565`。确认服务端日志显示 `agent0 joined the game`，且客户端日志显示 `WS server listening on port 30001` 后再录制。

## 2. 单材质元世界

可选地表：

```text
grass_block, dirt, coarse_dirt, sand, red_sand,
stone, granite, diorite, andesite, clay
```

单 worker 手动录制时，每种材质对应一个可复用的世界存档：

```text
server/vla_surface_<surface>/
```

并有对应元数据：

```text
server/plugins/vla-purpur/surface-worlds/<surface>/world-meta.json
```

元数据记录世界名、地表材质、世界 seed、地表高度 `63` 和空气起始高度 `64`。首次选择材质时创建存档，之后重复选择同一材质会复用该存档。

### 通过服务器命令切换

```text
/vla surface agent0 sand 42
```

### 通过 Python API 切换

```python
api.grpc.select_surface_world(surface="sand", seed=42)
```

通常不必单独调用：`SeedReplayApi.reset(..., surface="sand")` 和 `MinecraftEnv.reset(options={"surface": "sand"})` 会在 reset 前自动切换元世界。

## 3. 多 worker 并发录制（持久客户端，不重复启动）

> **并发隔离单位**：一个 worker 固定绑定一个 Fabric 客户端、一个离线玩家身份、一个
> WS 端口和一张专属地图。不能让多个 worker 同时录制同一张 `vla_surface_sand`，因为
> reset、任务目标、天气和时间均会相互影响。

服务端现在按玩家身份创建世界；例如：

```text
agent00 + sand -> server/vla_surface_sand__agent00/
agent01 + sand -> server/vla_surface_sand__agent01/
```

对应元数据分别位于：

```text
server/plugins/vla-purpur/surface-worlds/agent00/sand/world-meta.json
server/plugins/vla-purpur/surface-worlds/agent01/sand/world-meta.json
```

`map_seed` 仅在该 worker 专属地图首次创建时生效；随后每个 job 的 `seed` 只控制
episode 的目标、时间/天气、镜头与人类化节奏。元数据中的 `map_seed` 可以用来审计此
区别。

### 3.1 启动共享 Purpur 服务端（heap 上限 4G）

```bash
cd server
VLA_SERVER_XMS=1G VLA_SERVER_XMX=4G bash start.sh
```

`VLA_SERVER_XMX=4G` 限制的是 Java heap；若要求**进程 RSS 严格**不超过 4G，应在
宿主机以 cgroup / systemd / 容器额外施加内存限制。

### 3.2 为两个 worker 生成独立任务队列

先准备一个 JSONL。例如 `runtime/sand_jobs.jsonl`：

```jsonl
{"job_id":"sand_stone_0001","task":"dig_stone","seed":1}
{"job_id":"sand_dirt_0002","task":"dig_dirt","seed":2}
{"job_id":"sand_wood_0003","task":"collect_wood","seed":3}
{"job_id":"sand_stone_0004","task":"dig_stone","seed":4}
```

从 `vla_env/` 运行分配器。它按 round-robin 写出每个 worker 独占的
`runtime/worker-XX/jobs.jsonl`：

```bash
cd vla_env
.venv/bin/python -u scripts/record_coordinator.py \
  --jobs ../runtime/sand_jobs.jsonl \
  --surface sand \
  --workers 2 \
  --runtime-root ../runtime \
  --out-root ../datasets/demo_human \
  --map-seed-base 10000
```

### 3.3 只启动一次多个 Fabric 客户端

回到仓库根目录：

```bash
bash tools/start_recording_clients.sh \
  --count 2 \
  --server 127.0.0.1:25565 \
  --base-ws-port 30001 \
  --client-xmx 1G
```

该脚本会生成独立运行目录和 autojoin 配置：

```text
runtime/worker-00/client-run/autojoin.txt  -> agent00, WS 30001
runtime/worker-01/client-run/autojoin.txt  -> agent01, WS 30002
```

客户端日志与 PID 分别在 `runtime/worker-XX/client.log` 和
`runtime/worker-XX/client.pid`。每个客户端在其 worker 的多个 episode 之间保持存活；
**不要每录一条数据重新运行 `gradlew runClient`**。

也可手动启动单个客户端：

```bash
cd fabric-vla-client
./gradlew --no-daemon runClient \
  -PvlaRunDir=../runtime/worker-00/client-run \
  -PvlaWsPort=30001 \
  -PvlaClientXmx=1G
```

### 3.4 启动持久 Python 录制 worker

每个命令连接一个已启动的 Fabric client。worker 启动时只调用一次
`SelectSurfaceWorld(surface, map_seed)`；之后每条 job 都使用同一 client 调用
`reset(..., select_surface=False)`。

终端一：

```bash
cd vla_env
.venv/bin/python -u scripts/record_worker.py \
  --worker-id worker-00 \
  --player agent00 \
  --ws-url ws://127.0.0.1:30001 \
  --surface sand \
  --map-seed 10000 \
  --jobs ../runtime/worker-00/jobs.jsonl \
  --out-root ../datasets/demo_human
```

终端二：

```bash
cd vla_env
.venv/bin/python -u scripts/record_worker.py \
  --worker-id worker-01 \
  --player agent01 \
  --ws-url ws://127.0.0.1:30002 \
  --surface sand \
  --map-seed 10001 \
  --jobs ../runtime/worker-01/jobs.jsonl \
  --out-root ../datasets/demo_human
```

结果写入：

```text
runtime/worker-00/results.jsonl
runtime/worker-01/results.jsonl
datasets/demo_human/sand/<job_id>/
datasets/demo_human/sand/<job_id>.mp4
```

录制先落在 `datasets/demo_human/.partial/<worker>/`；只有成功、对齐通过并完成 MP4
后才原子移动到最终目录。失败 job 保留 partial 文件用于调试，且不会阻塞同一 client
继续处理下一条任务。

完成标志：

```text
WORKER_EPISODE_OK id=... progress=1.00 ...
WORKER_DONE worker=worker-00 processed=N success=N failed=0
```

### 3.5 并发使用约束

- 一个 player / WS endpoint / `jobs.jsonl` 只能由一个 `record_worker.py` 使用。
- 一台机器可共享一个 Purpur；每个 Fabric client 必须独立 `runDir` 和 WS 端口。
- 同一 worker 固定一种 surface；若需 grass、sand、dirt 混合采集，请按 surface 建多个
  worker pool。
- 服务器重启后需重启对应 Fabric clients（当前 autojoin 只在 client 启动时执行一次），
  再启动或恢复 Python worker。

## 4. 单条 Demo 录制

必须从 `vla_env/` 目录执行：

```bash
cd vla_env
```

### 石头任务（沙地）

```bash
.venv/bin/python -u scripts/demo_human.py \
  ../datasets/demo_human/dig_stone_3_sand \
  --task dig_stone \
  --surface sand \
  --seed 3 \
  --capture 640x360 \
  --max-steps 500
```

### 泥土任务（泥土地）

```bash
.venv/bin/python -u scripts/demo_human.py \
  ../datasets/demo_human/dig_dirt_12_dirt \
  --task dig_dirt \
  --surface dirt \
  --seed 12 \
  --capture 640x360 \
  --max-steps 500
```

### 砍树任务（草地）

```bash
.venv/bin/python -u scripts/demo_human.py \
  ../datasets/demo_human/collect_wood_5_grass \
  --task collect_wood \
  --surface grass_block \
  --seed 5 \
  --capture 640x360 \
  --max-steps 650
```

录制成功时输出：

```text
HUMAN_DEMO_OK ... progress=1.00 ... align_ok=True
```

每个 episode 目录包含：

```text
meta.json
frames/*.jpg
trajectory.jsonl
keys.jsonl
actions.jsonl
state.jsonl
align_assertions.jsonl
episode_summary.json
```

同名 MP4 位于 episode 目录旁，例如：

```text
datasets/demo_human/dig_stone_3_sand.mp4
```

## 5. Episode 随机化和确定性

`--seed` 同时控制：

- 目标位置和目标形状；
- 石头/泥土的柱、矮墙、平台或阶梯形态；
- 树高（3 至 7 格）；
- 初始镜头朝向目标附近的偏移；
- 时间与天气。

每次 reset 都固定为和平难度，并冻结天气/昼夜循环。时间分布为：

- 明亮白天：50%；
- 黎明：15%；
- 黄昏：15%；
- 夜间：20%。

天气分布为：晴天 60%、下雨 32%、雷雨 8%。同一个 seed 会复现同一 episode 环境。

## 6. 采集状态机

采集任务不在导航时攻击。逻辑严格分为：

```text
SCAN_REMAINING
  -> PLAN_STAND
  -> MOVE_TO_STAND (move_only)
  -> ARRIVED_STAND
  -> DIG_BATCH
  -> SCAN_REMAINING
  -> PILLAR_PLAN（仅当无任何地面站位可挖剩余高处目标）
  -> PILLAR
  -> DIG_BATCH
```

### MOVE_TO_STAND

客户端收到 `goto_path` 的 `mode="move_only"` 后：

- 不携带任务目标挖掘计划；
- 不输出攻击；
- 不跳跃；
- 不挖穿障碍；
- 不自动搭桥。

### DIG_BATCH

站位规划器选择离 bot 最近且能覆盖尽量多目标的合法落脚点。到达后，一次挖完该站位正常站立可触及的目标块，再选择下一个站位。

攻击前必须满足：

1. bot 到已规划站位距离不超过 `1.05`；
2. 客户端准星实际命中目标方块；
3. `aimed_block_distance <= 4.25`；
4. 攻击帧 `jump=false`。

### PILLAR

只有没有任何地面站位能触及剩余高处目标时，bot 才会先走到目标下方/侧下方的平地，然后 `pillar_up` 在脚底正下方垫泥土。Pillar 阶段不攻击，禁止跳挖；垫高后重新批量扫描该高度可挖的目标。

`actions.jsonl` 中可检查以下语义事件：

```text
plan_stand
approach_target
goto_path (mode=move_only)
arrived_stand
dig_batch_begin
dig_aim
dig_at
dig_batch_end
pillar_plan
pillar_up
```

## 7. 回放

```bash
cd vla_env
.venv/bin/python -u scripts/demo_human.py \
  --replay ../datasets/demo_human/dig_stone_3_sand
```

回放读取 `meta.json` 中的 `seed`、`task_id`、`surface`，并重建对应元世界、任务与 tick 级按键序列。
