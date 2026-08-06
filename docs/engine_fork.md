# 引擎 fork 指南（Luanti → mcl2-agent fork）

> 目标：以**最小 C++ 改动**补齐三个能力，其余全部用 Lua mod + Python 实现。
> 参考：Craftium (ICML 2025) 的等价实现（GPL-3.0，注意许可证兼容）。
>
> 当前引擎版本基于仓库内 `luanti/`（5.12.x）。

## 0. 改动总览

| 编号 | 改动 | 文件 | 用途 |
|---|---|---|---|
| A | `core.set_player_control(player, controls)` | `src/script/lua_api/l_object.cpp` + `src/script/lua_api/l_object.h` | 服务器注入按键（走真实输入管线） |
| B | 客户端抓帧 → 共享内存环形缓冲 | `src/client/renderingengine.cpp/h` + `src/client/client.h` | 第一人称 RGB → Python |
| C | （可选）`core.freeze_environment()` | `src/serverenvironment.cpp` | 数据采集时暂停生物/掉落物 |
| D | （可选）TCP bridge 传输 | 新增 `src/script/lua_api/l_bridge.cpp` | 替代文件轮询传输 |

> 不做 `set_player_control` 时：动作层降级为 `set_pos` 瞬移 + `dig_node/place_node`，
> 仅支持语义任务（无法跑步/跳跃等物理行为）。见 DESIGN.md §11。

## 1. 改动 A：服务器按键注入

### 背景

引擎的玩家输入模型：客户端每 tick 把按键打包成 `PlayerControl` 发给服务器，
服务器存到 `RemotePlayer::control`（类型 `PlayerControl`），在 `Player::step`
中消费（`src/player.h:44`）。**服务器侧目前没有写入口**。

### 实现要点

1. 在 `src/script/lua_api/l_object.cpp` 注册新方法（挂在 `ObjectRef` 上）：

```cpp
// l_object.cpp（伪代码）
int ObjectRef::l_set_player_control(lua_State *L)
{
    NO_MAP_LOCK_REQUIRED;
    ObjectRef *ref = checkObject<ObjectRef>(L, 1);
    if (ref->getObject() == nullptr)
        return 0;
    RemotePlayer *player = ...;  // 由 getPlayer() 取得
    PlayerControl &c = player->control;   // src/player.h

    luaL_checktype(L, 2, LUA_TTABLE);
    c.up     = getfloatfield(L, 2, "up", c.up);
    c.down   = getfloatfield(L, 2, "down", c.down);
    c.left   = getfloatfield(L, 2, "left", c.left);
    c.right  = getfloatfield(L, 2, "right", c.right);
    c.jump   = getboolfield(L, 2, "jump", c.jump);
    c.sneak  = getboolfield(L, 2, "sneak", c.sneak);
    c.aux1   = getboolfield(L, 2, "sprint", c.aux1);  // aux1 对应疾跑键
    c.dig    = getboolfield(L, 2, "dig", c.dig);
    c.place  = getboolfield(L, 2, "place", c.place);
    return 0;
}
```

2. 在 `l_object.h` 的 `ObjectRef` 注册表加 `set_player_control`。

3. 关键约束：
   - `PlayerControl::pitch/yaw` **服务器不可用**（源码注释）→ 视角仍用现有 `set_look_*`。
   - 特权校验：该 API 应要求 `mcl2_agent` 特权或 `give`，防止任意玩家被操控。
   - 注入值在下一 tick 被客户端实际按键覆盖前是有效的——数据采集时用 `core.set_player_control` 每个 tick 重新写即可。

### 观测辅助

- `player:get_velocity()`：服务器对玩家速度估计不精确。**推荐**在 Lua 侧用位置差分
  （`(pos - prev_pos) / dtime`）计算，无需改引擎。若要精确速度，走改动 B 的客户端上报通道。

## 2. 改动 B：客户端抓帧

### 背景

`src/client/renderingengine.h:131` 的 `draw_scene(...)` 是每帧渲染入口。
在 draw 之后读 FBO 即可拿到第一人称画面。

### 实现要点

1. 渲染管线（`src/client/renderingengine.cpp`）中 `draw_scene()` 末尾：

