"""与服务端 gRPC 通信桩（M0 里程碑）。

职责：Python ↔ Purpur 插件 的 gRPC 通道（DESIGN.md §6.2 / §9.1），
调用 `vla.proto` 定义的 `VlaServer` 服务：

    ResetWorld / GetStepResult / GetState / GetVoxels / ComputePath /
    SetTask / GenerateTask / ClearRegion / Teleport / SpawnEntity / SetBlock

关键约定（§4.2）：服务端写操作一律主线程执行；reward/done 以服务端为权威
（§14.2），`GetStepResult` 阻塞等待 k ticks 结算。

依赖里程碑：M1（通信底座，gRPC 连接 + 生成 vla_pb2/vla_pb2_grpc）→
M4（ResetEngine/GetState）→ M5（TaskManager）→ M6（GetVoxels/ComputePath）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ServerGrpc:
    """服务端 gRPC client 桩。方法签名与 proto RPC 一一对应。

    依赖 M1：grpcio 客户端 + vla_env.proto 生成的 stub。
    """

    def __init__(self, address: str = "127.0.0.1:50051", player: str = "agent0") -> None:
        self.address = address
        self.player = player

    def connect(self) -> None:
        """建立 gRPC 通道 + stub。依赖 M1：grpcio.Channel。"""
        raise NotImplementedError("M1 实现：grpc 通道 + VlaServerStub")

    def reset_world(self, task: Optional[str] = None, seed: Optional[int] = None) -> Dict[str, Any]:
        """ResetWorld：重置世界与任务，返回 ResetReply（含 server_tick）。

        依赖 M4：ResetEngine L1 区域快照回滚。
        """
        raise NotImplementedError("M4 实现：调用 ResetWorld RPC")

    def get_step_result(self, await_ticks: int = 4) -> Dict[str, Any]:
        """GetStepResult：阻塞等待 k ticks 后结算。

        返回 dict（对齐 proto StepReply）：reward / terminated / truncated /
        server_tick / progress / info。
        依赖 M5：TaskManager 服务端事件判定。
        """
        raise NotImplementedError("M5 实现：调用 GetStepResult RPC")

    def get_state(self) -> Dict[str, Any]:
        """GetState：玩家/背包/统计等本地状态。

        依赖 M4：player/inventory/stats/compass（§8）。
        """
        raise NotImplementedError("M4 实现：调用 GetState RPC")

    def get_voxels(self) -> Dict[str, Any]:
        """GetVoxels：32³ 服务端体素，返回 {"palette", "data"}。

        依赖 M6：NMS 体素读取 + palette 编码。
        """
        raise NotImplementedError("M6 实现：调用 GetVoxels RPC")

    def compute_path(self, start: Any, goal: Any) -> list:
        """ComputePath：3D A* 寻路，返回航点列表。

        依赖 M6：服务端 AStar 输出拐点序列。
        """
        raise NotImplementedError("M6 实现：调用 ComputePath RPC")

    def set_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """SetTask：设置任务。依赖 M5：TaskManager。"""
        raise NotImplementedError("M5 实现：调用 SetTask RPC")

    def generate_task(self, prompt: str) -> Dict[str, Any]:
        """GenerateTask：课程/LLM 生成任务。依赖 M5。"""
        raise NotImplementedError("M5 实现：调用 GenerateTask RPC")

    def clear_region(self, region: Dict[str, Any]) -> None:
        """ClearRegion：清空指定区域实体/方块。依赖 M4。"""
        raise NotImplementedError("M4 实现：调用 ClearRegion RPC")

    def teleport(self, pos: Any) -> None:
        """Teleport：传送玩家。依赖 M4。"""
        raise NotImplementedError("M4 实现：调用 Teleport RPC")

    def spawn_entity(self, entity: Dict[str, Any]) -> None:
        """SpawnEntity：生成实体。依赖 M4。"""
        raise NotImplementedError("M4 实现：调用 SpawnEntity RPC")

    def set_block(self, pos: Any, block: str) -> None:
        """SetBlock：设置方块（God Mode）。依赖 M4。"""
        raise NotImplementedError("M4 实现：调用 SetBlock RPC")

    def close(self) -> None:
        """关闭 gRPC 通道。依赖 M1。"""
        raise NotImplementedError("M1 实现：关闭 grpc 通道")
