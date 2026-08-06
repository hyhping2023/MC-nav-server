# M1 协议规范：引擎 fork + 视觉观测上线

> M1 目标（DESIGN.md §11）：
> 1. `core.set_player_control`（服务器按键注入，C++ fork）
> 2. 客户端抓帧 → 共享内存（第一人称 RGB → Python）
> 3. bot 改为真实玩家驱动（客户端连接 bot1，服务器权威控制）
> 4. `random_agent.py` 跑通带视觉观测的循环
>
> 前置：M0 已打通（文件 IPC + 逻辑 bot + craft_planks 全链路），见 docs/m0_protocol.md。

## 0. 重要背景事实（已核验源码）

- `PlayerControl` 结构：`src/player.h:44`，字段 `up/down/left/right`（float 模拟量）、
  `jump/sneak/aux1/zoom/dig/place`（bool）、`movement_speed/movement_direction`。
- `Player::control` 是**公共成员**（`src/player.h:217`），可直接赋值。
- **关键**：服务器物理读取的是 `control.movement_speed` 与 `control.movement_direction`，
  不是 up/down/left/right。所以 set_player_control 赋值后必须调用
  `player->control.setMovementFromKeys()`（该方法在 `src/player.h:73`）从方向键计算速度/方向。
- `PlayerControl::pitch/yaw` 服务器不可用 → 视角一律用现有 `set_look_*`。
- ObjectRef 方法注册表在 `src/script/lua_api/l_object.cpp:3090`（`luamethod(ObjectRef, ...)`），
  player 获取用 `getplayer(ref)`（l_object.cpp:66）。
- 客户端渲染入口：`src/client/renderingengine.cpp` 的 `draw_scene(...)`
  （声明见 `renderingengine.h:131`）。

## 1. core.set_player_control

### Lua 签名

```lua
core.set_player_control(player, {
    up = 0.0, down = 0.0, left = 0.0, right = 0.0,  -- float 模拟量
    jump = false, sneak = false, sprint = false,    -- sprint -> aux1
    dig = false, place = false, zoom = false,
})
-- 无返回值；player 非玩家或不是 valid ObjectRef 时无操作
```

### C++ 实现要点（l_object.cpp）

```cpp
// int ObjectRef::l_set_player_control(lua_State *L)
// 1) ObjectRef *ref = checkObject<ObjectRef>(L, 1);
// 2) RemotePlayer *player = getplayer(ref);  if (!player) return 0;
// 3) 读表字段（getfloatfield / getboolfield，缺省保持原值）
// 4) player->control.up = ...; player->control.dig = ...; 等
// 5) player->control.setMovementFromKeys();   // 必须，否则不产生移动
// 注册：luamethod(ObjectRef, set_player_control)
```

- 特权：不要求（本地开发服务器）；如需保护可加 `interact` 校验，但 M1 默认放开。
- dig/place：服务器 `PlayerSAO::step` 依据 `control.dig/place` 对玩家 look 方向 raycast
  的结果启动挖掘/放置，因此配合 `set_look_*` 即可工作（M1 验证随机移动即可，挖掘验证可选）。

## 2. 客户端抓帧 → 共享内存环形缓冲

### 启用方式

客户端配置（client.conf）：
```
mcl2_frame_enable = true
mcl2_frame_shm = /mcl2_frames
mcl2_frame_width = 224
mcl2_frame_height = 224
mcl2_frame_fps = 30        # 抓帧上限（渲染实际帧率）
```
未启用时行为完全不变。

### 共享内存布局（与 Python 侧 renderer/engine_fork.py 严格一致）

```
struct Mcl2FrameHeader {            // C 紧凑布局（= 44 字节）
    uint32_t magic;        // 0x4D434C32
    uint32_t width;        // 224
    uint32_t height;       // 224
    uint32_t stride;       // width * 4（BGRA）
    uint32_t depth;        // 32
    uint64_t write_idx;    // 单调递增，槽位 = write_idx % depth
    uint64_t read_idx;     // 保留给读者
    int64_t  server_tick;  // 0（客户端无精确 tick，见对齐策略）
};
// 紧随其后 stride * height * depth 字节的帧数据
```

