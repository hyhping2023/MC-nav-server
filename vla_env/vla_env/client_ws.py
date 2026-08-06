"""与客户端 WebSocket 通信桩（M0 里程碑）。

职责：Python ↔ Fabric 客户端 的 WebSocket 通道（DESIGN.md §6.2 / §9.2）：

- 下行（P→C）：`{"cmd": "mode|action|reset_camera|disconnect", ...}`；
  其中 `action` 为 §7.1 原始动作 dict。
- 上行（C→P）：
  - 帧：二进制消息 `[4B frame_id][4B server_tick][8B wall_nanos][JPEG bytes]`
    （§9.2，M3 解码为观测 pov）；
  - 状态：`{"frame_id", "last_server_tick", "aimed_block", "held_item", "fps"}`。

依赖里程碑：M1（通信底座，心跳/断线清理）→ M2（动作下发）→ M3（帧上行）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ClientWs:
    """客户端 WS client 桩。

    - `send(msg)`：下行 JSON 消息。
    - `recv_frame(timeout) -> bytes | None`：阻塞读取一帧二进制上行。
    - `recv_state(timeout) -> dict | None`：读取状态 JSON 上行。
    """

    def __init__(self, url: str = "127.0.0.1:30001") -> None:
        self.url = url
        self.connected = False

    def connect(self) -> None:
        """建立 WS 连接。依赖 M1：websockets 客户端 + 心跳。"""
        raise NotImplementedError("M1 实现：建立 WebSocket 连接")

    def send(self, msg: Dict[str, Any]) -> None:
        """下行动作/模式指令。

        依赖 M2：action_space.to_ws 的产物在此下发。
        """
        raise NotImplementedError("M1 实现：下行 JSON 发送")

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
        """断开连接。依赖 M1。"""
        raise NotImplementedError("M1 实现：断开 WebSocket")
