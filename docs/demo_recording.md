# 受控单材质世界与 Demo 录制

本文是当前录制系统的完整使用说明，覆盖：

- 单个客户端的单条 Demo 录制；
- 多个 worker 并发录制；
- 一个 Fabric 客户端连续录制多个 episode；
- worker 专属地图、`map_seed` 与 episode `seed` 的区别；
- 任务队列、断点续录、失败重试和结果校验。

本项目使用“单材质元世界”录制受控第一视角任务数据：

- 地表固定在 `Y=63`；
- `Y>=64` 默认为空气；
- 服务端只在玩家附近生成当前任务目标；
- 地面方块受到服务端保护，任务只能操作目标方块或任务允许的对象；
- reward、progress、success 和 terminated 以 Purpur 服务端为准；
- 客户端负责执行导航、瞄准、挖掘、垫高和抓帧；
- Python 负责任务编排和 canonical 数据落盘。

> 所有 Python 命令都必须在 `vla_env/` 目录内执行，避免仓库根目录下的
> `vla_env/` 目录遮蔽 Python 包。

---

## 1. 组件和端口

| 组件 | 默认地址/端口 | 说明 |
|---|---:|---|
| Purpur 服务端 | `127.0.0.1:25565` | Minecraft 登录入口 |
| Purpur gRPC | `127.0.0.1:50051` | Python ↔ 服务端 |
| worker-00 Fabric WS | `127.0.0.1:30001` | Python ↔ worker-00 客户端 |
| worker-01 Fabric WS | `127.0.0.1:30002` | Python ↔ worker-01 客户端 |
| worker-N Fabric WS | `127.0.0.1:30001+N` | 每个 Fabric 客户端必须独占端口 |

并发录制的隔离单位是：

```text
一个 worker = 一个 Fabric 客户端 + 一个 player + 一个 WS 端口 + 一张专属地图
```

不能让两个 worker 共用同一个 player、WS 端口、`runDir` 或任务队列。

推荐目录结构：

```text
runtime/
  worker-00/
    client-run/
      autojoin.txt
    client.log
    client.pid
    jobs.jsonl
    results.jsonl
  worker-01/
    client-run/
    client.log
    client.pid
    jobs.jsonl
    results.jsonl

datasets/
  demo_human/
    sand/
      <job_id>/
      <job_id>.mp4
    .partial/
```

`runtime/` 已加入 `.gitignore`，不会提交到 Git。

---

## 2. 前置条件

### 2.1 构建服务端插件和客户端

如果代码或协议刚刚修改，先构建：

```bash
cd purpur-vla-plugin
./gradlew --no-daemon build
```

将插件部署到服务端：

```bash
cp build/libs/vla-purpur.jar ../server/plugins/
```

构建 Fabric 客户端：

```bash
cd ../fabric-vla-client
./gradlew --no-daemon build
```

开发环境直接使用 `runClient` 时，Loom 会使用当前源码 classpath。生产/非开发启动
时要确认客户端 `mods/` 中使用的是最新构建出的 `vla-client`。

### 2.2 Python 环境

```bash
cd vla_env
.venv/bin/python -c "import vla_env; print(vla_env.__version__)"
```

如果 Python protobuf 协议发生变化，重新生成：

```bash
bash scripts/gen_proto.sh
```

### 2.3 ffmpeg

`demo_human.py` 和 `record_worker.py` 会使用 `ffmpeg` 将 JPEG 帧合成为 MP4：

```bash
ffmpeg -version
```

如果系统没有 `ffmpeg`，录制仍可能生成帧和 JSONL，但最终 episode 会因 MP4 生成失败
而被判定为失败。

---

## 3. 启动 Purpur 服务端

### 3.1 启动命令

```bash
cd server
VLA_SERVER_XMS=1G VLA_SERVER_XMX=4G bash start.sh
```

默认配置：