```cpp
// 伪代码
void RenderingEngine::draw_scene(...)
{
    // ...现有渲染...
    m_client->getCamera()->...;  // 拿到视口大小
    if (m_frame_grabber) {
        video::IImage *img = m_device->getVideoDriver()->createImageFromScreen(...);
        m_frame_grabber->submit(img);  // 缩放到 224x224, 转 BGRA, 写环形缓冲
    }
}
```

2. 帧共享协议（与 `mcl2_env/renderer/engine_fork.py` 对齐）：

```
共享内存命名: /mcl2_frames（POSIX shm，mmap）
环形缓冲头（struct，C 布局）:
  uint32 magic = 0x4D434C32
  uint32 width, height, stride   // stride = width*4（BGRA）
  uint32 depth                   // 环形深度，建议 32
  uint64 read_idx, write_idx     // 单调递增序号（%depth 定位槽位）
帧体: stride * height * depth bytes

通知: eventfd / pipe 写一个字节（可选，避免轮询）
```

3. 元数据对齐：
   - 帧落盘序号 = 服务器 `tick` 由 Lua 侧 `get_player_control` 读到的 tick 对齐。
   - 客户端可把 `server_tick`（从 `RemoteClient::getLastSentPosition` 或握手时间戳）
     一并写入帧头扩展字段，Python 用它在 `states.jsonl` 里对齐。
   - 简化方案：Lua 每 tick 上报相机参数（`vision.lua`），Python 用"最近 tick 的相机"
     标注图像即可，误差 <1 tick。

4. 采集模式开关：`client.setFrameGrab(enabled)` 或由 Python 通过 env 变量
   `MCL2_FRAME_SHM` / `MCL2_FRAME_EVENT_FD` 开启。

## 3. 改动 C（可选）：冻结环境

数据采集要求确定性（DESIGN.md §9）：
- 冻结生物 AI：遍历 `ServerEnvironment::m_active_objects`，跳过 `on_step` 或
  设 `m_abm_timeout` 拉大。
- 冻结掉落物物理：设 `m_abm_timeout` 或跳过 item entity 的 `on_step`。
- 冻结时间：`Env->setTimeOfDaySpeed(0)` 已有 Lua API（`set_time_speed`），无需改引擎。

实现建议：优先用 Lua 层 `minetest.set_time_speed(0)` + 关闭天气随机 +
ABM 全局禁用（Mineclonia 提供 `minetest.register_abm` 的运行时开关较难，可只冻生物 spawn 间距）。

## 4. 改动 D（可选）：TCP bridge 传输

Lua 侧 `bridge.lua` 已实现协议层与文件轮询降级。要换 TCP：

1. 新增 `src/script/lua_api/l_bridge.cpp`：
   - `core.bridge_listen(port)`：在专用线程开 `socket`，只接受本地回环。
   - `core.bridge_send(frame)`：把编码帧推给 Python 端（write + 长度前缀）。
   - 收到帧后 `ModApiBridge` 回调 Lua 的 `bridge.on_frame`。
2. 注意：引擎主线程与 socket 线程之间用队列 + mutex 交接，帧不超过 Lua 栈。
3. 这比在 Lua 里做 `luasocket` 干净：不引入外部依赖，且能复用 `l_ipc.cpp`
   （已存在的 mod IPC store）作为内部通道。

## 5. 构建与验证

```bash
cd luanti
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(sysctl -n hw.ncpu)
```

冒烟验证：
1. 起服务器 + 打补丁的客户端，确认 `core.set_player_control` 能让 bot 前进/跳跃。
2. 启动 `mcl2_env/examples/random_agent.py`，确认观测图像帧存在且与 `states.jsonl`
   的 `tick` 对齐。
3. 检查 `meta.json` 里 `world_seed` 与两次采集的同一 `reset_seed` 下画面一致。

## 6. 许可证注意

- Luanti 引擎：LGPL-2.1（可 fork，改动保留版权头）。
- Craftium 的 patch 思路可参考，但其代码 GPL-3.0，**不要直接复制代码**，按本指南自行实现。
- Mineclonia 游戏：GPL-3.0（游戏内容与引擎分开，互不影响许可证）。
- `mcl2_agent` mod 与 `mcl2_env` Python 包：MIT（自研）。
