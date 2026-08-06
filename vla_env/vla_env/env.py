"""gymnasium.Env 完整实现 —— MinecraftEnv（M7 Env 闭环）。

职责：VLA 控制中枢的标准 Gymnasium 接口（DESIGN.md §6.3），lockstep 模式：

    reset(task, seed):
        gRPC ResetWorld（区域回滚 + 玩家态）→ gRPC SetTask
        → WS mode=api + reset_camera{yaw:0,pitch:0} → 收首帧 → GetState → obs

    step(action, ticks=None):
        WS send_action → recv_frame → gRPC GetStepResult(await ticks) 结算
        → GetState → obs / reward / terminated / truncated / info

server-authoritative（§14.2）：reward/done 只信 gRPC 结算；帧仅作视觉输入。
暴露 `self.grpc` / `self.ws` 供脚本策略直接使用（get_voxels / reset_camera 等）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces
except ImportError:  # pragma: no cover - 依赖缺失时保持可 import
    gym = None
    spaces = None

from .client_ws import ClientWs
from .obs import build_obs
from .server_grpc import ServerGrpc

# gymnasium 可用时继承 gym.Env（§6.3 标准接口）；缺失时回退 object。
if gym is not None:
    _GymEnvBase = gym.Env
else:  # pragma: no cover
    _GymEnvBase = object

_BUTTON_NAMES = (
    "forward", "back", "left", "right", "jump", "sneak", "sprint",
    "attack", "use", "drop", "inventory",
)

# camera 增量范围（度/步，与 action_space.CAMERA_DELTA_MAX 一致）。
_CAMERA_DELTA_MAX = 30.0


class MinecraftEnv(_GymEnvBase):
    """Minecraft VLA 环境的 Gymnasium 封装（M7 Env 闭环）。"""

    metadata = {"render_modes": []}

    def __init__(
        self,
        player: str = "agent0",
        task: str = "collect_wood",
        ws_url: str = "ws://127.0.0.1:30001",
        grpc_host: str = "127.0.0.1",
        grpc_port: int = 50051,
        ticks_per_step: int = 2,
        res: int = 224,
    ) -> None:
        self.player = player
        self.task = task
        self.ticks_per_step = int(ticks_per_step)
        self.res = int(res)
        self._episode_count = 0
        self.episode_id = "ep-000000"
        self._last_frame = None

        self.grpc = ServerGrpc(host=grpc_host, port=grpc_port, player=player)
        self.ws = ClientWs(url=ws_url)

        self.action_space = self._make_action_space()
        self.observation_space = self._make_observation_space()

    # ---- 空间定义 ----

    def _make_action_space(self):
        return spaces.Dict({
            name: spaces.Discrete(2) for name in _BUTTON_NAMES
        } | {
            "hotbar": spaces.Discrete(9),
            "camera": spaces.Box(
                low=-_CAMERA_DELTA_MAX, high=_CAMERA_DELTA_MAX,
                shape=(2,), dtype=np.float32,
            ),
        })

    def _make_observation_space(self):
        text_cls = getattr(spaces, "Text", None)
        text = (lambda n: text_cls(n)) if text_cls else (lambda n: spaces.Discrete(64))
        return spaces.Dict({
            "pov": spaces.Box(0, 255, shape=(self.res, self.res, 3), dtype=np.uint8),
            "inventory": spaces.Dict({
                "selected_slot": spaces.Discrete(9),
                "held_item": text(64),
            }),
            "player": spaces.Dict({
                "pos": spaces.Box(-1e6, 1e6, shape=(3,), dtype=np.float32),
                "hp": spaces.Box(0.0, 20.0, shape=(1,), dtype=np.float32),
                "hunger": spaces.Box(0.0, 20.0, shape=(1,), dtype=np.float32),
                "on_ground": spaces.Discrete(2),
            }),
            "stats": spaces.Box(-1e6, 1e6, shape=(3,), dtype=np.float32),
            "task": spaces.Dict({
                "id": text(64),
                "progress": spaces.Box(0.0, 1.0, shape=(1,), dtype=np.float32),
                "success": spaces.Discrete(2),
            }),
            "agent": spaces.Dict({
                "episode_id": text(64),
                "server_tick": spaces.Box(0, 2 ** 31 - 1, shape=(1,), dtype=np.int64),
                "frame_id": spaces.Box(0, 2 ** 31 - 1, shape=(1,), dtype=np.int64),
            }),
        })

    # ---- gymnasium 接口 ----

    def reset(
        self,
        task: Optional[str] = None,
        seed: Optional[int] = None,
        *,
        options: Optional[dict] = None,
    ) -> Tuple[Any, dict]:
        """重置世界与任务，返回初始观测 (obs, info)。

        流程：gRPC ResetWorld → SetTask → WS mode=api + reset_camera → 收首帧
        → GetState → build_obs。
        """
        if task is not None:
            self.task = task
        self._episode_count += 1
        self.episode_id = f"ep-{self._episode_count:06d}"

        resp = self.grpc.reset_world(player=self.player, task=self.task, seed=seed)
        if not resp.get("ok"):
            raise RuntimeError(f"reset_world failed: ok={resp.get('ok')} msg={resp.get('message')}")

        self.grpc.set_task(player=self.player, task=self.task, seed=seed)

        # 客户端接管 + 相机归零
        self.ws.connect()
        self.ws.send_mode("api")
        self.ws.send({"cmd": "reset_camera", "yaw": 0.0, "pitch": 0.0})

        frame = self.ws.recv_frame_latest(timeout=5.0)
        if frame is None:
            raise TimeoutError("reset: 5s 内未收到首帧（客户端未连 WS / 未进服？）")
        self._last_frame = frame

        state = self.grpc.get_state(player=self.player)
        task_info = {"task": self.task, "progress": 0.0, "success": False}
        obs = build_obs(
            frame, state, task_info,
            episode_id=self.episode_id,
            server_tick=resp.get("server_tick", frame.server_tick),
            frame_id=frame.frame_id,
        )
        return obs, {"episode_id": self.episode_id}

    def step(
        self,
        action: Any,
        ticks: Optional[int] = None,
    ) -> Tuple[Any, float, bool, bool, dict]:
        """执行一步动作，返回 (obs, reward, terminated, truncated, info)。

        lockstep：WS send_action → recv_frame → gRPC GetStepResult(await ticks)。
        info 至少含 progress / server_tick（+ 服务端 info 原样）。
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
        return obs, float(step["reward"]), bool(step["terminated"]), bool(step["truncated"]), info

    def close(self) -> None:
        """释放 gRPC / WS 连接。"""
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
