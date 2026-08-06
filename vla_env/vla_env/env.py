"""gymnasium.Env 占位 —— MinecraftEnv（M0 里程碑桩）。

职责：作为 VLA 控制中枢的标准 Gymnasium 接口（DESIGN.md §6.3），
封装 `reset / step / close` 三件套。真实实现依赖：

- M2 客户端控制（client_ws 动作下行 + 帧上行）
- M4 世界引擎（server_grpc ResetWorld / GetStepResult / GetState）
- M3 观测（obs.build 拼装 pov/state/voxels/task）

M0 阶段仅提供可 import 的类骨架，功能性方法抛
``NotImplementedError("M<里程碑> 实现")`` 指明依赖里程碑。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

try:
    import gymnasium as gym
    from gymnasium import spaces
except ImportError:  # pragma: no cover - 依赖缺失时保持可 import
    gym = None
    spaces = None

# gymnasium 可用时继承 gym.Env（§6.3 标准接口）；缺失时回退 object，
# 保证惰性导入约定下模块始终可 import。
if gym is not None:
    _GymEnvBase = gym.Env
else:  # pragma: no cover - gymnasium 缺失时的回退基类
    _GymEnvBase = object


class MinecraftEnv(_GymEnvBase):
    """Minecraft VLA 环境的 Gymnasium 占位。

    依赖里程碑：
    - M2（客户端控制）、M4（世界引擎）→ `reset` / `step`
    - M3（观测拼装）→ `_observe`
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        player: str = "agent0",
        task: str = "collect_wood",
        ticks_per_step: int = 4,
        res: int = 224,
        seed: Optional[int] = None,
    ) -> None:
        """构造参数与 DESIGN.md §6.3 对齐（M7 时接入真实服务端/客户端）。"""
        self.player = player
        self.task = task
        self.ticks_per_step = ticks_per_step
        self.res = res
        self.seed = seed
        # M7 时定义真正的 Dict/Discrete 空间；M0 仅占位。
        self.observation_space = None
        self.action_space = None

    def reset(
        self,
        *,
        seed: Optional[int] = None,
        options: Optional[dict] = None,
    ) -> Tuple[Any, dict]:
        """重置世界与任务，返回初始观测。

        依赖 M2 + M4：gRPC ResetWorld + WS mode/reset_camera。
        """
        raise NotImplementedError("M4 实现：接入 server_grpc.reset_world 与 client_ws 模式切换")

    def step(self, action: Any) -> Tuple[Any, float, bool, bool, dict]:
        """执行一步动作，返回 (obs, reward, terminated, truncated, info)。

        依赖 M2 + M4 + M3：动作下发 → 帧上行 → gRPC 结算 → 观测拼装。
        """
        raise NotImplementedError("M2 实现：接入 client_ws 动作下发与 server_grpc.get_step_result")

    def close(self) -> None:
        """释放 gRPC / WS 连接。依赖 M1 通信底座。"""
        raise NotImplementedError("M1 实现：关闭 server_grpc 与 client_ws 连接")

    def _observe(self) -> Any:
        """拼装观测。依赖 M3（obs.build）。"""
        raise NotImplementedError("M3 实现：调用 obs.build(pov, state, task_info)")
