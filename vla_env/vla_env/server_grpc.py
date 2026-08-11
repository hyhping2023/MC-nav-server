"""与服务端 gRPC 通信客户端（M1 底座 + M7 Env 闭环全 RPC）。

职责：Python ↔ Purpur 插件 的 gRPC 通道（DESIGN.md §6.2 / §9.1），
调用 `vla.proto` 定义的 `VlaServer` 服务：

    Ping / ResetWorld / GetStepResult / GetState / GetVoxels / ComputePath /
    SetTask / GenerateTask / ClearRegion / Teleport / SpawnEntity / SetBlock /
    ShowPath / SelectSurfaceWorld

关键约定（§4.2）：服务端写操作一律主线程执行；reward/done 以服务端为权威
（§14.2），`GetStepResult` 阻塞等待 k ticks 结算。

M7 已实现全部 RPC 封装（替换 M0/M1 桩，保留 ping）：
- `reset_world` / `set_task` / `get_step_result` / `get_state`
- `get_voxels` → (palette, numpy 3D data, origin, size)
- `compute_path` → waypoints list
- 其余 RPC（generate_task / clear_region / teleport / spawn_entity / set_block）
  一并接通（clearRegion 等服务端仍为 UNIMPLEMENTED，会如实抛错）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

import grpc
import numpy as np

from .proto import vla_pb2, vla_pb2_grpc


class ServerGrpc:
    """服务端 gRPC client。方法签名与 proto RPC 一一对应。

    M1：`__init__` 即建立 insecure channel + `VlaServerStub`；
    `ping()` 调 Ping RPC 返回服务端权威时钟等。
    M7：全部业务 RPC 实现（见模块 docstring）。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 50051,
        player: str = "agent0",
    ) -> None:
        self.address = f"{host}:{port}"
        self.player = player
        self.channel = grpc.insecure_channel(self.address)
        self.stub = vla_pb2_grpc.VlaServerStub(self.channel)

    def connect(self) -> None:
        """建立 gRPC 通道 + stub（channel 惰性连接，做一次连通性预检）。"""
        self.ping()

    # ---- 通信底座（M1）----

    def ping(self) -> Dict[str, Any]:
        """Ping：连通性检查 + 权威 server_tick。

        返回 dict（对齐 proto PingReply）：server_tick / tps / version /
        world_name。
        """
        reply: vla_pb2.PingReply = self.stub.Ping(
            vla_pb2.PingRequest(client="vla_env_py")
        )
        return {
            "server_tick": reply.server_tick,
            "tps": reply.tps,
            "version": reply.version,
            "world_name": reply.world_name,
        }

    # ---- 重置 / 任务 / 结算（M4/M5，M7 接线）----

    def reset_world(
        self,
        player: Optional[str] = None,
        task: Optional[str] = None,
        seed: Optional[int] = None,
        region: Optional[Dict[str, Any]] = None,
        items: Optional[Sequence[str]] = None,
        spawn: Optional[Sequence[float]] = None,
    ) -> Dict[str, Any]:
        """ResetWorld：重置世界（区域回滚 + 玩家态），返回 ResetReply。

        参数：
        - player: 玩家名（缺省用 self.player）
        - task: 任务 id（服务端 resetWorld 当前不使用，任务经 set_task 设置）
        - seed: 世界/任务种子（int；M11 起服务端存入 ResetSpec 供回放校验）
        - region: 可选 dict {x, y, z, half_extent} 指定重置区域中心与半宽
        - items: M11 初始物品覆盖，如 ["minecraft:diamond_pickaxe", "minecraft:dirt@64"]，
          非空时覆盖任务默认 initialItems（固定工具包）
        - spawn: M11.5 自定义出生点 (x, y, z[, yaw])——重置传送到此处（难点③）；
          区域未显式给出时以 spawn 为中心

        返回 {"server_tick", "ok", "message"}（message 为区域 checksum）。
        """
        req = vla_pb2.ResetRequest(player=player or self.player)
        if task:
            req.task = task
        if seed is not None:
            req.seed = int(seed)
        if items:
            req.items.extend(str(i) for i in items)
        if spawn is not None:
            req.has_spawn = True
            req.spawn_x = float(spawn[0])
            req.spawn_y = float(spawn[1])
            req.spawn_z = float(spawn[2])
            if len(spawn) > 3:
                req.spawn_yaw = float(spawn[3])
        if region:
            if region.get("x") is not None:
                req.region_x = int(region["x"])
            if region.get("y") is not None:
                req.region_y = int(region["y"])
            if region.get("z") is not None:
                req.region_z = int(region["z"])
            if region.get("half_extent") is not None:
                req.region_half_extent = int(region["half_extent"])
        reply: vla_pb2.ResetReply = self.stub.ResetWorld(req)
        return {
            "server_tick": reply.server_tick,
            "ok": reply.ok,
            "message": reply.message,
        }

    def set_task(
        self,
        player: Optional[str] = None,
        task: str = "collect_wood",
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        """SetTask：设置任务并重置该玩家 episode 状态。

        返回 TaskReply dict：id / instruction / instruction_zh / difficulty /
        progress / success / timeout_ticks。
        """
        req = vla_pb2.TaskRequest(player=player or self.player, task=task)
        if seed is not None:
            req.seed = int(seed)
        reply: vla_pb2.TaskReply = self.stub.SetTask(req)
        return {
            "id": reply.id,
            "instruction": reply.instruction,
            "instruction_zh": reply.instruction_zh,
            "difficulty": reply.difficulty,
            "progress": reply.progress,
            "success": reply.success,
            "timeout_ticks": reply.timeout_ticks,
        }

    def select_surface_world(
        self,
        surface: str,
        player: Optional[str] = None,
        seed: int = 0,
    ) -> Dict[str, Any]:
        """选择/创建一个单材质元世界，并传送玩家到该世界的 Y=64 平面出生点。

        每种 surface 对应一个独立保存的 Minecraft 世界目录和元数据文件；重复选择同一
        材质会复用已有世界存档。可选值：grass_block、dirt、coarse_dirt、sand、
        red_sand、stone、granite、diorite、andesite、clay。
        """
        req = vla_pb2.SelectSurfaceWorldRequest(
            player=player or self.player,
            surface=str(surface),
            seed=int(seed),
        )
        reply: vla_pb2.SelectSurfaceWorldReply = self.stub.SelectSurfaceWorld(req)
        return {
            "world_name": reply.world_name,
            "surface_id": reply.surface_id,
            "surface_material": reply.surface_material,
            "created": reply.created,
            "metadata_path": reply.metadata_path,
            "surface_y": reply.surface_y,
            "worker_id": reply.worker_id,
            "map_seed": reply.map_seed,
        }

    def get_step_result(
        self,
        player: Optional[str] = None,
        await_ticks: int = 4,
    ) -> Dict[str, Any]:
        """GetStepResult：阻塞等待 k ticks 后服务端结算（server-authoritative）。

        返回 dict（对齐 proto StepReply）：reward / terminated / truncated /
        server_tick / progress / info（info 为 {task, ...}）。
        """
        req = vla_pb2.StepRequest(player=player or self.player, await_ticks=int(await_ticks))
        reply: vla_pb2.StepReply = self.stub.GetStepResult(req)
        return {
            "reward": reply.reward,
            "terminated": reply.terminated,
            "truncated": reply.truncated,
            "server_tick": reply.server_tick,
            "progress": reply.progress,
            "info": dict(reply.info),
        }

    # ---- 状态 / 体素 / 路径（M6，只读）----

    def get_state(self, player: Optional[str] = None) -> Dict[str, Any]:
        """GetState：玩家/背包/统计等本地状态（JSON 编码，§8）。

        返回解析后的 dict：{"player": {pos,hp,hunger,yaw,pitch,on_ground,...},
        "inventory": {selected_slot, held_item, main}, "stats": {...}}。
        """
        reply: vla_pb2.StateReply = self.stub.GetState(
            vla_pb2.StateRequest(player=player or self.player)
        )
        return json.loads(reply.json)

    def get_voxels(
        self,
        player: Optional[str] = None,
        half_extent: int = 16,
        center: Optional[Sequence[float]] = None,
    ) -> Tuple[List[str], np.ndarray, Tuple[int, int, int], int]:
        """GetVoxels：服务端体素矩阵。

        返回 (palette, data, origin, size)：
        - palette: 块 id 字符串表（如 "minecraft:oak_log[axis=y]"）
        - data: numpy int32 3D 数组，shape=(size,size,size)，索引 [y][z][x]，
          值为 palette 局部索引（与 proto 的 (x,y,z) 序遍历一致）
        - origin: (ox, oy, oz) 立方体最小角世界坐标
        - size: 边长（2*half_extent+1，默认 33）
        """
        req = vla_pb2.VoxelRequest(
            player=player or self.player, half_extent=int(half_extent)
        )
        if center is not None:
            req.center_x = int(center[0])
            req.center_y = int(center[1])
            req.center_z = int(center[2])
        reply: vla_pb2.VoxelReply = self.stub.GetVoxels(req)
        palette: List[str] = list(reply.palette)
        size = reply.size
        data = np.asarray(reply.data, dtype=np.int32).reshape(size, size, size)
        origin = (reply.origin_x, reply.origin_y, reply.origin_z)
        return palette, data, origin, size

    def compute_path(
        self,
        player: Optional[str] = None,
        goal: Optional[Sequence[float]] = None,
        start: Optional[Sequence[float]] = None,
        cost_mode: str = "default",
    ) -> Tuple[List[Tuple[float, float, float]], List[Dict[str, Any]]]:
        """ComputePath：服务端动作级 3D A* 寻路（NavV2）。

        参数：
        - goal: (x, y, z) 目标世界坐标（必填）
        - start: (x, y, z) 起点；缺省用玩家当前位置
        - cost_mode: "default"（不挖穿）| "dig"（挖穿/下挖）| "place"（+垫方块爬高）

        返回 (waypoints, details)：
        - waypoints: 拐点序列 [(x,y,z), ...]（纯位置，向后兼容）；未找到为空列表
        - details: 动作级航点 [{pos, action, target}, ...]，action ∈
          walk|jump|fall|dig|dig_down|place；target 仅 dig/dig_down/place 非空。
        """
        req = vla_pb2.PathRequest(
            player=player or self.player, cost_mode=cost_mode or "default"
        )
        if goal is not None:
            req.goal.CopyFrom(
                vla_pb2.Vec3(x=float(goal[0]), y=float(goal[1]), z=float(goal[2]))
            )
        if start is not None:
            req.start.CopyFrom(
                vla_pb2.Vec3(x=float(start[0]), y=float(start[1]), z=float(start[2]))
            )
        reply: vla_pb2.PathReply = self.stub.ComputePath(req)
        waypoints = [(w.x, w.y, w.z) for w in reply.waypoints]
        details = [
            {
                "pos": (d.pos.x, d.pos.y, d.pos.z),
                "action": d.action,
                "target": None if not d.HasField("target") else (d.target.x, d.target.y, d.target.z),
            }
            for d in reply.details
        ]
        return waypoints, details

    # ---- 课程 / God Mode（M12 / 预留）----

    def generate_task(
        self,
        player: Optional[str] = None,
        prompt: str = "",
        difficulty: int = 0,
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GenerateTask：课程/LLM 生成任务（M11 起支持确定性 seed）。

        seed != 0 时服务端用 java.util.Random(seed) 从注册表确定性选任务
        （同 seed → 同任务，支撑种子回放）；seed=0 回退随机。
        """
        req = vla_pb2.GenerateRequest(
            player=player or self.player, prompt=prompt, difficulty=int(difficulty)
        )
        if seed is not None:
            req.seed = int(seed)
        reply: vla_pb2.TaskReply = self.stub.GenerateTask(req)
        return {
            "id": reply.id,
            "instruction": reply.instruction,
            "instruction_zh": reply.instruction_zh,
            "difficulty": reply.difficulty,
            "progress": reply.progress,
            "success": reply.success,
            "timeout_ticks": reply.timeout_ticks,
        }

    def clear_region(
        self,
        player: Optional[str] = None,
        center: Optional[Sequence[float]] = None,
        half_extent: int = 16,
        clear_blocks: bool = False,
        clear_entities: bool = True,
    ) -> None:
        """ClearRegion：清空区域实体/方块（服务端尚未实现，会抛 UNIMPLEMENTED）。"""
        req = vla_pb2.ClearRequest(
            player=player or self.player,
            half_extent=int(half_extent),
            clear_blocks=clear_blocks,
            clear_entities=clear_entities,
        )
        if center is not None:
            req.center_x = int(center[0])
            req.center_y = int(center[1])
            req.center_z = int(center[2])
        self.stub.ClearRegion(req)

    def teleport(
        self,
        player: Optional[str] = None,
        pos: Optional[Sequence[float]] = None,
        yaw: float = 0.0,
        pitch: float = 0.0,
    ) -> None:
        """Teleport：传送玩家（God Mode；M12 已实现，主线程调度）。"""
        req = vla_pb2.TeleportRequest(
            player=player or self.player, yaw=float(yaw), pitch=float(pitch)
        )
        if pos is not None:
            req.pos.CopyFrom(
                vla_pb2.Vec3(x=float(pos[0]), y=float(pos[1]), z=float(pos[2]))
            )
        self.stub.Teleport(req)

    def spawn_entity(
        self,
        player: Optional[str] = None,
        entity_type: str = "minecraft:zombie",
        pos: Optional[Sequence[float]] = None,
        count: int = 1,
    ) -> None:
        """SpawnEntity：生成实体（服务端已实现，God Mode）。"""
        req = vla_pb2.SpawnRequest(
            player=player or self.player,
            entity_type=entity_type,
            count=int(count),
        )
        if pos is not None:
            req.pos.CopyFrom(
                vla_pb2.Vec3(x=float(pos[0]), y=float(pos[1]), z=float(pos[2]))
            )
        self.stub.SpawnEntity(req)

    def set_block(
        self,
        player: Optional[str] = None,
        pos: Optional[Sequence[float]] = None,
        block: str = "minecraft:stone",
        apply_physics: bool = False,
    ) -> None:
        """SetBlock：设置方块（God Mode；服务端尚未实现，会抛 UNIMPLEMENTED）。"""
        req = vla_pb2.SetBlockRequest(
            player=player or self.player,
            block=block,
            apply_physics=apply_physics,
        )
        if pos is not None:
            req.pos.CopyFrom(
                vla_pb2.Vec3(x=float(pos[0]), y=float(pos[1]), z=float(pos[2]))
            )
        self.stub.SetBlock(req)

    def show_path(
        self,
        player: Optional[str] = None,
        waypoints: Optional[Sequence[Sequence[float]]] = None,
        goal: Optional[Sequence[float]] = None,
        clear: bool = False,
        lifetime_ticks: int = 0,
        path_type: str = "server",
    ) -> None:
        """ShowPath：路径可视化（两层导航 M10）。

        参数：
        - waypoints: 方块整数坐标序列 [(x,y,z), ...]（服务端在方块中心刷粒子）
        - goal: 目标方块坐标 (x,y,z)，单独高亮（目标树定位）
        - clear: True 时清除该玩家当前路径特效（忽略 waypoints/goal）
        - lifetime_ticks: 特效保留 tick（0 = 默认 1200，即 60s）
        - path_type: "server"（默认，黄色 Dust 服务端长程航点）| "client"（白色 Dust 客户端局部路径）
        """
        req = vla_pb2.ShowPathRequest(
            player=player or self.player,
            clear=clear,
            lifetime_ticks=int(lifetime_ticks),
            path_type=path_type or "server",
        )
        if not clear:
            for w in waypoints or []:
                req.waypoints.add(x=float(w[0]), y=float(w[1]), z=float(w[2]))
            if goal is not None:
                req.goal.CopyFrom(
                    vla_pb2.Vec3(x=float(goal[0]), y=float(goal[1]), z=float(goal[2]))
                )
        self.stub.ShowPath(req)

    def close(self) -> None:
        """关闭 gRPC 通道（M1 已实现）。"""
        try:
            self.channel.close()
        except Exception:  # noqa: BLE001
            pass
