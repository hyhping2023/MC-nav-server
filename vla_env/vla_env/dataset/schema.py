"""语义标签 schema（Oracle 轨迹生成器，DESIGN.md §11.5）。

职责：定义逐帧语义标签的字段枚举与构造 helper——每帧记录「这一帧为什么是
这个动作」，与原始动作（buttons/camera/hotbar）和服务端权威 reward/progress
一一对应（labels 与 actions 同一行号）。

字段（全部小写下划线，JSON 序列化）：
- intent：顶层行为意图（见 INTENTS）
- subgoal：当前中间目标（见 SUBGOALS，粒度从模式机三态细化为 11 类）
- target：目标对象 {type: block|entity|waypoint|shore, pos: [x,y,z]}
- strategy：本次选择的执行策略（见 STRATEGIES）
- reason：触发原因（固定词表，见 REASONS）
- params：行为模式参数 dict（每模式自定子集）
- mode：策略机当前模式（attack/approach/none）快照

label() 构造：校验枚举合法性（非法值打 WARN 不抛——采集不因标签失败中断），
未知枚举值仍落库（供下游发现新行为）。
"""

from __future__ import annotations

import json
import sys
import warnings
from typing import Any, Dict, Iterable, List, Optional

# ---- 枚举词表（采集与校验共用；扩展新行为时在此追加） ----

# 顶层行为意图（每帧必填）。
INTENTS = (
    "approach",       # 朝目标导航（goto 驱动）
    "dig_target",     # 挖任务块（attack 长按）
    "dig_obstacle",   # 挖障碍/路径穿行块（Python 或客户端驱动）
    "goto_move",      # 客户端 goto 驱动移动（Python 只发空动作）
    "goto_dig",       # 客户端 goto 驱动挖掘（本地 digTargets）
    "explore_wander", # 无目标游走
    "water_escape",   # 溺水自救
    "reposition",     # 后退清视线
    "replan_local",   # 本地绕行（blocked_wall/stuck 后的侧移 goto）
    "stuck_recover",  # 卡死恢复（游走/兜底）
    "place_block",    # 垫方块（爬高/过水/搭桥）
    "noop",           # 等待（settle/工具切换/空动作）
)

# 中间目标（模式机子状态细粒度）。
SUBGOALS = (
    "goto_target_block",   # 双航点 goto 到目标块
    "goto_side_waypoint",  # 本地绕行侧移航点
    "dig_cover",           # 挖穿覆盖层（collect_stone cover 块）
    "dig_path_block",      # 挖穿路径阻挡块
    "dig_down_target",     # 向下挖掘（collect_stone 无浅层时）
    "swim_to_shore",       # 游向岸边
    "place_ladder",        # 垫 dirt 阶梯（爬高）
    "pillar_up",           # 原地垫柱爬高（挖头顶 fy+2 → 朝正下 → 跳 → 顶点放块）
    "place_bridge",        # 垫块过水/过坑
    "reachable_attack",    # 目标在 reach 内，直接攻击
    "scan_targets",        # 扫描体素找目标
    "swap_tool",           # 工具切换（hotbar）
    "settle_aim",          # 站定瞄准
)

# 执行策略（本次决策选择的实现路径）。
STRATEGIES = (
    "client_goto",      # 双航点 goto（客户端 NavExecutor 局部绕障）
    "local_detour",     # Python 侧移绕行（blocked_wall 后）
    "python_dig_through",  # Python 站原地挖穿阻挡块
    "wander",           # 随机游走探索
    "swim",             # 游泳自救
    "place_ladder",     # 垫 dirt 爬高
    "pillar_up",        # 客户端 PillarExecutor 原地垫柱爬高（M11）
    "direct_attack",    # 直接攻击任务块/实体
    "step_back",        # 后退清视线
    "stuck_dig",        # 卡死挖面前方块
)

# 触发原因（固定词表；自由文本请放 params.reason_detail）。
REASONS = (
    "initial_scan",        # 初始扫描
    "target_in_reach",     # 目标在采集距离内
    "target_selected",     # 选定新目标
    "target_gone",         # 目标消失/被挖掉
    "arrived",             # goto 到达
    "arrived_too_far",     # 到达但目标不可达
    "blocked_breakable",   # 客户端上报可挖阻挡
    "blocked_wall",        # 客户端上报墙（本地绕障用尽）
    "stuck",               # 卡死
    "dig_give_up",         # 挖不动放弃（MAX_DIG_TRY）
    "blacklist",           # 目标进黑名单
    "water_stuck",         # 水中卡墙
    "no_target",           # 扫描无目标
    "climb_high_target",   # 目标在头顶高处（垫块爬高）
    "random_noise",        # 目标选择噪声
    "budget_reached",      # 同目标挖块数达上限（换目标）
    "path_no_route",       # 无路（直线不可达）→ 游走
    "tool_missing",        # 工具缺失
    "periodic_rescan",     # 周期重扫
)

