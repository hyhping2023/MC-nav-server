"""状态 / 动作 schema。

与 Lua 侧 mcl2_agent/api/state.lua、api/action.lua 保持一致。
字段命名尽量对齐 MineStudio（observation.pov / action.camera / action.buttons）。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- observation

class Vec3(BaseModel):
    x: float
    y: float
    z: float


class CameraState(BaseModel):
    yaw: float
    pitch: float
    dir: Vec3


class InventorySlot(BaseModel):
    item: str
    count: int
    meta: Optional[dict[str, Any]] = None


class InventoryState(BaseModel):
    main: list[Optional[InventorySlot]] = Field(default_factory=list)
    armor: list[Optional[InventorySlot]] = Field(default_factory=list)
    offhand: list[Optional[InventorySlot]] = Field(default_factory=list)
    cursor: Optional[InventorySlot] = None
    slots_total: int = 36


class NearbyBlock(BaseModel):
    pos: Vec3
    name: str
    param2: int = 0


class EntityState(BaseModel):
    id: str
    name: str
    pos: Vec3
    hp: Optional[int] = None


class ItemOnGround(BaseModel):
    item: str
    pos: Vec3


class WorldState(BaseModel):
    timeofday: float
    day_count: int
    biome: str
    weather: str
    seed: Optional[int] = None
    nearby_blocks: list[NearbyBlock] = Field(default_factory=list)
    aimed_block: Optional[NearbyBlock] = None
    entities: list[EntityState] = Field(default_factory=list)
    items_on_ground: list[ItemOnGround] = Field(default_factory=list)
    voxels: Optional[list[Any]] = None


class PlayerState(BaseModel):
    pos: Vec3
    look: CameraState
    velocity: Vec3
    on_ground: bool
    hp: int
    max_hp: int
    breath: int
    saturation: float
    hunger: int
    armor: float
    selected_slot: int
    held_item: str
    dimension: str
    effects: list[dict[str, Any]] = Field(default_factory=list)


class StatsState(BaseModel):
    xp: int
    level: int
    kills: int
    deaths: int
    playtime: float


class TaskState(BaseModel):
    id: str
    instruction: str
    instruction_zh: Optional[str] = None
    type: Optional[str] = None
    difficulty: Optional[int] = None
    progress: dict[str, Any] = Field(default_factory=dict)
    success: bool = False
    steps: int = 0


class EpisodeInfo(BaseModel):
    episode_id: str
    run_id: str
    world_seed: Optional[int] = None
    task_seed: Optional[int] = None
    server_tick: int = 0
    wall_time: float = 0.0


class Observation(BaseModel):
    """完整观测。image 字段由 Python 侧渲染器注入，不在 Lua 状态内。"""

    image: Optional[Any] = None  # np.ndarray (H,W,3) 或 None
    image_path: Optional[str] = None
    player: PlayerState
    inventory: InventoryState
    world: WorldState
    stats: StatsState
    task: Optional[TaskState] = None
    episode: Optional[EpisodeInfo] = None


# ---------------------------------------------------------------- actions

class ActionPrimitive(BaseModel):
    """原始动作，字段对齐 MineStudio / VPT。"""

    forward: bool = False
    back: bool = False
    left: bool = False
    right: bool = False
    jump: bool = False
    sneak: bool = False
    sprint: bool = False
    attack: bool = False
    use: bool = False
    drop: bool = False
    hotbar: Optional[int] = None
    camera: Optional[list[float]] = None  # [pitch_delta, yaw_delta]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ActionSemantic(BaseModel):
    """语义动作。"""

    id: str
    args: dict[str, Any] = Field(default_factory=dict)


# 统一的动作输入：二选一
class Action(BaseModel):
    type: Literal["primitive", "semantic"]
    primitive: Optional[ActionPrimitive] = None
    semantic: Optional[ActionSemantic] = None


# ---------------------------------------------------------------- misc

class TaskDef(BaseModel):
    id: str
    instruction: str
    instruction_zh: Optional[str] = None
    type: str
    difficulty: int
    tags: list[str] = Field(default_factory=list)


class RolloutSpec(BaseModel):
    """一次 episode 的全部种子与元信息（写进 meta.json 保证可还原）。"""

    run_id: str
    episode_id: str
    world_seed: Optional[int] = None
    mapgen: Optional[dict[str, Any]] = None
    task_id: str
    task_seed: int
    reset_seed: int
    engine: dict[str, str] = Field(default_factory=dict)
    game: dict[str, str] = Field(default_factory=dict)
    physics: Optional[dict[str, Any]] = None
