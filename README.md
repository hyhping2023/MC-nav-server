# MC Nav Server / VLA Minecraft 录制环境

这是一个用于 Minecraft 1.20.1 第一视角任务录制、导航和 VLA 数据采集的完整环境。

项目由三部分组成：

```text
Purpur 服务端插件   purpur-vla-plugin/
Fabric 受控客户端   fabric-vla-client/
Python 控制与录制   vla_env/
```

运行时数据流：

```text
Python worker
    ├── WebSocket ──> Fabric client：动作、导航、抓帧、按键事件
    └── gRPC ───────> Purpur server：reset、任务、状态、体素、奖励
```

服务端是世界和任务判定的权威来源；客户端负责执行动作和抓取第一视角画面；Python
负责任务编排、锁步交互和数据落盘。

详细录制状态机和协议说明：

- [docs/demo_recording.md](docs/demo_recording.md)：单条和多 worker 录制完整说明；
- [docs/p1_protocol.md](docs/p1_protocol.md)：Python ↔ Fabric WebSocket 协议；
- [docs/p2_protocol.md](docs/p2_protocol.md)：Python ↔ Purpur gRPC 协议；
- [docs/p3_alignment.md](docs/p3_alignment.md)：tick/frame 对齐约定；
- [DESIGN.md](DESIGN.md)：完整设计和实现记录。

---

## 1. 系统要求

### 必需软件

- JDK 21；
- Python 3.10 或更高版本；
- `ffmpeg`，用于 JPEG 帧合成 MP4；
- Git；
- 可访问 Maven、Fabric、Mojang 和 Purpur 下载源的网络。

### Java 说明

项目当前默认使用 macOS Homebrew JDK 21 路径：

```text
/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
```

如果是在 Linux、Windows 或其他 JDK 安装路径下运行，需要调整：

- `fabric-vla-client/gradle.properties` 中的 `org.gradle.java.home`；
- `server/start.sh` 中的 `JAVA_HOME`。

确认 Java：

```bash
java -version
```

### macOS 示例

```bash
brew install openjdk@21 ffmpeg
```

### Ubuntu/Debian 示例

```bash
sudo apt update
sudo apt install openjdk-21-jdk ffmpeg python3-venv
```

---

## 2. 首次安装

以下命令从仓库根目录执行。

### 2.1 下载 Purpur 服务端

Purpur 不会由 `server/start.sh` 自动下载。第一次使用时执行：

```bash
cd server
bash download.sh
```

脚本会下载 Purpur 1.20.1 到：

```text
server/purpur.jar
```

如果文件已经存在，脚本会跳过下载。

### 2.2 构建 Purpur 插件

```bash
cd ../purpur-vla-plugin
./gradlew --no-daemon build
mkdir -p ../server/plugins
cp build/libs/vla-purpur.jar ../server/plugins/
```

插件包含：

- 受控单材质平原生成器；
- worker 专属世界；
- gRPC 服务；
- episode reset 和任务判定；
- 服务端路径规划；
- 任务目标放置；
- tick 广播。

### 2.3 创建 Python 环境

```bash
cd ../vla_env
python3 -m venv .venv
.venv/bin/pip install -e .
```

验证：

```bash
.venv/bin/python -c "import vla_env; print(vla_env.__version__)"
```

如果 `vla.proto` 修改过，重新生成 Python gRPC 文件：

```bash
bash scripts/gen_proto.sh
```

### 2.4 准备 Fabric 客户端

Loom 会在首次构建或启动时自动下载 Minecraft、Fabric Loader、Fabric API、Yarn
mappings 和 Maven 依赖：

```bash
cd ../fabric-vla-client
./gradlew --no-daemon build
```

注意：Gradle/Loom 自动下载的是客户端构建所需依赖，不会替代 JDK 安装，也不会下载
Purpur 服务端。Purpur 必须单独执行 `server/download.sh`。

