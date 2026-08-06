"""原始动作与语义动作的映射桩（M0 里程碑）。

职责：DESIGN.md §7 定义的动作空间三层映射：

- 原始动作（tick 级，MineRL/VPT 对齐）：buttons（forward/back/left/right/jump/
  sneak/sprint/attack/use/drop/inventory + hotbar 0-8）+ camera **121 bin（11×11）**；
- VPT 离散 token（可选，§7.2）：buttons 分层组合为单个离散 token；
- 语义动作（§7.3，与 Mineflayer 命名对齐）：goto / dig / place / craft / equip /
  attack_entity / use_block / eat…，以 ``action_id`` 异步执行，双标签记录。

M0 仅定义常量与转换接口，不实现具体映射逻辑。
依赖里程碑：M2（客户端动作注入）——`to_ws` 输出发给 client_ws；
M5（任务系统）——语义动作经服务端分解/判定。
"""

from __future__ import annotations

from typing import Any, Dict

# camera 121 bin（11×11），对齐 MineRL/MineDojo/MineStudio（DESIGN.md §7.1）。
CAMERA_BINS = 121
CAMERA_BIN_SIDE = 11  # 11×11 = 121

# 原始按键集合（顺序与 VPT/MineRL 约定对齐）。
BUTTONS = (
    "forward",
    "back",
    "left",
    "right",
    "jump",
    "sneak",
    "sprint",
    "attack",
    "use",
    "drop",
    "inventory",
)

# 语义动作名（DESIGN.md §7.3，命名与 mineflayer 对齐）。
SEMANTIC_ACTIONS = (
    "goto",
    "look_at",
    "dig",
    "place",
    "equip",
    "select_slot",
    "craft",
    "attack_entity",
    "use_block",
    "eat",
)


class ActionSpace:
    """原始/语义动作映射接口桩。

    - `to_ws(action) -> dict`：原始动作 dict → WS 下行动作（M2 实现）。
    - `semantic_to_primitive(semantic) -> list[dict]`：语义动作 → 原始动作序列
      （M2/M5 实现），轨迹双标签记录。
    """

    def __init__(self, mode: str = "discrete", camera_bins: int = CAMERA_BINS) -> None:
        self.mode = mode  # "discrete" | "continuous" | "vpt_token"
        self.camera_bins = camera_bins

    def to_ws(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """原始动作 dict → WS 下行消息（客户端可执行）。

        依赖 M2：client_ws 的 ActionApplier 注入。
        """
        raise NotImplementedError("M2 实现：buttons + camera 121 bin 编码为 WS action")

    def semantic_to_primitive(self, semantic: Dict[str, Any]) -> list:
        """语义动作 → 原始动作序列。

        依赖 M2 + M5：goto→ComputePath→逐航点；craft→合成 UI 操作序列。
        """
        raise NotImplementedError("M2 实现：语义动作分解为原始动作序列")

    def vpt_token(self, action: Dict[str, Any]) -> int:
        """VPT 离散 token 编码（可选模式，§7.2）。

        依赖 M2：与 STEVE-1 / VPT 预训练对齐的单 token 输出。
        """
        raise NotImplementedError("M2 实现：VPT 分层 token 编码")