```text
Java Xms = 1G
Java Xmx = 4G
Minecraft = 25565
gRPC = 127.0.0.1:50051
控制台 FIFO = /tmp/vla_server_console
```

也可以使用默认值：

```bash
bash start.sh
```

### 3.2 内存限制说明

`VLA_SERVER_XMX=4G` 限制的是 Java heap，不等于整个进程 RSS 严格不超过 4G。

不包含或不完全包含：

- Metaspace；
- Java 线程栈；
- Netty/direct buffer；
- JVM native memory；
- 文件系统缓存；
- Fabric 客户端的 GPU/native 内存。

如果必须限制整个进程的 RSS，应在宿主机额外使用 systemd/cgroup/Docker 等机制。

### 3.3 检查服务端是否就绪

确认日志中出现类似：

```text
gRPC server started on 127.0.0.1:50051
vla-purpur enabled
```

也可以通过 FIFO 发送命令。FIFO 命令**不带 `/` 前缀**：

```bash
echo "vla status" > /tmp/vla_server_console
```

不要写成：

```bash
echo "/vla status" > /tmp/vla_server_console
```

---

## 4. 单材质地图

支持的地表：

```text
grass_block
dirt
coarse_dirt
sand
red_sand
stone
granite
diorite
andesite
clay
```

### 4.1 worker 专属地图命名

当前服务端按 player 身份生成专属地图：

```text
agent00 + sand       -> server/vla_surface_sand__agent00/
agent01 + sand       -> server/vla_surface_sand__agent01/
agent00 + grass_block -> server/vla_surface_grass_block__agent00/
```

元数据路径：

```text
server/plugins/vla-purpur/surface-worlds/agent00/sand/world-meta.json
```

服务端会将 player 名称正规化为小写安全路径，例如 `Agent00` 会映射为
`agent00`。

### 4.2 `map_seed` 与 episode `seed`

两种 seed 的职责不同：

| seed | 作用 | 使用时机 |
|---|---|---|
| `map_seed` | 创建 worker 专属地图时使用的存档 seed | worker 启动时一次 |
| episode `seed` | 目标位置/形状、天气、时间、初始视角、humanizer | 每个 job 一次 |

持久 worker 的正确顺序：

```text
启动 worker
  -> SelectSurfaceWorld(surface, map_seed)
  -> job 1: reset(episode_seed_1, select_surface=False)
  -> job 2: reset(episode_seed_2, select_surface=False)
  -> job 3: reset(episode_seed_3, select_surface=False)
```

不要把每个 episode 的 seed 反复传给 `SelectSurfaceWorld` 作为地图 seed。

### 4.3 手动切换地图

服务端命令：

```text
vla surface agent00 sand 10000
```

Python：

```python
from vla_env.server_grpc import ServerGrpc

grpc = ServerGrpc(player="agent00")
selected = grpc.select_surface_world(
    player="agent00",
    surface="sand",
    seed=10000,
)
print(selected)
```

返回信息包括：

```python
{
    "world_name": "vla_surface_sand__agent00",
    "surface_id": "sand",
    "surface_material": "minecraft:sand",
    "created": True,
    "worker_id": "agent00",
    "map_seed": 10000,
    "surface_y": 63,
    "metadata_path": ".../world-meta.json",
}
```

---

## 5. 单条 Demo 录制

单条录制适合调试任务、检查客户端或验证新代码。

### 5.1 启动单个 Fabric 客户端

最简单方式：

```bash
cd fabric-vla-client
./gradlew --no-daemon runClient
```

默认读取：

```text
fabric-vla-client/run/autojoin.txt
```

内容格式为两行：

```text
127.0.0.1:25565
agent0
```

确认：

```text
服务端日志：agent0 joined the game
客户端日志：WS server listening on port 30001
```

### 5.2 单条 `demo_human.py`