---

## 3. 端口和目录

默认端口：

| 组件 | 地址 |
|---|---|
| Minecraft 服务端 | `127.0.0.1:25565` |
| Purpur gRPC | `127.0.0.1:50051` |
| worker-00 Fabric WS | `127.0.0.1:30001` |
| worker-01 Fabric WS | `127.0.0.1:30002` |
| worker-N Fabric WS | `127.0.0.1:30001 + N` |

多 worker 时，每个客户端都必须拥有独立的：

- WS 端口；
- `runDir`；
- autojoin 配置；
- player 名称；
- 任务队列；
- worker 专属地图。

运行时目录示例：

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
```

`runtime/`、Minecraft 世界、日志和构建产物不会提交到 Git。

---

## 4. 启动服务端

### 4.1 启动命令

```bash
cd server
VLA_SERVER_XMS=1G VLA_SERVER_XMX=4G bash start.sh
```

也可以直接使用默认值：

```bash
bash start.sh
```

当前默认：

```text
Xms=1G
Xmx=4G
FIFO=/tmp/vla_server_console
```

`Xmx=4G` 限制的是 Java heap，不等于整个进程 RSS 严格不超过 4G。若必须限制整个
进程，应额外使用 systemd/cgroup/Docker 等系统级限制。

### 4.2 检查服务端

日志中应出现类似：

```text
gRPC server started on 127.0.0.1:50051
vla-purpur enabled
```

可通过 FIFO 查看状态。命令不带 `/`：

```bash
echo "vla status" > /tmp/vla_server_console
```

### 4.3 服务端重启

服务端重启后，建议同时重启 Fabric 客户端和 Python worker。当前 autojoin 只在客户端
启动时执行一次，不保证服务端重启后客户端自动重新入服。

---

## 5. 单客户端和单条 Demo

### 5.1 autojoin 配置

默认客户端使用：

```text
fabric-vla-client/run/autojoin.txt
```

文件内容为两行：

```text
127.0.0.1:25565
agent0
```

### 5.2 启动客户端

```bash
cd fabric-vla-client
./gradlew --no-daemon runClient
```

确认客户端日志出现：

```text
WS server listening on port 30001
```

确认 Purpur 日志出现：

```text
agent0 joined the game
```

### 5.3 录制单条 Demo

Python 脚本必须从 `vla_env/` 目录执行：

```bash
cd ../vla_env
.venv/bin/python -u scripts/demo_human.py \
  ../datasets/demo_human/dig_stone_3_sand \
  --task dig_stone \
  --surface sand \
  --seed 3 \
  --capture 640x360 \
  --max-steps 500
```

支持的任务：

| CLI 任务 | 服务端任务 | 目标 |
|---|---|---|
| `dig_stone` | `collect_stone` | 挖 4 个石头方块 |
| `dig_dirt` | `dig_dirt` | 挖 4 个泥土方块 |
| `kill_animal` | `kill_animal` | 击杀 2 只猪 |
| `place_dirt` | `place_dirt` | 放置 3 个泥土方块 |
| `collect_wood` | `collect_wood` | 砍 4 根原木 |

其他示例：

```bash
# 泥土
.venv/bin/python -u scripts/demo_human.py \
  ../datasets/demo_human/dig_dirt_12_dirt \
  --task dig_dirt \
  --surface dirt \
  --seed 12 \
  --capture 640x360 \
  --max-steps 500

# 砍树
.venv/bin/python -u scripts/demo_human.py \
  ../datasets/demo_human/collect_wood_5_grass \
  --task collect_wood \
  --surface grass_block \
  --seed 5 \
  --capture 640x360 \
  --max-steps 650
