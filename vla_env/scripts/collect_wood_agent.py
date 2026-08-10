#!/usr/bin/env python3
"""M7 验收 + 3D 导航升级：collect_wood_agent —— 脚本策略（--task 参数化）。

用法（在 vla_env/ 目录内，避免 namespace 遮蔽）：
    .venv/bin/python scripts/collect_wood_agent.py [--task collect_wood] [--max-steps 600] [--ticks 2]

支持任务（--task，见 TASK_CONFIG）：
- collect_wood：挖 4 个 minecraft:oak_log（M7 原任务，向后兼容）
- collect_stone：挖 8 个 minecraft:stone（石头常被草方块/泥土覆盖，先挖穿 cover 块暴露）
- kill_animal：击杀 2 只 minecraft:pig（reset 后 gRPC spawn_entity 生成，钻石剑近战）

策略（V3，NavV2 动作级 3D 导航，DESIGN.md §4.5）：
  1. **A* 动作级 3D 导航**：approach 阶段调 `ComputePath(cost_mode=dig)`，服务端返回
     动作注解航点 {pos, action, target}；执行器按动作分派：walk/jump/fall 移动、
     dig/dig_down 挖穿挡路方块（悬浮树叶/覆盖层）、place 垫方块爬高。
  2. **挖块期不动、不跳、不重瞄**：进入 attack 先"站稳"（settle 只发相机），再锁定目标
     方块连续按住 attack。任何移动/跳跃都会让准星漂移 → 方块破坏进度清零。
  3. **目标粘滞**：不频繁重扫换目标；attack 期间每 N 步确认目标是否还在（被挖掉换下一个）。
  4. **挖完停顿**：方块破坏后掉落物从眼前下落会挡射线，给 2 步间隔再继续。
  5. **受阻换目标**：A* 无路 / 卡死时游走换下一个，不硬钻。
  6. **collect_stone 挖穿覆盖层**：stone 被 grass_block/dirt/gravel 覆盖（无空气相邻面）时，
     改为攻击其正上方 cover 块以暴露 stone。
  7. **kill_animal 追打**：动物会逃，entity attack 不 settle、每步用最新 get_state 重定位，
     逃出 reach 立即切回 approach 重追。

成功判定：terminated → `M7_COLLECT_WOOD_OK steps=N`（collect_wood）或
`M7_TASK_OK task=<task> steps=N`（其余任务）exit 0。

可复用核心：`collect_wood_policy(env, step_fn, ...)` 供 demo_record.py 录制
第一视角视频时调用（传入不同 step_fn 即可：env.step 或 录帧版 send+grpc）。
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from vla_env.env import MinecraftEnv

OAK_LOG = "minecraft:oak_log"
EYE_HEIGHT = 1.62          # 站立眼睛高度
RES = 224
REACH = 4.0                # 挖掘可达阈值（原版 4.5，留余量）
REACH_MIN_H = 0.6          # 水平过近时准星贴脸，视为换目标
APPROACH_DY_DOWN = 6.0     # 允许 approach 接近比玩家低最多 6 格的目标（山坡/树冠下可走下去接近）
WANDER_STEPS = 14
SETTLE_STEPS = 3           # 进入 attack 后先停几拍（只相机不动不挖）
DIG_SCAN_EVERY = 12        # attack 期间每隔 N 步确认目标是否还在
MAX_DIG_TRY = 90           # 同一目标连续挖 N 步未破坏则放弃
APPROACH_RESCAN = 5        # approach 期间每隔 N 步重扫（检测转为 attack）
STUCK_STEPS = 20           # 位置基本不动 N 步 → 卡死
APPROACH_STUCK_STEPS = 6   # approach 朝当前航点连续 N 步几乎没动 → 重算路径
STUCK_MOVED = 0.02         # 水平位移 < 此值视为"没动"
GOTO_WATCHDOG_STEPS = 60   # goto 活跃但连续无 goto_status 步数 → 取消重规划（客户端 30 tick 卡死≈15 步，60 步远超）
REPLAN_STALL_STEPS = 6    # XZ 几乎不动 N 步 → 本地重规划（≈1.2s 及时；STUCK_STEPS=20 游走太慢）
REPLAN_MAX = 3            # 同一目标本地重规划上限，超过才走 stuck 游走/黑名单兜底
WP_ARRIVE_DIST = 1.5       # 距航点 < 此值视为到达
JUMP_Y_THRESH = 0.5        # 下一航点 y 高于玩家此值 → 跳跃（翻 1 格台阶）

# NavV2 动作级导航（DESIGN.md §4.5）：服务端 A* 输出动作注解航点，
# 执行器按 action 分派。dig 模式开启挖穿/下挖；place 模式额外开启垫方块爬高。
PATH_COST_MODE_DIG = "dig"
PATH_COST_MODE_DEFAULT = "default"
PATH_COST_MODE_PLACE = "place"   # A* place 模式：额外开启垫方块爬高（低处→高处阶梯脱困）
PLACE_ITEM = "minecraft:dirt"   # reset 补发到 hotbar 5（TaskRegistry PLACE_BLOCK）
PLACE_SLOT = 5
PLACE_SETTLE_STEPS = 3          # 放置前 look_at 收敛拍数
PLACE_MAX_TRY = 12              # 放置失败重试上限
# 垫方块爬高（M11 客户端 PillarExecutor 技能）
PILLAR_MAX_BLOCKS = 6           # 单次技能最多垫几块（客户端按此停）
PILLAR_WATCHDOG_STEPS = 120     # Python 侧看门狗：无终态事件的最大步数（客户端另有 tick 预算）
STUCK_DIG_MAX = 2               # 卡死→强制挖面前方块脱困的最大次数（mc-collector stuckDig 借鉴）
WATER_STUCK_TICKS = 5     # 水中 py 连续 N 步几乎不升（<0.1）→ 视为卡墙，垫 dirt 阶梯脱困
WATER_PLACE_MAX = 24      # 垫块尝试步数上限，超过放弃垫块恢复纯游泳
WATER_BLOCK = "minecraft:dirt"   # 垫块材料（reset 已发放 dirt×64 到 hotbar）
TURN_IN_PLACE_DEG = 45.0        # |yaw 误差| 超过此值时放前进原地转向（转向-前进分离，防绕圈）
TURN_MAX_TICKS = 8              # 原地转向的最大连续步数（超时视为卡死，防无限打转）
STRAFE_DEG = 8.0                # |yaw 误差| 超过此值时前进同时侧移纠偏（strafe 朝目标）

# 按方块选择最佳工具（reset 发放：0 镐 / 1 斧 / 2 铲 / 3 剑 / 4 锄 / 5 dirt×64）。
# 挖掘路径穿过石头/泥土/原木时切换对应工具，避免用任务主工具（如斧）硬挖石头（极慢）。
_TOOL_FOR_BLOCK = {
    "minecraft:stone": "minecraft:diamond_pickaxe",
    "minecraft:cobblestone": "minecraft:diamond_pickaxe",
    "minecraft:deepslate": "minecraft:diamond_pickaxe",
    "minecraft:granite": "minecraft:diamond_pickaxe",
    "minecraft:diorite": "minecraft:diamond_pickaxe",
    "minecraft:andesite": "minecraft:diamond_pickaxe",
    "minecraft:oak_log": "minecraft:diamond_axe",
    "minecraft:oak_planks": "minecraft:diamond_axe",
    "minecraft:oak_leaves": "minecraft:diamond_axe",
    "minecraft:grass_block": "minecraft:diamond_shovel",
    "minecraft:dirt": "minecraft:diamond_shovel",
    "minecraft:sand": "minecraft:diamond_shovel",
    "minecraft:gravel": "minecraft:diamond_shovel",
    "minecraft:clay": "minecraft:diamond_shovel",
    "minecraft:podzol": "minecraft:diamond_shovel",
    "minecraft:coarse_dirt": "minecraft:diamond_shovel",
    "minecraft:mycelium": "minecraft:diamond_shovel",
    "minecraft:snow_block": "minecraft:diamond_shovel",
    "minecraft:grass": "minecraft:diamond_shovel",     # 矮草/草丛用铲
    "minecraft:short_grass": "minecraft:diamond_shovel",
    "minecraft:tall_grass": "minecraft:diamond_shovel",
    "minecraft:vine": "minecraft:diamond_shovel",      # 藤蔓
    "minecraft:dead_bush": "minecraft:diamond_shovel",
    "minecraft:netherrack": "minecraft:diamond_pickaxe",
    "minecraft:nether_bricks": "minecraft:diamond_pickaxe",
    "minecraft:blackstone": "minecraft:diamond_pickaxe",
    "minecraft:basalt": "minecraft:diamond_pickaxe",
    "minecraft:coal_ore": "minecraft:diamond_pickaxe",
    "minecraft:iron_ore": "minecraft:diamond_pickaxe",
    "minecraft:copper_ore": "minecraft:diamond_pickaxe",
    "minecraft:gold_ore": "minecraft:diamond_pickaxe",
    "minecraft:redstone_ore": "minecraft:diamond_pickaxe",
    "minecraft:diamond_ore": "minecraft:diamond_pickaxe",
    "minecraft:emerald_ore": "minecraft:diamond_pickaxe",
    "minecraft:lapis_ore": "minecraft:diamond_pickaxe",
    "minecraft:stone_bricks": "minecraft:diamond_pickaxe",
    "minecraft:cobbled_deepslate": "minecraft:diamond_pickaxe",
    "minecraft:gravel": "minecraft:diamond_shovel",
    "minecraft:calcite": "minecraft:diamond_pickaxe",
    "minecraft:tuff": "minecraft:diamond_pickaxe",
    "minecraft:dripstone_block": "minecraft:diamond_pickaxe",
}

# 低处→高处阶梯脱困触发：目标高于玩家超过此值且多次失败 → 切 place 模式垫方块爬高。
CLIMB_TARGET_DY = 2.0
CLIMB_MAX_DY = 12.0            # climb_mode 时 select_target 允许选比玩家高多少格的目标
CLIMB_RESET_DY = 3.0            # 玩家已爬升这么多格 → 恢复普通模式

# stuckDig 只挖的廉价方块（树叶/草/土/沙等），石头/原木等硬方块不挖（防穿墙/护栏掉水）
_CHEAP_DIGGABLE = {
    "minecraft:oak_leaves", "minecraft:birch_leaves", "minecraft:spruce_leaves",
    "minecraft:jungle_leaves", "minecraft:acacia_leaves", "minecraft:dark_oak_leaves",
    "minecraft:grass", "minecraft:short_grass", "minecraft:tall_grass",
    "minecraft:grass_block", "minecraft:dirt", "minecraft:sand", "minecraft:gravel",
    "minecraft:vine", "minecraft:cobweb", "minecraft:dead_bush",
}

# 任务配置表：--task 的完整策略语义（目标类型 / 目标集合 / 数量 / 攻击距离）。
# cover 仅 collect_stone：覆盖在 stone 上方、需要挖穿才能暴露 stone 的块。
# equip：任务主工具（reset 已发放全套钻石工具到 hotbar 0-4，此处按目标选择）。
TASK_CONFIG = {
    "collect_wood": {
        "target_type": "block",
        "targets": {"minecraft:oak_log"},
        "count": 4,
        "reach": 4.0,
        "equip": "minecraft:diamond_axe",
    },
    "collect_stone": {
        "target_type": "block",
        "targets": {"minecraft:stone"},
        "count": 8,
        "reach": 4.0,
        "cover": {"minecraft:grass_block", "minecraft:dirt", "minecraft:gravel"},
        "equip": "minecraft:diamond_pickaxe",
    },
    "kill_animal": {
        "target_type": "entity",
        "targets": {"minecraft:pig"},
        "count": 2,
        "reach": 3.0,        # 近战攻击距离
        "equip": "minecraft:diamond_sword",
    },
}


def log(*a, **kw) -> None:
    """打印并立即 flush；支持 print 的 kwargs（如 file=sys.stderr）。"""
    print(*a, flush=True, **kw)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="M7 脚本策略验收（A*+跳跃 3D 导航）：collect_wood / collect_stone / kill_animal")
    p.add_argument("--task", choices=list(TASK_CONFIG), default="collect_wood",
                   help="任务：collect_wood（挖 4 原木）/ collect_stone（挖 8 石头，挖穿覆盖层）"
                        "/ kill_animal（钻石剑击杀 2 猪）")
    p.add_argument("--max-steps", type=int, default=600)
    p.add_argument("--ticks", type=int, default=2, help="每步服务端 tick 数")
    p.add_argument("--half-extent", type=int, default=16, help="体素半宽")
    p.add_argument("--player", default="agent0")
    return p.parse_args()


def blocks_3d(palette: List[str], data: np.ndarray, size: int) -> np.ndarray:
    """palette+data → names[y][z][x] 3D 块名数组（去掉块状态，如 [axis=y]）。

    data 索引顺序（proto VoxelReply）：y 外层 → z → x 最内层，即 arr[y][z][x]。
    """
    names = [p.split("[")[0] for p in palette]
    idx = np.asarray(data, dtype=np.int32).reshape(size, size, size)
    return np.asarray(names)[idx]


def find_blocks(
    palette: List[str],
    data: np.ndarray,
    origin: Tuple[int, int, int],
    target_names: set,
) -> List[Tuple[int, int, int]]:
    """palette+data 还原成 3D 块名数组，返回所有 target_names 内块的世界坐标。"""
    size = data.shape[0]
    names3 = blocks_3d(palette, data, size)
    ox, oy, oz = origin
    matches: List[Tuple[int, int, int]] = []
    for y in range(size):
        for z in range(size):
            for x in range(size):
                if str(names3[y, z, x]) in target_names:
                    matches.append((ox + x, oy + y, oz + z))
    return matches


def find_logs(palette, data, origin):
    """向后兼容包装：find_blocks 查找 oak_log（旧调用点/外部引用）。"""
    return find_blocks(palette, data, origin, {OAK_LOG})


def name_at(blocks3: np.ndarray, origin: Tuple[int, int, int],
            pos: Tuple[int, int, int]) -> Optional[str]:
    """世界坐标 → 3D 块名；越界视为 None（即空气）。

    pos 容忍浮点（proto 的 Vec3 为 double），强制取整。
    """
    ox, oy, oz = origin
    x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
    size = blocks3.shape[0]
    lx, ly, lz = x - ox, y - oy, z - oz
    if not (0 <= lx < size and 0 <= ly < size and 0 <= lz < size):
        return None
    return str(blocks3[ly, lz, lx])


def is_exposed(bx: int, by: int, bz: int, blocks3: np.ndarray,
               origin: Tuple[int, int, int]) -> bool:
    """目标块是否暴露：水平 4 邻或正上方任一为空气即视为暴露（覆盖层判据）。"""
    for nx, ny, nz in ((bx - 1, by, bz), (bx + 1, by, bz),
                       (bx, by, bz - 1), (bx, by, bz + 1), (bx, by + 1, bz)):
        name = name_at(blocks3, origin, (nx, ny, nz))
        if name is None or name == "minecraft:air":
            return True
    return False


def find_entities(state: dict, entity_types: set) -> List[Tuple[float, float, float]]:
    """从 state["entities"] 过滤目标实体，返回 (x, y, z) 坐标列表。"""
    out: List[Tuple[float, float, float]] = []
    for e in state.get("entities", []):
        if e.get("type") in entity_types:
            out.append((float(e["x"]), float(e["y"]), float(e["z"])))
    return out


def _select_block_target(
    found: List[Tuple[int, int, int]],
    px: float, py: float, pz: float,
    exclude: Optional[Dict[Tuple[int, int, int], int]],
    cfg: dict,
    blocks3: Optional[np.ndarray],
    origin: Optional[Tuple[int, int, int]],
    step: int,
    max_dy_up: float = 2.6,
) -> Tuple[str, Optional[Tuple[int, int, int]]]:
    """block 任务目标选择：优先可达任务块（attack），否则最近块（approach），无 → none。

    collect_stone 特例：
    - 只保留**浅层** stone（正上方是空气或 cover 块）——深埋的（正上方是石头/实心）
      A* 无可站目标且挖不到，直接排除；
    - 排除水平过近（脚下）的块（准星贴脸挖不到，A* 会原地打转）；
    - 可达 stone 被覆盖时攻击其正上方 cover 块（target 换成 (bx, by+1, bz)）以暴露，
      并把 cover 块短暂加入黑名单（防挖穿后反复选它）。
    """
    skip = set(exclude.keys()) if exclude else set()
    eye_y = py + EYE_HEIGHT
    cover = cfg.get("cover", set())
    targets = cfg["targets"]

    candidates = list(found)
    if cover and blocks3 is not None and origin is not None:
        shallow = [
            b for b in candidates
            if name_at(blocks3, origin, (b[0], b[1] + 1, b[2])) in (None, "minecraft:air")
            or name_at(blocks3, origin, (b[0], b[1] + 1, b[2])) in cover
        ]
        if shallow:
            candidates = shallow
    # 排除水平过近（脚下）的块 + 垂直不可及（|dy|>max_dy_up 挖不到的高目标，
    # A* 兜底会给出"原地平凡路径"导致原地打转——NavV2 直接过滤；climb_mode 放宽
    # 到 CLIMB_MAX_DY 允许垫方块爬高够到高处的树）。
    # 但只严格限"比玩家高"（树冠够不着）；"比玩家低"放宽到 APPROACH_DY_DOWN——
    # 真实地形（山坡/树丛）里 agent 可能站在高处，低层的树仍应能"走近再挖"。
    candidates = [
        b for b in candidates
        if math.hypot(b[0] + 0.5 - px, b[2] + 0.5 - pz) >= REACH_MIN_H
        and b[1] - py <= max_dy_up
        and py - b[1] <= APPROACH_DY_DOWN
    ]

    reachable: List[Tuple[float, int, int, int]] = []
    for (bx, by, bz) in candidates:
        if (bx, by, bz) in skip:
            continue
        dx = bx + 0.5 - px
        dy = by + 0.5 - eye_y
        dz = bz + 0.5 - pz
        h = math.hypot(dx, dz)
        d3 = math.hypot(dy, h)
        if d3 <= REACH and abs(dy) <= 2.6 and REACH_MIN_H <= h <= 3.0:
            reachable.append((d3, bx, by, bz))
    if reachable:
        reachable.sort(key=lambda t: t[0])
        if cover and blocks3 is not None and origin is not None:
            # 优先选暴露的任务块；全部被覆盖 → 挖穿最近的 cover 块
            for _, bx, by, bz in reachable:
                if is_exposed(bx, by, bz, blocks3, origin):
                    return "attack", (bx, by, bz)
            for _, bx, by, bz in reachable:
                cover_pos = (bx, by + 1, bz)
                if cover_pos in skip:
                    continue
                name = name_at(blocks3, origin, cover_pos)
                if name is not None and name != "minecraft:air" and (
                        name in cover or name not in targets):
                    if exclude is not None:
                        exclude[cover_pos] = step + 60
                    return "attack", cover_pos
            _, bx, by, bz = reachable[0]
            if cover:
                # collect_stone：可达但挖不到（覆盖块不可挖）→ 交给向下挖掘
                return "none", None
            return "approach", (bx, by, bz)
        _, bx, by, bz = reachable[0]
        return "attack", (bx, by, bz)
    if candidates:
        if cover:
            # collect_stone：无可达石头不 approach（石头到处都在地下，直接向下挖）
            return "none", None
        # 簇级选择（mc-collector TreeMiner 借鉴）：6 面连通聚成树簇，
        # 取最近簇的**最矮**原木（自底向上挖，树冠级联掉落），跳过黑名单
        cand = [b for b in candidates if b not in skip]
        if not cand:
            return "none", None
        clusters = [cl for cl in _log_clusters(cand) if any(b not in skip for b in cl)]
        if not clusters:
            return "none", None
        best_cluster = min(
            clusters,
            key=lambda cl: min(
                (b[0] + 0.5 - px) ** 2 + (b[1] + 0.5 - py) ** 2 + (b[2] + 0.5 - pz) ** 2
                for b in cl),
        )
        n = min(
            (b for b in best_cluster if b not in skip),
            key=lambda b: (b[1], (b[0] - px) ** 2 + (b[2] - pz) ** 2),  # 最矮优先，次水平近
            default=None,
        )
        if n is not None:
            return "approach", n
    return "none", None


def _select_entity_target(
    ents: List[Tuple[float, float, float]],
    px: float, py: float, pz: float,
    reach: float,
) -> Tuple[str, Optional[Tuple[float, float, float]]]:
    """entity 任务目标选择：最近动物 ≤ reach → attack，否则 approach，无动物 → none。"""
    if not ents:
        return "none", None
    n = min(ents, key=lambda e: (e[0] - px) ** 2 + (e[1] - py) ** 2 + (e[2] - pz) ** 2)
    if math.hypot(n[0] - px, n[1] - py, n[2] - pz) <= reach:
        return "attack", n
    return "approach", n


def select_target(
    found: List[Tuple[float, float, float]],
    px: float, py: float, pz: float,
    exclude: Optional[Dict[Tuple[int, int, int], int]] = None,
    task: str = "collect_wood",
    blocks3: Optional[np.ndarray] = None,
    origin: Optional[Tuple[int, int, int]] = None,
    step: int = 0,
    max_dy_up: float = 2.6,
) -> Tuple[str, Optional[Tuple[float, float, float]]]:
    """目标选择（按 TASK_CONFIG[task] 分支），返回 (mode, target)，mode ∈ attack/approach/none。

    max_dy_up：允许选比玩家高多少格的目标（默认 2.6；climb_mode 放宽到 CLIMB_MAX_DY）。
    """
    cfg = TASK_CONFIG[task]
    if cfg["target_type"] == "entity":
        return _select_entity_target(found, px, py, pz, cfg["reach"])
    return _select_block_target(found, px, py, pz, exclude, cfg, blocks3, origin, step, max_dy_up)


def look_at(
    px: float, py: float, pz: float,
    bx: int, by: int, bz: int,
    pitch_clamp: Optional[float] = None,
) -> Tuple[float, float]:
    """玩家 pos 与目标点（块中心/航点），返回 (yaw, pitch)。pitch_clamp 夹紧 |pitch|。

    目标与玩家**同一列**（正上方的头顶块、正下方的垫块落点）时 h≈0：pitch 按 dy 符号
    给 ∓90（MC 约定 -90=正上、+90=正下），而不是退化成 0（平视）——否则「挖头顶」
    「瞄脚下」永远瞄不中。与客户端 dev.vla.client.nav.Aim 同口径。
    """
    dx, dy, dz = bx - px, by - py, bz - pz
    h = math.hypot(dx, dz)
    yaw = math.degrees(math.atan2(-dx, dz))
    if h > 1e-6:
        pitch = math.degrees(math.atan2(-dy, h))
    elif abs(dy) > 1e-6:
        pitch = -90.0 if dy > 0 else 90.0
    else:
        pitch = 0.0
    if pitch_clamp is not None:
        pitch = max(-pitch_clamp, min(pitch_clamp, pitch))
    return yaw, pitch


def dist3(px, py, pz, bx, by, bz) -> float:
    return math.sqrt((bx - px) ** 2 + (by - py) ** 2 + (bz - pz) ** 2)


def _tool_slot(env, item: str) -> Optional[int]:
    """在 hotbar 0-8 中查找 item 的槽位（reset 已把钻石工具固定在 0-4）。"""
    st = env.grpc.get_state(player=env.player)
    for it in st["inventory"].get("main", []):
        if it.get("item") == item:
            s = int(it["slot"])
            if 0 <= s < 9:
                return s
    return None


def ensure_equip(env, item: str) -> bool:
    """确认手持 item：不在手则从 hotbar 0-8 选槽（仅发一次 hotbar 切换）。

    返回是否已确认持有（找到槽位）。找不到工具时打印告警并返回 False，
    调用方应避免发出攻击动作，防止用错工具/空手硬挖。
    """
    st = env.grpc.get_state(player=env.player)
    held = st["inventory"].get("held_item", "")
    if held == item:
        return True
    slot = _tool_slot(env, item)
    if slot is None:
        log(f"  [equip] WARN: {item} 不在 hotbar 0-8，main={[(i.get('item'), i.get('slot')) for i in st['inventory'].get('main', [])]}")
        return False
    env.ws.send_action({"hotbar": slot})
    log(f"  [equip] held={held} -> select hotbar slot {slot} ({item})")
    return True


def _ensure_tool_for_block(env, block_name: Optional[str], fallback: str) -> bool:
    """按方块类型确保手持正确工具（镐/斧/铲）；未知方块用 fallback。

    挖掘路径穿越的方块（石头/泥土/原木）与任务主工具常不同：collect_wood 主斧
    挖石头极慢，应切镐。返回是否已确认持有。
    """
    tool = _TOOL_FOR_BLOCK.get(block_name or "", fallback)
    if tool is None:
        return False
    return ensure_equip(env, tool)


def _block_target_alive(
    target: Tuple[int, int, int],
    palette: List[str],
    data: np.ndarray,
    origin: Tuple[int, int, int],
    targets: set,
) -> bool:
    """attack 确认目标仍在：任务块仍在，或（cover 目标）该位置仍非空气。"""
    if target in find_blocks(palette, data, origin, targets):
        return True
    name = name_at(blocks_3d(palette, data, data.shape[0]), origin, target)
    return name is not None and name != "minecraft:air"


def _pos_is_block(blocks3: np.ndarray, origin: Tuple[int, int, int],
                  pos: Tuple[int, int, int]) -> bool:
    """世界坐标处是否仍有方块（非空气；越界视为空气）。NavV2 路径 dig 完成判定用。"""
    name = name_at(blocks3, origin, pos)
    return name is not None and name != "minecraft:air"


def _block_ahead(yaw: float, px: float, py: float, pz: float,
                 blocks3: np.ndarray, origin: Tuple[int, int, int]) -> Optional[Tuple[int, int, int]]:
    """玩家朝向正前方 1-2 格内首个**廉价可挖**方块（脚格+头格），供卡死脱困挖掘。

    mc-collector stuckDig 借鉴：卡死时挖穿面前树叶/土/草等廉价方块脱困；
    石头/原木等硬方块不挖（防 tunnel 穿墙/护栏导致掉水）。
    """
    yaw_rad = math.radians(yaw)
    fx, fz = -math.sin(yaw_rad), math.cos(yaw_rad)
    for k in (1, 2):
        for h in (0, 1):
            pos = (int(math.floor(px + fx * k)), int(math.floor(py + h)), int(math.floor(pz + fz * k)))
            n = name_at(blocks3, origin, pos)
            if n is not None and n in _CHEAP_DIGGABLE:
                return pos
    return None


def _nearest_shore(px: float, py: float, pz: float,
                   blocks3: np.ndarray, origin: Tuple[int, int, int],
                   max_r: int = 10) -> Optional[Tuple[int, int, int]]:
    """玩家周围 max_r 内最近的"可站陆地格"（脚格非液体，且若为空气则脚下有实心）。

    溺水自救用：朝该点游即可上岸。优先选**贴近水面**的岸点：岸格 y 在
    [水面_y-1, 水面_y+1]（水面_y = 玩家脚格向上扫到第一个非水/岩浆格的 y），
    相邻格中有水（玩家能游到边缘爬上），且上方未被实心墙盖住（垂直坑壁爬不上，
    会永远困在坑里）。找不到水面附近的岸点再退回原"同水平距离优先高位"逻辑。
    """
    ox, oy, oz = origin
    size = blocks3.shape[0]
    ipx, ipy, ipz = int(px), int(py), int(pz)

    # 水面_y：从玩家脚格向上扫，第一个非水/岩浆的 y（头已出水则≈脚格+1）
    water_y = ipy + 1
    lwx, lwz = ipx - ox, ipz - oz
    if 0 <= lwx < size and 0 <= lwz < size:
        for y in range(ipy, ipy + 8):
            ly = y - oy
            if not (0 <= ly < size):
                break
            if str(blocks3[ly, lwz, lwx]) not in ("minecraft:water", "minecraft:lava"):
                water_y = y
                break

    def _water(ly: int, lz: int, lx: int) -> bool:
        if not (0 <= lx < size and 0 <= ly < size and 0 <= lz < size):
            return False
        return str(blocks3[ly, lz, lx]) in ("minecraft:water", "minecraft:lava")

    def _near_surface(y: int, x: int, z: int) -> bool:
        """贴近水面且可爬上岸：y 在水面±1、4 向相邻格有水、上方未被实心墙盖住。"""
        if abs(y - water_y) > 1:
            return False
        ly, lx, lz = y - oy, x - ox, z - oz
        if not (0 <= lx < size and 0 <= ly < size and 0 <= lz < size):
            return False
        ly_up = ly + 1
        if 0 <= ly_up < size:
            up = str(blocks3[ly_up, lz, lx])
            if up not in ("minecraft:air", "minecraft:water", "minecraft:lava"):
                return False  # 上方被实心墙盖住，爬不上去
        for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if _water(ly, lz + d[1], lx + d[0]):
                return True  # 相邻格有水 → 玩家能游到边缘爬上
        return False

    best: Optional[Tuple[int, int, int]] = None
    best_score = None
    near_best: Optional[Tuple[int, int, int]] = None
    near_best_score = None
    for dy in range(-4, 5):
        for dx in range(-max_r, max_r + 1):
            for dz in range(-max_r, max_r + 1):
                x, y, z = ipx + dx, ipy + dy, ipz + dz
                lx, ly, lz = x - ox, y - oy, z - oz
                if not (0 <= lx < size and 0 <= ly < size and 0 <= lz < size):
                    continue
                foot = str(blocks3[ly, lz, lx])
                if foot in ("minecraft:water", "minecraft:lava"):
                    continue
                if foot == "minecraft:air":
                    if ly - 1 < 0:
                        continue
                    below = str(blocks3[ly - 1, lz, lx])
                    if below in ("minecraft:air", "minecraft:water", "minecraft:lava"):
                        continue
                # 同水平距离优先选高位格（坑里要攀爬出水口），低位格额外惩罚；
                # 只用水平距离（dy 不计入，高位天然更近出口）
                d2 = dx * dx + dz * dz
                score = d2 + max(0.0, py - y) * 25.0
                if best_score is None or score < best_score:
                    best_score = score
                    best = (x, y, z)
                if _near_surface(y, x, z):
                    if near_best_score is None or score < near_best_score:
                        near_best_score = score
                        near_best = (x, y, z)
    return near_best if near_best is not None else best


def _water_escape_action(env, state, px: float, py: float, pz: float,
                         swim_shore: Tuple[int, int, int], half_extent: int,
                         water_place_tries: int, water_last_py: float,
                         water_wall_ticks: int):
    """水中自救动作：正常朝岸游；连续 WATER_STUCK_TICKS 步 py 几乎不升（卡墙）→ 垫 dirt 阶梯爬出。

    垂直积水坑的坑壁爬不上，纯游泳会永远困在坑里——改往岸方向在当前脚格上方
    1 格的前方格垫 dirt（瞄其底面 use），下一拍 forward+jump 跳上，逐格垫高爬出。
    放置格被墙占时向左右偏移找可放格；water_place_tries > WATER_PLACE_MAX 仍没
    脱困 → 放弃垫块恢复纯游泳。
    返回 (action, water_place_tries, water_last_py, water_wall_ticks)。
    """
    sx, sy, sz = swim_shore

    def _swim() -> dict:
        need_up = (sy - py) > 0.3
        return {"forward": True, "jump": need_up, "camera": [0.0, 0.0]}

    # 卡墙判定：py 相对上次每步升高 < 0.1 → 计一步卡墙（连续 WATER_STUCK_TICKS 步进入垫块）
    if py - water_last_py < 0.1:
        water_wall_ticks += 1
    else:
        water_wall_ticks = 0
    water_last_py = py

    placing = water_place_tries > 0 or water_wall_ticks >= WATER_STUCK_TICKS
    if not placing:
        return _swim(), water_place_tries, water_last_py, water_wall_ticks

    water_place_tries += 1
    if water_place_tries > WATER_PLACE_MAX:
        log(f"  [water] 垫 {WATER_BLOCK} {WATER_PLACE_MAX} 次仍未脱困，放弃垫块恢复纯游泳")
        return _swim(), water_place_tries, water_last_py, water_wall_ticks

    # 朝岸方向（XZ 主分量）的"脚格上方 1 格"前方格；被墙占时左右偏移找可放格
    dx, dz = sx - px, sz - pz
    if abs(dx) >= abs(dz):
        dirx, dirz = (1 if dx > 0 else -1), 0
    else:
        dirx, dirz = 0, (1 if dz > 0 else -1)
    fx, fy, fz = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
    cands = [(fx + dirx, fy + 1, fz + dirz)]
    for odx, odz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        c = (fx + dirx + odx, fy + 1, fz + dirz + odz)
        if c not in cands:
            cands.append(c)

    # 找第一个可放格：目标格是 air/water、下方有支撑（非 air/water/lava）、
    # 上方两格非实心（跳上去后身体/头不被墙挡）
    pick = None
    try:
        palette, data, origin, _ = env.grpc.get_voxels(player=env.player, half_extent=half_extent)
        b3 = blocks_3d(palette, data, data.shape[0])
    except Exception:  # noqa: BLE001 —— 体素获取失败按无可放格处理（继续游）
        b3 = None
    if b3 is not None:
        for (cx, cy, cz) in cands:
            n = name_at(b3, origin, (cx, cy, cz))
            below = name_at(b3, origin, (cx, cy - 1, cz))
            above1 = name_at(b3, origin, (cx, cy + 1, cz))
            above2 = name_at(b3, origin, (cx, cy + 2, cz))
            if n in ("minecraft:air", "minecraft:water") \
                    and below not in (None, "minecraft:air", "minecraft:water", "minecraft:lava") \
                    and above1 in (None, "minecraft:air", "minecraft:water") \
                    and above2 in (None, "minecraft:air", "minecraft:water"):
                pick = (cx, cy, cz)
                break
    if pick is None:
        return _swim(), water_place_tries, water_last_py, water_wall_ticks

    # 选 dirt 槽：仅未手持时发一次 hotbar（hotbar 是电平保持的一次性字段，
    # 本步 action 的 use 在下一次 step 时已切好槽）
    if state["inventory"].get("held_item", "") != WATER_BLOCK:
        slot = None
        for it in state["inventory"].get("main", []):
            if it.get("item") == WATER_BLOCK:
                s = int(it["slot"])
                if 0 <= s < 9:
                    slot = s
                    break
        if slot is None:
            return _swim(), water_place_tries, water_last_py, water_wall_ticks
        env.ws.send_action({"hotbar": slot})
        log(f"  [water] 选 {WATER_BLOCK} 槽 {slot}")
    # 瞄放置格底面 use（本步不前进，跳上在下一拍 forward+jump 完成）
    bx, by, bz = pick
    env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": float(by), "z": bz + 0.5})
    return {"use": True, "camera": [0.0, 0.0]}, water_place_tries, water_last_py, water_wall_ticks


def _log_clusters(logs: List[Tuple[int, int, int]]) -> List[List[Tuple[int, int, int]]]:
    """6 面连通（上/下/北/南/东/西）把原木聚成树簇。

    mc-collector TreeMiner 借鉴：同一棵树的原木连成簇，取簇内最矮原木自底向上挖，
    树冠/上层原木会级联掉落，避免对同一棵树重复寻路。
    """
    clusters: List[List[Tuple[int, int, int]]] = []
    unvisited = set(logs)
    while unvisited:
        start = unvisited.pop()
        cluster = [start]
        stack = [start]
        while stack:
            b = stack.pop()
            for n in ((b[0] - 1, b[1], b[2]), (b[0] + 1, b[1], b[2]),
                      (b[0], b[1] - 1, b[2]), (b[0], b[1] + 1, b[2]),
                      (b[0], b[1], b[2] - 1), (b[0], b[1], b[2] + 1)):
                if n in unvisited:
                    unvisited.remove(n)
                    cluster.append(n)
                    stack.append(n)
        clusters.append(cluster)
    return clusters


def _sign3(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> Tuple[int, int, int]:
    """两点坐标符号差（-1/0/1），用于航点方向比较。"""
    return (int((b[0] > a[0]) - (b[0] < a[0])),
            int((b[1] > a[1]) - (b[1] < a[1])),
            int((b[2] > a[2]) - (b[2] < a[2])))


def _goto_plan(waypoints: List, details: List[Dict]) -> Tuple[List[Tuple[int, int, int]], List[Tuple[int, int, int]]]:
    """从 PathReply 构建客户端 goto 计划：(移动航点, A* 计划挖的方块列表)。

    移动航点 = 服务端 **LOS 稀疏化**导航点（直线段可达，客户端在两点间自由走，
    不再逐格罚站/蛇形）；dig 列表 = 服务端 A* 计划要挖的方块（客户端本地跟随**只挖**
    这些，撞到其他实心上报 breakable 由 Python 挖穿/绕行——杜绝乱挖掘）。
    dig_down 目标一并收进 dig 列表。
    """
    wps: List[Tuple[int, int, int]] = [tuple(int(v) for v in w) for w in (waypoints or [])]
    digs: List[Tuple[int, int, int]] = []
    for d in (details or []):
        act = d["action"]
        if act in ("dig", "dig_down") and d.get("target") is not None:
            digs.append(tuple(int(v) for v in d["target"]))
    return wps, digs


def _trivial_goto(wps: List[Tuple[int, int, int]], px, py, pz) -> bool:
    """goto 平凡路径判定：除起点外所有航点都已在玩家脚下（< WP_ARRIVE_DIST）。

    此时 A* 终点 = adjustGoal 兜底到原地（目标不可达），直接发给客户端会立即
    arrived → 重选同一目标 → churn；应黑名单换目标。
    """
    return all(dist3(px, py, pz, *w) < WP_ARRIVE_DIST for w in wps[1:])


def _resume_wps(wps: List[Tuple[int, int, int]], at_pos: Tuple[int, int, int]) -> List[Tuple[int, int, int]]:
    """挖穿阻挡块后恢复 goto 的剩余航点。

    正常情况阻挡块是下一个 walk 航点格（dig target == 下一格）→ 从它开始；
    侧墙（不在航点列表）→ 从离阻挡块最近的航点继续。
    """
    if not wps:
        return []
    if at_pos in wps:
        return list(wps[wps.index(at_pos):])
    j = min(range(len(wps)), key=lambda i: dist3(*wps[i], *at_pos))
    return list(wps[j:])


def _compress_details(details: List[Dict], max_span: int = 2) -> List[Dict]:
    """压缩动作级航点：连续 walk/jump/fall 只保留方向拐点 + 每段 ≤max_span 补点，
    dig/place 一律保留。消除执行器"逐格停顿"并减少路径重算频率（A* 详情未压缩时
    每个格子都是一个航点，走几格就重算一次）。
    """
    if not details:
        return details
    out = [details[0]]
    last_kept = details[0]
    i = 1
    while i < len(details) - 1:
        d = details[i]
        nxt = details[i + 1]
        prev = details[i - 1]
        if d["action"] in ("walk", "jump", "fall", "step_up") \
                and nxt["action"] in ("walk", "jump", "fall", "step_up"):
            turn = _sign3(prev["pos"], d["pos"]) != _sign3(d["pos"], nxt["pos"])
            span = max(abs(d["pos"][0] - last_kept["pos"][0]),
                       abs(d["pos"][1] - last_kept["pos"][1]),
                       abs(d["pos"][2] - last_kept["pos"][2])) >= max_span
            if turn or span:
                out.append(d)
                last_kept = d
        else:
            out.append(d)
            last_kept = d
        i += 1
    if details[-1] != out[-1]:
        out.append(details[-1])
    return out


def collect_wood_policy(
    env: MinecraftEnv,
    step_fn: Callable[[Dict, int], Dict],
    max_steps: int = 600,
    ticks: int = 2,
    half_extent: int = 16,
    task: str = "collect_wood",
    on_path: Optional[Callable[[List, List, Optional[Tuple]], None]] = None,
) -> Tuple[bool, int, float]:
    """脚本策略核心（V2：A* + 跳跃 3D 导航，按 task 参数化）。

    - `env`：已 reset 的 MinecraftEnv
    - `step_fn(action, ticks) -> dict{progress, terminated, truncated}`：
      普通版传 `lambda a, t: env.step(a, ticks=t)[2:] 对应字段`；录帧版传
      send_action + get_step_result（帧由独立线程消费）
    - `task`：TASK_CONFIG 键，默认 "collect_wood"（兼容 demo_record 旧调用）
    - `on_path(waypoints, details, goal)`：可选回调，每次 ComputePath 返回后调用
      （含无路：waypoints 为空即清除）；demo_dig_tree 用它驱动路径粒子可视化。
    返回 (success, steps, max_progress)。假设 env.reset 已由调用方完成。
    """
    cfg = TASK_CONFIG[task]
    target_type = cfg["target_type"]
    targets = cfg["targets"]
    reach = cfg["reach"]
    target_label = "pigs" if target_type == "entity" else "blocks"

    # 策略状态
    mode = "none"
    target: Optional[Tuple[float, float, float]] = None
    waypoints: List[Tuple[float, float, float]] = []
    wp: Optional[Tuple[float, float, float]] = None
    path_details: List[Dict] = []          # NavV2：动作级航点 [{pos, action, target}]
    pd: Optional[Dict] = None              # 当前动作 detail
    path_dig_try = 0                       # 路径 dig 子状态尝试步数
    place_try = 0                          # place 子状态尝试步数
    place_settle = 0                       # place 前 look_at 收敛拍数
    place_equipped = False                 # 是否已选 dirt 槽
    path_cost_mode = PATH_COST_MODE_DIG if cfg["target_type"] == "block" else PATH_COST_MODE_DEFAULT
    goal_stall = 0                        # 已到目标附近但不可达的连续步数（防重算 churn）
    stuck_dig_count = 0                   # 卡死→强制挖面前方块脱困次数（mc-collector stuckDig 借鉴，≤2 次）
    swim_shore: Optional[Tuple[int, int, int]] = None  # 溺水自救：最近的岸边目标（None=不在水中）
    turn_ticks = 0                    # 连续原地转向步数（超 TURN_MAX_TICKS 视为卡死）
    settle_steps = 0
    dig_try = 0
    reposition = 0              # 挖掉方块后后退几步（清掉落物挡视线）
    failed_targets: Dict[Tuple[int, int, int], int] = {}   # 黑名单 {pos: expire_step}
    wander_left = 0
    wander_yaw = 0.0
    wander_jump = 0
    stuck_count = 0
    wp_stuck = 0            # approach 朝当前航点连续不动步数
    target_fails: Dict[Tuple[int, int, int], int] = {}   # 同一目标 approach 卡死次数
    last_pos: Optional[Tuple[float, float, float]] = None
    max_progress = 0.0
    targets_found = 0
    aim_cache: Optional[Tuple[float, float]] = None
    scan_timer = 0
    # 客户端本地路径跟随（goto）：服务端规划全局航点，客户端逐 tick 跟随 + 碰撞箱。
    goto_active = False                          # 客户端 goto 进行中（block 任务 approach）
    goto_wps: List[Tuple[int, int, int]] = []    # 全部航点（重发/恢复用）
    goto_digs: List[Tuple[int, int, int]] = []   # 服务端 A* 计划挖的方块（客户端只挖这些）
    goto_dig_pos: Optional[Tuple[int, int, int]] = None  # blocked_breakable 后 Python 要挖的块
    goto_dig_try = 0                             # 挖穿尝试步数
    goto_silent = 0                              # goto 活跃但连续无 goto_status 步数（看门狗）
    # 低处→高处脱困：多次失败且目标在头顶 → place 模式垫方块爬高（阶梯）
    climb_mode = False
    climb_base_y = 0.0
    # XZ 不动 → 及时本地重规划（先于 STUCK_STEPS 游走；客户端 NavExecutor 只护 goto）
    replan_stall = 0                       # XZ 不动连续步数（本地重规划触发用）
    replan_count = 0                       # 同一目标本地重规划次数（> REPLAN_MAX 交 stuck 游走兜底）
    # 水中垫 dirt 阶梯脱困
    water_place_tries = 0                  # 垫块模式尝试步数（> WATER_PLACE_MAX 放弃）
    water_last_py = 0.0                    # 上一步 py（卡墙判定：每步升高 <0.1 计卡墙）
    water_wall_ticks = 0                   # py 几乎不升的连续步数（≥ WATER_STUCK_TICKS 进入垫块）

    # 任务工具：确认手持对应工具（reset 已发放全套钻石工具到 hotbar 0-4）
    equip_item = cfg.get("equip")
    current_tool = equip_item                    # 当前应持工具：任务主工具或按方块切换的挖掘工具
    if equip_item:
        ensure_equip(env, equip_item)

    for step in range(1, max_steps + 1):
        state = env.grpc.get_state(player=env.player)
        px, py, pz = (float(v) for v in state["player"]["pos"])
        if last_pos is None:
            last_pos = (px, py, pz)
        moved = math.hypot(px - last_pos[0], pz - last_pos[2])
        last_pos = (px, py, pz)
        dist = -1.0
        jump = False

        # 阶梯脱困完成判定：玩家已爬升 ≥CLIMB_RESET_DY → 恢复普通导航
        if climb_mode and py - climb_base_y >= CLIMB_RESET_DY:
            climb_mode = False
            log(f"  [climb] 已爬升 {py - climb_base_y:.1f} 格，恢复普通模式")

        # ---- 工具守卫：非挖掘子状态且手持与当前工具不一致时先切槽（不消耗任务步）。
        # 挖掘子状态（goto 挖穿 / 路径 dig/place / path_dig）由各分支自己切工具，守卫不覆盖。 ----
        busy_dig = (goto_dig_pos is not None
                    or (pd is not None and pd.get("action") in ("dig", "dig_down", "place"))
                    or path_dig_try > 0
                    or water_place_tries > 0)   # 水中垫块：保持 dirt 手持，工具守卫让路
        if equip_item and not busy_dig \
                and state["inventory"].get("held_item", "") != current_tool:
            if ensure_equip(env, current_tool):
                env.ws.send_action({"camera": [0.0, 0.0]})
            continue

        # ---- 0) 溺水自救：水中 → 朝最近岸边游（每 5 步重扫岸，其余步沿用 swim_shore） ----
        in_water = False
        if reposition == 0 and mode != "attack":
            if swim_shore is None and step % 5 == 0:
                # 脚在水里（含浅水/坑底 on_ground 站着的情况）→ 溺水自救。
                # 不只看 !on_ground：掉进积水坑底部时 on_ground=True 但脚格仍是水，
                # 若在 on_ground 时清 swim_shore 会永远游不出坑。脚不在水里才清。
                try:
                    palette, data, origin, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3 = blocks_3d(palette, data, data.shape[0])
                    feet_name = name_at(b3, origin,
                                         (int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))))
                    if feet_name in ("minecraft:water", "minecraft:lava"):
                        swim_shore = _nearest_shore(px, py, pz, b3, origin)
                    else:
                        swim_shore = None  # 已上岸，清游泳目标与垫块状态
                        water_place_tries = 0
                        water_wall_ticks = 0
                        water_last_py = 0.0
                except Exception:  # noqa: BLE001
                    pass
            if swim_shore is not None:
                in_water = True
                sx, sy, sz = swim_shore
                yaw, pitch = look_at(px, py, pz, sx, sy, sz, pitch_clamp=20.0)
                env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                # 水中 A* 路径无意义（水=不可通行），清空导航状态纯游泳。
                # 不疾跑、仅水位低于岸边时才跳 → 上岸后普通走、落地即 on_ground 停游（防冲过头）
                mode, target = "none", None
                waypoints, wp = [], None
                path_details, pd = [], None
                wp_stuck, goal_stall, stuck_count = 0, 0, 0
                if goto_active:  # 清客户端本地导航，防 NavExecutor 覆盖游泳动作
                    env.ws.send_goto_cancel()
                    goto_active = False
                    goto_wps, goto_digs = [], []
                action, water_place_tries, water_last_py, water_wall_ticks = _water_escape_action(
                    env, state, px, py, pz, swim_shore, half_extent,
                    water_place_tries, water_last_py, water_wall_ticks)

        # ---- 1) 挖后后退：清掉落物挡视线，不瞄准新目标 ----
        if in_water:
            pass  # action 已由溺水自救设置，跳过其余导航
        elif reposition > 0:
            reposition -= 1
            mode, target = "none", None
            waypoints, wp = [], None
            path_details, pd = [], None
            wp_stuck = 0
            if goto_active:  # 清客户端本地导航，防 NavExecutor 覆盖后退动作
                env.ws.send_goto_cancel()
                goto_active = False
                goto_wps, goto_digs = [], []
            action = {"back": True, "camera": [0.0, 0.0]}

        # ---- 2) 游走 ----
        elif wander_left > 0:
            wander_left -= 1
            env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
            jump = False
            if wander_jump > 0:
                wander_jump -= 1
                jump = True
            else:
                # 前方障碍物检测：眼睛高度前方 1 格实心、其上方是空气 → 跳跃翻越；
                # 脚下前方是水/岩浆 → 转头（防游走进水域/熔岩卡死）
                try:
                    palette, data, origin, size = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3 = blocks_3d(palette, data, size)
                    yaw_rad = math.radians(float(state["player"].get("yaw", 0.0)))
                    fx, fz = -math.sin(yaw_rad), math.cos(yaw_rad)
                    ex, ey, ez = px + fx, py + EYE_HEIGHT, pz + fz
                    ahead = (int(math.floor(ex)), int(math.floor(ey)), int(math.floor(ez)))
                    above = (ahead[0], ahead[1] + 1, ahead[2])
                    n_ahead = name_at(b3, origin, ahead)
                    n_above = name_at(b3, origin, above)
                    if n_ahead is not None and n_ahead != "minecraft:air" and (
                            n_above is None or n_above == "minecraft:air"):
                        jump = True
                    # 脚前方 1-2 格是水/岩浆 → 转向
                    for k in (1, 2):
                        fpos = (int(math.floor(px + fx * k)), int(math.floor(py)), int(math.floor(pz + fz * k)))
                        fname = name_at(b3, origin, fpos)
                        if fname in ("minecraft:water", "minecraft:lava"):
                            wander_yaw = wander_yaw + random.uniform(90.0, 160.0)
                            env.ws.send({"cmd": "reset_camera",
                                         "yaw": float(wander_yaw), "pitch": 0.0})
                            break
                except Exception:  # noqa: BLE001 —— 障碍检测失败不阻塞游走
                    pass
            sprint = target_type == "entity"  # 追逃跑动物时疾跑（原木/石头不疾跑，防准星漂移）
            action = {"forward": True, "jump": jump, "sprint": sprint, "camera": [0.0, 0.0]}

        # ---- 3) attack：block 挖矿 / entity 近战追打 ----
        elif mode == "attack" and target is not None:
            bx, by, bz = target
            dig_try += 1
            # 每步按目标方块类型切换正确工具（挖石头用镐、原木用斧、泥土用铲；
            # collect_wood 主斧挖石头极慢，collect_stone 主镐挖泥土极慢）。
            if target_type == "block":
                try:
                    palette_t, data_t, origin_t, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3_t = blocks_3d(palette_t, data_t, data_t.shape[0])
                    _ensure_tool_for_block(env, name_at(b3_t, origin_t, (bx, by, bz)),
                                           cfg["equip"])
                except Exception:  # noqa: BLE001
                    pass
            if target_type == "entity":
                # 不 settle：每步用最新 get_state 重定位；动物逃出 reach 立即切回 approach
                ents = find_entities(state, targets)
                if not ents:
                    log("  [attack] no pigs in view, reposition")
                    reposition = 2
                    mode, target = "none", None
                    waypoints, wp = [], None
                    path_details, pd = [], None
                    wp_stuck = 0
                    action = {"camera": [0.0, 0.0]}
                else:
                    # 追离上次目标最近的猪（被杀后自动换剩下的）
                    n = min(ents, key=lambda e: (e[0] - bx) ** 2 + (e[1] - by) ** 2 + (e[2] - bz) ** 2)
                    bx, by, bz = n
                    target = n
                    dist = dist3(px, py, pz, bx, by, bz)
                    if dist > reach:
                        mode = "approach"
                        wp_stuck = 0
                        action = {"camera": [0.0, 0.0]}
                    else:
                        # 每步重瞄身体中心（ey+0.5）：猪会逃，准星必须持续追踪才能命中
                        env.ws.send({"cmd": "look_at", "x": bx, "y": by + 0.5, "z": bz})
                        action = {"attack": True, "camera": [0.0, 0.0]}
            else:
                dist = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
                if dig_try % DIG_SCAN_EVERY == 0:
                    palette, data, origin, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    if _block_target_alive(target, palette, data, origin, targets):
                        action = {"attack": True, "camera": [0.0, 0.0]}
                    else:
                        log(f"  [dig] target {target} gone after {dig_try} tries")
                        # 同树/附近可能还有任务块：若已在挖掘范围内，直接用刚扫的体素
                        # 续挖（省掉 reposition 的无用后退——reposition 只为清掉落物挡
                        # 视线，目标在手边时不需要）。范围内无目标才走原后退逻辑。
                        new_mode, new_target = select_target(
                            find_blocks(palette, data, origin, targets),
                            px, py, pz, exclude=failed_targets, task=task,
                            blocks3=blocks_3d(palette, data, data.shape[0]),
                            origin=origin, step=step,
                            max_dy_up=(CLIMB_MAX_DY if climb_mode else 2.6))
                        if new_mode == "attack":
                            mode, target = "attack", new_target
                            settle_steps, dig_try = 0, 0
                            waypoints, wp = [], None
                            path_details, pd = [], None
                            wp_stuck = 0
                            aim_cache = None
                            bx, by, bz = target
                            env.ws.send({"cmd": "look_at",
                                         "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                            settle_steps = 1
                            action = {"camera": [0.0, 0.0]}
                            log(f"  [attack] target={target} "
                                f"dist={dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5):.2f}")
                        else:
                            reposition = 2
                            mode, target = "none", None
                            waypoints, wp = [], None
                            path_details, pd = [], None
                            wp_stuck = 0
                            aim_cache = None
                            action = {"camera": [0.0, 0.0]}
                elif dig_try > MAX_DIG_TRY:
                    # 挖不动：黑名单 + 游走换目标（防无限重选同一目标）
                    log(f"  [dig] give up target {target} after {MAX_DIG_TRY} tries, blacklist+wander")
                    failed_targets[target] = step + 60
                    wander_left = WANDER_STEPS + 6
                    wander_jump = 6
                    wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                    mode, target = "none", None
                    waypoints, wp = [], None
                    path_details, pd = [], None
                    wp_stuck = 0
                    aim_cache = None
                    action = {"camera": [0.0, 0.0]}
                elif settle_steps < SETTLE_STEPS:
                    if settle_steps == 0:
                        # 客户端用自身眼位精确瞄准目标块中心（消除服务端 pos 滞后偏差）
                        env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                    settle_steps += 1
                    action = {"camera": [0.0, 0.0]}
                elif dig_try % 20 == 0:
                    # 周期重瞄同一目标中心（位置漂移导致准星偏时校准；换目标才会重置进度）
                    env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                    action = {"attack": True, "camera": [0.0, 0.0]}
                else:
                    action = {"attack": True, "camera": [0.0, 0.0]}

        # ---- 4) approach：A* 3D 导航（逐航点 + 台阶跳跃） ----
        elif mode == "approach" and target is not None:
            bx, by, bz = target
            if target_type == "entity":
                dist = dist3(px, py, pz, bx, by, bz)
            else:
                dist = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
            scan_timer += 1

            # 周期重扫：目标可能已可达（转 attack）或已消失（换目标）
            if scan_timer % APPROACH_RESCAN == 0:
                if target_type == "entity":
                    ents = find_entities(state, targets)
                    targets_found = len(ents)
                    new_mode, new_target = select_target(
                        ents, px, py, pz, exclude=failed_targets, task=task)
                    if new_mode == "attack":
                        # 已可达 → 切 attack
                        mode, target = "attack", new_target
                        dig_try, settle_steps = 0, 0
                        aim_cache = None
                        wp_stuck = 0
                        bx, by, bz = target
                        env.ws.send({"cmd": "look_at", "x": bx, "y": by + 0.5, "z": bz})
                        action = {"camera": [0.0, 0.0]}
                        log(f"  [attack] target={target} dist={dist:.2f}")
                    elif not ents:
                        # 目标消失（被杀/超出视距）→ 后退清状态再重扫
                        reposition = 2
                        mode, target = "none", None
                        waypoints, wp = [], None
                        path_details, pd = [], None
                        wp_stuck = 0
                        action = {"camera": [0.0, 0.0]}
                    else:
                        # 目标还在但未可达 → 刷新为当前坐标（动物会动），清路径待重算
                        n = min(ents, key=lambda e: (e[0] - bx) ** 2 + (e[1] - by) ** 2 + (e[2] - bz) ** 2)
                        bx, by, bz = n
                        target = n
                        path_details, pd = [], None
                else:
                    palette, data, origin, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    blocks3 = blocks_3d(palette, data, data.shape[0])
                    blocks = find_blocks(palette, data, origin, targets)
                    targets_found = len(blocks)
                    new_mode, new_target = select_target(
                        blocks, px, py, pz, exclude=failed_targets, task=task,
                        blocks3=blocks3, origin=origin, step=step,
                        max_dy_up=(CLIMB_MAX_DY if climb_mode else 2.6))
                    if new_mode == "attack":
                        # 已可达 → 切 attack（停掉客户端 goto）
                        env.ws.send_goto_cancel()
                        goto_active = False
                        goto_wps = []
                        goto_dig_pos = None
                        goto_dig_try = 0
                        mode, target = "attack", new_target
                        settle_steps, dig_try = 0, 0
                        aim_cache = None
                        wp_stuck = 0
                        bx, by, bz = target
                        env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                        settle_steps = 1
                        action = {"camera": [0.0, 0.0]}
                        log(f"  [attack] target={target} dist={dist:.2f}")
                    elif target not in blocks:
                        # 目标被破坏/消失 → 后退清视线再重选（停掉客户端 goto）
                        env.ws.send_goto_cancel()
                        goto_active = False
                        goto_wps = []
                        goto_dig_pos = None
                        goto_dig_try = 0
                        reposition = 2
                        mode, target = "none", None
                        waypoints, wp = [], None
                        path_details, pd = [], None
                        wp_stuck = 0
                        action = {"camera": [0.0, 0.0]}
                    # 否则保持当前 approach 目标

            # NavV2 动作级航点跟随：按 {pos, action, target} 分派执行。
            # action ∈ walk/jump/fall（移动）/ dig/dig_down（挖穿，原地不动）/ place（垫方块）。
            if mode == "approach":
                # 取当前动作：pd 为空时从 path_details pop；跳过已到达的 walk/jump/fall
                # 起点格/重复格（dig/place 是原地动作，不按到达跳过）。实体与 block 任务共用。
                while True:
                    if pd is None:
                        if not path_details:
                            break
                        pd = path_details.pop(0)
                    if pd["action"] in ("walk", "jump", "fall", "step_up") \
                            and dist3(px, py, pz, *pd["pos"]) < WP_ARRIVE_DIST:
                        pd = None  # 已到达 → 继续取下一个
                        continue
                    break
                if target_type == "entity":
                    if pd is None and not path_details:
                        # 路径消费完 → 重算。若重算结果仍是"平凡路径"（所有 detail 都已在脚下，
                        # 如玩家在树冠、目标高不可达导致 adjustGoal 兜底到原地），累计 goal_stall
                        # 后退重选，防"actions=2 每步重算"churn（对标旧 wp_stuck 行为）。
                        waypoints, details = env.grpc.compute_path(
                            player=env.player, goal=(bx, by, bz), cost_mode=path_cost_mode)
                        if on_path is not None:
                            on_path(list(waypoints), list(details), target)
                        if waypoints:
                            path_details = _compress_details(list(details))
                            pd = None
                            log(f"  [path] target={target} actions={len(path_details)}")
                            action = {"camera": [0.0, 0.0]}
                            # 平凡路径判定：跳过起点后，所有 walk/jump/fall 都已在脚下
                            trivial = True
                            for d in path_details[1:]:
                                if d["action"] not in ("walk", "jump", "fall", "step_up"):
                                    trivial = False  # 有 dig/place → 可推进
                                    break
                                if dist3(px, py, pz, *d["pos"]) >= WP_ARRIVE_DIST:
                                    trivial = False
                                    break
                            if trivial:
                                # 平凡路径 = A* 终点已在脚下（near() 切比雪夫≤1 在 g 邻格终止）。
                                # 若目标本身已在采集距离内 → 玩家可直接挖，**不黑名单**，
                                # 等重扫切 attack（此前误拉黑导致丢目标/游走丢树）。
                                target_d = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
                                if target_d <= REACH:
                                    # 已在采集距离内 → 立即切 attack（不再等重扫）
                                    mode, target = "attack", target
                                    settle_steps, dig_try = 0, 0
                                    waypoints, wp = [], None
                                    path_details, pd = [], None
                                    aim_cache = None
                                    bx, by, bz = target
                                    env.ws.send({"cmd": "look_at",
                                                 "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                                    settle_steps = 1
                                    action = {"camera": [0.0, 0.0]}
                                    log(f"  [attack] target={target} dist={target_d:.2f}")
                                elif stuck_dig_count < STUCK_DIG_MAX:
                                    palette, data, origin, _ = env.grpc.get_voxels(
                                        player=env.player, half_extent=half_extent)
                                    dig_pos = _block_ahead(
                                        float(state["player"].get("yaw", 0.0)), px, py, pz,
                                        blocks_3d(palette, data, data.shape[0]), origin)
                                    if dig_pos is not None:
                                        stuck_dig_count += 1
                                        log(f"  [stuck-dig] 挖面前方块 {dig_pos} 脱困（第 {stuck_dig_count} 次）")
                                        path_details = [{"action": "dig", "pos": (px, py, pz),
                                                         "target": dig_pos}]
                                        pd = None
                                        path_dig_try = 0
                                        action = {"camera": [0.0, 0.0]}
                                    else:
                                        log(f"  [approach] 路径平凡（目标 {target} 不可达，无方块可挖），黑名单+后退重选")
                                        failed_targets[target] = step + 60
                                        waypoints, wp = [], None
                                        path_details, pd = [], None
                                        stuck_dig_count = 0
                                        reposition = 3
                                        mode, target = "none", None
                                else:
                                    stuck_dig_count = 0
                                    log(f"  [approach] 路径平凡（目标 {target} 不可达），黑名单+后退重选")
                                    failed_targets[target] = step + 60
                                    waypoints, wp = [], None
                                    path_details, pd = [], None
                                    reposition = 3
                                    mode, target = "none", None
                        else:
                            # A* 无路（目标在树冠/悬崖/被树叶包围）→ 黑名单 + 游走换目标
                            log(f"  [path] no path to {target}, blacklist+wander")
                            failed_targets[target] = step + 60
                            wander_left = WANDER_STEPS
                            wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                            mode, target = "none", None
                            waypoints, wp = [], None
                            path_details, pd = [], None
                            wp_stuck = 0
                            action = {"forward": True, "camera": [0.0, 0.0]}

                    if mode == "approach" and pd is not None:
                        act = pd["action"]
                        if act in ("walk", "jump", "fall", "step_up"):
                            wx, wy, wz = pd["pos"]
                            # 瞄航点**格中心**（+0.5）而非格子角：旧实现 yaw 对最近航点偏
                            # ~20-35°，走几格就漂离路径线、撞上旁边障碍。look_at 由客户端用
                            # 自身眼位精确算向（pitch_clamp=20 平视前进，不低头看脚下航点）。
                            env.ws.send({"cmd": "look_at", "x": wx + 0.5, "y": wy + 0.5,
                                         "z": wz + 0.5, "pitch_clamp": 20.0})
                            yaw, _ = look_at(px, py, pz, wx + 0.5, wy + 0.5, wz + 0.5,
                                             pitch_clamp=20.0)
                            # step_up：目标脚格比当前高 1 格 → 前进+跳（原版自动上台阶，
                            # 加 jump 更稳）；fall 目标低 → 直接前进自然下落
                            jump = (act == "jump") or (act == "step_up") or (wy - py) > JUMP_Y_THRESH
                            sprint = target_type == "entity"  # 追逃跑动物时疾跑（原木/石头不疾跑，防准星漂移）
                            # 转向-前进分离 + 侧移纠偏：|yawErr| 大时原地转向（相机 maxTurnDeg=90
                            # 快速到位）；中角度差前进 + left/right strafe 朝目标（边走边转，
                            # 不再每拐点罚站，也减少"纯 forward 撞墙"）
                            cur_yaw = float(state["player"].get("yaw", 0.0))
                            yaw_err = (yaw - cur_yaw + 540.0) % 360.0 - 180.0
                            if abs(yaw_err) > TURN_IN_PLACE_DEG:
                                turn_ticks += 1
                                if turn_ticks <= TURN_MAX_TICKS:
                                    # 正常转向：放前进原地转，不计卡死
                                    action = {"camera": [0.0, 0.0]}
                                    turning = True
                                else:
                                    # 转太久了（转不过去/服务端 yaw 不更新）→ 视为卡死，
                                    # 继续前进让 wp_stuck/全局卡死触发，防无限原地打转
                                    turn_ticks = TURN_MAX_TICKS
                                    action = {"forward": True, "jump": jump, "sprint": sprint,
                                              "camera": [0.0, 0.0]}
                                    turning = False
                            else:
                                turn_ticks = 0
                                action = {"forward": True, "jump": jump, "sprint": sprint,
                                          "camera": [0.0, 0.0]}
                                # 侧移纠偏：yaw_err<0 → 目标在更低 yaw（玩家左侧）→ strafe left；
                                # yaw_err>0 → 右侧。幅度小（≤STRAFE_DEG）则纯前进。
                                if yaw_err < -STRAFE_DEG:
                                    action["left"] = True
                                elif yaw_err > STRAFE_DEG:
                                    action["right"] = True
                                turning = False
                            if dist3(px, py, pz, wx, wy, wz) < WP_ARRIVE_DIST:
                                pd = None  # 到达 → 下个循环取下一动作

                            # 移动卡死检测（dig/place 原地不动、转向中不触发）
                            if pd is not None:
                                if turning:
                                    wp_stuck = 0
                                else:
                                    wp_stuck = wp_stuck + 1 if moved < STUCK_MOVED else 0
                            else:
                                wp_stuck = 0
                            if wp_stuck >= APPROACH_STUCK_STEPS:
                                wp_stuck = 0
                                waypoints, wp = [], None
                                path_details, pd = [], None
                                action = {"camera": [0.0, 0.0]}
                                fails = target_fails.get(target, 0) + 1
                                target_fails[target] = fails
                                if fails >= 2:
                                    log(f"  [approach] target {target} 卡死 {fails} 次，黑名单+游走")
                                    failed_targets[target] = step + 60
                                    wander_left = WANDER_STEPS + 6
                                    wander_jump = 6
                                    wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                                    mode, target = "none", None
                                else:
                                    log(f"  [approach] stuck {APPROACH_STUCK_STEPS} 步→后退解卡")
                                    reposition = 3
                                    mode, target = "none", None
                        elif act in ("dig", "dig_down"):
                            # 站原地挖穿目标块（dig_down 目标在脚下，挖穿后下落）
                            tx, ty, tz = pd["target"]
                            if path_dig_try == 0 or path_dig_try % DIG_SCAN_EVERY == 0:
                                palette, data, origin, _ = env.grpc.get_voxels(
                                    player=env.player, half_extent=half_extent)
                                b3 = blocks_3d(palette, data, data.shape[0])
                                if not _pos_is_block(b3, origin, (tx, ty, tz)):
                                    pd = None  # 已挖穿 → 下一动作
                                    path_dig_try = 0
                                    current_tool = equip_item
                                    action = {"camera": [0.0, 0.0]}
                                else:
                                    # 按目标方块切正确工具（挖石头用镐、泥土用铲）
                                    _ensure_tool_for_block(env, name_at(b3, origin, (tx, ty, tz)),
                                                           equip_item)
                                    current_tool = _TOOL_FOR_BLOCK.get(
                                        name_at(b3, origin, (tx, ty, tz)), equip_item)
                                    env.ws.send({"cmd": "look_at",
                                                 "x": tx + 0.5, "y": ty + 0.5, "z": tz + 0.5})
                                    action = {"attack": True, "camera": [0.0, 0.0]}
                                    path_dig_try += 1
                            else:
                                env.ws.send({"cmd": "look_at",
                                             "x": tx + 0.5, "y": ty + 0.5, "z": tz + 0.5})
                                action = {"attack": True, "camera": [0.0, 0.0]}
                                path_dig_try += 1
                            if path_dig_try > MAX_DIG_TRY:
                                log(f"  [path-dig] 挖不穿 {(tx, ty, tz)}，重算")
                                path_dig_try = 0
                                current_tool = equip_item
                                waypoints, wp = [], None
                                path_details, pd = [], None
                                action = {"camera": [0.0, 0.0]}
                        elif act == "place":
                            # 垫方块：选 dirt 槽 → look_at 放置点底面 → use → 下一动作（jump 上块）。
                            # 瞄准目标格**底面**（ty，即下方支撑块的顶面）：MC 方块放置是对着已有
                            # 方块的面放，瞄格子中心可能被远处墙截走 → 放到错误的格。
                            tx, ty, tz = pd["target"]
                            if not place_equipped:
                                slot = _tool_slot(env, PLACE_ITEM)
                                if slot is None:
                                    log(f"  [path-place] WARN: 无 {PLACE_ITEM}，跳过")
                                    pd = None
                                else:
                                    env.ws.send_action({"hotbar": slot})
                                    place_equipped = True
                                    place_settle = 0
                                    action = {"camera": [0.0, 0.0]}
                            elif place_settle < PLACE_SETTLE_STEPS:
                                env.ws.send({"cmd": "look_at",
                                             "x": tx + 0.5, "y": ty, "z": tz + 0.5})
                                place_settle += 1
                                action = {"camera": [0.0, 0.0]}
                            else:
                                env.ws.send({"cmd": "look_at",
                                             "x": tx + 0.5, "y": ty, "z": tz + 0.5})
                                action = {"use": True, "camera": [0.0, 0.0]}
                                place_try += 1
                                if place_try >= PLACE_MAX_TRY or place_try % DIG_SCAN_EVERY == 0:
                                    palette, data, origin, _ = env.grpc.get_voxels(
                                        player=env.player, half_extent=half_extent)
                                    placed = _pos_is_block(blocks_3d(palette, data, data.shape[0]),
                                                           origin, (tx, ty, tz))
                                    if not placed and place_try >= PLACE_MAX_TRY:
                                        log(f"  [path-place] 放置失败 {(tx, ty, tz)}，跳过")
                                    pd = None
                                    place_try, place_settle, place_equipped = 0, 0, False

                else:
                    # ---- 客户端 goto 驱动（block 任务）：服务端规划全局航点，
                    # 客户端逐 tick 转向/侧移/跳跃 + 碰撞箱 + 到达/卡死检测 ----
                    if path_details:
                        # Python 动作执行器路径进行中（place/dig_down 阶梯/下挖）：
                        # 由上方 `if mode == "approach" and pd is not None` 执行器消费，
                        # 此处只发空动作，不重算不抢航点（防每步重算丢弃剩余动作）。
                        action = {"camera": [0.0, 0.0]}
                    elif goto_dig_pos is not None:
                        # blocked_breakable 后 Python 站原地挖穿阻挡块
                        tx, ty, tz = goto_dig_pos
                        if goto_dig_try == 0 or goto_dig_try % DIG_SCAN_EVERY == 0:
                            palette, data, origin, _ = env.grpc.get_voxels(
                                player=env.player, half_extent=half_extent)
                            b3 = blocks_3d(palette, data, data.shape[0])
                            if not _pos_is_block(b3, origin, (tx, ty, tz)):
                                # 已挖穿 → 恢复 goto（剩余航点）
                                cleared = (tx, ty, tz)
                                goto_dig_pos = None
                                goto_dig_try = 0
                                current_tool = equip_item
                                if goto_active and goto_wps:
                                    resume = _resume_wps(goto_wps, cleared)
                                    if resume:
                                        env.ws.send_goto_path(resume, dig=goto_digs)
                                        action = {"camera": [0.0, 0.0]}
                                        log(f"  [goto] 挖穿 {cleared}，续走 {len(resume)} 航点")
                                    else:
                                        goto_active = False
                                        action = {"camera": [0.0, 0.0]}
                                else:
                                    action = {"camera": [0.0, 0.0]}
                            else:
                                # 按阻挡块类型切正确工具（挖石头用镐、泥土用铲）
                                _ensure_tool_for_block(env, name_at(b3, origin, (tx, ty, tz)),
                                                       equip_item)
                                current_tool = _TOOL_FOR_BLOCK.get(
                                    name_at(b3, origin, (tx, ty, tz)), equip_item)
                                env.ws.send({"cmd": "look_at",
                                             "x": tx + 0.5, "y": ty + 0.5, "z": tz + 0.5})
                                action = {"attack": True, "camera": [0.0, 0.0]}
                                goto_dig_try += 1
                        else:
                            env.ws.send({"cmd": "look_at",
                                         "x": tx + 0.5, "y": ty + 0.5, "z": tz + 0.5})
                            action = {"attack": True, "camera": [0.0, 0.0]}
                            goto_dig_try += 1
                        if goto_dig_try > MAX_DIG_TRY:
                            log(f"  [goto-dig] 挖不穿 {(tx, ty, tz)}，重规划")
                            env.ws.send_goto_cancel()
                            goto_active = False
                            goto_wps = []
                            goto_dig_pos = None
                            goto_dig_try = 0
                            current_tool = equip_item
                            failed_targets[target] = step + 60
                            wander_left = WANDER_STEPS
                            wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                            mode, target = "none", None
                            waypoints, wp = [], None
                            path_details, pd = [], None
                            action = {"forward": True, "camera": [0.0, 0.0]}
                    elif not goto_active:
                        # goto 未启动（异常兜底）→ 重算并进入 goto
                        cost = PATH_COST_MODE_PLACE if climb_mode else path_cost_mode
                        waypoints, details = env.grpc.compute_path(
                            player=env.player, goal=(bx, by, bz), cost_mode=cost)
                        if on_path is not None:
                            on_path(list(waypoints), list(details), target)
                        goto_wps, goto_digs = _goto_plan(waypoints, details)
                        if not goto_wps:
                            # 无移动航点 → 游走（不黑名单）
                            wander_left = WANDER_STEPS
                            wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                            mode, target = "none", None
                            goto_active = False
                            action = {"forward": True, "camera": [0.0, 0.0]}
                        elif _trivial_goto(goto_wps, px, py, pz):
                            log(f"  [goto] 目标 {target} 平凡路径，黑名单+游走")
                            failed_targets[target] = step + 60
                            wander_left = WANDER_STEPS
                            wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                            mode, target = "none", None
                            goto_active = False
                            goto_wps = []
                            goto_digs = []
                            action = {"forward": True, "camera": [0.0, 0.0]}
                        elif any(d["action"] in ("place", "dig_down") for d in details):
                            # goto 无法执行 place/dig_down（垫方块阶梯/下挖）→ Python 动作执行器
                            path_details = _compress_details(list(details))
                            pd = path_details.pop(0)
                            goto_active = False
                            goto_wps, goto_digs = [], []
                            goto_dig_pos = None
                            goto_dig_try = 0
                            action = {"camera": [0.0, 0.0]}
                            log(f"  [goto] 路径含 place/dig_down → Python 动作执行器（{len(path_details) + 1} 动作）")
                        else:
                            env.ws.send_goto_path(goto_wps, dig=goto_digs)
                            goto_active = True
                            goto_dig_pos = None
                            goto_dig_try = 0
                            action = {"camera": [0.0, 0.0]}
                            log(f"  [goto] target={target} waypoints={len(goto_wps)} dig={len(goto_digs)}")
                    else:
                        # 正常 goto：移动由客户端驱动，Python 只发空动作
                        action = {"camera": [0.0, 0.0]}
        # ---- 5) 无目标：重扫/选择 ----
        else:
            if target_type == "entity":
                ents = find_entities(state, targets)
                targets_found = len(ents)
                new_mode, new_target = select_target(
                    ents, px, py, pz, exclude=failed_targets, task=task)
                if new_mode == "none":
                    wander_left = WANDER_STEPS
                    wander_yaw = float(state["player"].get("yaw", 0.0)) + random.uniform(-30.0, 30.0)
                    env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                    action = {"forward": True, "camera": [0.0, 0.0]}
                    log(f"  [explore] no pigs in view, wander {WANDER_STEPS} steps")
                else:
                    mode, target = new_mode, new_target
                    stuck_dig_count = 0
                    goal_stall = 0
                    wp_stuck = 0
                    bx, by, bz = target
                    dist = dist3(px, py, pz, bx, by, bz)
                    if mode == "attack":
                        dig_try = 0
                        env.ws.send({"cmd": "look_at", "x": bx, "y": by + 0.5, "z": bz})
                        action = {"camera": [0.0, 0.0]}
                        log(f"  [attack] target={target} dist={dist:.2f}")
                    else:  # approach：立刻算 A* 路径（NavV2 动作级，下个循环由子状态机消费）
                        waypoints, details = env.grpc.compute_path(
                            player=env.player, goal=(bx, by, bz), cost_mode=path_cost_mode)
                        if on_path is not None:
                            on_path(list(waypoints), list(details), target)
                        path_details = _compress_details(list(details)) if waypoints else []
                        pd = None
                        log(f"  [approach] target={target} dist={dist:.2f} actions={len(path_details)}")
                        if not waypoints:
                            wander_left = WANDER_STEPS
                            wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                            mode, target = "none", None
                            action = {"forward": True, "camera": [0.0, 0.0]}
                        else:
                            action = {"camera": [0.0, 0.0]}
            else:
                palette, data, origin, size = env.grpc.get_voxels(
                    player=env.player, half_extent=half_extent)
                blocks3 = blocks_3d(palette, data, size)
                blocks = find_blocks(palette, data, origin, targets)
                targets_found = len(blocks)
                new_mode, new_target = select_target(
                    blocks, px, py, pz, exclude=failed_targets, task=task,
                    blocks3=blocks3, origin=origin, step=step,
                    max_dy_up=(CLIMB_MAX_DY if climb_mode else 2.6))
                if new_mode == "none":
                    if task == "collect_stone":
                        # 找不到浅层石头 → 向下挖掘：瞄准脚下草/土/石头逐层下挖（挖到石头层即出矿）
                        fx, fy, fz = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
                        floor_name = name_at(blocks3, origin, (fx, fy - 1, fz))
                        if floor_name not in (None, "minecraft:air", "minecraft:bedrock"):
                            env.ws.send({"cmd": "look_at",
                                         "x": fx + 0.5, "y": fy - 0.5, "z": fz + 0.5})
                            action = {"attack": True, "camera": [0.0, 0.0]}
                        else:
                            wander_left = WANDER_STEPS
                            wander_yaw = float(state["player"].get("yaw", 0.0)) + random.uniform(-30.0, 30.0)
                            env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                            action = {"forward": True, "camera": [0.0, 0.0]}
                            log(f"  [explore] no blocks, wander {WANDER_STEPS} steps")
                    else:
                        if blocks:
                            # 扫到任务块但无可选目标（多在树冠/山坡，|dy| 超限）：
                            # 朝最近块走，走近后低层块就可达——替代随机游走（真实森林必需）
                            nearest = min(
                                blocks,
                                key=lambda b: (b[0] - px) ** 2 + (b[1] - py) ** 2 + (b[2] - pz) ** 2)
                            mode, target = "approach", nearest
                            stuck_dig_count = 0
                            goal_stall = 0
                            wp_stuck = 0
                            bx, by, bz = nearest
                            dist = dist3(px, py, pz, bx, by, bz)
                            waypoints, details = env.grpc.compute_path(
                                player=env.player, goal=(bx, by, bz),
                                cost_mode=(PATH_COST_MODE_PLACE if climb_mode else path_cost_mode))
                            if on_path is not None:
                                on_path(list(waypoints), list(details), target)
                            goto_wps, goto_digs = _goto_plan(waypoints, details)
                            pd = None
                            if not goto_wps:
                                log(f"  [explore] 朝最近 {target_label} {nearest} 无移动航点，游走")
                                wander_left = WANDER_STEPS
                                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                                mode, target = "none", None
                                action = {"forward": True, "camera": [0.0, 0.0]}
                            elif _trivial_goto(goto_wps, px, py, pz):
                                log(f"  [explore] 朝最近 {target_label} {nearest} 平凡路径，游走")
                                wander_left = WANDER_STEPS
                                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                                mode, target = "none", None
                                action = {"forward": True, "camera": [0.0, 0.0]}
                            elif any(d["action"] in ("place", "dig_down") for d in details):
                                # 垫方块/下挖 goto 无法执行 → Python 动作执行器
                                path_details = _compress_details(list(details))
                                pd = path_details.pop(0)
                                goto_active = False
                                goto_wps, goto_digs = [], []
                                action = {"camera": [0.0, 0.0]}
                                log(f"  [explore] 路径含 place/dig_down → Python 动作执行器")
                            else:
                                env.ws.send_goto_path(goto_wps, dig=goto_digs)
                                goto_active = True
                                goto_dig_pos = None
                                goto_dig_try = 0
                                log(f"  [explore] 朝最近 {target_label} {nearest} 走"
                                    f"（{len(goto_wps)} 航点 dig={len(goto_digs)}）")
                                action = {"camera": [0.0, 0.0]}
                        else:
                            wander_left = WANDER_STEPS
                            wander_yaw = float(state["player"].get("yaw", 0.0)) + random.uniform(-30.0, 30.0)
                            env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                            action = {"forward": True, "camera": [0.0, 0.0]}
                            log(f"  [explore] no {target_label} in voxels, wander {WANDER_STEPS} steps")
                else:
                    mode, target = new_mode, new_target
                    stuck_dig_count = 0
                    goal_stall = 0
                    wp_stuck = 0
                    bx, by, bz = target
                    dist = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
                    if mode == "attack":
                        settle_steps, dig_try = 0, 0
                        aim_cache = None
                        env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                        settle_steps = 1
                        action = {"camera": [0.0, 0.0]}
                        log(f"  [attack] target={target} dist={dist:.2f}")
                    else:  # approach：立刻算 A* 路径；block 任务交客户端 goto 执行
                        waypoints, details = env.grpc.compute_path(
                            player=env.player, goal=(bx, by, bz),
                            cost_mode=(PATH_COST_MODE_PLACE if climb_mode else path_cost_mode))
                        if on_path is not None:
                            on_path(list(waypoints), list(details), target)
                        if target_type == "block":
                            goto_wps, goto_digs = _goto_plan(waypoints, details)
                            if not goto_wps:
                                # 无移动航点（A* 无路/纯 dig）→ 游走（不黑名单，稍后重选）
                                wander_left = WANDER_STEPS
                                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                                mode, target = "none", None
                                action = {"forward": True, "camera": [0.0, 0.0]}
                            elif _trivial_goto(goto_wps, px, py, pz):
                                # 平凡路径（航点全在脚下）→ 黑名单换目标
                                log(f"  [goto] 目标 {target} 平凡路径，黑名单+游走")
                                failed_targets[target] = step + 60
                                wander_left = WANDER_STEPS
                                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                                mode, target = "none", None
                                action = {"forward": True, "camera": [0.0, 0.0]}
                            elif any(d["action"] in ("place", "dig_down") for d in details):
                                # 垫方块/下挖 goto 无法执行 → Python 动作执行器
                                path_details = _compress_details(list(details))
                                pd = path_details.pop(0)
                                goto_active = False
                                goto_wps, goto_digs = [], []
                                goto_dig_pos = None
                                goto_dig_try = 0
                                action = {"camera": [0.0, 0.0]}
                                log(f"  [goto] 路径含 place/dig_down → Python 动作执行器（{len(path_details) + 1} 动作）")
                            else:
                                env.ws.send_goto_path(goto_wps, dig=goto_digs)
                                goto_active = True
                                goto_dig_pos = None
                                goto_dig_try = 0
                                action = {"camera": [0.0, 0.0]}
                                log(f"  [goto] target={target} waypoints={len(goto_wps)}")
                        else:
                            path_details = _compress_details(list(details)) if waypoints else []
                            pd = None
                            log(f"  [approach] target={target} dist={dist:.2f} actions={len(path_details)}")
                            if not waypoints:
                                wander_left = WANDER_STEPS
                                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                                mode, target = "none", None
                                action = {"forward": True, "camera": [0.0, 0.0]}
                            else:
                                action = {"camera": [0.0, 0.0]}

        # ---- 统一 step ----
        res = step_fn(action, ticks)
        progress = float(res["progress"])
        max_progress = max(max_progress, progress)

        # 客户端 goto 状态处理（block 任务 approach 期间；挖穿子状态不在此处理）
        if mode == "approach" and target_type == "block" and goto_active \
                and goto_dig_pos is None:
            # 先消费帧流让文本路由进 _text_q（帧堆积会堵住 goto_status/path_debug）
            env.ws.recv_frame_latest(timeout=0.2)
            for st_msg in env.ws.drain_json(timeout=0.0):
                # M10：path_debug（客户端局部路径，白色 dots 可视化）
                if st_msg.get("type") == "path_debug":
                    pts = st_msg.get("points", [])
                    if pts and on_path is not None:
                        try:
                            env.grpc.show_path(player=env.player, waypoints=pts,
                                               path_type="client",
                                               lifetime_ticks=200)
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                if st_msg.get("type") != "goto_status":
                    continue  # action_ok/look_ok 等命令回显，直接丢弃（防缓冲积压）
                st_state = st_msg.get("state")
                goto_silent = 0  # 收到任意状态 → 看门狗复位
                if st_state == "arrived":
                    env.ws.send_goto_cancel()
                    goto_active = False
                    goto_wps = []
                    # px,py,pz 是 step 前取值，客户端已在 step 内走完 → 取新鲜位置判可达，
                    # 防"实际已够到却误判不可达"重扫 churn。
                    try:
                        fresh = env.grpc.get_state(player=env.player)
                        cxp, cyp, czp = (float(v) for v in fresh["player"]["pos"])
                    except Exception:  # noqa: BLE001
                        cxp, cyp, czp = px, py, pz
                    target_d = dist3(cxp, cyp, czp, bx + 0.5, by + 0.5, bz + 0.5)
                    if target_d <= REACH:
                        mode, target = "attack", target
                        settle_steps, dig_try = 0, 0
                        aim_cache = None
                        wp_stuck = 0
                        bx, by, bz = target
                        env.ws.send({"cmd": "look_at",
                                     "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                        settle_steps = 1
                        log(f"  [goto] arrived → [attack] target={target} dist={target_d:.2f}")
                    else:
                        mode, target = "none", None
                        waypoints, wp = [], None
                        path_details, pd = [], None
                        log(f"  [goto] arrived 但目标不可达 {target_d:.2f}，重扫")
                elif st_state == "blocked_breakable":
                    pos = tuple(int(v) for v in (st_msg.get("pos") or (0, 0, 0)))
                    env.ws.send_goto_cancel()
                    goto_dig_pos = pos
                    goto_dig_try = 0
                    log(f"  [goto] blocked_breakable {pos}，Python 挖穿")
                elif st_state in ("blocked_wall", "stuck"):
                    env.ws.send_goto_cancel()
                    goto_active = False
                    goto_wps = []
                    goto_digs = []
                    goto_dig_pos = None  # 防同批先 breakable 后 stuck 残留挖穿子状态
                    goto_dig_try = 0
                    # 先重规划同一目标（blocked_wall 多为路径漂移/客户端侧判断偏差，
                    # 重算即可；stuck 也先重试）——只有连败 2 次才黑名单+游走，
                    # 否则真实森林里目标明明可达却因一次 wall 被弃掉，永远找不到树。
                    fails = target_fails.get(target, 0) + 1
                    target_fails[target] = fails
                    if fails >= 2:
                        log(f"  [goto] {st_state} 目标 {target} 失败 {fails} 次，黑名单+游走")
                        failed_targets[target] = step + 60
                        wander_left = WANDER_STEPS
                        wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                    else:
                        log(f"  [goto] {st_state}，重规划目标 {target}（第 {fails} 次）")
                    # 目标在头顶且多次失败 → 启用 place 阶梯模式（低处垫方块爬高脱困）
                    if target is not None and target[1] - py > CLIMB_TARGET_DY:
                        climb_mode = True
                        climb_base_y = py
                        log(f"  [climb] 目标在头顶上方 {target[1] - py:.0f} 格，启用 place 阶梯模式")
                    mode, target = "none", None
                    waypoints, wp = [], None
                    path_details, pd = [], None

        # goto 看门狗：客户端不上报（掉线/拒收/player==null）→ 取消并重规划，防无限空动作
        # 挖穿子状态（goto_dig_pos）由 Python 驱动、客户端不报状态，不计入看门狗。
        if goto_active and goto_dig_pos is None:
            goto_silent += 1
            if goto_silent >= GOTO_WATCHDOG_STEPS:
                log(f"  [goto] 看门狗：{GOTO_WATCHDOG_STEPS} 步无 goto_status，取消重规划")
                env.ws.send_goto_cancel()
                goto_active = False
                goto_wps, goto_digs = [], []
                if target is not None and target[1] - py > CLIMB_TARGET_DY:
                    climb_mode = True
                    climb_base_y = py
                    log(f"  [climb] 看门狗后目标在头顶上方 {target[1] - py:.0f} 格，启用 place 阶梯模式")
                mode, target = "none", None
                waypoints, wp = [], None
                path_details, pd = [], None

        # 挖掘子状态结束 → 恢复任务主工具（下一轮工具守卫据此切回）
        if goto_dig_pos is None \
                and (pd is None or pd.get("action") not in ("dig", "dig_down", "place")) \
                and path_dig_try == 0:
            current_tool = equip_item

        # 卡死检测：非 attack 且基本没动 → 游走/重算。
        # 路径 dig/dig_down/place 子状态（站原地挖穿/垫块）不动是正常的，不累计；
        # goto 挖穿子状态（goto_dig_pos）同样不动是正常的。
        # goto 活跃期间也不累计——客户端 NavExecutor 自带卡死检测（30 tick 无进展
        # 上报 stuck），Python 不再用服务端 pos 慢判（20 步 ≈2s 太慢且误伤挖穿期）。
        path_stationary = (pd is not None and pd.get("action") in ("dig", "dig_down", "place")) \
            or (goto_dig_pos is not None)
        goto_driving = mode == "approach" and goto_active

        # ---- XZ 不动 → 及时本地重规划（不慢等 STUCK_STEPS 游走）。
        # goto 活跃/挖穿子状态/转向中不累计：客户端 NavExecutor 自带卡死检测。
        if moved < 0.02 and mode != "attack" and not goto_driving \
                and not path_stationary and reposition == 0:
            replan_stall += 1
        else:
            replan_stall = 0
        if replan_stall >= REPLAN_STALL_STEPS and target is not None and replan_count < REPLAN_MAX:
            replan_count += 1
            replan_stall = 0
            log(f"  [replan] XZ 不动 {REPLAN_STALL_STEPS} 步，本地重规划目标 {target}（第 {replan_count} 次）")
            # 本地重规划：保留目标，清空导航状态，下一循环从当前 XZ 重新 ComputePath
            env.ws.send_goto_cancel()
            goto_active = False
            goto_wps, goto_digs = [], []
            goto_dig_pos = None
            goto_dig_try = 0
            waypoints, wp = [], None
            path_details, pd = [], None
            mode = "approach"
            action = {"camera": [0.0, 0.0]}

        if moved < 0.02 and mode != "attack" and not goto_driving \
                and not path_stationary and reposition == 0:
            stuck_count += 1
        else:
            stuck_count = 0
            if moved >= STUCK_MOVED:
                stuck_dig_count = 0  # 玩家有位移 → 脱困成功/目标已变，清零卡死挖掘历史
                replan_count = 0     # 本地重规划成功推进（玩家恢复移动）→ 重置重规划次数

        # 过期黑名单清理（每 30 步一次）
        if step % 30 == 0 and failed_targets:
            failed_targets = {k: v for k, v in failed_targets.items() if v > step}
        if stuck_count >= STUCK_STEPS:
            log(f"  [stuck] {STUCK_STEPS} 步未移动（mode={mode}），游走")
            wander_left = WANDER_STEPS
            wander_jump = 6
            wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
            if target is not None and target[1] - py > CLIMB_TARGET_DY:
                climb_mode = True
                climb_base_y = py
                log(f"  [climb] 卡死且目标在头顶上方 {target[1] - py:.0f} 格，启用 place 阶梯模式")
            mode, target = "none", None
            waypoints, wp = [], None
            path_details, pd = [], None
            wp_stuck = 0
            aim_cache = None
            stuck_count = 0

        if step % 20 == 0 or res.get("terminated"):
            dist_str = f"{dist:.2f}" if dist >= 0 else "-"
            log(f"step={step} progress={progress:.2f} max={max_progress:.2f} "
                f"dist={dist_str} mode={mode} {target_label}={targets_found} jump={jump}")
        if res.get("terminated"):
            # 任务成功后立即替换掉最后一次 attack 电平，避免客户端继续挥空剑/挖空气。
            env.ws.send_action({"camera": [0.0, 0.0]})
            return True, step, progress
        if res.get("truncated"):
            log(f"M7_TASK_TIMEOUT task={task}: truncated（steps 超时）", file=sys.stderr)
            return False, step, max_progress

    log(f"M7_TASK_FAIL task={task}: {max_steps} 步未完成（progress={max_progress:.2f}）",
        file=sys.stderr)
    return False, max_steps, max_progress


def main() -> int:
    args = parse_args()
    env = MinecraftEnv(player=args.player, task=args.task, ticks_per_step=args.ticks)
    try:
        obs = None
        for attempt in range(30):
            try:
                obs, _ = env.reset()
                break
            except Exception as e:  # noqa: BLE001
                log(f"[reset] attempt {attempt + 1} failed: {type(e).__name__}: {e}",
                    file=sys.stderr)
                time.sleep(2)
        if obs is None:
            log("M7_FAIL: env.reset 30 次未成功（客户端未就绪？）", file=sys.stderr)
            return 1

        # kill_animal：reset 后生成猪（只生成一次；策略循环自身 get_state 扫描）
        if args.task == "kill_animal":
            env.grpc.spawn_entity(player=args.player, entity_type="minecraft:pig", count=2)
            time.sleep(0.5)

        pov = obs["pov"]
        assert pov.shape == (RES, RES, 3), f"pov shape 应为 (224,224,3)，实为 {pov.shape}"
        player_pos = obs["player"]["pos"]
        log(f"[reset] OK ep={obs['agent']['episode_id']} pov={pov.shape} "
            f"pos=({player_pos[0]:.1f},{player_pos[1]:.1f},{player_pos[2]:.1f})")

        def step_fn(action, ticks):
            _obs, reward, terminated, truncated, info = env.step(action, ticks=ticks)
            return {"progress": float(info["progress"]), "terminated": terminated,
                    "truncated": truncated, "reward": reward}

        ok, steps, max_progress = collect_wood_policy(
            env, step_fn, max_steps=args.max_steps, ticks=args.ticks,
            half_extent=args.half_extent, task=args.task)
        if ok:
            if args.task == "collect_wood":
                log(f"M7_COLLECT_WOOD_OK steps={steps} progress={max_progress:.2f}")
            else:
                log(f"M7_TASK_OK task={args.task} steps={steps} progress={max_progress:.2f}")
            return 0
        return 1
    finally:
        env.close()


# =============================================================================
# Oracle 轨迹生成策略（Oracle V1，2026-08-08）：去服务端全局 A*。
#
# 目标：生成"合理但非最优"的多样轨迹供 VLA 训练，逐帧语义标签
# （intent/subgoal/strategy/reason/mode + params）。
#
# 与 collect_wood_policy 的差异：
# - 不调用 compute_path（服务端全局 A* 已退役为直线占位）。approach 直接用
#   客户端 goto_path 双航点（玩家脚格 → 目标块格），中间 30 米交给客户端
#   NavExecutor/LocalPathfinder 局部绕障/挖穿/跳台；
# - blocked_wall/stuck 事件 → Python 本地绕行（侧移 3 格二次 goto）→ 失败才
#   游走换目标（不再立即黑名单）；
# - 目标选择加噪声（最近 K 候选加权随机）+ --budget-per-target（同目标挖块
#   上限）制造多样性；
# - 每分支发动作前调用 tag() 构造语义标签（recorder 为 None 时短路不记录）。
# =============================================================================

# Oracle 常量（与 collect_wood_policy 复用同模块常量，另加以下）：
DETOUR_RETRIES = 1            # blocked_wall/stuck 后本地绕行次数（0 = 立即游走）
LOCAL_DETOUR_RADIUS = 3       # 侧移绕行探测半径（格）
ORACLE_RESCAN = 5             # approach 期间周期重扫步数（同 APPROACH_RESCAN）
ORACLE_BUDGET_DEFAULT = 3     # 同目标挖块上限（--budget-per-target 默认）
ORACLE_TARGET_NOISE = 0.3     # 目标选择噪声权重（0 = 确定性最近簇最矮）
ORACLE_GOTO_WATCHDOG = 60     # goto 看门狗（同 GOTO_WATCHDOG_STEPS）
ORACLE_SIDE_DIST = 6.0        # 本地绕行侧移目标距目标块最小距离（防绕到目标脚下）
ORACLE_DIG_ABANDON = 30       # 挖穿子状态无进展放弃步数（≈1.5s，防空挥斧头）

# 行为三档预设（P1 多样性主来源，DESIGN.md §11.5）：efficient 与现状完全一致
# 作基线；cautious/aggressive 只放大既有"非最优行为"的出现频率（放弃/重试/卡死），
# 不改策略逻辑本身。oracle_cfg["behavior"] 选择档位，显式参数覆盖档位值。
BEHAVIOR_PRESETS = {
    "efficient": {   # 基线：与 collect_wood_policy 现状常量一致
        "max_dig_try": 90,
        "stuck_steps": 20,
        "replan_max": 3,
        "goto_watchdog": 60,
        "approach_stuck_steps": 6,
        "turn_in_place_deg": 45.0,
        "water_place_max": 24,
        "target_fails_threshold": 2,
        "wander_noise": 30.0,
        "max_dy_up": 2.6,
        "detour_retries": 1,
    },
    "cautious": {    # 更快放弃/更保守：更多失败与换目标（反事实点密集）
        "max_dig_try": 60,
        "stuck_steps": 12,
        "replan_max": 1,
        "goto_watchdog": 40,
        "approach_stuck_steps": 4,
        "turn_in_place_deg": 45.0,
        "water_place_max": 12,
        "target_fails_threshold": 1,
        "wander_noise": 0.0,       # 确定性 +90 转向
        "max_dy_up": 2.6,
        "detour_retries": 0,
    },
    "aggressive": {   # 更耐心/更敢冲：更多硬磨、卡死、激进失误
        "max_dig_try": 140,
        "stuck_steps": 30,
        "replan_max": 5,
        "goto_watchdog": 90,
        "approach_stuck_steps": 10,
        "turn_in_place_deg": 70.0,
        "water_place_max": 40,
        "target_fails_threshold": 3,
        "wander_noise": 90.0,
        "max_dy_up": 4.0,
        "detour_retries": 2,
    },
}


def oracle_wood_policy(
    env: MinecraftEnv,
    step_fn: Callable[[Dict, int], Dict],
    max_steps: int = 600,
    ticks: int = 2,
    half_extent: int = 16,
    task: str = "collect_wood",
    recorder=None,
    oracle_cfg: Optional[Dict] = None,
    spawn_pos: Optional[Tuple[float, float, float]] = None,
) -> Tuple[bool, int, float]:
    """Oracle 轨迹生成策略（去 A* 版，DESIGN.md §11.5）。

    - `recorder`：可选 StepRecorder（每步 on_step 落盘）；None 时纯跑不记录
      （兼容无记录调试）。
    - `oracle_cfg`：行为参数 dict，键见下方默认值（P1 行为参数化注入点）。
    返回 (success, steps, max_progress)。假设 env.reset 已由调用方完成。
    """
    cfg = TASK_CONFIG[task]
    target_type = cfg["target_type"]
    targets = cfg["targets"]
    reach = cfg["reach"]
    target_label = "pigs" if target_type == "entity" else "blocks"

    o = oracle_cfg or {}
    # 行为档位（efficient/cautious/aggressive）+ 显式覆盖
    behavior = o.get("behavior", "efficient")
    preset = dict(BEHAVIOR_PRESETS.get(behavior, BEHAVIOR_PRESETS["efficient"]))
    for k in preset:
        if k in o:
            preset[k] = o[k]
    detour_retries = int(preset.get("detour_retries", DETOUR_RETRIES))
    budget_per_target = int(o.get("budget_per_target", ORACLE_BUDGET_DEFAULT))
    target_noise = float(o.get("target_noise", ORACLE_TARGET_NOISE))
    noise_rng = random.Random(int(o.get("noise_seed", 0))) if o.get("noise_seed") is not None else random
    max_dig_try = int(preset.get("max_dig_try", MAX_DIG_TRY))
    stuck_steps_b = int(preset.get("stuck_steps", STUCK_STEPS))
    goto_watchdog = int(preset.get("goto_watchdog", ORACLE_GOTO_WATCHDOG))
    target_fails_thresh = int(preset.get("target_fails_threshold", 2))
    max_dy_up_b = float(preset.get("max_dy_up", 2.6))
    wander_noise = float(preset.get("wander_noise", 30.0))
    # 每步发动作前调用（构造语义标签；recorder 为 None 时短路）
    def tag(intent=None, subgoal=None, target=None, strategy=None, reason=None,
            params=None, mode=None):
        from vla_env.dataset import schema
        return schema.label(intent=intent or "noop", subgoal=subgoal, strategy=strategy,
                            reason=reason, params=params, mode=mode, target=target)

    # 策略状态
    mode = "none"
    target: Optional[Tuple[float, float, float]] = None
    budget_left = 0                    # 同目标剩余可挖块数（--budget-per-target）
    failed_targets: Dict[Tuple[int, int, int], int] = {}
    target_fails: Dict[Tuple[int, int, int], int] = {}   # 同目标失败次数（绕行/游走阈值）
    reposition = 0
    wander_left = 0
    wander_yaw = 0.0
    wander_jump = 0
    stuck_count = 0
    replan_stall = 0
    replan_count = 0
    settle_steps = 0
    dig_try = 0
    scan_timer = 0
    swim_shore: Optional[Tuple[int, int, int]] = None
    water_place_tries = 0
    water_last_py = 0.0
    water_wall_ticks = 0
    goto_active = False
    goto_dig_pos: Optional[Tuple[int, int, int]] = None
    goto_dig_try = 0
    goto_silent = 0
    last_dist_to_target: Optional[float] = None   # goto 卡死检测：到目标距离基准
    goto_stall = 0                                # goto 卡死累计步数（跨步持久）
    # 垫方块脱困（M11）：客户端 PillarExecutor 技能——挖头顶 fy+2 → 朝正下 → 跳 →
    # 顶点放块 → 落地站上，每轮净升 1 格。Python 只下发 pillar_up 并消费 pillar_status。
    place_climb = False
    place_climb_try = 0
    place_climb_start_y = 0.0
    place_climb_sent = False                    # 本轮 pillar_up 是否已下发（只发一次）
    place_climb_placed = 0                      # 客户端已垫块数（pillar_status.placed）
    place_climb_reason = ""                     # 终态原因（done/head_blocked/in_fluid/...）
    place_climb_target_y: Optional[int] = None  # 目标脚格 Y（None = 只受 max_blocks 约束）
    # 阶梯爬升 skill（被困低处时：先规划脱困路径，再按路径挖块+跳跃逐级爬升）
    stair_climb = False          # 阶梯爬升进行中
    stair_path: Optional[List[Tuple[int, int, int]]] = None  # 规划的脱困路径（块序列）
    stair_target: Optional[Tuple[int, int, int]] = None  # 当前要挖的块
    stair_cleared_pos: Optional[Tuple[int, int, int]] = None  # 已挖穿的缺口坐标（跳上用）
    stair_target_prev: Optional[Tuple[int, int, int]] = None  # 上一个挖穿的块（日志用）
    stair_try = 0                # 挖方块尝试步数
    stair_jump_pending = False   # 挖穿后待跳上
    stair_start_y = 0.0          # 爬升起始 y（判定是否已爬出）
    stair_max_try = 60           # 单次爬升总尝试上限
    detour_left = 0                    # 本地绕行剩余次数
    detour_wp: Optional[Tuple[int, int, int]] = None
    last_pos: Optional[Tuple[float, float, float]] = None
    max_progress = 0.0
    targets_found = 0
    current_tool = cfg.get("equip")

    if cfg.get("equip"):
        ensure_equip(env, cfg["equip"])

    for step in range(1, max_steps + 1):
        state = env.grpc.get_state(player=env.player)
        px, py, pz = (float(v) for v in state["player"]["pos"])
        if last_pos is None:
            last_pos = (px, py, pz)
        moved = math.hypot(px - last_pos[0], pz - last_pos[2])
        last_pos = (px, py, pz)
        jump = False
        dist = -1.0
        st_tag: Dict = {}
        action: Dict = {"camera": [0.0, 0.0]}

        # ---- 0) 溺水判定（独立 if，不进 if/elif 链；计算 swim_shore） ----
        in_water = False
        if reposition == 0 and mode != "attack":
            if swim_shore is None and step % 5 == 0:
                try:
                    palette, data, origin, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3 = blocks_3d(palette, data, data.shape[0])
                    feet_name = name_at(b3, origin,
                                        (int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))))
                    if feet_name in ("minecraft:water", "minecraft:lava"):
                        swim_shore = _nearest_shore(px, py, pz, b3, origin)
                    else:
                        swim_shore = None
                        water_place_tries = 0
                        water_wall_ticks = 0
                        water_last_py = 0.0
                except Exception:  # noqa: BLE001
                    pass

        # ---- 0b) 溺水自救（if/elif 链第一个分支） ----
        if in_water or swim_shore is not None:
            in_water = True
            sx, sy, sz = swim_shore
            yaw, pitch = look_at(px, py, pz, sx, sy, sz, pitch_clamp=20.0)
            env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
            mode, target = "none", None
            if goto_active:
                env.ws.send_goto_cancel()
                goto_active = False
            goto_dig_pos = None
            action, water_place_tries, water_last_py, water_wall_ticks = _water_escape_action(
                env, state, px, py, pz, swim_shore, half_extent,
                water_place_tries, water_last_py, water_wall_ticks)
            st_tag = tag(intent="water_escape", subgoal="swim_to_shore",
                         target={"type": "shore", "pos": [sx, sy, sz]},
                         strategy="swim", reason="water_stuck",
                         params={"water_place_tries": water_place_tries},
                         mode=mode)

        # ---- 1) 挖后后退 ----
        elif reposition > 0:
            reposition -= 1
            mode, target = "none", None
            if goto_active:
                env.ws.send_goto_cancel()
                goto_active = False
            action = {"back": True, "camera": [0.0, 0.0]}
            st_tag = tag(intent="reposition", subgoal="settle_aim",
                         strategy="step_back", reason="target_gone", mode=mode)

        # ---- 2) 游走 ----
        elif wander_left > 0:
            wander_left -= 1
            env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
            jump = wander_jump > 0
            if wander_jump > 0:
                wander_jump -= 1
            else:
                # 前方障碍/水域检测（复用原逻辑）
                try:
                    palette, data, origin, size = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3 = blocks_3d(palette, data, size)
                    yaw_rad = math.radians(float(state["player"].get("yaw", 0.0)))
                    fx, fz = -math.sin(yaw_rad), math.cos(yaw_rad)
                    ahead = (int(math.floor(px + fx)), int(math.floor(py + EYE_HEIGHT)),
                             int(math.floor(pz + fz)))
                    above = (ahead[0], ahead[1] + 1, ahead[2])
                    n_ahead = name_at(b3, origin, ahead)
                    n_above = name_at(b3, origin, above)
                    if n_ahead is not None and n_ahead != "minecraft:air" and (
                            n_above is None or n_above == "minecraft:air"):
                        jump = True
                    for k in (1, 2):
                        fpos = (int(math.floor(px + fx * k)), int(math.floor(py)),
                                int(math.floor(pz + fz * k)))
                        fname = name_at(b3, origin, fpos)
                        if fname in ("minecraft:water", "minecraft:lava"):
                            wander_yaw = wander_yaw + random.uniform(90.0, 160.0)
                            env.ws.send({"cmd": "reset_camera",
                                         "yaw": float(wander_yaw), "pitch": 0.0})
                            break
                except Exception:  # noqa: BLE001
                    pass
            action = {"forward": True, "jump": jump, "sprint": False, "camera": [0.0, 0.0]}
            st_tag = tag(intent="explore_wander", subgoal="scan_targets",
                         strategy="wander", reason="no_target", mode=mode)



        # ---- 2.4) 垫方块脱困（M11）：客户端 PillarExecutor 技能，Python 只下发+收状态 ----
        # 动作序列（人类垫楼方式）：挖头顶 fy+2 → 视角朝正下 → 原地跳 → 到跳跃顶点放一块
        # → 落到刚放的块上 → 重复。每轮净升 1 格。
        #
        # 为什么整套逻辑在客户端而不在这里：一个 step = ticks 个服务端 tick + 往返延迟
        # ≈ 5-10 tick，而放置窗口只有跳跃的第 3~8 tick（Δy>1.0 时脚格才空出、碰撞箱才不
        # 与目标格相交，服务端 isSpaceEmpty 也要过这一关）。Python 侧对不齐这个窗口。
        # 老实现还踩了两个 bug：挖 fy+1（那是玩家头格，恒空气）而非 fy+2；pitch 用 -90
        # （那是正上方，MC 正下是 +90）。
        elif place_climb:
            place_climb_try += 1
            if not place_climb_sent:
                # 首拍：交出按键所有权（客户端 onPillarUp 会先 cancel 导航）
                if goto_active:
                    env.ws.send_goto_cancel()
                    goto_active = False
                goto_dig_pos = None
                mode, target = "none", None
                env.ws.send_pillar_up(target_y=place_climb_target_y,
                                      max_blocks=PILLAR_MAX_BLOCKS, item=PLACE_ITEM)
                place_climb_sent = True
                log(f"  [pillar] 下发 pillar_up target_y={place_climb_target_y} "
                    f"max_blocks={PILLAR_MAX_BLOCKS} item={PLACE_ITEM}")

            # 消费 pillar_status（progress 每放一块一条，done/failed/cancelled 是终态）。
            # 帧流消费：place_climb 空转分支不发 step 帧、也不 recv——帧流堆积会把
            # WS 缓冲塞满，pillar_up 命令发不出去（客户端收不到 → pillar 永不开始），
            # pillar_status 也进不来（_text_q 空）→ 空转死循环。先 recv_frame_latest
            # 消费 socket 让文本消息路由进 _text_q（与脱困测试同款修复）。
            env.ws.recv_frame_latest(timeout=0.2)
            for st_msg in env.ws.drain_json(timeout=0.0):
                if st_msg.get("type") != "pillar_status":
                    continue
                st_state = st_msg.get("state")
                place_climb_placed = int(st_msg.get("placed", 0))
                if st_state == "progress":
                    log(f"  [pillar] +1 块（累计 {place_climb_placed}，feet_y="
                        f"{st_msg.get('feet_y')}）")
                    continue
                # 终态（done 也重置：垫到坑沿/目标高度后玩家已升高，下循环自然选更高目标）
                place_climb = False
                place_climb_sent = False
                place_climb_reason = str(st_msg.get("reason") or st_state)
                log(f"  [pillar] {st_state} placed={place_climb_placed} "
                    f"reason={place_climb_reason} detail={st_msg.get('detail')}")
                if st_state != "done" and place_climb_placed == 0:
                    # 一块都没垫上（头顶基岩/无 dirt/落水/反复放置失败）→ 交给挖阶梯兜底
                    stair_climb = True
                    stair_try = 0
                    stair_start_y = py
                    stair_path = None
                    stair_target = None
                    stair_jump_pending = False
                    log(f"  [pillar] 垫块不可行（{place_climb_reason}）→ 转挖阶梯脱困")
                break

            # 看门狗：客户端有 TOTAL_TIMEOUT 兜底，这里只防「事件全丢」的极端情况。
            # 同样先消费帧流（recv_frame_latest），否则事件一直被 TCP 缓冲滞留
            # （与上方 pillar_status 消费同因）。
            if place_climb and place_climb_try > PILLAR_WATCHDOG_STEPS:
                env.ws.recv_frame_latest(timeout=0.2)
                env.ws.send_pillar_cancel()
                place_climb = False
                place_climb_sent = False
                place_climb_reason = "watchdog"
                log(f"  [pillar] {place_climb_try} 步无终态事件 → cancel")

            # 客户端拥有全部按键，Python 只发空动作（不能带任何水平输入，否则飘出方块列）
            action = {"camera": [0.0, 0.0]}
            st_tag = tag(intent="place_block", subgoal="pillar_up",
                         strategy="pillar_up", reason="climb_high_target",
                         params={"placed": place_climb_placed,
                                 "target_y": place_climb_target_y,
                                 "step": place_climb_try},
                         mode=mode)

        # ---- 2.5) 阶梯爬升 skill：被困低处时——先规划脱困路径，再按路径挖块+跳跃 ----
        elif stair_climb:
            stair_try += 1
            if stair_try > stair_max_try or py - stair_start_y >= CLIMB_RESET_DY:
                # 已爬出（升了 3+ 格）→ 恢复导航；尝试耗尽没爬出（卡死上下跳）→ teleport
                climbed = py - stair_start_y
                stair_climb = False
                stair_path = None
                stair_target = None
                stair_jump_pending = False
                mode, target = "none", None
                if climbed < 1.0 and stair_try > stair_max_try:
                    # 挖了 60 步没爬出 1 格 → 卡死（缺口跳不上/死胡同）→ teleport 回出生点
                    if spawn_pos is not None:
                        try:
                            env.grpc.teleport(player=env.player, pos=spawn_pos)
                            time.sleep(0.5)
                        except Exception:  # noqa: BLE001
                            pass
                    log(f"  [stair] 爬升失败（{stair_try} 步只爬 {climbed:.1f} 格），teleport 回出生点")
                else:
                    log(f"  [stair] 爬升完成（+{climbed:.1f} 格），恢复导航")
                action = {"camera": [0.0, 0.0]}
                st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                             strategy="wander", reason="climb_high_target",
                             params={"climbed": round(climbed, 1)}, mode=mode)
            elif stair_jump_pending:
                # 上一块已挖穿 → 朝**缺口位置**（已挖穿的块）look_at + forward+jump：
                # 缺口在头顶斜前方时斜跳，不在原地朝上跳（原地跳进不了缺口）
                stair_jump_pending = False
                jx, jy, jz = stair_cleared_pos  # 已挖穿的缺口坐标
                stair_target = None
                stair_cleared_pos = None
                yaw_j, pitch_j = look_at(px, py, pz,
                                         jx + 0.5, jy + 0.5, jz + 0.5, pitch_clamp=30.0)
                env.ws.send({"cmd": "reset_camera", "yaw": float(yaw_j), "pitch": float(pitch_j)})
                action = {"forward": True, "jump": True, "camera": [0.0, 0.0]}
                st_tag = tag(intent="dig_obstacle", subgoal="place_ladder",
                             strategy="stuck_dig", reason="stuck",
                             params={"stair_try": stair_try, "gap": [jx, jy, jz]}, mode=mode)
                log(f"  [stair] 挖穿 {stair_target_prev}，朝缺口跳上（第 {stair_try} 步）")
            elif stair_target is None:
                # 先规划：找「通往空气的完整脱困路径」（连续可挖块序列）
                if stair_path is None or not stair_path:
                    palette, data, origin, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3 = blocks_3d(palette, data, data.shape[0])
                    stair_path = _plan_escape_path(env, px, py, pz, b3, origin)
                    if not stair_path:
                        # 无可行脱困路径（露天/洞穴/硬块）→ 放弃爬升，恢复正常导航
                        stair_climb = False
                        stair_path = None
                        mode, target = "none", None
                        action = {"camera": [0.0, 0.0]}
                        st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                                     strategy="wander", reason="stuck", mode=mode)
                        log(f"  [stair] 无可行脱困路径，放弃爬升")
                        continue
                    log(f"  [stair] 规划脱困路径 {len(stair_path)} 块：{stair_path}")
                # 取路径第一块（当前要挖的）
                stair_target = stair_path.pop(0)
                tx, ty, tz = stair_target
                _ensure_tool_for_block(env, name_at(b3, origin, (tx, ty, tz)), cfg["equip"])
                env.ws.send({"cmd": "look_at", "x": tx + 0.5, "y": ty + 0.5, "z": tz + 0.5})
                action = {"attack": True, "camera": [0.0, 0.0]}
                st_tag = tag(intent="dig_obstacle", subgoal="dig_path_block",
                             target={"type": "block", "pos": [tx, ty, tz]},
                             strategy="stuck_dig", reason="stuck",
                             params={"stair_try": stair_try}, mode=mode)
                log(f"  [stair] 挖脱困块 {stair_target}（路径剩 {len(stair_path)} 块，第 {stair_try} 步）")
            else:
                # 持续挖同一目标块，每 10 步确认是否已挖穿 + 切正确工具
                tx, ty, tz = stair_target
                if stair_try % 10 == 5:
                    palette, data, origin, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3 = blocks_3d(palette, data, data.shape[0])
                    if not _pos_is_block(b3, origin, (tx, ty, tz)):
                        # 挖穿 → 记录缺口坐标，下一拍朝缺口跳上（跳上后取路径下一块）
                        stair_jump_pending = True
                        stair_cleared_pos = (tx, ty, tz)
                        stair_target_prev = stair_target
                        action = {"camera": [0.0, 0.0]}
                        st_tag = tag(intent="dig_obstacle", subgoal="dig_path_block",
                                     target={"type": "block", "pos": [tx, ty, tz]},
                                     strategy="stuck_dig", reason="stuck",
                                     params={"cleared": True}, mode=mode)
                    else:
                        _ensure_tool_for_block(env, name_at(b3, origin, (tx, ty, tz)), cfg["equip"])
                        env.ws.send({"cmd": "look_at", "x": tx + 0.5, "y": ty + 0.5, "z": tz + 0.5})
                        action = {"attack": True, "camera": [0.0, 0.0]}
                        st_tag = tag(intent="dig_obstacle", subgoal="dig_path_block",
                                     target={"type": "block", "pos": [tx, ty, tz]},
                                     strategy="stuck_dig", reason="stuck",
                                     params={"stair_try": stair_try}, mode=mode)
                else:
                    # 持续挖同一块：每次确认工具匹配（防切回任务主工具后挖不动）
                    try:
                        palette_t, data_t, origin_t, _ = env.grpc.get_voxels(
                            player=env.player, half_extent=half_extent)
                        b3_t = blocks_3d(palette_t, data_t, data_t.shape[0])
                        _ensure_tool_for_block(env, name_at(b3_t, origin_t, (tx, ty, tz)),
                                               cfg["equip"])
                    except Exception:  # noqa: BLE001
                        pass
                    env.ws.send({"cmd": "look_at", "x": tx + 0.5, "y": ty + 0.5, "z": tz + 0.5})
                    action = {"attack": True, "camera": [0.0, 0.0]}
                    st_tag = tag(intent="dig_obstacle", subgoal="dig_path_block",
                                 target={"type": "block", "pos": [tx, ty, tz]},
                                 strategy="stuck_dig", reason="stuck",
                                 params={"stair_try": stair_try}, mode=mode)

        # ---- 3) attack：挖任务块 ----
        elif mode == "attack" and target is not None:
            bx, by, bz = target
            dig_try += 1
            # 每步按目标方块类型切换正确工具（Oracle 同款，见普通策略注释）
            if target_type == "block":
                try:
                    palette_t, data_t, origin_t, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3_t = blocks_3d(palette_t, data_t, data_t.shape[0])
                    _ensure_tool_for_block(env, name_at(b3_t, origin_t, (bx, by, bz)),
                                           cfg["equip"])
                except Exception:  # noqa: BLE001
                    pass
            dist = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
            if dig_try % DIG_SCAN_EVERY == 0:
                palette, data, origin, _ = env.grpc.get_voxels(
                    player=env.player, half_extent=half_extent)
                if _block_target_alive(target, palette, data, origin, targets):
                    action = {"attack": True, "camera": [0.0, 0.0]}
                    st_tag = tag(intent="dig_target", subgoal="reachable_attack",
                                 target={"type": "block", "pos": [bx, by, bz]},
                                 strategy="direct_attack", reason="target_in_reach",
                                 params={"dig_try": dig_try}, mode=mode)
                else:
                    log(f"  [dig] target {target} gone after {dig_try} tries")
                    new_mode, new_target = select_target(
                        find_blocks(palette, data, origin, targets),
                        px, py, pz, exclude=failed_targets, task=task,
                        blocks3=blocks_3d(palette, data, data.shape[0]),
                        origin=origin, step=step, max_dy_up=max_dy_up_b)
                    if new_mode == "attack":
                        mode, target = "attack", new_target
                        settle_steps, dig_try = 0, 0
                        budget_left = budget_per_target
                        bx, by, bz = target
                        env.ws.send({"cmd": "look_at",
                                     "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                        settle_steps = 1
                        action = {"camera": [0.0, 0.0]}
                        st_tag = tag(intent="noop", subgoal="settle_aim",
                                     target={"type": "block", "pos": [bx, by, bz]},
                                     strategy="direct_attack", reason="target_selected",
                                     mode=mode)
                    else:
                        reposition = 2
                        mode, target = "none", None
                        budget_left = 0
                        action = {"camera": [0.0, 0.0]}
                        st_tag = tag(intent="reposition", subgoal="settle_aim",
                                     strategy="step_back", reason="target_gone", mode=mode)
            elif dig_try > max_dig_try:
                log(f"  [dig] give up target {target} after {max_dig_try} tries, blacklist+wander")
                failed_targets[target] = step + 60
                budget_left = 0
                wander_left = WANDER_STEPS + 6
                wander_jump = 6
                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                mode, target = "none", None
                action = {"camera": [0.0, 0.0]}
                st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                             strategy="wander", reason="dig_give_up",
                             params={"dig_try": dig_try}, mode=mode)
            elif settle_steps < SETTLE_STEPS:
                if settle_steps == 0:
                    env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                settle_steps += 1
                action = {"camera": [0.0, 0.0]}
                st_tag = tag(intent="noop", subgoal="settle_aim",
                             target={"type": "block", "pos": [bx, by, bz]},
                             strategy="direct_attack", reason="target_in_reach",
                             params={"settle_steps": settle_steps}, mode=mode)
            elif dig_try % 20 == 0:
                env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                action = {"attack": True, "camera": [0.0, 0.0]}
                st_tag = tag(intent="dig_target", subgoal="reachable_attack",
                             target={"type": "block", "pos": [bx, by, bz]},
                             strategy="direct_attack", reason="target_in_reach",
                             params={"dig_try": dig_try}, mode=mode)
            else:
                action = {"attack": True, "camera": [0.0, 0.0]}
                st_tag = tag(intent="dig_target", subgoal="reachable_attack",
                             target={"type": "block", "pos": [bx, by, bz]},
                             strategy="direct_attack", reason="target_in_reach",
                             params={"dig_try": dig_try}, mode=mode)

        # ---- 4) approach：双航点 goto（客户端局部绕障） ----
        elif mode == "approach" and target is not None:
            bx, by, bz = target
            dist = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
            scan_timer += 1
            # 周期重扫（复用 select_target：可达 → attack；消失 → 换目标）
            if scan_timer % ORACLE_RESCAN == 0:
                palette, data, origin, _ = env.grpc.get_voxels(
                    player=env.player, half_extent=half_extent)
                blocks3 = blocks_3d(palette, data, data.shape[0])
                blocks = find_blocks(palette, data, origin, targets)
                targets_found = len(blocks)
                new_mode, new_target = select_target(
                    blocks, px, py, pz, exclude=failed_targets, task=task,
                    blocks3=blocks3, origin=origin, step=step, max_dy_up=max_dy_up_b)
                if new_mode == "attack":
                    env.ws.send_goto_cancel()
                    goto_active = False
                    goto_dig_pos = None
                    mode, target = "attack", new_target
                    settle_steps, dig_try = 0, 0
                    budget_left = budget_per_target
                    bx, by, bz = target
                    env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                    settle_steps = 1
                    action = {"camera": [0.0, 0.0]}
                    st_tag = tag(intent="noop", subgoal="settle_aim",
                                 target={"type": "block", "pos": [bx, by, bz]},
                                 strategy="direct_attack", reason="target_in_reach", mode=mode)
                    log(f"  [attack] target={target} dist={dist:.2f}")
                elif target not in blocks:
                    env.ws.send_goto_cancel()
                    goto_active = False
                    goto_dig_pos = None
                    reposition = 2
                    mode, target = "none", None
                    budget_left = 0
                    action = {"camera": [0.0, 0.0]}
                    st_tag = tag(intent="reposition", subgoal="settle_aim",
                                 strategy="step_back", reason="target_gone", mode=mode)
                else:
                    st_tag = tag(intent="goto_move", subgoal="goto_target_block",
                                 target={"type": "block", "pos": [bx, by, bz]},
                                 strategy="client_goto", reason="periodic_rescan",
                                 params={"dist": round(dist, 2)}, mode=mode)
                    action = {"camera": [0.0, 0.0]}

            # goto 状态处理（客户端事件）
            if goto_active:
                # 先消费帧流让文本路由进 _text_q（帧堆积会堵住 goto_status）
                env.ws.recv_frame_latest(timeout=0.2)
                for st_msg in env.ws.drain_json(timeout=0.0):
                    if st_msg.get("type") == "path_debug":
                        continue
                    if st_msg.get("type") != "goto_status":
                        continue
                    goto_silent = 0
                    st_state = st_msg.get("state")
                    if st_state == "arrived":
                        env.ws.send_goto_cancel()
                        goto_active = False
                        # 取新鲜位置判可达
                        try:
                            fresh = env.grpc.get_state(player=env.player)
                            cxp, cyp, czp = (float(v) for v in fresh["player"]["pos"])
                        except Exception:  # noqa: BLE001
                            cxp, cyp, czp = px, py, pz
                        target_d = dist3(cxp, cyp, czp, bx + 0.5, by + 0.5, bz + 0.5)
                        if target_d <= reach:
                            mode, target = "attack", target
                            settle_steps, dig_try = 0, 0
                            budget_left = budget_per_target
                            bx, by, bz = target
                            env.ws.send({"cmd": "look_at",
                                         "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                            settle_steps = 1
                            action = {"camera": [0.0, 0.0]}
                            st_tag = tag(intent="noop", subgoal="settle_aim",
                                         target={"type": "block", "pos": [bx, by, bz]},
                                         strategy="direct_attack", reason="arrived", mode=mode)
                            log(f"  [goto] arrived → [attack] target={target}")
                        else:
                            fails = target_fails.get(target, 0) + 1
                            target_fails[target] = fails
                            # 目标过高/过低直接黑名单（玩家够不到，等累计太慢——8 格高树
                            # 让 goto 卡死几百步）。dy 超 reach 即不可达。
                            target_dy = target[1] - py if target else 0
                            if fails >= target_fails_thresh or abs(target_dy) > reach + 1.0:
                                log(f"  [goto] arrived 但目标不可达（{target_d:.1f} dy={target_dy:.1f}），黑名单+游走")
                                failed_targets[target] = step + 60
                                wander_left = WANDER_STEPS
                                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                                mode, target = "none", None
                                budget_left = 0
                                action = {"forward": True, "camera": [0.0, 0.0]}
                                st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                                             strategy="wander", reason="arrived_too_far", mode=mode)
                            else:
                                mode, target = "none", None
                                action = {"camera": [0.0, 0.0]}
                                st_tag = tag(intent="noop", subgoal="scan_targets",
                                             strategy="wander", reason="arrived_too_far", mode=mode)
                    elif st_state == "blocked_breakable":
                        pos = tuple(int(v) for v in (st_msg.get("pos") or (0, 0, 0)))
                        env.ws.send_goto_cancel()
                        goto_dig_pos = pos
                        goto_dig_try = 0
                        st_tag = tag(intent="dig_obstacle", subgoal="dig_path_block",
                                     target={"type": "block", "pos": [pos[0], pos[1], pos[2]]},
                                     strategy="python_dig_through", reason="blocked_breakable",
                                     mode=mode)
                        log(f"  [goto] blocked_breakable {pos}，Python 挖穿")
                    elif st_state in ("blocked_wall", "stuck"):
                        env.ws.send_goto_cancel()
                        goto_active = False
                        goto_dig_pos = None
                        fails = target_fails.get(target, 0) + 1
                        target_fails[target] = fails
                        # 本地绕行：侧移格二次 goto（不立即黑名单）
                        if detour_left > 0 and fails < target_fails_thresh:
                            detour_left -= 1
                            side = _side_waypoint(env, px, py, pz, bx, by, bz,
                                                  half_extent, LOCAL_DETOUR_RADIUS)
                            if side is not None:
                                detour_wp = side
                                env.ws.send_goto_path([side, (bx, by, bz)])
                                goto_active = True
                                action = {"camera": [0.0, 0.0]}
                                st_tag = tag(intent="replan_local", subgoal="goto_side_waypoint",
                                             target={"type": "waypoint", "pos": list(side)},
                                             strategy="local_detour", reason=st_state,
                                             params={"detour_left": detour_left}, mode=mode)
                                log(f"  [detour] {st_state} → 侧移绕行 {side}")
                            else:
                                wander_left = WANDER_STEPS
                                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                                mode, target = "none", None
                                budget_left = 0
                                action = {"forward": True, "camera": [0.0, 0.0]}
                                st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                                             strategy="wander", reason=st_state, mode=mode)
                        elif fails >= 2:
                            log(f"  [goto] {st_state} 目标 {target} 失败 {fails} 次，黑名单+游走")
                            failed_targets[target] = step + 60
                            wander_left = WANDER_STEPS
                            wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                            mode, target = "none", None
                            budget_left = 0
                            action = {"forward": True, "camera": [0.0, 0.0]}
                            st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                                         strategy="wander", reason=st_state, mode=mode)
                        else:
                            mode, target = "none", None
                            action = {"camera": [0.0, 0.0]}
                            st_tag = tag(intent="noop", subgoal="scan_targets",
                                         strategy="wander", reason=st_state, mode=mode)

            # goto 挖穿子状态：站原地挖穿阻挡块
            if goto_dig_pos is not None:
                tx, ty, tz = goto_dig_pos
                if goto_dig_try == 0 or goto_dig_try % DIG_SCAN_EVERY == 0:
                    palette, data, origin, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3 = blocks_3d(palette, data, data.shape[0])
                    if not _pos_is_block(b3, origin, (tx, ty, tz)):
                        goto_dig_pos = None
                        goto_dig_try = 0
                        action = {"camera": [0.0, 0.0]}
                        st_tag = tag(intent="goto_move", subgoal="goto_target_block",
                                     target={"type": "block", "pos": [bx, by, bz]},
                                     strategy="client_goto", reason="blocked_breakable",
                                     params={"dig_cleared": True}, mode=mode)
                    elif goto_dig_try >= ORACLE_DIG_ABANDON:
                        # 目标块还在但挖了很久没挖掉（准星没对准/不可达）→ 黑名单换目标，
                        # 防无限空挥（ORACLE_DIG_ABANDON=30 步 ≈1.5s，够挖掉普通阻挡块）。
                        # 同时黑名单当前目标（阻挡块 + 目标树），防重扫选回同一目标死循环。
                        log(f"  [goto-dig] 挖不穿 {(tx, ty, tz)}（{goto_dig_try} 步无进展），黑名单+重扫")
                        failed_targets[(tx, ty, tz)] = step + 60
                        if target is not None:
                            failed_targets[target] = step + 60
                        env.ws.send_goto_cancel()
                        goto_active = False
                        goto_dig_pos = None
                        goto_dig_try = 0
                        mode, target = "none", None
                        budget_left = 0
                        action = {"camera": [0.0, 0.0]}
                        st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                                     strategy="wander", reason="dig_give_up", mode=mode)
                    else:
                        _ensure_tool_for_block(env, name_at(b3, origin, (tx, ty, tz)), cfg["equip"])
                        current_tool = _TOOL_FOR_BLOCK.get(name_at(b3, origin, (tx, ty, tz)), cfg["equip"])
                        env.ws.send({"cmd": "look_at", "x": tx + 0.5, "y": ty + 0.5, "z": tz + 0.5})
                        action = {"attack": True, "camera": [0.0, 0.0]}
                        goto_dig_try += 1
                        st_tag = tag(intent="dig_obstacle", subgoal="dig_path_block",
                                     target={"type": "block", "pos": [tx, ty, tz]},
                                     strategy="python_dig_through", reason="blocked_breakable",
                                     params={"dig_try": goto_dig_try}, mode=mode)
                else:
                    env.ws.send({"cmd": "look_at", "x": tx + 0.5, "y": ty + 0.5, "z": tz + 0.5})
                    action = {"attack": True, "camera": [0.0, 0.0]}
                    goto_dig_try += 1
                    st_tag = tag(intent="dig_obstacle", subgoal="dig_path_block",
                                 target={"type": "block", "pos": [tx, ty, tz]},
                                 strategy="python_dig_through", reason="blocked_breakable",
                                 params={"dig_try": goto_dig_try}, mode=mode)
                if goto_dig_try > max_dig_try:
                    log(f"  [goto-dig] 挖不穿 {(tx, ty, tz)}，重规划")
                    env.ws.send_goto_cancel()
                    goto_active = False
                    goto_dig_pos = None
                    goto_dig_try = 0
                    wander_left = WANDER_STEPS
                    wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                    mode, target = "none", None
                    budget_left = 0
                    action = {"forward": True, "camera": [0.0, 0.0]}
                    st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                                 strategy="wander", reason="dig_give_up", mode=mode)

            # 未启动 goto → 发双航点
            if mode == "approach" and target is not None and not goto_active and goto_dig_pos is None:
                if goto_silent >= goto_watchdog:
                    log(f"  [goto] 看门狗：{goto_watchdog} 步无 goto_status，重规划")
                    goto_silent = 0
                    env.ws.send_goto_cancel()
                    mode, target = "none", None
                    budget_left = 0
                    action = {"camera": [0.0, 0.0]}
                    st_tag = tag(intent="noop", subgoal="scan_targets",
                                 strategy="wander", reason="stuck", mode=mode)
                else:
                    px_i, py_i, pz_i = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
                    env.ws.send_goto_path([(px_i, py_i, pz_i), (bx, by, bz)])
                    goto_active = True
                    detour_left = detour_retries
                    action = {"camera": [0.0, 0.0]}
                    st_tag = tag(intent="goto_move", subgoal="goto_target_block",
                                 target={"type": "block", "pos": [bx, by, bz]},
                                 strategy="client_goto", reason="target_selected",
                                 params={"dist": round(dist, 2)}, mode=mode)
                    log(f"  [goto] target={target} dist={dist:.2f}")

        # ---- 5) 无目标：扫描/选择（目标噪声 + budget） ----
        else:
            palette, data, origin, size = env.grpc.get_voxels(
                player=env.player, half_extent=half_extent)
            blocks3 = blocks_3d(palette, data, size)
            blocks = find_blocks(palette, data, origin, targets)
            targets_found = len(blocks)
            new_mode, new_target = select_target(
                blocks, px, py, pz, exclude=failed_targets, task=task,
                blocks3=blocks3, origin=origin, step=step, max_dy_up=max_dy_up_b)
            if new_mode == "none":
                if task == "collect_stone":
                    # 无浅层石头 → 向下挖掘
                    fx, fy, fz = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
                    floor_name = name_at(blocks3, origin, (fx, fy - 1, fz))
                    if floor_name not in (None, "minecraft:air", "minecraft:bedrock"):
                        env.ws.send({"cmd": "look_at", "x": fx + 0.5, "y": fy - 0.5, "z": fz + 0.5})
                        action = {"attack": True, "camera": [0.0, 0.0]}
                        st_tag = tag(intent="dig_target", subgoal="dig_down_target",
                                     target={"type": "block", "pos": [fx, fy - 1, fz]},
                                     strategy="direct_attack", reason="no_target", mode=mode)
                    else:
                        wander_left = WANDER_STEPS
                        wander_yaw = float(state["player"].get("yaw", 0.0)) + random.uniform(-wander_noise, wander_noise)
                        env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                        action = {"forward": True, "camera": [0.0, 0.0]}
                        st_tag = tag(intent="explore_wander", subgoal="scan_targets",
                                     strategy="wander", reason="no_target", mode=mode)
                        log(f"  [explore] no blocks, wander {WANDER_STEPS} steps")
                else:
                    if blocks:
                        # 扫到任务块但无可选目标（多在树冠/山坡）→ 朝最近块走
                        nearest = min(blocks, key=lambda b: (b[0] - px) ** 2
                                      + (b[1] - py) ** 2 + (b[2] - pz) ** 2)
                        mode, target = "approach", nearest
                        budget_left = budget_per_target
                        dist = dist3(px, py, pz, nearest[0] + 0.5, nearest[1] + 0.5, nearest[2] + 0.5)
                        px_i, py_i, pz_i = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
                        env.ws.send_goto_path([(px_i, py_i, pz_i), (nearest[0], nearest[1], nearest[2])])
                        goto_active = True
                        detour_left = detour_retries
                        action = {"camera": [0.0, 0.0]}
                        st_tag = tag(intent="goto_move", subgoal="goto_target_block",
                                     target={"type": "block", "pos": list(nearest)},
                                     strategy="client_goto", reason="target_selected",
                                     params={"dist": round(dist, 2)}, mode=mode)
                        log(f"  [explore] 朝最近 {target_label} {nearest}")
                    else:
                        wander_left = WANDER_STEPS
                        wander_yaw = float(state["player"].get("yaw", 0.0)) + random.uniform(-wander_noise, wander_noise)
                        env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                        action = {"forward": True, "camera": [0.0, 0.0]}
                        st_tag = tag(intent="explore_wander", subgoal="scan_targets",
                                     strategy="wander", reason="no_target", mode=mode)
                        log(f"  [explore] no {target_label} in voxels, wander {WANDER_STEPS} steps")
            else:
                mode, target = new_mode, new_target
                budget_left = budget_per_target
                bx, by, bz = target
                dist = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
                if mode == "attack":
                    settle_steps, dig_try = 0, 0
                    env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                    settle_steps = 1
                    action = {"camera": [0.0, 0.0]}
                    st_tag = tag(intent="noop", subgoal="settle_aim",
                                 target={"type": "block", "pos": [bx, by, bz]},
                                 strategy="direct_attack", reason="target_in_reach", mode=mode)
                    log(f"  [attack] target={target} dist={dist:.2f}")
                else:
                    # 目标噪声：从**已通过 select_target 过滤**的候选里加权随机
                    # （不再从 blocks 全量选——会绕过 dy 过滤选到高处的树，玩家够不到卡死）
                    if target_noise > 0 and len(blocks) >= 3:
                        k = min(5, len(blocks))
                        cands = sorted(blocks, key=lambda b: (b[0] - px) ** 2
                                       + (b[1] - py) ** 2 + (b[2] - pz) ** 2)[:k]
                        # 只保留 dy 在可选范围内的（与新目标同高度语义，防选高树）
                        cands = [c for c in cands
                                 if c[1] - py <= max_dy_up_b and py - c[1] <= APPROACH_DY_DOWN]
                        if not cands:
                            cands = sorted(blocks, key=lambda b: (b[0] - px) ** 2
                                           + (b[1] - py) ** 2 + (b[2] - pz) ** 2)[:k]
                        weights = [1.0 / (1.0 + math.hypot(c[0] + 0.5 - px, c[2] + 0.5 - pz))
                                   for c in cands]
                        total = sum(weights)
                        pick = noise_rng.random() * total
                        acc = 0.0
                        picked = cands[-1]
                        for c, w in zip(cands, weights):
                            acc += w
                            if pick <= acc:
                                picked = c
                                break
                        target = picked
                        bx, by, bz = target
                        dist = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
                        log(f"  [noise] 目标噪声：从最近 {len(cands)} 候选选 {target}（dist={dist:.1f}）")
                    px_i, py_i, pz_i = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
                    env.ws.send_goto_path([(px_i, py_i, pz_i), (bx, by, bz)])
                    goto_active = True
                    detour_left = detour_retries
                    action = {"camera": [0.0, 0.0]}
                    st_tag = tag(intent="goto_move", subgoal="goto_target_block",
                                 target={"type": "block", "pos": [bx, by, bz]},
                                 strategy="client_goto", reason="target_selected",
                                 params={"dist": round(dist, 2),
                                         "noise": round(target_noise, 2)}, mode=mode)
                    log(f"  [goto] target={target} dist={dist:.2f}")

        # ---- 通用收尾：卡死检测 / 黑名单过期 / 工具守卫 ----
        # goto_active 时也检测 XZ 不动（goto_stall）——客户端可能不发任何事件就卡死
        # （LocalPathfinder 绕不过去/目标不可达），不能只靠 goto_silent（无事件才累计）。
        # goto_stall 是跨步持久状态（在策略状态初始化处声明），不在每步重置！
        if os.environ.get("ORACLE_DEBUG"):
            print(f"  [tail] mode={mode} goto_active={goto_active} goto_dig_pos={goto_dig_pos} "
                  f"stair_climb={stair_climb} dist={dist:.2f} moved={moved:.3f} "
                  f"last_dt={last_dist_to_target} goto_silent={goto_silent} "
                  f"goto_stall={goto_stall}", flush=True)
        if mode == "attack" or goto_dig_pos is not None:
            replan_stall = 0
            stuck_count = 0
            goto_stall = 0
            last_dist_to_target = None
        elif goto_active:
            # 卡死检测用「到目标距离无改善」而非 moved（打转时 moved 微动但 dist 不变）
            if last_dist_to_target is None or dist < 0:
                last_dist_to_target = dist
                goto_stall = 0
            elif abs(dist - last_dist_to_target) < 0.5:
                goto_stall += 1
            else:
                goto_stall = 0
                last_dist_to_target = dist
            # goto 卡死阈值 8 步（≈0.4s 无进展即换）——比 stuck_steps_b(20) 更敏感，
            # 防「目标不可达但客户端不发事件」的长时间空转
            if goto_stall >= 8:
                log(f"  [goto-stall] goto 卡死，评估脱困方法")
                env.ws.send_goto_cancel()
                goto_active = False
                # 脱困方法评估（先评估后执行）：
                # 1) 有 dirt 且头顶可挖通 → 垫方块脱困（挖头顶→跳→空中放脚下，逐级爬出）
                # 2) 无 dirt / 头顶被硬块挡 → 挖阶梯脱困（_plan_escape_path：斜向挖+跳）
                # 3) 都不可行 → teleport 回出生点（不浪费时间）
                has_dirt = any(
                    it.get("item") == "minecraft:dirt" and int(it.get("count", 0)) > 0
                    for it in state.get("inventory", {}).get("main", []))
                # 垫方块的前提是**头顶格 fy+2** 空或可挖：玩家高 1.8，脚格 fy / 头格 fy+1
                # 是自身碰撞箱（fy+1 恒为空气，检查它没有意义——老代码就查错在这里）。
                # fy+2 有方块 → 天花板 fy+2.0 → 最大跳高 2.0-1.8 = 0.2 格，永远垫不上去。
                # fy+3 不必检查：它有方块时最大跳高 1.2 > 1.0，仍够放置。
                try:
                    palette_v, data_v, origin_v, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3_v = blocks_3d(palette_v, data_v, data_v.shape[0])
                    fxv, fyv, fzv = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
                    head2 = name_at(b3_v, origin_v, (fxv, fyv + 2, fzv))
                    head_open = (head2 in (None, "minecraft:air", "minecraft:water")
                                 or head2 in _CHEAP_DIGGABLE or head2 in (
                                    "minecraft:stone", "minecraft:granite",
                                    "minecraft:diorite", "minecraft:andesite"))
                except Exception:  # noqa: BLE001
                    head_open = False
                if has_dirt and head_open:
                    stair_climb = False
                    # 垫方块脱困（M11）：客户端 PillarExecutor 逐 tick 执行整套循环。
                    # 目标高度：露天坑先估算坑沿（找到头顶第 1 个空气格 = 地面高度），
                    # 垫到坑沿即停（target_y = 坑沿），避免垫过头飘进树冠/卡住。
                    place_climb = True
                    place_climb_try = 0
                    place_climb_start_y = py
                    place_climb_sent = False
                    place_climb_placed = 0
                    place_climb_reason = ""
                    # 目标 Y：优先坑沿（向上找第一个 air 的 y，即地面高度）；无则
                    # 用已知高处目标脚下；都没有则 None（只受 max_blocks 约束）。
                    target_y_c = None
                    if target is not None and target[1] - py > CLIMB_TARGET_DY:
                        target_y_c = int(target[1]) - 1
                    else:
                        try:
                            # 从头顶 fy+2 向上扫描：第一个 air 格 = 坑沿/地面（玩家站在
                            # y+1 可站 → 垫到 y-1 即 target_y_c = 那个 air 的 y）。
                            scan_py = fyv + 2
                            while scan_py - fyv < 12:
                                nm = name_at(b3_v, origin_v, (fxv, scan_py, fzv))
                                if nm in (None, "minecraft:air", "minecraft:water"):
                                    target_y_c = scan_py - 1   # 站上空气格下面一块
                                    break
                                scan_py += 1
                        except Exception:  # noqa: BLE001
                            target_y_c = None
                    place_climb_target_y = target_y_c
                    action = {"camera": [0.0, 0.0]}
                    st_tag = tag(intent="place_block", subgoal="pillar_up",
                                 strategy="pillar_up", reason="stuck",
                                 params={"method": "pillar",
                                         "target_y": place_climb_target_y}, mode=mode)
                    log(f"  [escape] 背包有 dirt 且头顶 fy+2 可挖通 → 垫方块脱困"
                        f"（target_y={place_climb_target_y}）")
                    continue
                # 无 dirt → 挖阶梯
                try:
                    palette, data, origin, _ = env.grpc.get_voxels(
                        player=env.player, half_extent=half_extent)
                    b3 = blocks_3d(palette, data, data.shape[0])
                    path = _plan_escape_path(env, px, py, pz, b3, origin)
                except Exception:  # noqa: BLE001
                    path = None
                if path:
                    stair_climb = True
                    stair_path = list(path)
                    stair_target = None  # 子状态机从路径取第一块
                    stair_cleared_pos = None
                    stair_target_prev = None
                    stair_try = 0
                    stair_jump_pending = False
                    stair_start_y = py
                    st_tag = tag(intent="dig_obstacle", subgoal="dig_path_block",
                                 strategy="stuck_dig", reason="stuck", mode=mode)
                    log(f"  [stair] 触发阶梯爬升，规划 {len(path)} 块脱困路径")
                elif spawn_pos is not None:
                    try:
                        env.grpc.teleport(player=env.player, pos=spawn_pos)
                        time.sleep(0.5)
                    except Exception:  # noqa: BLE001
                        pass
                    mode, target = "none", None
                    budget_left = 0
                    action = {"camera": [0.0, 0.0]}
                    st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                                 strategy="wander", reason="stuck", mode=mode)
                else:
                    mode, target = "none", None
                    budget_left = 0
                    action = {"camera": [0.0, 0.0]}
                    st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                                 strategy="wander", reason="stuck", mode=mode)
        else:
            if moved < 0.02 and reposition == 0:
                replan_stall += 1
            else:
                replan_stall = 0
            if replan_stall >= REPLAN_STALL_STEPS:
                replan_stall = 0
                log(f"  [replan] XZ 不动 {REPLAN_STALL_STEPS} 步，重扫")
                mode, target = "none", None
                budget_left = 0
                action = {"camera": [0.0, 0.0]}
                st_tag = tag(intent="noop", subgoal="scan_targets",
                             strategy="wander", reason="stuck", mode=mode)
            if moved < 0.02:
                stuck_count += 1
            else:
                stuck_count = 0
            if stuck_count >= stuck_steps_b:
                log(f"  [stuck] {stuck_steps_b} 步未移动，teleport 回出生点重扫")
                # Oracle 数据采集：卡死（掉坑/被卡）直接 teleport 回出生点平地，
                # 比死磕爬坑高效（真实爬坑行为无训练价值）
                if spawn_pos is not None:
                    try:
                        env.grpc.teleport(player=env.player, pos=spawn_pos)
                        time.sleep(0.5)
                    except Exception:  # noqa: BLE001
                        pass
                wander_left = WANDER_STEPS
                wander_jump = 6
                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                mode, target = "none", None
                budget_left = 0
                action = {"forward": True, "camera": [0.0, 0.0]}
                st_tag = tag(intent="stuck_recover", subgoal="scan_targets",
                             strategy="wander", reason="stuck", mode=mode)
        if step % 30 == 0 and failed_targets:
            failed_targets = {k: v for k, v in failed_targets.items() if v > step}

        # ---- 统一 step ----
        res = step_fn(action, ticks)
        progress = float(res["progress"])
        max_progress = max(max_progress, progress)
        # goto 看门狗（客户端不上报 → 取消重扫）
        if goto_active and goto_dig_pos is None:
            goto_silent += 1

        # ---- 记录（step_fn 之后：res 权威 reward/progress；帧取录帧线程最新帧） ----
        if recorder is not None:
            frame = getattr(recorder, "_last_frame", None)
            rec = recorder.on_step(step, st_tag, action, state, frame, res)
            if not rec.get("ok"):
                log(f"  [align] step={step} MISALIGN {rec}", file=sys.stderr)

        if step % 20 == 0 or res.get("terminated"):
            dist_str = f"{dist:.2f}" if dist >= 0 else "-"
            log(f"step={step} progress={progress:.2f} max={max_progress:.2f} "
                f"dist={dist_str} mode={mode} {target_label}={targets_found} jump={jump}")
        if res.get("terminated"):
            env.ws.send_action({"camera": [0.0, 0.0]})
            return True, step, progress
        if res.get("truncated"):
            log(f"ORACLE_TASK_TIMEOUT task={task}: truncated（steps 超时）", file=sys.stderr)
            return False, step, max_progress

    log(f"ORACLE_TASK_FAIL task={task}: {max_steps} 步未完成（progress={max_progress:.2f}）",
        file=sys.stderr)
    return False, max_steps, max_progress


def _plan_escape_path(env, px, py, pz, b3, origin) -> Optional[List[Tuple[int, int, int]]]:
    """规划阶梯式脱困路径：挖斜上方块形成阶梯（每级挖 1 块 + 跳上），斜向推进到地面。

    核心：**阶梯不是竖井**——玩家站 (x, y)，挖斜上方 (x+dx, y+1)，挖穿后站上
    (x+dx, y+1)（斜向爬升），下一级再挖 (x+2dx, y+2)……逐级向上，直到某一级
    上方是空气（露天/地面）即脱困。

    返回要挖的块序列（自下而上）；**只向上**（每级 y+1，绝不越挖越低）；
    无可行路径返回 None（调用方 teleport 兜底）。

    每块的工具由调用方按块类型切换（dirt→铲、stone→镐）——先规划路径，执行时再切工具。
    """
    ipx, ipy, ipz = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
    _STAIR_DIGGABLE = {
        "minecraft:dirt", "minecraft:grass_block", "minecraft:stone",
        "minecraft:sand", "minecraft:gravel", "minecraft:podzol",
        "minecraft:coarse_dirt", "minecraft:andesite", "minecraft:granite",
        "minecraft:diorite", "minecraft:oak_log", "minecraft:oak_planks",
    }
    # 候选方向：4 个斜向（阶梯式爬升，不是竖井）
    dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    best_path = None
    best_score = None
    for (dx, dz) in dirs:
        path = []
        x, z, y = ipx, ipz, ipy
        # 从玩家当前位置开始，斜向逐级向上挖：每级挖 (x+dx, y+1)，然后站上 (x+dx, y+1)
        while len(path) < 6:
            nx, nz, ny = x + dx, z + dz, y + 1
            nm = name_at(b3, origin, (nx, ny, nz))
            if nm in _STAIR_DIGGABLE:
                # 该级可挖：挖 (nx,ny) 后站上 (nx,ny)。但需检查站上后上方是否通畅
                # （站上 (nx,ny) 后头格 (nx,ny+1) 需可站）
                head = name_at(b3, origin, (nx, ny + 1, nz))
                if head in (None, "minecraft:air") or head in _STAIR_DIGGABLE:
                    path.append((nx, ny, nz))
                    x, z, y = nx, nz, ny
                    # 站上后上方是空气 → 已脱困（挖穿这一级即可出地面）
                    if head in (None, "minecraft:air"):
                        break
                    continue
                break  # 站上后头格被实心墙挡 → 这方向不行
            break  # 斜上方不可挖（硬块/基岩）→ 这方向不行
        if len(path) >= 1:
            # 评分：路径越短越好（2 级就脱困最优）
            score = len(path)
            if best_score is None or score < best_score:
                best_score = score
                best_path = path
    return best_path


def _side_waypoint(env, px, py, pz, bx, by, bz, half_extent, radius) -> Optional[Tuple[int, int, int]]:
    """本地绕行：找朝目标方向的侧移格（左右 radius 内可站格），返回世界坐标或 None。

    从玩家当前位置出发，在水平半径 radius 内找与目标方向夹角最小的可站格
    （脚格+头格可站、脚下有地面、水平距离 ≥ ORACLE_SIDE_DIST），供二次 goto。
    """
    try:
        palette, data, origin, _ = env.grpc.get_voxels(player=env.player, half_extent=half_extent)
        b3 = blocks_3d(palette, data, data.shape[0])
    except Exception:  # noqa: BLE001
        return None
    ox, oy, oz = origin
    ipx, ipy, ipz = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
    # 目标方向（XZ 单位向量）
    dx, dz = bx + 0.5 - px, bz + 0.5 - pz
    h = math.hypot(dx, dz)
    if h < 1e-6:
        return None
    ux, uz = dx / h, dz / h
    best = None
    best_score = None
    for dy in range(-2, 3):
        for dxo in range(-radius, radius + 1):
            for dzo in range(-radius, radius + 1):
                if dxo == 0 and dzo == 0:
                    continue
                x, y, z = ipx + dxo, ipy + dy, ipz + dzo
                lx, ly, lz = x - ox, y - oy, z - oz
                size = b3.shape[0]
                if not (0 <= lx < size and 0 <= ly < size and 0 <= lz < size):
                    continue
                foot = str(b3[ly, lz, lx])
                if foot != "minecraft:air":
                    continue
                head = str(b3[ly + 1, lz, lx]) if ly + 1 < size else "minecraft:air"
                if head != "minecraft:air":
                    continue
                below = str(b3[ly - 1, lz, lx]) if ly - 1 >= 0 else "minecraft:air"
                if below in ("minecraft:air", "minecraft:water", "minecraft:lava"):
                    continue
                # 侧移格与目标方向夹角评分：越小越好；距离目标块 ≥ ORACLE_SIDE_DIST
                gx, gz = x - ipx, z - ipz
                gh = math.hypot(gx, gz)
                if gh < 1e-6:
                    continue
                dot = (gx * ux + gz * uz) / gh
                score = (1.0 - max(-1.0, min(1.0, dot))) * 2.0 + gh * 0.1
                # 至少向前走一点（dot > 0.3）
                if dot < 0.3:
                    continue
                if math.hypot(x - bx, z - bz) < ORACLE_SIDE_DIST:
                    continue
                if best_score is None or score < best_score:
                    best_score = score
                    best = (x, y, z)
    return best


if __name__ == "__main__":
    sys.exit(main())