```bash
cd vla_env

.venv/bin/python -u scripts/demo_human.py \
  ../datasets/demo_human/dig_stone_3_sand \
  --task dig_stone \
  --surface sand \
  --seed 3 \
  --capture 640x360 \
  --max-steps 500
```

可用任务：

| CLI task | 服务端 task | 目标 | 工具槽 |
|---|---|---|---:|
| `dig_stone` | `collect_stone` | 4 个石头方块 | 0 |
| `dig_dirt` | `dig_dirt` | 4 个泥土方块 | 2 |
| `kill_animal` | `kill_animal` | 2 只猪 | 1 |
| `place_dirt` | `place_dirt` | 放置 3 个泥土 | 3 |
| `collect_wood` | `collect_wood` | 4 根原木 | 4 |

示例：

```bash
# 泥土任务
.venv/bin/python -u scripts/demo_human.py \
  ../datasets/demo_human/dig_dirt_12_dirt \
  --task dig_dirt \
  --surface dirt \
  --seed 12 \
  --capture 640x360 \
  --max-steps 500

# 砍树任务
.venv/bin/python -u scripts/demo_human.py \
  ../datasets/demo_human/collect_wood_5_grass \
  --task collect_wood \
  --surface grass_block \
  --seed 5 \
  --capture 640x360 \
  --max-steps 650
```

### 5.3 `demo_human.py` 常用参数

```text
--task TASK
--seed SEED
--surface SURFACE
--player PLAYER
--ws-url ws://127.0.0.1:30001
--grpc-host 127.0.0.1
--grpc-port 50051
--max-steps 600
--ticks 2
--half-extent 16
--capture native|WxH
--no-hud
--no-humanize
--no-provision
--tail-seconds N
--spawn x,y,z[,yaw]
--replay EPISODE_DIR
```

多 worker 或手动使用非默认 client 时，要显式指定：

```bash
--player agent00 --ws-url ws://127.0.0.1:30001 --grpc-port 50051
```

### 5.4 输出文件

例如：

```text
datasets/demo_human/dig_stone_3_sand/
  meta.json
  trajectory.jsonl
  frames/
    f_000000.jpg
    ...
  keys.jsonl
  actions.jsonl
  state.jsonl
  align_assertions.jsonl
  episode_summary.json

datasets/demo_human/dig_stone_3_sand.mp4
```

成功输出：

```text
HUMAN_DEMO_OK ... progress=1.00 ... align_ok=True
```

---

## 6. 多 worker 并发录制

### 6.1 并发原则

一个 worker 必须独占：

- 一个 `player`；
- 一个 Fabric `runDir`；
- 一个 WS 端口；
- 一个 `jobs.jsonl`；
- 一个 worker 专属地图。

例如：

```text
worker-00 -> agent00 -> WS 30001 -> vla_surface_sand__agent00
worker-01 -> agent01 -> WS 30002 -> vla_surface_sand__agent01
```

多个 worker 可以共享同一个 Purpur 服务端，但不能共享同一张 worker 地图。

同一 worker 当前固定一种 surface。若需要同时录制多种 surface，应为每种 surface
分别创建 worker pool。

### 6.2 准备 job JSONL

创建：

```bash
mkdir -p runtime
cat > runtime/sand_jobs.jsonl <<'EOF'
{"job_id":"sand_stone_0001","task":"dig_stone","seed":1}
{"job_id":"sand_dirt_0002","task":"dig_dirt","seed":2}
{"job_id":"sand_wood_0003","task":"collect_wood","seed":3}
{"job_id":"sand_stone_0004","task":"dig_stone","seed":4,"max_steps":500}
EOF
```

每行至少需要：

```json
{
  "job_id": "sand_stone_0001",
  "task": "dig_stone",
  "seed": 1
}
```

`job_id` 可选；如果省略，coordinator 会自动生成。

支持的可选字段：

