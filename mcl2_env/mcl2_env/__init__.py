"""mcl2_env — VLA data collection and agent framework for Luanti + Mineclonia.

Design: see DESIGN.md (repo root).

可选依赖说明：gymnasium / pydantic 未安装时，schemas 与 GymnasiumEnv 不可用，
但 bridge、renderer、dataset（仅需 numpy + Pillow）仍可导入——
random_agent 与单元测试走这条轻依赖路径。
"""

import logging

_log = logging.getLogger(__name__)

__version__ = "0.1.0"

try:  # pydantic 依赖
    from .schemas import (
        ActionPrimitive,
        ActionSemantic,
        CameraState,
        EpisodeInfo,
        InventoryState,
        Observation,
        PlayerState,
        TaskState,
        WorldState,
    )
except ImportError:  # pragma: no cover - 可选依赖未安装
    _log.warning("pydantic 未安装：schemas 相关导出置空")
    ActionPrimitive = ActionSemantic = CameraState = EpisodeInfo = None  # type: ignore[assignment]
    InventoryState = Observation = PlayerState = TaskState = WorldState = None  # type: ignore[assignment]

try:  # gymnasium 依赖
    from .env import GymnasiumEnv, RemoteEnv
except ImportError:  # pragma: no cover - 可选依赖未安装
    _log.warning("gymnasium 未安装：GymnasiumEnv/RemoteEnv 置空（random_agent 走 bridge 直驱）")
    GymnasiumEnv = None  # type: ignore[assignment]
    RemoteEnv = None  # type: ignore[assignment]

__all__ = [
    "GymnasiumEnv",
    "RemoteEnv",
    "ActionPrimitive",
    "ActionSemantic",
    "CameraState",
    "EpisodeInfo",
    "InventoryState",
    "Observation",
    "PlayerState",
    "TaskState",
    "WorldState",
    "__version__",
]