# 模式机状态（顶层可见性快照）。
MODES = ("attack", "approach", "none")

# ---- label() 构造 ----

_VALID = {
    "intent": set(INTENTS),
    "subgoal": set(SUBGOALS),
    "strategy": set(STRATEGIES),
    "reason": set(REASONS),
    "mode": set(MODES),
}

# 已见过的非法值（只 WARN 一次，防刷屏）。
_WARNED: set = set()


def label(
    intent: str,
    subgoal: Optional[str] = None,
    target: Optional[Dict[str, Any]] = None,
    strategy: Optional[str] = None,
    reason: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """构造一帧语义标签。

    校验枚举合法性：非法值打 WARN 并原样落库（不抛异常——采集不因标签失败
    中断）。params 拷贝（防调用方后续修改污染已记录标签）。
    """
    _check("intent", intent)
    if subgoal is not None:
        _check("subgoal", subgoal)
    if strategy is not None:
        _check("strategy", strategy)
    if reason is not None:
        _check("reason", reason)
    if mode is not None:
        _check("mode", mode)

    tag: Dict[str, Any] = {"intent": intent}
    if subgoal is not None:
        tag["subgoal"] = subgoal
    if target is not None:
        tag["target"] = dict(target)
    if strategy is not None:
        tag["strategy"] = strategy
    if reason is not None:
        tag["reason"] = reason
    if params is not None:
        tag["params"] = dict(params)
    if mode is not None:
        tag["mode"] = mode
    return tag


def _check(field: str, value: str) -> None:
    if value in _VALID.get(field, set()):
        return
    key = (field, value)
    if key in _WARNED:
        return
    _WARNED.add(key)
    warnings.warn(f"[schema] 非法 {field}={value!r}（未在词表中，仍落库）")


# ---- intent → 语义动作名映射（DESIGN.md §7.3 对齐） ----

# 每帧记录「语义动作」（goto/dig/place/...），与原始动作双标签。
INTENT_TO_SEMANTIC = {
    "approach": "goto",
    "dig_target": "dig",
    "dig_obstacle": "dig",
    "goto_move": "goto",
    "goto_dig": "dig",
    "explore_wander": "goto",
    "water_escape": "goto",
    "reposition": "goto",
    "replan_local": "goto",
    "stuck_recover": "goto",
    "place_block": "place",
    "noop": "look_at",
}


def semantic_from_intent(intent: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """intent → 语义动作记录 {name, args}（与 DESIGN.md §7.3 语义动作对齐）。"""
    name = INTENT_TO_SEMANTIC.get(intent, "look_at")
    return {"name": name, "args": dict(args or {})}


# ---- meta 构造 ----

def oracle_meta(
    *,
    episode_id: str,
    task: str,
    world_seed: int,
    task_seed: Optional[int],
    reset_seed: Optional[int],
    oracle_cfg: Dict[str, Any],
    spawn_pos: List[float],
    frame_count: int,
    server_tick_start: int,
    server_tick_end: int,
    versions: Optional[Dict[str, str]] = None,
    render: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造 episode meta.json（DESIGN.md §11.2 字段子集 + oracle 配置）。"""
    meta: Dict[str, Any] = {
        "episode_id": episode_id,
        "task": task,
        "world_seed": world_seed,
        "task_seed": task_seed,
        "reset_seed": reset_seed,
        "mapgen": {"type": "minecraft:default", "structures": True},
        "versions": versions or {
            "vla_env": "0.1.0",
        },
        "render": render or {"res": 224, "fov": 70, "fps": 20, "hud": False},
        "action_space": {
            "mode": "discrete",
            "version": "1.0",
            "semantic_version": "1.0",
            "camera_bins": 121,
        },
        "oracle": dict(oracle_cfg),
        "spawn_pos": list(spawn_pos),
        "server_tick": {"start": server_tick_start, "end": server_tick_end},
        "frame_count": frame_count,
    }
    if extra:
        meta.update(extra)
    return meta


# ---- JSONL 读写 helper ----

def read_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    """逐行读取 JSONL（跳过空行/坏行，容错）。"""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                warnings.warn(f"[schema] JSONL 坏行（跳过）: {path}: {line[:80]!r}")
                continue


def write_jsonl(path: str, rows: Iterable[Dict[str, Any]]) -> None:
    """整批写 JSONL（原子：先写临时文件再 rename）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    import os

    os.replace(tmp, path)


def append_jsonl(path: str, row: Dict[str, Any]) -> None:
    """追加一行 JSONL（供采集过程边采边写）。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
