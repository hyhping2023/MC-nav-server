"""与 Lua 侧 mcl2_agent/api/bridge.lua 的 TCP 客户端。

帧协议（与 bridge.lua 一致）：
    帧 = [4B big-endian length][1B type][JSON body]
    type: 'r'=request, 'p'=response, 'e'=event

阻塞、线程安全（request 串行）。图像帧不走此通道（见 renderer/）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("mcl2_env.bridge")


class BridgeError(RuntimeError):
    pass


class BridgeClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 25585,
        timeout: float = 10.0,
        auto_reconnect: bool = True,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.auto_reconnect = auto_reconnect
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._lock = threading.Lock()
        self._req_id = 0
        self._event_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._connected = False

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        if self._sock:
            return
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._connected = True
        log.info("bridge connected to %s:%d", self.host, self.port)

    def close(self) -> None:
        with self._lock:
            if self._sock:
                self._sock.close()
                self._sock = None
                self._connected = False

    # ------------------------------------------------------------ framing

    @staticmethod
    def _encode(msg_type: bytes, payload: dict[str, Any]) -> bytes:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return struct.pack(">I", len(body) + 1) + msg_type + body

    def _send_frame(self, frame: bytes) -> None:
        with self._lock:
            if not self._sock:
                raise BridgeError("bridge not connected")
            self._sock.sendall(frame)

    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self._sock.recv(remaining)  # type: ignore[union-attr]
            if not chunk:
                raise ConnectionError("bridge closed connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _recv_frame(self) -> dict[str, Any]:
        while True:
            while len(self._buf) < 5:
                try:
                    self._buf += self._recv_exact(64 * 1024)
                except ConnectionError:
                    if self.auto_reconnect:
                        self._reconnect()
                        self._buf = b""
                        continue
                    raise
            (length,) = struct.unpack(">I", self._buf[:4])
            msg_type = self._buf[4:5]
            if len(self._buf) < 4 + length:
                self._buf += self._recv_exact(4 + length - len(self._buf))
            body = json.loads(self._buf[5 : 4 + length].decode("utf-8"))
            self._buf = self._buf[4 + length :]
            return body

    def _reconnect(self) -> None:
        log.warning("bridge connection lost, reconnecting...")
        self._sock = None
        self._connected = False
        self._connect_locked()

    # ------------------------------------------------------------ API

    def request(self, op: str, **kwargs: Any) -> Any:
        """发送请求并等待响应。"""
        with self._lock:
            if not self._connected:
                self._connect_locked()
            self._req_id += 1
            req_id = self._req_id
            self._sock.sendall(self._encode(b"r", {"req_id": req_id, "op": op, **kwargs}))  # type: ignore[union-attr]

        # 同步等待响应（期间可处理事件帧）
        deadline = None
        while True:
            frame = self._recv_frame()
            if frame.get("req_id") == req_id:
                if not frame.get("ok"):
                    raise BridgeError(frame.get("result", {}).get("error", "unknown error"))
                return frame.get("result")
            if frame.get("event"):
                for handler in self._event_handlers:
                    handler(frame)

    def on_event(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._event_handlers.append(handler)

    # ------------------------------------------------------------ convenience

    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def observe(self, player: str = "bot1") -> dict[str, Any]:
        return self.request("observe", player=player)

    def tasks(self) -> list[dict[str, Any]]:
        return self.request("tasks")["tasks"]

    def begin_episode(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self.request("begin_episode", **spec)

    def end_episode(self, success: bool, player: str = "bot1") -> None:
        self.request("end_episode", player=player, success=success)

    def execute(self, action: str, args: dict[str, Any] | None = None, player: str = "bot1") -> dict[str, Any]:
        return self.request("execute", player=player, action=action, args=args or {})

    def step(self, primitive: dict[str, Any], player: str = "bot1") -> dict[str, Any]:
        return self.request("step", player=player, primitive=primitive)

    def set_config(self, value: dict[str, Any]) -> None:
        self.request("set_config", value=value)


def _event_num_key(name: str) -> int:
    """从 ev_<seq>.json 提取 seq 用于排序（未命中按 0 处理）。"""
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else 0


class FileBridgeClient:
    """基于文件 IPC 的客户端（M0 实现，契约见 docs/m0_protocol.md）。

    与 TCP 版 BridgeClient 的消息体同构（{"req_id", "op", ...}），仅传输层不同。

    IPC 根目录：<world>/mcl2_agent/ipc/
        ready.json                     Lua 启动后写入 {"ready": true, "version": "..."}
        requests/req_<seq>.json        Python → Lua；Lua 处理后删除
        responses/resp_<req_id>.json   Lua → Python；Python 读后删除
        events/ev_<seq>.json           Lua → Python；Python 读后删除

    写文件一律"临时名 + os.replace"原子替换，读端用 UTF-8、ensure_ascii=False。
    """

    def __init__(
        self,
        world_dir: str | os.PathLike[str],
        timeout: float = 30.0,
        poll_interval: float = 0.1,
    ):
        self.world_dir = Path(world_dir)
        self.ipc_root = self.world_dir / "mcl2_agent" / "ipc"
        self.requests_dir = self.ipc_root / "requests"
        self.responses_dir = self.ipc_root / "responses"
        self.events_dir = self.ipc_root / "events"
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._req_id = 0
        self._seq = 0
        for d in (self.requests_dir, self.responses_dir, self.events_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """文件传输无长连接，无需清理。"""

    # ------------------------------------------------------------ ready

    def wait_ready(self, timeout: float | None = None) -> dict[str, Any]:
        """轮询 ready.json，读到后用 ping 握手确认服务器真正就绪；超时抛 BridgeError。

        握手原因：ready.json 可能来自上一会话残留（新服务器尚未完成 init 的 reset，
        会清掉 Python 刚写入的首个请求）。读到 ready 后发 ping，若被 reset 吃掉则
        重试，直到 ping 返回——保证调用方后续请求不会被 reset 清掉。
        """
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        path = self.ipc_root / "ready.json"
        while time.monotonic() < deadline:
            data = self._try_read_json(path)
            if data is not None and data.get("ready"):
                # 握手：短超时 ping，被 reset 吞掉就继续轮询
                if self._handshake_ping(short_timeout=5.0):
                    log.info("bridge (file) ready: %s", data.get("version"))
                    return data
            time.sleep(self.poll_interval)
        raise BridgeError(f"server not ready within {timeout:.0f}s (missing {path})")

    def _handshake_ping(self, short_timeout: float) -> bool:
        """发一次 ping；请求被 reset 吞掉（文件消失无响应）时返回 False。"""
        try:
            with self._lock:
                self._req_id += 1
                self._seq += 1
                req_id, seq = self._req_id, self._seq
                self._atomic_write(
                    self.requests_dir / f"req_{seq}.json",
                    {"req_id": req_id, "op": "ping"},
                )
                return self._wait_response("ping", req_id, short_timeout) is not None
        except BridgeError:
            return False

    # ------------------------------------------------------------ request / response

    def request(self, op: str, timeout: float | None = None, **kwargs: Any) -> Any:
        """同步请求。req_id 与 resp_<req_id> 文件名一一对应，读后删除。"""
        with self._lock:
            self._req_id += 1
            self._seq += 1
            req_id, seq = self._req_id, self._seq
            self._atomic_write(
                self.requests_dir / f"req_{seq}.json",
                {"req_id": req_id, "op": op, **kwargs},
            )
            return self._wait_response(op, req_id, timeout)

    def _wait_response(self, op: str, req_id: int, timeout: float | None = None) -> Any:
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        path = self.responses_dir / f"resp_{req_id}.json"
        while time.monotonic() < deadline:
            data = self._try_read_json(path)
            if data is not None:
                self._safe_unlink(path)
                if not data.get("ok"):
                    err = (data.get("result") or {}).get("error", "unknown error")
                    raise BridgeError(f"{op}: {err}")
                return data.get("result")
            time.sleep(self.poll_interval)
        raise BridgeError(f"{op}: timeout waiting response req_id={req_id} (> {timeout:.0f}s)")

    # ------------------------------------------------------------ events

    def poll_events(self) -> list[dict[str, Any]]:
        """读取 events/ev_*.json 并删除，返回事件列表（按 seq 排序）。"""
        events: list[dict[str, Any]] = []
        try:
            paths = sorted(
                self.events_dir.glob("ev_*.json"),
                key=lambda p: _event_num_key(p.name),
            )
        except OSError:
            return events
        for path in paths:
            data = self._try_read_json(path)
            if data is None:
                continue  # 半截文件（对方正在写），留给下次轮询
            self._safe_unlink(path)
            events.append(data)
        return events

    # ------------------------------------------------------------ convenience

    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def observe(self, player: str = "bot1") -> dict[str, Any]:
        return self.request("observe", player=player)

    def tasks(self) -> list[dict[str, Any]]:
        return self.request("tasks")["tasks"]

    def begin_episode(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self.request("begin_episode", **spec)

    def end_episode(self, success: bool, player: str = "bot1") -> dict[str, Any]:
        return self.request("end_episode", player=player, success=success)

    def execute(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        player: str = "bot1",
    ) -> dict[str, Any]:
        return self.request("execute", player=player, action=action, args=args or {})

    def step(self, primitive: dict[str, Any], player: str = "bot1") -> dict[str, Any]:
        return self.request("step", player=player, primitive=primitive)

    def set_config(self, value: dict[str, Any]) -> dict[str, Any]:
        return self.request("set_config", value=value)

    # ------------------------------------------------------------ internals

    @staticmethod
    def _try_read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        """临时名 + os.replace 原子写，ensure_ascii=False。"""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)