```json
{
  "job_id": "sand_stone_0001",
  "task": "dig_stone",
  "seed": 1,
  "surface": "sand",
  "max_steps": 600,
  "ticks": 2,
  "half_extent": 16,
  "capture": "640x360",
  "hud": true,
  "humanize": true,
  "provision": true,
  "tail_seconds": 0,
  "spawn": [0.5, 64.0, 0.5, 0.0]
}
```

注意：

- 一个 worker 的所有 job 必须使用同一个 `surface`；
- `seed` 应在同一 job 队列中保持唯一，便于审计；
- `job_id` 应全局唯一，避免结果覆盖；
- `spawn` 是可选的 `[x,y,z]` 或 `[x,y,z,yaw]`；
- job 的 `surface` 如果填写，必须与 coordinator 的 `--surface` 一致。

### 6.3 分配 job

例如分配给 2 个 worker：

```bash
cd vla_env

.venv/bin/python -u scripts/record_coordinator.py \
  --jobs ../runtime/sand_jobs.jsonl \
  --surface sand \
  --workers 2 \
  --runtime-root ../runtime \
  --out-root ../datasets/demo_human \
  --start-index 0 \
  --base-ws-port 30001 \
  --player-prefix agent \
  --map-seed-base 10000
```

生成：

```text
runtime/worker-00/jobs.jsonl
runtime/worker-01/jobs.jsonl
```

并打印每个 worker 的启动命令。

如果目标队列已经存在，coordinator 默认拒绝覆盖：

```text
COORDINATOR_FAIL: existing queue ...
```

确认旧队列不再需要后，显式使用：

```bash
--overwrite-queues
```

`--start-index` 适合追加新 worker。例如已有 `worker-00` 和 `worker-01`，新增两个：

```bash
--workers 2 --start-index 2 --base-ws-port 30001
```

这会使用：

```text
worker-02 -> agent02 -> WS 30003
worker-03 -> agent03 -> WS 30004
```

### 6.4 启动持久 Fabric 客户端

在仓库根目录：

```bash
bash tools/start_recording_clients.sh \
  --count 2 \
  --start-index 0 \
  --base-ws-port 30001 \
  --server 127.0.0.1:25565 \
  --player-prefix agent \
  --client-xmx 1G \
  --runtime-root runtime
```

脚本会为每个 worker 创建：

```text
runtime/worker-00/client-run/autojoin.txt
runtime/worker-01/client-run/autojoin.txt
```

`worker-00` 的 autojoin 内容：

```text
127.0.0.1:25565
agent00
```

客户端日志和 PID：

```text
runtime/worker-00/client.log
runtime/worker-00/client.pid
runtime/worker-01/client.log
runtime/worker-01/client.pid
```

检查启动状态：

```bash
cat runtime/worker-00/client.log
cat runtime/worker-01/client.log
```

需要看到：

```text
WS server listening on port 30001
WS server listening on port 30002
```

并在 Purpur 日志看到：

```text
agent00 joined the game
agent01 joined the game
```

客户端只启动一次，之后同一 worker 会连续录制多条 episode。

手动启动单个客户端时：

```bash
cd fabric-vla-client
./gradlew --no-daemon runClient \
  -PvlaRunDir=../runtime/worker-00/client-run \
  -PvlaWsPort=30001 \
  -PvlaClientXmx=1G
```

### 6.5 启动 Python worker

终端一：

```bash
cd vla_env

.venv/bin/python -u scripts/record_worker.py \
  --worker-id worker-00 \
  --player agent00 \
  --ws-url ws://127.0.0.1:30001 \
  --grpc-host 127.0.0.1 \
  --grpc-port 50051 \
  --surface sand \
  --map-seed 10000 \
  --jobs ../runtime/worker-00/jobs.jsonl \
  --out-root ../datasets/demo_human
```

终端二：

```bash
.venv/bin/python -u scripts/record_worker.py \
  --worker-id worker-01 \
  --player agent01 \
  --ws-url ws://127.0.0.1:30002 \
  --grpc-host 127.0.0.1 \
  --grpc-port 50051 \
  --surface sand \
  --map-seed 10001 \
  --jobs ../runtime/worker-01/jobs.jsonl \
  --out-root ../datasets/demo_human
```

