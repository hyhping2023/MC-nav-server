# P1 协议契约：Phase 1 WebSocket 协议（Python ↔ 客户端）

> 来源：DESIGN.md §5.3 / §5.5 / §5.6 / §7.1 / §9.2 / §13.3（任务 0.5）· §13.4（P1）
> 本文件只收录 DESIGN.md 已定义的内容，作为 Phase 1 受控客户端的实现契约（实现即文档）。
> 适用通道：**Python（P）↔ Fabric 客户端（C）**，高频动作/帧流专用。

---

## 1. 连接与端口

| 项 | 约定 | 来源 |
|---|---|---|
| 监听地址 | `127.0.0.1`（仅本地回环） | §13.2 |
| 端口 | `30001 + env_idx`（每 env 独立 WS 端口） | §13.2 / §6.4 |
| 实现 | 客户端内嵌 WS Server（Java-WebSocket 1.5.6，shadow 进 mod jar） | §5.1 / P1.1 |
| 心跳 | `ping/pong` 心跳与断线清理 | P1.1 |
| 多 env | 每 env 进程 = 1 个客户端 WS 连接 + 1 个玩家身份（共享同一 Purpur 服务器） | §6.4 |

> 端口冲突风险与动态分配：并行 env 各自绑定唯一 WS 端口（`30001+n`），动态分配 + 启动探测（§14.9）。

---

## 2. 消息 Schema

### 2.1 下行（P→C）指令

统一 JSON 外壳：`{ "cmd": "mode|action|reset_camera|disconnect", ... }`（§9.2）。

| cmd | 附加字段 | 说明 | 来源 |
|---|---|---|---|
| `mode` | `mode: "api" \| "human"` | 切换输入模式；客户端同时清空按键状态（`KeyBinding.unpressAll()` 防"粘键"） | §5.4 / §9.2 |
| `action` | §7.1 原始动作 dict（见 2.2） | 下发一步动作 | §9.2 |
| `reset_camera` | `yaw`、`pitch` | 重置视角，例：`{"cmd":"reset_camera","yaw":0,"pitch":0}` | §6.3 |
| `disconnect` | — | 断开 | §9.2 |

### 2.2 `action` 字段表（§7.1 原始动作，tick 级，MineRL/VPT 对齐）

| 字段 | 类型 | 说明 |
|---|---|---|
| `forward` / `back` / `left` / `right` | bool | 移动 |
| `jump` / `sneak` / `sprint` | bool | 跳跃 / 潜行 / 疾跑 |
| `attack` | bool | 挖掘/攻击（长按驱动原版挖掘进度） |
| `use` | bool | 使用/放置 |
| `drop` | bool | 丢弃 |
| `inventory` | bool | 打开物品栏 |
| `hotbar` | int 0-8 | 快捷栏槽位 |
| `camera` | `[pitch_delta, yaw_delta]` 或离散 bin | 视角增量 |

离散模式（默认，对齐 MineRL/MineDojo/MineStudio）：`camera` 为 **121 bin（11×11）**，buttons 每个 `Discrete(2)`，`hotbar` 为 10 选 1 → 直接喂分类模型。
连续模式：`camera` 为 Box（rad/s 增量），适合 DDPG/连续 VLA。

### 2.3 上行（C→P）帧：二进制

二进制消息头 + JPEG 载荷（§9.2 / P1.5）：

```
[4B frame_id][4B last_server_tick][8B wall_nanos][JPEG bytes]
```

| 段 | 字节 | 说明 |
|---|---|---|
| `frame_id` | 4B int | 帧序号（§9.2 记作 `frame_id`） |
| `last_server_tick` | 4B int | 客户端最近一次经 `vla:tick` 收到的服务端 tick（§9.2 简记 `server_tick`，语义一致） |
| `wall_nanos` | 8B long | 渲染墙钟时间（配合时钟漂移校正，§9.3） |
| JPEG | 变长 | 224×224 无 HUD 第一人称帧（调试可用 Base64 JSON 代替） |

### 2.4 上行（C→P）状态：JSON

`{"frame_id":.., "last_server_tick":.., "aimed_block":.., "held_item":.., "fps":..}`（§9.2）

每帧上行消息携带：`{frame_id, last_server_tick, render_wall_time}`，Python 据此做帧↔tick 对齐（§5.6 / §9.3）。

---

## 3. 输入隔离与动作注入约定（§5.3，核心）

设计目标：`API_MODE` 下**物理键鼠完全失效**，一切输入来自 WS 指令；`HUMAN_MODE` 下完全透明。

