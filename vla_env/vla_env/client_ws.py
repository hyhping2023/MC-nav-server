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

import json
from typing import Any, Dict, Optional

from websockets.sync.client import connect as _ws_connect


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
        """建立 WS 连接（M1 已实现；惰性，重复调用幂等）。"""
        if self._conn is not None:
            return
        self._conn = _ws_connect(self.url)

    def _send_json(self, msg: Dict[str, Any]) -> None:
        self.connect()
        assert self._conn is not None
        self._conn.send(json.dumps(msg, ensure_ascii=False))

    def _recv_json(self, timeout: float = 5.0) -> Dict[str, Any]:
        assert self._conn is not None
        raw = self._conn.recv(timeout=timeout)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"非 JSON 对象上行: {raw!r}")
        return data

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

    def recv_frame(self, timeout: float = 2.0) -> Optional[bytes]:
        """阻塞接收一帧上行帧（二进制）。

        依赖 M3：解析 `[4B frame_id][4B tick][8B wall_nanos][JPEG]` 帧头。
        """
        raise NotImplementedError("M3 实现：帧头解析 + 二进制帧接收")

    def recv_state(self, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        """接收状态 JSON 上行。

        依赖 M3：aimed_block / held_item / fps 等客户端侧状态。
        """
        raise NotImplementedError("M3 实现：状态 JSON 接收")

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