worker 启动时会：

1. 等待 gRPC、WS 和 player 就绪；
2. 调用一次 `SelectSurfaceWorld(surface, map_seed)`；
3. 校验服务端返回的 `worker_id` 与 player 一致；
4. 逐个读取 job；
5. 每个 job 调用 `reset(..., select_surface=False)`；
6. 录制帧、按键、状态和语义动作；
7. 合成 MP4；
8. 成功后原子发布 episode；
9. 继续处理下一条 job。

### 6.6 worker 参数

```text
--worker-id ID
--player PLAYER
--ws-url URL
--grpc-host HOST
--grpc-port PORT
--surface SURFACE
--map-seed INTEGER
--jobs FILE
--out-root DIR
--results FILE
--ticks INTEGER
--max-steps INTEGER
--half-extent INTEGER
--capture native|WxH
--no-hud
--no-humanize
--no-provision
--max-jobs INTEGER
--retry-failed
--ready-timeout SECONDS
```

常用示例：

```bash
# 只处理前 10 条任务
--max-jobs 10

# 允许重跑 results.jsonl 中之前失败的任务
--retry-failed

# 使用 640x360、关闭 HUD
--capture 640x360 --no-hud

# 关闭客户端 humanizer
--no-humanize
```

### 6.7 输出和状态

worker 结果：

```text
runtime/worker-00/results.jsonl
runtime/worker-01/results.jsonl
```

成功 episode：

```text
datasets/demo_human/sand/<job_id>/
datasets/demo_human/sand/<job_id>.mp4
```

录制中间目录：

```text
datasets/demo_human/.partial/worker-00/
datasets/demo_human/.partial/worker-01/
```

成功日志：

```text
WORKER_EPISODE_OK id=... progress=1.00 ...
WORKER_DONE worker=worker-00 processed=N success=N failed=0
```

`results.jsonl` 中的常见状态：

```json
{"job_id":"...", "status":"ok", "...":"..."}
{"job_id":"...", "status":"failed", "error":"...", "...":"..."}
```

默认恢复行为：

- `status=ok` 的 job 跳过；
- 已有失败结果的 job 默认也跳过；
- 使用 `--retry-failed` 才重试失败 job；
- 已存在完整 episode 目录和 MP4 的 job 跳过；
- 失败的 partial 目录保留，不自动删除。

### 6.8 停止和恢复

停止单个 Fabric client：

```bash
kill "$(cat runtime/worker-00/client.pid)"
```

停止多个客户端：

```bash
for f in runtime/worker-*/client.pid; do
  test -s "$f" && kill "$(cat "$f")" 2>/dev/null || true
done
```

Python worker 可以使用 `Ctrl-C` 停止。已完成的结果会保留在 `results.jsonl`，重新启动
同一个 worker 时会跳过已经成功的 job。

当前客户端的 autojoin 只在客户端进程启动时执行一次。Purpur 重启后，建议按以下顺序
恢复：

```text
1. 停止 Python workers；
2. 停止并重新启动 Fabric clients；
3. 等待所有 player 重新 join；
4. 重新启动 Python workers；
5. 使用相同 jobs.jsonl 和 --retry-failed 恢复未完成任务。
```

---

## 7. 录制确定性和隔离验证

### 7.1 验证每个 worker 的 world

通过 worker 日志确认：

```text
[map] world=vla_surface_sand__agent00 worker=agent00 ...
[map] world=vla_surface_sand__agent01 worker=agent01 ...
```

不能出现两个 worker 使用相同的 `world=`。

### 7.2 验证 episode

检查：

```bash
cat datasets/demo_human/sand/<job_id>/meta.json
cat datasets/demo_human/sand/<job_id>/episode_summary.json
```

重点字段：

