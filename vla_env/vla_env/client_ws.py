"""与客户端 WebSocket 通信桩（M0 里程碑 + M1 通信底座）。

职责：Python ↔ Fabric 客户端 的 WebSocket 通道（DESIGN.md §6.2 / §9.2）：

- 下行（P→C）：`{"cmd": "mode|action|reset_camera|disconnect", ...}`；
  其中 `action` 为 §7.1 原始动作 dict。
- 上行（C→P）：
  - 帧：二进制消息 `[4B frame_id][4B server_tick][8B wall_nanos][JPEG bytes]`
    （§9.2，M3 解码为观测 pov）；
  - 状态：`{"frame_id", "last_server_tick", "aimed_block", "held_item", "fps"}`。

M1 已实现：`ClientWs` 用 `websockets.sync.client.connect` 建立连接，
`ping()` / `send_mode()` 打通 JSON 命令往返（pong / mode_ok），`close()`
发送 disconnect 后干净关闭；帧上行（recv_frame/recv_state）留待 M3。
"""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np
from PIL import Image
from websockets.sync.client import connect as _ws_connect


@dataclass(frozen=True)
class Frame:
    """一帧像素观测（M3，DESIGN.md §9.2 二进制帧协议）。

    - `frame_id`：客户端单调递增帧号（4B BE）
    - `server_tick`：帧采集时已知的最新服务端 tick（4B BE；M3 占位 0，M8 tick 对齐启用）
    - `wall_nanos`：采集墙钟时间戳（8B BE）
    - `rgb`：JPEG 解码后的 RGB 像素（HxWx3 uint8，默认 224×224）
    """

    frame_id: int
    server_tick: int
    wall_nanos: int
    rgb: np.ndarray


def _decode_frame(raw: bytes) -> Frame:
    """解析一条二进制帧消息：`[4B frame_id BE][4B server_tick BE][8B wall_nanos BE][JPEG]`。"""
    frame_id = int.from_bytes(raw[0:4], "big", signed=False)
    server_tick = int.from_bytes(raw[4:8], "big", signed=False)
    wall_nanos = int.from_bytes(raw[8:16], "big", signed=False)
    jpeg = raw[16:]
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    rgb = np.asarray(img, dtype=np.uint8)
    return Frame(frame_id=frame_id, server_tick=server_tick,
                 wall_nanos=wall_nanos, rgb=rgb)


class ClientWs:
    """客户端 WS client（M1：websockets.sync 客户端）。

    - `ping()`：发 `{"cmd":"ping"}`，校验并返回 `{"type":"pong", ...}`。
    - `send_mode(mode)`：切 api/human 模式，返回 `{"type":"mode_ok", ...}`。
    - `close()`：发 disconnect（等服务端 bye）后关闭连接。
    """

    def __init__(self, url: str = "ws://127.0.0.1:30001") -> None:
        self.url = url
        self._conn = None

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
                raw = self._conn.recv(timeout=remaining)
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
                raw = self._conn.recv(timeout=remaining)
            except TimeoutError:
                return None
            if not isinstance(raw, bytes):
                continue  # JSON 文本（action_ok/mode_ok 等），跳过继续等帧
            if len(raw) < 16:
                raise ValueError(f"二进制帧数据不足 16B 帧头: {len(raw)}B")
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
        # 1) 等首帧（忽略 JSON 文本）
        while first is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = self._conn.recv(timeout=remaining)
            except TimeoutError:
                return None
            if isinstance(raw, bytes) and len(raw) >= 16:
                first = _decode_frame(raw)
            # 非 bytes（JSON）或坏帧头跳过继续等
        # 2) 短窗口排空积压，返回最新一帧
        last = first
        idle = time.monotonic() + drain_window
        while True:
            rem = idle - time.monotonic()
            if rem <= 0:
                return last
            try:
                more = self._conn.recv(timeout=rem)
            except TimeoutError:
                return last
            if isinstance(more, bytes) and len(more) >= 16:
                last = _decode_frame(more)

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
                raw = self._conn.recv(timeout=remaining)
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
            conn.recv(timeout=1.0)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