```

成功标志：

```text
HUMAN_DEMO_OK ... progress=1.00 ... align_ok=True
```

### 5.4 常用参数

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

### 5.5 录制输出

一个 episode 包含：

```text
meta.json
trajectory.jsonl
frames/*.jpg
keys.jsonl
actions.jsonl
state.jsonl
align_assertions.jsonl
episode_summary.json
```

同名 MP4 位于 episode 目录旁：

```text
datasets/demo_human/dig_stone_3_sand.mp4
```

---

## 6. 多 worker 并发录制

### 6.1 隔离模型

```text
worker-00 -> agent00 -> WS 30001 -> vla_surface_sand__agent00
worker-01 -> agent01 -> WS 30002 -> vla_surface_sand__agent01
```

服务端按 player 和 surface 创建地图：

```text
server/vla_surface_sand__agent00/
server/vla_surface_sand__agent01/
```

对应元数据：

```text
server/plugins/vla-purpur/surface-worlds/agent00/sand/world-meta.json
server/plugins/vla-purpur/surface-worlds/agent01/sand/world-meta.json
```

`map_seed` 只用于首次创建 worker 地图；每条 job 的 `seed` 用于任务目标、形状、
时间、天气、镜头和 humanizer。

### 6.2 准备任务队列

```bash
cd ..
mkdir -p runtime
cat > runtime/sand_jobs.jsonl <<'EOF'
{"job_id":"sand_stone_0001","task":"dig_stone","seed":1}
{"job_id":"sand_dirt_0002","task":"dig_dirt","seed":2}
{"job_id":"sand_wood_0003","task":"collect_wood","seed":3}
{"job_id":"sand_stone_0004","task":"dig_stone","seed":4,"max_steps":500}
EOF
```

每行至少包含：

```json
{"job_id":"unique_id","task":"dig_stone","seed":1}
```

可选字段：

```json
{
  "job_id": "unique_id",
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
  "spawn": [0.5, 64.0, 0.0, 0.0]
}
```

要求：

- 同一 worker 的所有 job 应使用同一个 surface；
- `job_id` 应全局唯一；
- `seed` 建议唯一，方便审计和复现；
- job 的 `surface` 如果填写，必须与 coordinator 的 `--surface` 一致。

### 6.3 分配任务

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

输出：

```text
runtime/worker-00/jobs.jsonl
runtime/worker-01/jobs.jsonl
```

如果队列已存在，默认不会覆盖。确认需要重建队列时使用：

```bash
--overwrite-queues
```

新增 worker 编号时使用 `--start-index`：

```bash
--workers 2 --start-index 2 --base-ws-port 30001
```

此时会使用：

```text
worker-02 -> agent02 -> WS 30003
worker-03 -> agent03 -> WS 30004
```

### 6.4 启动持久 Fabric 客户端

回到仓库根目录：

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

脚本会自动生成：

```text
runtime/worker-00/client-run/autojoin.txt
runtime/worker-01/client-run/autojoin.txt
```

客户端日志和 PID：

```text
runtime/worker-00/client.log
runtime/worker-00/client.pid
runtime/worker-01/client.log
runtime/worker-01/client.pid
```

客户端只启动一次，之后由 Python worker 连续录制多个 episode。

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

worker 会：

1. 等待 gRPC、WS 和 player 就绪；
2. 调用一次 `SelectSurfaceWorld(surface, map_seed)`；
3. 校验服务端返回的 worker 身份；
4. 逐条读取 job；
5. 使用 `reset(..., select_surface=False)` 重置 episode；
6. 录制帧、按键、状态和语义动作；
7. 合成 MP4；
8. 成功后发布到最终目录；
9. 继续处理下一条 job。

### 6.6 Worker 参数

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

恢复或限量运行：

```bash
# 只录制前 10 条
--max-jobs 10

# 重试 results.jsonl 中之前失败的任务
--retry-failed
```

### 6.7 结果和断点续录

结果文件：

```text
runtime/worker-00/results.jsonl
runtime/worker-01/results.jsonl
```

最终输出：

```text
datasets/demo_human/sand/<job_id>/
datasets/demo_human/sand/<job_id>.mp4
```

临时输出：

```text
datasets/demo_human/.partial/worker-00/
datasets/demo_human/.partial/worker-01/
```

成功日志：

```text
WORKER_EPISODE_OK id=... progress=1.00 ...
WORKER_DONE worker=worker-00 processed=N success=N failed=0
```

恢复规则：

- `status=ok` 的 job 跳过；
- 失败 job 默认跳过；
- `--retry-failed` 才会重试失败 job；
- 已有完整 episode 目录和 MP4 的 job 跳过；
- 失败的 partial 目录保留用于诊断。

---

## 7. 停止和恢复

停止单个客户端：

```bash
kill "$(cat runtime/worker-00/client.pid)"
```

停止全部客户端：

```bash
for f in runtime/worker-*/client.pid; do
  test -s "$f" && kill "$(cat "$f")" 2>/dev/null || true
