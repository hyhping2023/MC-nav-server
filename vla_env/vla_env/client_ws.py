"""与客户端 WebSocket 通信桩（M0 里程碑 + M1 通信底座）。

职责：Python ↔ Fabric 客户端 的 WebSocket 通道（DESIGN.md §6.2 / §9.2）：

- 下行（P→C）：`{"cmd": "mode|action|reset_camera|disconnect|state|set_key_log", ...}`；
  其中 `action` 为 §7.1 原始动作 dict。
- 上行（C→P）：
  - 帧：二进制消息 `[4B frame_id][4B server_tick][8B wall_nanos]
    [2B buttons][1B hotbar][2B yaw_delta][2B pitch_delta][JPEG bytes]`
    （§9.2 + M11 按键状态，M3 解码为观测 pov；M11 解码为 KeyState）；
  - 状态：`{"frame_id", "last_server_tick", "aimed_block", "held_item", "fps"}`；
  - 事件：`goto_status / path_debug / pillar_status / key_event`。

M1 已实现：`ClientWs` 用 `websockets.sync.client.connect` 建立连接，
`ping()` / `send_mode()` 打通 JSON 命令往返（pong / mode_ok），`close()`
发送 disconnect 后干净关闭；帧上行（recv_frame/recv_state）留待 M3。
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np
from PIL import Image
from websockets.sync.client import connect as _ws_connect

from .keys import HEADER_BYTES, KeyState, decode_keys


@dataclass(frozen=True)
class Frame:
    """一帧像素观测（M3，DESIGN.md §9.2 二进制帧协议）。

    - `frame_id`：客户端单调递增帧号（4B BE）
    - `server_tick`：帧采集时已知的最新服务端 tick（4B BE；M3 占位 0，M8 tick 对齐启用）
    - `wall_nanos`：采集墙钟时间戳（8B BE）
    - `rgb`：JPEG 解码后的 RGB 像素（HxWx3 uint8，默认 224×224）
    - `keys`：M11 帧采集时刻的按键状态（buttons 位掩码 + hotbar + 相机增量，
      帧↔按键按构造对齐）
    """

    frame_id: int
    server_tick: int
    wall_nanos: int
    rgb: np.ndarray
    keys: KeyState


def _decode_frame(raw: bytes) -> Frame:
    """解析一条二进制帧消息：`[4B frame_id][4B server_tick][8B wall_nanos]
    [2B buttons][1B hotbar][2B yaw_delta][2B pitch_delta][JPEG]`（23B 帧头）。"""
    if len(raw) < HEADER_BYTES:
        raise ValueError(f"二进制帧数据不足 {HEADER_BYTES}B 帧头: {len(raw)}B")
    frame_id = int.from_bytes(raw[0:4], "big", signed=False)
    server_tick = int.from_bytes(raw[4:8], "big", signed=False)
    wall_nanos = int.from_bytes(raw[8:16], "big", signed=False)
    keys = decode_keys(raw)
    jpeg = raw[HEADER_BYTES:]
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    rgb = np.asarray(img, dtype=np.uint8)
    return Frame(frame_id=frame_id, server_tick=server_tick,
                 wall_nanos=wall_nanos, rgb=rgb, keys=keys)


class ClientWs:
    """客户端 WS client（M1：websockets.sync 客户端）。

    - `ping()`：发 `{"cmd":"ping"}`，校验并返回 `{"type":"pong", ...}`。
    - `send_mode(mode)`：切 api/human 模式，返回 `{"type":"mode_ok", ...}`。
    - `close()`：发 disconnect（等服务端 bye）后关闭连接。
    """

    def __init__(self, url: str = "ws://127.0.0.1:30001") -> None:
        self.url = url
        self._conn = None
        # 单连接多读者互斥（demo 录帧线程收二进制帧 + 主线程 drain JSON goto_status）：
        # websockets.sync 非线程安全，所有 recv 串行化（逐条读、锁粒度最小）。
        self._recv_lock = threading.Lock()
        # 文本消息共享队列：recv_frame/recv_frame_latest 读到 JSON 文本（goto_status /
        # action_ok 等）时路由到这里而非丢弃，主线程 drain_json 消费——否则 demo 录帧
        # 线程会把 goto_status 事件当帧跳过、Python 永远收不到 arrived/blocked。
        self._text_q: queue.Queue = queue.Queue()

    def _recv(self, timeout: float):
        """串行化的底层读取（见 __init__ 的 _recv_lock 说明）。

        注意：threading.Lock 不可重入——必须在锁内做真实 recv，绝不能再次调用本方法
        （曾误写 `return self._recv(...)` 自递归 → 锁死整个 demo）。
        """
        assert self._conn is not None
        with self._recv_lock:
            return self._conn.recv(timeout=timeout)

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def __enter__(self) -> "ClientWs":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def connect(self) -> None:
        """建立 WS 连接（M1 已实现；惰性，重复调用幂等）。

        关闭 keepalive ping（ping_interval=None）：本地回环帧流生产速率高于
        消费速率（客户端满帧率上行 vs Python 每 step 收一帧），TCP 缓冲区堆积时
        客户端 WS 线程会阻塞写，导致 keepalive pong 长时间无法发出、连接被
        websockets 判死（1011）。M7 改为不主动 ping，由帧流消费驱动流控。
        """
        if self._conn is not None:
            return
        self._conn = _ws_connect(self.url, ping_interval=None)

    def _send_json(self, msg: Dict[str, Any]) -> None:
        self.connect()
        assert self._conn is not None
        try:
            self._conn.send(json.dumps(msg, ensure_ascii=False))
        except Exception:  # noqa: BLE001 —— 断线/阻塞等，重连一次再试
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self.connect()
            assert self._conn is not None
            self._conn.send(json.dumps(msg, ensure_ascii=False))

    def _recv_json(self, timeout: float = 5.0) -> Dict[str, Any]:
        """接收一条 JSON 上行（mode_ok / pong / camera_ok / state 等）。

        客户端以满帧率持续上送二进制帧（FrameSender 30fps），上一 step 收帧后
        与本步 send 之间会有若干二进制帧堆积在 WS 缓冲。文本/二进制是独立通道
        但同一连接内按发送顺序排队：直接 ``recv()`` 可能拿到残留的二进制帧，
        进而被 ``json.loads`` 当 utf-32/utf-8 解析报错（M8 复现：episode 间
        env.reset 失败 5 次后才拿到 mode_ok）。

        修复：跳过 bytes（二进制帧归 recv_frame 消费），只把 str 交给 json.loads。
        """
        assert self._conn is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"_recv_json: {timeout}s 内未收到 JSON 文本上行")
            try:
                raw = self._recv(timeout=remaining)
            except TimeoutError:
                raise TimeoutError(f"_recv_json: {timeout}s 内未收到 JSON 文本上行")
            if isinstance(raw, (bytes, bytearray)):
                # 二进制帧（pixel frame），跳过继续等 JSON 文本
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 乱码/非 JSON 文本，跳过继续等
                continue
            if isinstance(data, dict):
                return data
            # 非 dict 的 JSON（如 list/字符串）也跳过
            continue

    def ping(self) -> Dict[str, Any]:
        """Ping：发送 `{"cmd":"ping"}` 并接收一条 JSON，校验 type=="pong"。

        返回 pong dict：`{"type": "pong", "ts": <epoch_ms>, "api_mode": <bool>}`。
        """
        self._send_json({"cmd": "ping"})
        reply = self._recv_json()
        if reply.get("type") != "pong":
            raise ValueError(f"预期 pong，收到: {reply!r}")
        return reply

    def send_mode(self, mode: str) -> Dict[str, Any]:
        """切换 api/human 模式，返回 mode_ok dict。

        仅接受 `"api"` / `"human"`（服务端同样校验并回 error）。
        """
        if mode not in ("api", "human"):
            raise ValueError(f"非法 mode: {mode!r}（仅支持 api/human）")
        self._send_json({"cmd": "mode", "mode": mode})
        reply = self._recv_json()
        if reply.get("type") != "mode_ok":
            raise ValueError(f"预期 mode_ok，收到: {reply!r}")
        return reply

    def send(self, msg: Dict[str, Any]) -> None:
        """下行 JSON 消息（M1 已实现，供 M2 动作下发复用）。"""
        self._send_json(msg)

    def send_action(self, action: Dict[str, Any]) -> None:
        """下行原始动作 dict（M7）：经 action_space.to_ws 转 `{"cmd":"action",...}`。

        注意客户端 ActionCmd#getBool 用 Gson getAsBoolean 解析，按键必须是真布尔
        （整数 1 会被解析为 false），to_ws 负责转换。
        """
        from .action_space import to_ws

        self._send_json(to_ws(action))

    def send_goto_path(self, waypoints: list, dig: Optional[list] = None) -> None:
        """下行 goto_path：启动客户端本地路径跟随（有序整数方块坐标）。

        客户端 NavExecutor 逐 tick 转向/侧移/跳跃 + 碰撞箱 + 到达/卡死检测，
        经 goto_status 事件上报；block 任务 approach 用它替代 Python 逐 step 驱动。
        dig=计划要挖的方块列表（客户端只挖这些，杜绝乱挖掘）。元素两种格式：
        `[x, y, z]` 三元组（无工具信息，客户端按方块 tag 自动选工具）或
        `{"x","y","z","block"?,"tool"?}` dict（M11.5 规划器标注工具，难点④）。
        """
        msg = {
            "cmd": "goto_path",
            "waypoints": [[int(x), int(y), int(z)] for x, y, z in waypoints],
        }
        if dig:
            out = []
            for d in dig:
                if isinstance(d, dict):
                    item = {"x": int(d["x"]), "y": int(d["y"]), "z": int(d["z"])}
                    if d.get("block"):
                        item["block"] = str(d["block"])
                    if d.get("tool"):
                        item["tool"] = str(d["tool"])
                    out.append(item)
                else:
                    x, y, z = d
                    out.append({"x": int(x), "y": int(y), "z": int(z)})
            msg["dig"] = out
        self._send_json(msg)

    def send_goto_cancel(self) -> None:
        """下行 goto_cancel：停止客户端本地路径跟随（幂等）。"""
        self._send_json({"cmd": "goto_cancel"})

    def send_pillar_up(self, target_y: Optional[int] = None, max_blocks: int = 8,
                       item: Optional[str] = "minecraft:dirt") -> None:
        """下行 pillar_up：启动客户端垫方块爬高技能（M11）。

        客户端 PillarExecutor 逐 tick 执行「挖头顶 fy+2 → 视角朝正下 → 原地跳 →
        到跳跃顶点（Δy≥1.05 且 vy≤0.02）放一块 → 落到块上」循环，每轮净升 1 格。

        必须在客户端做而不是 Python：一个 step = 2 服务端 tick + 往返延迟 ≈ 5-10 tick，
        而放置窗口只有跳跃的第 3~8 tick（Δy>1.0 时脚格才空出、碰撞箱才不与目标格相交）。

        进度/结束经 pillar_status 事件上行（drain_json 消费）：
        {"type":"pillar_status","state":"progress|done|failed|cancelled",
         "placed":n,"feet_y":y,"reason":"...","detail":"..."}

        target_y=None 表示不按高度停（只受 max_blocks 约束）。
        """
        msg: Dict[str, Any] = {"cmd": "pillar_up", "max_blocks": int(max_blocks)}
        if target_y is not None:
            msg["target_y"] = int(target_y)
        if item:
            msg["item"] = item
        self._send_json(msg)

    def send_pillar_cancel(self) -> None:
        """下行 pillar_cancel：停止客户端垫方块爬高（幂等）。"""
        self._send_json({"cmd": "pillar_cancel"})

    def send_state(self) -> None:
        """下行 state：请求客户端状态上行（aimed_block/held_item/fps/selected_slot）。

        客户端回 `{"type":"state", ...}` 文本；注意它与二进制帧/其他文本共享连接，
        由 recv_state 或 drain_json 消费（见 recv_state 的二进制跳过逻辑）。
        """
        self._send_json({"cmd": "state"})

    def send_set_key_log(self, enabled: bool) -> None:
        """下行 set_key_log：开关客户端 key_event 按键事件上行（人类演示录制）。"""
        self._send_json({"cmd": "set_key_log", "enabled": bool(enabled)})

    def drain_json(self, timeout: float = 0.0, idle: float = 0.02) -> list:
        """排空待处理 JSON 文本上行（goto_status / action_ok / look_ok 等）。

        文本消息由 recv_frame/recv_frame_latest 路由到共享 {@code _text_q}（绝不丢弃），
        本方法只读队列——不直接读 socket，避免与录帧线程抢帧/丢帧。返回 dict 列表。
        """
        out: list = []
        while True:
            try:
                raw = self._text_q.get_nowait()
            except queue.Empty:
                break
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(data, dict):
                out.append(data)
        return out

    def recv_frame(self, timeout: float = 2.0) -> Optional[Frame]:
        """阻塞接收一帧上行帧（M3：二进制帧头解析 + JPEG 解码）。

        二进制协议（DESIGN.md §9.2）：`[4B frame_id BE][4B server_tick BE]
        [8B wall_nanos BE][JPEG bytes]`。

        - 上行中可能夹杂 JSON 文本（action_ok / pong / state 等），自动跳过；
        - 超时未收到帧返回 `None`；数据不足 16B 报错。
        """
        assert self._conn is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = self._recv(timeout=remaining)
            except TimeoutError:
                return None
            if not isinstance(raw, bytes):
                # JSON 文本（goto_status/action_ok 等）路由到共享队列供 drain_json 消费，
                # 绝不丢弃——否则 demo 录帧线程会把 goto_status 事件当帧跳过。
                self._text_q.put(raw)
                continue
            if len(raw) < HEADER_BYTES:
                raise ValueError(f"二进制帧数据不足 {HEADER_BYTES}B 帧头: {len(raw)}B")
            return _decode_frame(raw)

    def recv_frame_latest(self, timeout: float = 2.0, drain_window: float = 0.03) -> Optional[Frame]:
        """收一帧并尽量排空积压帧（M7：帧流生产 >> 消费时的流控兜底）。

        客户端以满帧率上行、Python 每 step 收一帧，TCP 缓冲区会逐渐堆积导致
        客户端 WS 写阻塞（进而 keepalive 无 pong）。本方法在收到**首帧**后只再等
        {@code drain_window} 短窗口排空积压（消费掉但返回最新一帧），随即返回，
        保持 Python 侧读缓冲低位、且 step 不被 2s 帧等待拖慢。

        返回最新 Frame；超时未收到任何帧返回 None（跳过 JSON 文本消息）。
        """
        assert self._conn is not None
        deadline = time.monotonic() + timeout
        first: Optional[Frame] = None
        # 1) 等首帧（JSON 文本路由到共享队列，不丢）
        while first is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = self._recv(timeout=remaining)
            except TimeoutError:
                return None
            if isinstance(raw, bytes) and len(raw) >= HEADER_BYTES:
                first = _decode_frame(raw)
            elif not isinstance(raw, bytes):
                self._text_q.put(raw)  # JSON 文本 → 共享队列
        # 2) 短窗口排空积压，返回最新一帧
        last = first
        idle = time.monotonic() + drain_window
        while True:
            rem = idle - time.monotonic()
            if rem <= 0:
                return last
            try:
                more = self._recv(timeout=rem)
            except TimeoutError:
                return last
            if isinstance(more, bytes) and len(more) >= HEADER_BYTES:
                last = _decode_frame(more)
            elif not isinstance(more, bytes):
                self._text_q.put(more)  # JSON 文本 → 共享队列

    def recv_state(self, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        """接收状态 JSON 上行（aimed_block / held_item / fps 等）。

        状态上行是文本消息；若收到二进制帧则丢弃继续等 JSON。
        """
        assert self._conn is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = self._recv(timeout=remaining)
            except TimeoutError:
                return None
            if isinstance(raw, bytes):
                continue  # 二进制帧，跳过继续等 JSON 状态
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    def close(self) -> None:
        """断开连接：发 disconnect（等服务端 bye）再关闭（M1 已实现）。"""
        if self._conn is None:
            return
        conn = self._conn
        self._conn = None
        try:
            conn.send(json.dumps({"cmd": "disconnect"}, ensure_ascii=False))
            # 服务端会回 {"type":"bye"} 再关闭会话；尽力读一条，防止竞态残留。
            with self._recv_lock:
                conn.recv(timeout=1.0)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