```json
{
  "seed": 1,
  "map_seed": 10000,
  "worker_id": "worker-00",
  "player": "agent00",
  "world_name": "vla_surface_sand__agent00",
  "align_ok": true
}
```

### 7.3 同 seed 回放

```bash
cd vla_env

.venv/bin/python -u scripts/demo_human.py \
  --replay ../datasets/demo_human/sand/<job_id> \
  --player agent00 \
  --ws-url ws://127.0.0.1:30001 \
  --grpc-port 50051
```

回放读取 episode 的 `meta.json`，使用其中的 `seed`、`task_id`、`surface` 和 `spawn`
重建任务，并重新播放 `keys.jsonl` / `trajectory.jsonl`。

---

## 8. 采集状态机

采集任务不在导航时攻击，状态机为：

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

站位规划器选择离 bot 最近且能覆盖尽量多目标的合法落脚点。到达后，一次挖完该站位
正常站立可触及的目标块，再选择下一个站位。

攻击前必须满足：

1. bot 到已规划站位距离不超过 `1.05`；
2. 客户端准星实际命中目标方块；
3. `aimed_block_distance <= 4.25`；
4. 攻击帧 `jump=false`。

### PILLAR

只有没有任何地面站位能触及剩余高处目标时，bot 才会先走到目标下方/侧下方平地，
然后 `pillar_up` 在脚底正下方垫泥土。Pillar 阶段不攻击，禁止跳挖；垫高后重新批量
扫描该高度可挖的目标。

`actions.jsonl` 中可检查：

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

---

## 9. 常见问题

### 9.1 `player not found`

说明 Fabric 客户端尚未连接 Purpur，或者 autojoin 中的 player 名称与 Python 参数不一致。

检查：

```bash
cat runtime/worker-00/client-run/autojoin.txt
cat runtime/worker-00/client.log
```

确保三处一致：

```text
autojoin.txt        agent00
record_worker       --player agent00
服务端 join 日志    agent00 joined
```

### 9.2 `Address already in use`

通常是旧客户端仍占用 WS 端口：

```bash
lsof -nP -iTCP:30001 -sTCP:LISTEN
lsof -nP -iTCP:30002 -sTCP:LISTEN
```

结束旧进程后重新启动。不要让两个 client 共用同一个 WS 端口。

### 9.3 `WS server listening` 没出现

检查：

- Fabric client 是否真正启动；
- `vla-client` 是否加载；
- `runtime/worker-XX/client-run` 是否可写；
- 是否把旧 jar 放进了 `fabric-vla-client/run/mods/` 并覆盖了开发 classpath；
- `client.log` 中是否有 crash。

### 9.4 worker 一直等待 ready

worker 会依次检查：

```text
gRPC Ping
WS Ping
GetState(player)
```

确认 Purpur 已启动、客户端 WS 已监听、player 已 join。

### 9.5 episode 有帧但没有 MP4

检查：

```bash
ffmpeg -version
```

并查看 worker 日志中的 ffmpeg 错误。帧目录和 JSONL 可以保留用于诊断。

### 9.6 两个 worker 的地图相同

检查 worker 日志中的：

```text
[map] world=...
```

正确情况必须是：

```text
vla_surface_sand__agent00
vla_surface_sand__agent01
```

如果相同，通常是：

- 服务端插件不是最新构建；
- 两个 worker 使用了相同 player；
- 两个 Python worker 连接到了错误的 endpoint；
- 仍在使用旧服务端进程，需要完全重启 Purpur。

---

## 10. 回放

```bash
cd vla_env

.venv/bin/python -u scripts/demo_human.py \
  --replay ../datasets/demo_human/sand/<job_id> \
  --player agent00 \
  --ws-url ws://127.0.0.1:30001
```

回放读取 `meta.json` 中的 `seed`、`task_id`、`surface`、`spawn`，重建对应元世界、
任务与 tick 级按键序列。