done
```

Python worker 可以使用 `Ctrl-C` 停止。已完成任务会记录在 `results.jsonl`，之后使用
相同的 job 队列重新启动即可继续。

服务端重启后的建议顺序：

```text
1. 停止 Python workers；
2. 停止并重新启动 Fabric clients；
3. 等待所有 player 重新 join；
4. 重新启动 Python workers；
5. 使用相同 jobs.jsonl，必要时加 --retry-failed。
```

---

## 8. 回放和确定性验证

### 8.1 回放

```bash
cd vla_env
.venv/bin/python -u scripts/demo_human.py \
  --replay ../datasets/demo_human/sand/<job_id> \
  --player agent00 \
  --ws-url ws://127.0.0.1:30001 \
  --grpc-port 50051
```

回放读取 episode 的 `meta.json`，使用其中的 `seed`、`task_id`、`surface` 和 `spawn`
重建任务，并重放 tick 级按键和相机增量。

### 8.2 检查 worker 地图隔离

worker 日志中应看到不同 world：

```text
[map] world=vla_surface_sand__agent00 worker=agent00 ...
[map] world=vla_surface_sand__agent01 worker=agent01 ...
```

episode `meta.json` 应包含：

```json
{
  "seed": 1,
  "map_seed": 10000,
  "worker_id": "worker-00",
  "player": "agent00",
  "world_name": "vla_surface_sand__agent00"
}
```

### 8.3 对齐检查

检查：

```bash
cat datasets/demo_human/sand/<job_id>/episode_summary.json
```

重点字段：

```json
{
  "frames": 100,
  "key_events": 20,
  "align_violations": 0,
  "align_ok": true
}
```

---

## 9. 采集状态机

采集任务不在导航时攻击：

```text
SCAN_REMAINING
  -> PLAN_STAND
  -> MOVE_TO_STAND (move_only)
  -> ARRIVED_STAND
  -> DIG_BATCH
  -> SCAN_REMAINING
  -> PILLAR_PLAN
  -> PILLAR
  -> DIG_BATCH
