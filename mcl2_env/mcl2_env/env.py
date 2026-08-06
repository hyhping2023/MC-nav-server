"""gymnasium.Env 实现。

- GymnasiumEnv：直接连 Lua bridge，本地环境。
- RemoteEnv：通过 HTTP/WS 连 VLA Server（供远程模型使用）。

设计见 DESIGN.md §8。
"""

from __future__ import annotations

from typing import Any, Optional

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from .bridge import BridgeClient
from .schemas import ActionPrimitive, ActionSemantic, Observation

ImageArray = np.ndarray  # (H, W, 3) uint8


class GymnasiumEnv(gym.Env):
    """本地环境：Lua bridge + 渲染器。"""

    metadata = {"render_modes": ["rgb_array"]}

    def __init__(
        self,
        player: str = "bot1",
        bridge_host: str = "127.0.0.1",
        bridge_port: int = 25585,
        renderer: Any = None,   # renderer.base.Renderer 实例
        img_size: tuple[int, int] = (224, 224),
        action_mode: str = "semantic",   # "primitive" | "semantic"
        record: bool = True,
        run_id: str = "local",
        bridge: Any = None,     # 注入的 bridge 实例（默认 TCP BridgeClient）
        world_seed: Optional[int] = None,
    ):
        super().__init__()
        self.player = player
        self.bridge = bridge if bridge is not None else BridgeClient(bridge_host, bridge_port)
        self.renderer = renderer
        self.img_size = img_size
        self.action_mode = action_mode
        self.record = record
        self.run_id = run_id
        self.world_seed = world_seed
        self.episode_count = 0
        self._task: Optional[str] = None
        self._last_obs: Optional[Observation] = None

        # ---- 动作空间（骨架，真实维度待与环境握手后定）
        if action_mode == "primitive":
            self.action_space = spaces.Dict({
                "forward": spaces.Discrete(2),
                "back": spaces.Discrete(2),
                "left": spaces.Discrete(2),
                "right": spaces.Discrete(2),
                "jump": spaces.Discrete(2),
                "sneak": spaces.Discrete(2),
                "sprint": spaces.Discrete(2),
                "attack": spaces.Discrete(2),
                "use": spaces.Discrete(2),
                "drop": spaces.Discrete(2),
                "hotbar": spaces.Discrete(9),
                "camera": spaces.Box(-0.5, 0.5, shape=(2,), dtype=np.float32),
            })
        else:
            # 语义动作：离散 ID + 参数（实践中通常由高层 planner 直接构造 ActionSemantic）
            self.action_space = spaces.Discrete(1024)

        # 观测空间骨架（image + 状态）
        self.observation_space = spaces.Dict({
            "image": spaces.Box(0, 255, shape=(*self.img_size, 3), dtype=np.uint8),
            "instruction": spaces.Text(max_length=512),
            "player": spaces.Box(-np.inf, np.inf, shape=(13,), dtype=np.float32),  # 占位
        })

    # ------------------------------------------------------------ lifecycle

    def reset(self, *, seed: Optional[int] = None, options: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], dict[str, Any]]:
        super().reset(seed=seed)
        opts = options or {}
        task = opts.get("task")
        self._task = task
        self.episode_count += 1

        if self.renderer:
            self.renderer.start()

        # 生成 episode 元信息（种子保存，见 DESIGN.md §7.2；options 可覆盖）
        spec = {
            "player": self.player,
            "task_id": task,
            "run_id": opts.get("run_id", self.run_id),
            "episode_id": opts.get("episode_id", f"ep-{self.episode_count:06d}"),
            "world_seed": opts.get("world_seed", self._world_seed()),
            "task_seed": opts.get("task_seed", self._task_seed(seed)),
            "reset_seed": opts.get("reset_seed", self._reset_seed(seed)),
        }
        self.bridge.begin_episode(spec)

        obs = self._observe()
        info = {"episode_id": spec["episode_id"], **self._frame_info(obs.image)}
        return self._to_obs_dict(obs), info

    def step(self, action: Any) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        if self.action_mode == "primitive":
            self.bridge.step(self._primitive(action), player=self.player)
        # semantic 模式动作由高层调度，不在 step 内执行（见 examples）

        obs = self._observe()
        done = bool(obs.task and obs.task.success)
        truncated = self._is_truncated(obs)
        reward = self._reward(obs)

        if done or truncated:
            self.bridge.end_episode(success=done, player=self.player)
            if self.renderer:
                self.renderer.stop()
        return self._to_obs_dict(obs), reward, done, truncated, self._frame_info(obs.image)

    def render(self) -> ImageArray | None:
        if self._last_obs is not None and self._last_obs.image is not None:
            return self._last_obs.image
        return None

    def close(self) -> None:
        if self.renderer:
            self.renderer.stop()
        self.bridge.close()

    # ------------------------------------------------------------ internals

    def _observe(self) -> Observation:
        raw = self.bridge.observe(player=self.player)
        obs = Observation.model_validate(raw)

        # 注入图像帧（渲染器）；无帧时 obs.image 保持 None（降级，见 _to_obs_dict）
        if self.renderer:
            # voxel 类渲染器需要每步注入相机 + 体素网格（engine_fork 无 set_camera）
            if hasattr(self.renderer, "set_camera"):
                self.renderer.set_camera(obs.player.pos, obs.player.look, obs.world.voxels)
            frame = self.renderer.get_frame()
            if frame is not None:
                img = np.asarray(frame.image, dtype=np.uint8)
                if img.ndim == 3 and (img.shape[0], img.shape[1]) != tuple(self.img_size):
                    from PIL import Image

                    # PIL resize 用 (width, height)，img_size 为 (height, width)
                    img = np.array(Image.fromarray(img).resize((self.img_size[1], self.img_size[0])))
                obs.image = img
        self._last_obs = obs
        return obs

    @staticmethod
    def _frame_info(image: Any) -> dict[str, Any]:
        """渲染器可用性信息（供 random_agent 决定是否做图像非纯色断言）。"""
        if image is None:
            return {"frame_available": False}
        return {"frame_available": True, "image_std": float(np.std(image))}

    def _to_obs_dict(self, obs: Observation) -> dict[str, Any]:
        instruction = obs.task.instruction if obs.task else ""
        image = obs.image
        if image is None:
            image = np.zeros((*self.img_size, 3), dtype=np.uint8)
        return {"image": image, "instruction": instruction, "player": self._player_vec(obs)}

    def _player_vec(self, obs: Observation) -> np.ndarray:
        p = obs.player
        return np.array([
            p.pos.x, p.pos.y, p.pos.z,
            p.look.yaw, p.look.pitch,
            p.velocity.x, p.velocity.y, p.velocity.z,
            p.hp, p.hunger, p.saturation, p.breath,
            float(p.on_ground),
        ], dtype=np.float32)

    def _primitive(self, action: Any) -> dict[str, Any]:
        if isinstance(action, dict):
            d = {k: bool(v) for k, v in action.items() if k in {
                "forward", "back", "left", "right", "jump",
                "sneak", "sprint", "attack", "use", "drop",
            }}
            if "hotbar" in action:
                d["hotbar"] = int(action["hotbar"])
            if "camera" in action:
                d["camera"] = [float(action["camera"][0]), float(action["camera"][1])]
            return d
        raise TypeError(f"unsupported primitive action: {action!r}")

    # 种子来源：构造参数 world_seed，或由调用方经 options["world_seed"] 传入
    def _world_seed(self) -> Optional[int]:
        return self.world_seed
    def _task_seed(self, seed: Optional[int]) -> int:
        return seed or 0
    def _reset_seed(self, seed: Optional[int]) -> int:
        return (seed or 0) + 1

    def _reward(self, obs: Observation) -> float:
        # TODO: 从任务 reward_shaping 计算
        return 0.0

    def _is_truncated(self, obs: Observation) -> bool:
        # TODO: 超时由 Lua 侧标记
        return False


class RemoteEnv(gymnasium.Env):
    """远程环境：把 reset/step 转发给 VLA Server（HTTP/WS）。

    供部署在独立进程/机器的 VLA 模型使用。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000", ws_url: Optional[str] = None):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.ws_url = ws_url
        import httpx  # 延迟导入，避免硬依赖

        self._http = httpx.Client(timeout=30.0)

    def reset(self, *, seed: Optional[int] = None, options: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], dict[str, Any]]:
        task = (options or {}).get("task")
        r = self._http.post(f"{self.base_url}/reset", json={"task": task, "seed": seed})
        r.raise_for_status()
        data = r.json()
        return data["obs"], data.get("info", {})

    def step(self, action: Any) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        r = self._http.post(f"{self.base_url}/step", json={"action": action})
        r.raise_for_status()
        data = r.json()
        return data["obs"], data["reward"], data["terminated"], data["truncated"], data.get("info", {})

    def close(self) -> None:
        self._http.close()