| 控制项 | 注入方式 |
|---|---|
| 移动（前后左右/跳/潜行/疾跑） | Mixin `KeyboardInput#tick`：`@Inject(at=@At("HEAD"), cancellable=true)` 后，若 `API_MODE` 则 **cancel** 原逻辑，把 API 的浮点/布尔写入 `input` 字段（`pressingForward`、`movementForward/movementSideways`，1.20.5+ 为 `forwardImpulse/leftImpulse`） |
| 视角（pitch/yaw） | Mixin `MouseHandler#turnPlayer`（Yarn: `Mouse`）HEAD cancel；每 tick 直接 `player.setYRot/setXRot` 平滑插值——**天然规避万向节死锁**（原版方法已限制 pitch∈[-90,90]、yaw 360° 环绕） |
| 攻击/使用/丢弃 | Mixin `MouseHandler#onMouseButton` 屏蔽物理按键；API 动作通过 `Minecraft#startAttack()/startUseItem()` 或直接设置对应 `KeyBinding.setPressed()` 驱动原版逻辑（挖掘进度、攻击冷却自动处理） |
| 快捷栏 0-8 | `options.hotbarKeys[i].setPressed(bool)`（选中后 release，模拟按数字键） |
| 物品栏开关 | `options.keyInventory.setPressed(bool)` |

> 推荐：**移动/视角**用 Mixin 覆盖（更精细、可给模拟量）；**攻击/使用/热键**用 `KeyBinding.setPressed()`（复用原版全部时序逻辑，改动最小）。

注入伪代码（§5.3）：

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

双模式切换（§5.4）：

- `HUMAN_MODE`：Mixin 全部放行；客户端开启**演示录制**（帧 + 键位 + 视角原始数据流上行给 Python）。
- `API_MODE`：输入隔离生效；WS 指令驱动。
- 切换指令：`{cmd:"mode", mode:"api"|"human"}`，客户端同时清空按键状态（防"粘键"：`KeyBinding.unpressAll()`）。

---

## 4. 抓帧链路约定（§5.5，性能核心）

**关键认知：不需要额外渲染一个低分辨率 FBO。** Minecraft 每一帧已经把场景渲染进主帧缓冲（`Minecraft#getFramebuffer()`）。

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

约定要点：

| 项 | 约定 | 来源 |
|---|---|---|
| 挂载点 | `GameRendererMixin`（world 渲染之后、HUD 之前）→ 画面纯净（无准星/血条） | §5.5 |
| 分辨率 | 默认 224×224（VLA 常用）；窗口比例 1:1（或中心裁切）避免非等比拉伸 FOV 畸变；`fov` 可配置（默认 70） | §5.5 |
| 帧率 | 客户端 60/120 FPS 渲染，采集端按 `record.fps`（默认 20，与 tick 对齐）降采样 | §5.5 |
| 线程铁律 | 所有 GL 调用只在渲染线程；数据交递用无锁队列（`ConcurrentLinkedQueue`/MPSC ring buffer）；编码与网络传输放后台线程 | §5.5 / §14.1 |
| 先通后优 | P1.4 抓帧先同步 `glReadPixels` 求通，P3.2 再换 PBO 异步 | §13 开发原则 |

---

## 5. 实现进度（Progress）

### 5.1 M0 当前状态

> **M0：骨架占位，尚未实现。**

- 工程目录 `server/`、`fabric-vla-client/`、`vla_env/` 已创建（内容为空）。
- 本文件（任务 0.5「协议文档骨架」交付物之一）为 M0 已完成的唯一产出。
- 客户端 WS 链路相关交付物（`net/WsServer.java`、`VlaClient.java`、`gfx/FrameGrabber.java`、各 Mixin）**均未开始**。

### 5.2 随 M1-M3 填充项

| 里程碑 | 任务 | 交付物 | 填充内容 |
|---|---|---|---|
| M1 通信底座 | P1.1 | `net/WsServer.java`、`VlaClient.java` | WS 监听 30001、消息 schema（§9.2）、`AtomicReference<ActionCmd> currentAction`、`volatile boolean apiMode`、ping/pong 心跳与断线清理；Exit：WS `pong` 可通 |
| M2 客户端控制 | P1.2 / P1.3 | `mixin/KeyboardInputMixin.java`、`mixin/MouseMixin.java`、`input/ActionApplier.java` | §5.3 输入隔离与动作注入；`mode` 切换 `unpressAll()` 防粘键；Exit：API_MODE 物理键鼠完全失效、HUMAN_MODE 透明、无粘键 |
| M3 客户端视觉 | P1.4 / P1.5 / P1.6 | `gfx/FrameGrabber.java`、`mixin/GameRendererMixin.java`、`net/FrameSender.java`、`client_ws.py`、`action_space.py`、`scripts/random_agent.py` | §5.5 抓帧（先同步 `glReadPixels`）；帧头 `[4B frame_id][4B last_server_tick][8B wall_nanos][JPEG]` 上行；Exit：随机策略驱动移动/转向/攻击，224² 帧 ≥20fps 上行且与动作 1:1 |

> 状态约定沿用 §13.10：⬜ 待开始 · 🔄 进行中 · ✅ 完成 · ⚠️ 阻塞。当前 M1/M2/M3 均为 ⬜。