```

`MOVE_TO_STAND` 阶段：

- 不携带目标挖掘计划；
- 不攻击；
- 不跳跃；
- 不挖穿障碍；
- 不自动搭桥。

攻击前必须满足：

1. bot 到规划站位距离不超过 `1.05`；
2. 准星实际命中目标方块；
3. `aimed_block_distance <= 4.25`；
4. 攻击帧 `jump=false`。

语义事件会写入 `actions.jsonl`：

```text
plan_stand
approach_target
goto_path
arrived_stand
dig_batch_begin
dig_aim
dig_at
dig_batch_end
pillar_plan
pillar_up
```

---

## 10. 常见问题

### `purpur.jar` 不存在

```bash
cd server
bash download.sh
```

### `player not found`

客户端还没有连接服务端，或 player 名称不一致。检查：

```bash
cat runtime/worker-00/client-run/autojoin.txt
cat runtime/worker-00/client.log
```

三处名称必须一致：

```text
autojoin.txt        agent00
record_worker       --player agent00
服务端日志          agent00 joined the game
```

### `Address already in use`

检查 WS 端口：

```bash
lsof -nP -iTCP:30001 -sTCP:LISTEN
lsof -nP -iTCP:30002 -sTCP:LISTEN
```

停止旧客户端后再启动。不要让两个客户端共用端口。

### 客户端没有 `WS server listening`

检查：

- `runClient` 是否启动成功；
- `runDir` 是否可写；
- JDK 是否为 21；
- `fabric-vla-client/run/mods/` 中是否残留旧的本 mod jar；
- `client.log` 是否有 crash。

开发环境中，`runClient` 从 classpath 加载本 mod；不要把旧版本本 mod jar 放入
`run/mods/` 覆盖当前代码。

### worker 一直等待 ready

worker 会检查：

```text
gRPC Ping
WS Ping
GetState(player)
```

确认服务端、客户端 WS 和 player 都已就绪。

### 有帧但没有 MP4

确认 `ffmpeg`：

```bash
ffmpeg -version
```

并查看 worker 日志中的 ffmpeg 错误。

### 两个 worker 使用了同一张地图

确认 worker 日志中的 world 分别为：

```text
vla_surface_sand__agent00
vla_surface_sand__agent01
```

如果相同，检查：

- 服务端插件是否为最新构建；
- 两个 worker 是否使用了不同 player；
- WS endpoint 是否配置正确；
- Purpur 是否需要完全重启。

---

## 11. 开发和验证命令

### Python 语法检查

```bash
cd vla_env
.venv/bin/python -m py_compile \
  vla_env/interact.py \
  vla_env/env.py \
  vla_env/server_grpc.py \
  vla_env/dataset/human_episode.py \
  scripts/demo_human.py \
  scripts/record_worker.py \
  scripts/record_coordinator.py
```

### Shell 检查

```bash
cd ..
bash -n server/start.sh tools/start_recording_clients.sh
```

### Purpur 插件编译

```bash
cd purpur-vla-plugin
./gradlew --no-daemon compileJava
```

### Fabric 客户端编译

```bash
cd ../fabric-vla-client
./gradlew --no-daemon compileJava
```

---

## 12. 快速启动清单

新环境完整启动可以按以下顺序执行：

```bash
# 1. 下载服务端
cd server
bash download.sh

# 2. 构建插件并部署
cd ../purpur-vla-plugin
./gradlew --no-daemon build
mkdir -p ../server/plugins
cp build/libs/vla-purpur.jar ../server/plugins/

# 3. 创建 Python 环境
cd ../vla_env
python3 -m venv .venv
.venv/bin/pip install -e .
bash scripts/gen_proto.sh

# 4. 构建客户端依赖
cd ../fabric-vla-client
./gradlew --no-daemon build

# 5. 启动服务端
cd ../server
VLA_SERVER_XMS=1G VLA_SERVER_XMX=4G bash start.sh

# 6. 启动两个持久客户端
cd ..
bash tools/start_recording_clients.sh --count 2 --client-xmx 1G

# 7. 准备和分配任务
cd vla_env
.venv/bin/python -u scripts/record_coordinator.py \
  --jobs ../runtime/sand_jobs.jsonl \
  --surface sand \
  --workers 2 \
  --runtime-root ../runtime \
  --out-root ../datasets/demo_human

# 8. 启动 worker
.venv/bin/python -u scripts/record_worker.py \
  --worker-id worker-00 \
  --player agent00 \
  --ws-url ws://127.0.0.1:30001 \
  --surface sand \
  --map-seed 10000 \
  --jobs ../runtime/worker-00/jobs.jsonl \
  --out-root ../datasets/demo_human
```

第二个 worker 使用 `agent01`、`30002`、`10001` 和 `worker-01` 对应参数。