### 写入侧（客户端，C++）

- 在 `draw_scene()` 渲染完成后：
  1. `m_device->getVideoDriver()->createImageFromScreen(...)` 读取当前屏幕（或直接读 FBO）；
  2. 缩放到 w×h，转 BGRA；
  3. `slot = write_idx % depth`，memcpy 到 `base + slot*stride*height`；
  4. `__sync_synchronize()` 后 `write_idx++`（**最后一步**，保证读者看到完整帧）。
- 一次性初始化：`shm_open(name, O_CREAT|O_RDWR, 0644)` + `ftruncate(size)` + `mmap`。
  注意 macOS 也支持 POSIX shm（/dev/shm 语义由内核处理）。
- 尺寸改变或 magic 不符：重建/重映射，允许读者检测。

### 读取侧（Python，见 engine_fork.py）

- `mmap` 同一名字；读 header 校验 magic/尺寸；轮询 `write_idx`；
  取最新一帧 `idx = write_idx - 1`（若 write_idx 未动则复用缓存帧）。
- **对齐策略**：帧不带精确 tick。Python 在写 states.jsonl 时按 `wall_time` 就近匹配
  （每 tick Lua 上报 camera+状态，Python 用时间戳把图像贴到最近的状态行）。

## 3. bot 改为真实玩家驱动

- 客户端以玩家 `bot1` 自动连接服务器（`luanti --go --address 127.0.0.1 --port 30000 --name bot1`）。
- mod 侧：**移除加载期自动创建的逻辑 bot**，改为 `on_joinplayer` 创建会话；
  状态/动作/记录全部读真实玩家 ObjectRef。
- 动作：
  - 原始动作 `step`：`set_player_control`（移动/挖掘/放置）+ `set_look_*`（camera 增量）。
  - 语义动作：`goto`/`look_at` 用 `set_pos`/`set_look_*`；`dig`/`place` 用
    `core.dig_node(pos, player)`/`core.place_node(pos, node, player)`；
    `craft` 保持 M0 模拟合成（背包操作）。
- reset：用现成 `apply_player`（set_pos / 背包清空+give / set_hp / set_breath / timeofday）。
- 文件 IPC、begin/end_episode、record 落盘逻辑**保持不变**（m0_protocol.md §1-§5）。

## 4. 验收标准（random_agent.py）

1. 服务器 + 客户端都启动，`ready.json` 出现，bot1 加入会话。
2. `reset(task=collect_wood)` 返回 obs，`obs["image"]` 是 (H,W,3) 非纯色帧
   （方差 > 阈值，证明渲染真实）。
3. 随机原始动作循环 N 步：`player.pos` 随时间变化（证明 set_player_control 生效）。
4. 记录一个 episode，`observations/*.png` 非空，与 `states.jsonl` 的 frame 对齐。

## 5. 构建命令（在 luanti/ 下）

```bash
cmake -B build -DBUILD_CLIENT=TRUE -DBUILD_SERVER=TRUE \
      -DCMAKE_BUILD_TYPE=Release -DRUN_IN_PLACE=TRUE
cmake --build build -j$(sysctl -n hw.ncpu)
# 二进制：bin/luantiserver（server）、bin/luanti（client）
```

客户端依赖已装：sdl2 2.32.10 / libpng / jpeg-turbo 3.2.0 / libogg / libvorbis / freetype / zlib。

## 6. M1 之后衔接 M2

M2 数据管道依赖本阶段的真实图像帧：EpisodeWriter 写 observations/*.png、
meta.json 补 env 字段、导出工具（WebDataset / HuggingFace / RLDS / MineStudio 字段映射）、
用 agent 采集一小批轨迹并导出验证可加载。
