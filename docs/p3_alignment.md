# P3 对齐契约：Phase 3 Tick/Frame 对齐

> 来源：DESIGN.md §5.6 / §9.3 / §13.6（P3）· §13.10（M8）
> 本文件只收录 DESIGN.md 已定义的内容，作为 Phase 3 数据对齐与性能优化的实现契约（实现即文档）。
> 对齐目标是：**帧 / 状态 / 动作 / 奖励严格一一对应**，杜绝网络错位读 0 奖励（§14.2）。

---

## 1. 对齐背景

Minecraft 服务端 **20 TPS**，客户端可 **60-120 FPS**，二者各自独立运行，需在 Python 侧做 tick/frame 对齐。

- 服务端 tick 权威源：`Bukkit.getCurrentTick()`。
- 客户端侧经 `vla:tick` 插件消息获得（§9.3 / §5.6）。
- `ticks_per_step` 默认 **4**（0.2s/决策，VLA 典型）；human 数据采集可 1。

---

## 2. 对齐方程与容差（§9.3）

```
t 对齐方程：
  server_tick(client_frame) ≈ server_tick 权威（GetStepResult 返回）
  要求：|server_tick_client_frame - server_tick_reward| ≤ tolerance(默认 2 tick)
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `tolerance` | 2 tick | tick 差容差，超限判 invalid |
| `ticks_per_step` | 4（0.2s/决策） | 每步推进的游戏刻；human 采集可 1 |

---

## 3. Lockstep 四步（§9.3）

```
lockstep 保证：
  1. Python 发出 action_i 后，先等客户端 frame_i（含 last_server_tick）
  2. 再等服务端 GetStepResult（阻塞至 action_i 的 k ticks 结算，含权威 server_tick）
  3. 断言两者 tick 差在容差内 → 记录 (frame_i, action_i, reward_i, tick_i)
  4. 任一步超时/错位 → 该 step 标记 invalid 并可选丢弃
```

对应核心数据流（§3.2）：

```
Python  ──WS────► Client: action {buttons, camera, hotbar}    // 发送动作
Client  ──render──► 主线程渲染后 PBO 异步取帧 ──queue──► WS 线程 ──► Python: frame + frame_id + last_server_tick
Server  ──主线程──► 执行动作引发的世界变更 → 事件 → TaskManager 判定
Python  ──gRPC──► Server: GetStepResult()                     // reward / done / state / server_tick
Python  对齐:  (frame_i, action_i, reward_i, tick_i) 一一对应
```

Python 侧 `step()` 实现要点（§6.3）：阻塞等待 gRPC 确认后，才请求下一帧 → **杜绝"动作已执行但 reward 读 0"的网络错位**。

---

## 4. vla:tick 插件消息通道（§5.6）

客户端无法直接读取服务端 tick，通过 **Plugin Messaging** 下行广播：

| 项 | 约定 | 来源 |
|---|---|---|
| channel | `vla:tick`（`plugin.yml` 注册，出站） | §4.1 / §13.2 |
| 载荷 | 每 tick（或每 N tick）`[4B tick][8B wall_nanos]`（`int serverTick` + `long wallNanos`） | §5.6 / P3.1 |
| 客户端接收 | `ClientPlayNetworking.registerGlobalReceiver`（`mixin/PlayNetworkMixin.java`），写入 `volatile long lastServerTick` | §5.6 / P3.1 |
| 帧携带 | 每帧上行消息携带 `{frame_id, last_server_tick, render_wall_time}` | §5.6 |

---

## 5. 断言与 invalid 丢弃策略（§9.3）

- **断言**：lockstep 第 3 步对每步断言 `|server_tick_client_frame - server_tick_reward| ≤ tolerance`，通过则记录四元组 `(frame_i, action_i, reward_i, tick_i)`。
- **invalid 丢弃**：lockstep 任一步**超时/错位** → 该 step 标记 invalid 并可选丢弃。
- **验收基准**（M8 Exit）：10 episode 连续采集，帧/状态/动作/奖励数量一致，tick 对齐断言 **100%**。

---

## 6. 时钟漂移与补偿（§9.3）

- 客户端 `render_wall_nanos` 与 `server_wall_nanos`（gRPC 返回）配合做时钟漂移校正。
- 本地回环可忽略；集群部署时需 **NTP**。

---

## 7. 实现进度（Progress）

### 7.1 M0 当前状态

> **M0：骨架占位，尚未实现。**

- 工程目录 `server/`、`fabric-vla-client/`、`vla_env/` 已创建（内容为空）。
- 本文件（任务 0.5「协议文档骨架」交付物之一）为 M0 已完成的唯一产出。
- `vla:tick` 广播、`PlayNetworkMixin`、`lockstep.py` 断言逻辑均**未开始**。

### 7.2 随 M8 填充项

| 里程碑 | 任务 | 交付物 | 填充内容 |
|---|---|---|---|
| M8 数据对齐 | P3.1 | 插件 `vla:tick` 广播、`mixin/PlayNetworkMixin.java`、`lockstep.py` | 服务端每 tick 广播 `[4B tick][8B wall_nanos]`；客户端写 `volatile long lastServerTick`；帧元数据携带；lockstep 断言（§9.3）；Exit：10 episode 四者计数一致 + tick 断言 100% |

> 对齐链路的性能侧（P3.2 PBO 异步抓帧、P3.3 并行采样、P3.4 采集脚本）属 M9/M10 范畴（§13.10），不在本文档范围。
> 状态约定沿用 §13.10：⬜ 待开始 · 🔄 进行中 · ✅ 完成 · ⚠️ 阻塞。当前 M8 为 ⬜。
