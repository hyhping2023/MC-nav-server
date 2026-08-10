"""M11 种子回放 + VLA 交互 API（DESIGN.md §11.6）。

`SeedReplayApi` 是面向未来 VLA 策略的同步交互层：

- `reset(seed, task, items)` —— 确定性重置（同 seed → 同一区域基线 + 固定工具包
  物品 + 确定性任务），返回 `(frame, obs)`，frame 自带按键状态（帧↔按键对齐）。
- `step(action)` —— lockstep：WS 发动作 → 收帧 → gRPC 结算 → 状态，
  返回 `(frame, obs, reward, terminated, truncated, info)`。
- `step_discrete(button_mask, camera_bin, hotbar)` —— VPT/STEVE-1 离散 token 输入
  （encode_action/decode_tokens，action_space.py）。
- `play_script(script)` —— 重放录制的按键脚本（list of action dict），逐条返回帧。
- `verify_determinism(seed)` —— 同 seed 两次 reset，对比区域 checksum + 体素指纹。

固定工具包（M11）：镐/剑/铲 + 泥土，hotbar 0-3。任务局限在这四类物品。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .action_space import decode_tokens
from .client_ws import ClientWs, Frame
from .obs import build_obs
from .server_grpc import ServerGrpc
# M11.5：kit 常量收拢到 tasks.py（单一来源）；此处 re-export 保持向后兼容。
from .tasks import KIT_TOOL_SLOT, SURVIVAL_KIT  # noqa: F401

# kit 演示任务 → 主工具（兼容保留；新代码用 tasks.get_profile）
KIT_TASK_TOOL = {
    "collect_stone": "pickaxe",
    "dig_dirt": "shovel",
    "kill_animal": "sword",
    "place_dirt": "dirt",
}

# kit 演示任务 → 目标块/实体类型（兼容保留；新代码用 tasks.get_profile）
KIT_TASK_TARGET = {
    "collect_stone": "minecraft:stone",
    "dig_dirt": "minecraft:dirt",
    "kill_animal": "minecraft:pig",
    "place_dirt": None,
}


class SeedReplayApi:
    """种子回放 + VLA 交互 API：VLA 输出经 step/step_discrete 与客户端交互并返回帧。"""

    def __init__(
        self,
        player: str = "agent0",
        ws_url: str = "ws://127.0.0.1:30001",
        grpc_host: str = "127.0.0.1",
        grpc_port: int = 50051,
        ticks_per_step: int = 2,
        res: int = 224,
    ) -> None:
        self.player = player
        self.ticks_per_step = int(ticks_per_step)
        self.res = int(res)
        self.seed: Optional[int] = None
        self.task: Optional[str] = None
        self._episode_count = 0
        self.episode_id = "ep-000000"
        self.last_checksum = ""
        self._last_frame: Optional[Frame] = None

        self.grpc = ServerGrpc(host=grpc_host, port=grpc_port, player=player)
        self.ws = ClientWs(url=ws_url)

    # ---- 确定性重置 ----

    def reset(
        self,
        seed: Optional[int] = None,
        task: Optional[str] = None,
        items: Optional[Sequence[str]] = None,
        region: Optional[Dict[str, Any]] = None,
        ticks: Optional[int] = None,
        spawn: Optional[Sequence[float]] = None,
        humanize: bool = False,
    ) -> Tuple[Frame, Dict[str, Any]]:
        """确定性重置世界 + 收首帧 + obs。

        同 seed → 同一区域基线恢复（服务端缓存）+ 同一固定工具包 → 世界态可回放。
        spawn=(x, y, z[, yaw]) 自定义出生点（M11.5 难点③）。humanize=True 时开启
        客户端执行器人类化整形（seed 同步传入，整形序列可复现）。
        返回 (frame, obs)；checksum 存 self.last_checksum。
        """
        if task is not None:
            self.task = task
        self.seed = seed
        self._episode_count += 1
        self.episode_id = f"ep-{self._episode_count:06d}"

        resp = self.grpc.reset_world(
            player=self.player, task=self.task, seed=seed, region=region, items=items,
            spawn=spawn)
        if not resp.get("ok"):
            raise RuntimeError(
                f"reset_world failed: ok={resp.get('ok')} msg={resp.get('message')}")
        self.last_checksum = str(resp.get("message", ""))

        self.grpc.set_task(player=self.player, task=self.task, seed=seed)

        # 客户端接管 + 相机归零 + 人类化整形配置
        self.ws.connect()
        self.ws.send_mode("api")
        self.ws.send({"cmd": "reset_camera", "yaw": 0.0, "pitch": 0.0})
        self.ws.send({"cmd": "set_humanize", "enabled": bool(humanize),
                      "seed": int(seed or 0)})

        frame = self.ws.recv_frame_latest(timeout=5.0)
        if frame is None:
            raise TimeoutError("reset: 5s 内未收到首帧（客户端未连 WS / 未进服？）")
        self._last_frame = frame

        state = self.grpc.get_state(player=self.player)
        obs = build_obs(
            frame, state, {"task": self.task, "progress": 0.0, "success": False},
            episode_id=self.episode_id,
            server_tick=resp.get("server_tick", frame.server_tick),
            frame_id=frame.frame_id,
        )
        return frame, obs

    # ---- lockstep step ----

    def step(
        self,
        action: Any,
        ticks: Optional[int] = None,
    ) -> Tuple[Frame, Dict[str, Any], float, bool, bool, Dict[str, Any]]:
        """执行一步原始动作（11 按键 dict），返回带按键状态的 frame + obs + reward/done。

        返回 (frame, obs, reward, terminated, truncated, info)；frame 可能为
        None（收帧超时，罕见）；obs 用最近一帧拼装。
        """
        self.ws.send_action(action)
        frame = self.ws.recv_frame_latest(timeout=2.0)
        if frame is not None:
            self._last_frame = frame

        step = self.grpc.get_step_result(
            player=self.player,
            await_ticks=ticks if ticks is not None else self.ticks_per_step,
        )
        state = self.grpc.get_state(player=self.player)
        task_info = dict(step["info"])
        task_info["progress"] = step["progress"]
        task_info["success"] = step["terminated"]
        obs = build_obs(
            self._last_frame, state, task_info,
            episode_id=self.episode_id,
            server_tick=step["server_tick"],
            frame_id=self._last_frame.frame_id if self._last_frame is not None else 0,
        )
        info = dict(step["info"])
        info["progress"] = step["progress"]
        info["server_tick"] = step["server_tick"]
        return (frame, obs, float(step["reward"]),
                bool(step["terminated"]), bool(step["truncated"]), info)

    def step_discrete(
        self,
        button_mask: int,
        camera_bin: int,
        hotbar: int = -1,
        ticks: Optional[int] = None,
    ) -> Tuple[Frame, Dict[str, Any], float, bool, bool, Dict[str, Any]]:
        """VPT/STEVE-1 离散 token → 客户端 → 返回帧（VLA 输出标准入口）。"""
        return self.step(decode_tokens(button_mask, camera_bin, hotbar), ticks=ticks)

    # ---- 按键脚本重放 ----

    def play_script(
        self,
        script: Sequence[Dict[str, Any]],
        on_step=None,
        ticks: Optional[int] = None,
    ) -> List[Frame]:
        """重放录制的按键脚本（list of action dict），逐条 step，返回帧列表。

        on_step(frame, obs, reward, terminated, truncated, info) 可选回调。
        脚本耗尽或任务完成/截断即停。
        """
        frames: List[Frame] = []
        for action in script:
            frame, obs, reward, terminated, truncated, info = self.step(action, ticks=ticks)
            frames.append(frame)
            if on_step is not None:
                on_step(frame, obs, reward, terminated, truncated, info)
            if terminated or truncated:
                break
        return frames

    # ---- 种子确定性校验 ----

    def verify_determinism(
        self,
        seed: int,
        task: str = "collect_stone",
        items: Optional[Sequence[str]] = None,
        region: Optional[Dict[str, Any]] = None,
        half_extent: int = 16,
    ) -> Dict[str, Any]:
        """同 seed 两次重置，对比区域 checksum + 体素指纹（M11 种子回放验收）。"""
        checksums: List[str] = []
        voxel_fps: List[str] = []
        for _ in range(2):
            resp = self.grpc.reset_world(
                player=self.player, task=task, seed=seed, region=region, items=items)
            if not resp.get("ok"):
                raise RuntimeError(f"reset_world failed: {resp}")
            checksums.append(str(resp.get("message", "")))
            self.grpc.set_task(player=self.player, task=task, seed=seed)
            # 读玩家位置区域的体素指纹（reset 后玩家已回出生点，位置确定）
            palette, data, origin, size = self.grpc.get_voxels(
                player=self.player, half_extent=half_extent)
            voxel_fps.append(hashlib.md5(data.tobytes()).hexdigest())
        region_same = checksums[0] == checksums[1]
        voxels_same = voxel_fps[0] == voxel_fps[1]
        return {
            "seed": seed,
            "checksums": checksums,
            "voxel_fingerprints": voxel_fps,
            "region_deterministic": region_same,
            "voxels_deterministic": voxels_same,
            "deterministic": region_same and voxels_same,
        }

    # ---- 便捷查询 ----

    def get_state(self) -> Dict[str, Any]:
        return self.grpc.get_state(player=self.player)

    def drain_events(self, timeout: float = 0.0) -> List[Dict[str, Any]]:
        """排空客户端上行文本事件（goto_status / key_event / state 等）。"""
        return self.ws.drain_json(timeout=timeout)

    def close(self) -> None:
        ws = getattr(self, "ws", None)
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        grpc = getattr(self, "grpc", None)
        if grpc is not None:
            try:
                grpc.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "SeedReplayApi":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
