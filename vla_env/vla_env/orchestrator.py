"""M11.5 任务编排层（DESIGN.md §17.2「客户端为手、Python 为脑」）。

替代 SimHuman 的遥控职责：本层**不逐 tick 遥控**，只做——

1. 目标选择：体素扫描任务块 / 实体（真实世界坐标）；
2. 计划：服务端粗航点（compute_path）+ 挖块计划（带工具，tasks.TOOL_FOR_BLOCK）；
3. 技能派发：goto_path（客户端 NavExecutor 局部绕障/挖穿/自动选工具）、
   pillar_up（垫方块爬高）、近身补挖（reach 内 look_at + attack 电平）、
   近战爆发（kill）、放置（place）；
4. 脱困决策树（难点③）：客户端两级自愈（本地重规划→原地挖）用尽上报 STUCK 后——
   目标高于玩家 ≥2 格 → pillar_up 垫高；否则黑名单换目标；视野无目标 → 垫高重扫/游走；
5. 录制钩子：语义动作流（recorder.add_semantic）+ 每步状态。

逐 tick 按键由客户端合成（含 Humanizer 人类化整形），帧头按键采样如实记录。
本层每个 step 只是「泵」：发 idle/电平动作 + gRPC 结算拿 progress（server-authoritative）。
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .tasks import TaskProfile, kit_slot_for_tool, tool_for_block

# 采集可达距离（生存 reach 4.5，留余量；从脚位算）
REACH = 3.2
# 近战攻击距离
MELEE_REACH = 2.4
# place 任务目标距离窗（M11.6：选较远的目标格，不再"走到哪放到哪"）
PLACE_MIN_DIST = 4.0
PLACE_MAX_DIST = 8.0
PLACE_PREF_DIST = 6.0
# 空闲动作模板的按键名（与 action_space.BUTTONS 一致，避免循环依赖手写）
_BUTTONS = ("forward", "back", "left", "right", "jump", "sneak", "sprint",
            "attack", "use", "drop", "inventory")

# 非实心方块（支撑/落脚判定；与客户端 BlockTraits 口径近似——Python 侧只做粗筛）
NON_SOLID = {
    "minecraft:air", "minecraft:water", "minecraft:lava", "minecraft:cave_air",
    "minecraft:grass", "minecraft:short_grass", "minecraft:tall_grass",
    "minecraft:fern", "minecraft:large_fern", "minecraft:torch",
    "minecraft:oak_sapling", "minecraft:poppy", "minecraft:dandelion",
    "minecraft:snow",
}


class KitAgent:
    """kit 任务编排器：run(step_fn) 自动选目标 → 客户端技能执行 → 脱困决策。"""

    AIM_TURN_SPEED = 12.0        # 瞄准/导航平滑转角（度/tick，人类速度）
    GOTO_BUDGET = 120            # 单次 goto 泵步预算（step，1 step = ticks_per_step tick）
    DIG_BUDGET = 60              # 单块近身补挖预算（step）
    PILLAR_BUDGET = 90           # pillar 等待预算（step）
    WANDER_BUDGET = 25           # 游走预算（step）
    MAX_BLACKLIST_RESET = 2      # 黑名单清空重试轮数（全部目标失败后从头再试）

    def __init__(
        self,
        api: Any,
        profile: TaskProfile,
        rng: Optional[random.Random] = None,
        half_extent: int = 16,
        recorder: Any = None,
        events_source: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        on_no_target: Optional[Callable[[], bool]] = None,
        log: Callable[[str], None] = lambda s: print(s, flush=True),
    ) -> None:
        self.api = api
        self.profile = profile
        self.rng = rng or random.Random()
        self.half_extent = half_extent
        self.recorder = recorder
        self.log = log
        # 目标补给回调（kill 任务）：目标实体从世界消失（惊逃坠亡/despawn，击杀
        # 计数不涨 → episode 不可完成）时由 demo 层补 spawn；返回 True=已补。
        self.on_no_target = on_no_target
        self._no_target_rounds = 0
        self._reprovisions = 0
        # 事件源：录制器活跃时其后台线程会排空 WS 文本事件，goto/pillar 状态须经
        # 录制器转发（recorder.poll_events）；无录制器则直接 drain WS。
        if events_source is not None:
            self._events = events_source
        elif recorder is not None and hasattr(recorder, "poll_events"):
            self._events = recorder.poll_events
        else:
            self._events = lambda: self.api.ws.drain_json(timeout=0)

        self.progress = 0.0
        self.steps = 0
        self._step_fn: Optional[Callable] = None
        self._blacklist: Set[Tuple[int, int, int]] = set()
        self._blacklist_resets = 0
        # 已做过横向绕行重试的目标（每目标一次「换路」机会，§17.8）
        self._detoured: Set[Tuple[int, int, int]] = set()

    # ---- 顶层 ----

    def run(self, step_fn: Callable, max_steps: int) -> Tuple[bool, int, float]:
        """执行任务至完成/超步。step_fn(action_dict, ticks) → {progress, ...}。"""
        self._step_fn = step_fn
        self.api.ws.send({"cmd": "set_turn_speed", "deg": self.AIM_TURN_SPEED})
        # M11.6：按任务下发视线工具策略档位——kill 全程持剑（melee，防"镐子追猪"）、
        # dig 随准星切工具（auto，视线命中第一个方块换对应工具）、place 技能自己选
        # dirt 槽（none，防 auto 把 dirt 槽换走）。
        tool_mode = {"kill": "melee", "dig": "auto", "place": "none"}.get(
            self.profile.kind, "auto")
        self.api.ws.send({"cmd": "set_tool_mode", "mode": tool_mode})
        runner = {"dig": self._run_dig, "kill": self._run_kill,
                  "place": self._run_place}[self.profile.kind]
        while self.steps < max_steps and self.progress < 1.0:
            runner(max_steps)
        # 收尾：释放按键 + 停技能 + 清目标高亮
        self._highlight(None)
        self.api.ws.send_goto_cancel()
        self._pump(1)
        return self.progress >= 1.0, self.steps, self.progress

    # ---- dig（collect_stone / dig_dirt / collect_wood） ----

    def _run_dig(self, max_steps: int) -> None:
        target = self._select_target()
        if target is None:
            # 视野内无目标：换黑名单轮次 → 垫高重扫 → 游走
            self._highlight(None)
            if self._blacklist and self._blacklist_resets < self.MAX_BLACKLIST_RESET:
                self._blacklist_resets += 1
                self._blacklist.clear()
                self.log(f"  [dig] 无目标，清空黑名单重试（第 {self._blacklist_resets} 轮）")
                return
            self.log("  [dig] 视野内无目标 → pillar 垫高重扫")
            if not self._pillar(max_blocks=4):
                self._wander()
            return

        # M11.6 debug：高亮当前目标方块（服务端红色粒子，录制可见），方便定位
        # "在几个方块来回跳"的目标选择行为。
        self._highlight(target)

        pos = self._pos()
        if self._dist3(pos, self._center(target)) > REACH:
            goal = self._standable_near(target)
            digs = self._dig_plan(target)
            status = self._goto(goal if goal is not None else target, digs,
                                stop_near=target)
            if status in ("stuck", "blocked_wall", "blocked_breakable"):
                self._on_stuck(target, status)
                return
            # arrived / timeout / reach —— 落到下方统一的可达性检查

        pos = self._pos()
        if self._dist3(pos, self._center(target)) <= REACH:
            if not self._dig_at(target):
                self.log(f"  [dig] 挖不掉 {target} → 黑名单")
                self._blacklist.add(target)
        else:
            self.log(f"  [dig] 走位后仍不可达 {target} → 黑名单")
            self._blacklist.add(target)

    def _on_stuck(self, target: Tuple[int, int, int], status: str) -> None:
        """脱困决策树（§17.3 难点③ + §17.8）：横向绕行「换路」一次 → 目标显著
        高于玩家 → 垫方块 → 黑名单换目标。

        （客户端在上报 STUCK 前已用尽三级自愈：避让集重规划 ≤3（绕开失败走廊）+
        place_step 补落脚点 + 原地挖面前 ≤2；「阶梯式挖通道」也已编码为
        LocalPathfinder 的 dig_step_up 边。）
        """
        pos = self._pos()
        dy = target[1] - pos[1]
        # ①0 M11.6：目标显著低于玩家（nav 不能下跳 >3）——横向绕行（同高度）必然无效，
        # 直接黑名单换目标，避免"原地来回跳"浪费 ~100 步（dig_stone 首目标实测）。
        if dy <= -2:
            self.log(f"  [stuck] {status}，目标低 {-dy:.0f} 格（nav 下不去）→ 黑名单")
            self._semantic("stuck_decision", status=status, choice="below_gap", dy=dy)
            self._blacklist.add(target)
            return
        # ① 每目标一次「换路」：经垂直中线偏移 ±6 格的途径点重走（绕开整条失败走廊）
        if target not in self._detoured:
            self._detoured.add(target)
            dx = target[0] - pos[0]
            dz = target[2] - pos[2]
            h = math.hypot(dx, dz) or 1.0
            side = self.rng.choice((-1.0, 1.0))
            mid = [int(pos[0] + dx * 0.5 - dz / h * 6 * side),
                   int(pos[1]),
                   int(pos[2] + dz * 0.5 + dx / h * 6 * side)]
            self.log(f"  [stuck] {status} → 横向绕行重试 via={mid}")
            self._semantic("stuck_decision", status=status, choice="lateral_detour",
                           via=mid)
            goal = self._standable_near(target)
            self._goto(goal if goal is not None else target,
                       self._dig_plan(target), stop_near=target, via=mid)
            return   # 主循环重新评估（可达则挖，仍卡走 ②③）
        dy = target[1] - pos[1]
        if dy >= 2.0:
            self.log(f"  [stuck] {status}，目标高 {dy:.0f} 格 → pillar_up 垫高")
            self._semantic("stuck_decision", status=status, choice="pillar_up", dy=dy)
            if self._pillar(target_y=int(target[1])):
                return   # 垫高成功 → 主循环重扫（目标可能已可达）
        self.log(f"  [stuck] {status} → 黑名单换目标")
        self._semantic("stuck_decision", status=status, choice="blacklist")
        self._blacklist.add(target)

    # ---- kill（kill_animal） ----

    def _run_kill(self, max_steps: int) -> None:
        ent = self._find_entity(self.profile.entity)
        if ent is None:
            # 无目标实体：可能走远（游走找）或已从世界消失（惊逃坠亡不计击杀 →
            # episode 不可完成）。连续两轮找不到且有补给回调 → 补 spawn（≤3 次）。
            self._no_target_rounds += 1
            if (self._no_target_rounds >= 2 and self._reprovisions < 3
                    and self.on_no_target is not None):
                self._no_target_rounds = 0
                self._reprovisions += 1
                self.log(f"  [kill] 目标从世界消失 → 补给 spawn（第 {self._reprovisions} 次）")
                self._semantic("reprovision", count=self._reprovisions)
                if self.on_no_target():
                    self._pump(2)   # 等实体入世界/入 state
                    return
            self.log("  [kill] 视野内无目标实体 → 游走")
            self._wander()
            return
        self._no_target_rounds = 0
        pos = self._pos()
        dist = self._dist3(pos, ent)
        if dist > MELEE_REACH:
            # 追击持剑（M11.6）：确认目标后立即选剑，追击全程持剑——不再只在爆发前选，
            # 避免"镐子追猪"；客户端视线工具策略（melee 档）兜底：挖穿绕障时换工具，
            # 准星对回猪自动换回剑。先 cancel 旧导航释放按键再选槽（工具竞争 §17.8）。
            self.api.ws.send_goto_cancel()
            self._pump(1)
            self._select_slot(self.profile.tool_slot)
            self._semantic("arm_sword", target=list(ent), dist=round(dist, 2))
            # 追击：goto 到实体脚位（客户端局部绕障）；实体会动 → 短预算高频重规划
            feet = (int(math.floor(ent[0])), int(math.floor(ent[1])), int(math.floor(ent[2])))
            self._goto(feet, [], budget=30, stop_near=feet, arrive_dist=MELEE_REACH)
            return
        # 近战点击爆发（人类节奏：按 3-5 tick / 松 8-12 tick，尊重攻击冷却）。
        # 被击中的猪会惊逃（1.25×）——挥击时保持 forward 追身（人类边追边打），
        # 松手期间由主循环重新 goto 追击。
        # 时序：cancel 后必须泵一步再选剑——cancel 的按键释放在客户端是调度执行的，
        # 紧跟的 hotbar 动作会被吞掉（§17.8 工具竞争；客户端已改为保留 one-shot，
        # 此处保序是双保险）。
        self.api.ws.send_goto_cancel()
        self._pump(1)
        self._select_slot(self.profile.tool_slot)
        self.api.ws.send({"cmd": "look_at", "x": ent[0], "y": ent[1] + 0.7, "z": ent[2]})
        self._pump(1)   # 瞄准收敛
        # M11.5 出剑门控：客户端准星（crosshairTarget）必须**实际套住目标实体**且
        # 在攻击距离（3.0）内才挥击——否则只贴身不出手。修两类假挥：
        # ① 超距乱挥（服务端坐标距离≈2.4 但准星还没套住）；② 猪侧移后准星落在
        # 它身前的方块上，剑开始砍方块。
        aimed = self._aimed_entity()
        if aimed is None or aimed[0] != self.profile.entity or aimed[1] > 3.0:
            chase = self._idle()
            chase["forward"] = True
            self._step(chase, 1)
            return
        self._semantic("attack_burst", target=list(ent), aimed_dist=round(aimed[1], 2))
        a = self._idle()
        a["attack"] = True
        a["forward"] = dist > 1.6
        a["sprint"] = a["forward"]
        self._step(a, 1)
        for _ in range(self.rng.randint(4, 6)):   # 攻击充能间隙（剑 ~12 tick）：追身不挥击
            chase = self._idle()
            chase["forward"] = True
            chase["sprint"] = True
            self._step(chase, 1)

    # ---- place（place_dirt） ----

    def _run_place(self, max_steps: int) -> None:
        """M11.6：放置目标逻辑——体素扫描选一个较远（偏好 ~6 格、4-8m）的可放置地面格，
        走过去（停在其 ~2 格处）→ 选泥土 → 瞄地面顶面 use 脉冲 → 服务端权威校验。
        不再"走到哪放到哪"（旧版永远放在面前 2 格，位置无逻辑）。"""
        spot = self._select_place_spot()
        if spot is None:
            self._highlight(None)
            self._wander(budget=6)
            return
        gx, gy, gz = spot                       # gy = 实心地面格 Y；放置格 = 上方 1 格
        cell = (gx, gy + 1, gz)
        self._highlight(cell)
        self._semantic("place_target", target=list(cell))

        pos = self._pos()
        px, py, pz = (float(v) for v in pos)
        # 停靠格：从 C 朝玩家方向回退 2 格的地面格（玩家站这里，C 就在前方 ~2 格）
        dx, dz = px - (gx + 0.5), pz - (gz + 0.5)
        h = math.hypot(dx, dz) or 1.0
        sx = int(math.floor(gx + 0.5 + dx / h * 2.0))
        sz = int(math.floor(gz + 0.5 + dz / h * 2.0))
        sy = self._ground_y(sx, int(py), sz)
        if sy is None:
            sy = gy
        stand = (sx, sy + 1, sz)
        self._semantic("place_approach", target=list(cell), stand=list(stand))
        status = self._goto(stand, [], stop_near=stand, arrive_dist=1.2)
        if status in ("stuck", "blocked_wall", "blocked_breakable"):
            self.log(f"  [place] 走不到 {cell}（{status}）→ 黑名单")
            self._blacklist.add(cell)
            self._wander(budget=4)
            return

        self._select_slot(self.profile.tool_slot)
        self.api.ws.send({"cmd": "look_at", "x": gx + 0.5, "y": gy + 0.95, "z": gz + 0.5})
        self._semantic("place_use", target=list(cell))
        self._pump(2)                            # 瞄准收敛
        before = self.progress
        a = self._idle()
        a["use"] = True
        self._step(a, 1)
        self._pump(2)
        if self.progress <= before:
            # 没放上（射线被挡/站位不佳）→ 换目标
            self.log(f"  [place] 放置失败 {cell} → 黑名单")
            self._blacklist.add(cell)
            self._wander(budget=4)

    # ---- 技能：goto / pillar / 近身挖 ----

    def _goto(
        self,
        goal: Sequence[int],
        digs: List[Dict[str, Any]],
        budget: Optional[int] = None,
        stop_near: Optional[Sequence[float]] = None,
        arrive_dist: float = REACH,
        via: Optional[Sequence[int]] = None,
    ) -> str:
        """服务端粗航点 + 客户端 goto_path；泵空动作等终态。

        via 非空时不请求服务端路径，直接走 [feet, via, goal]（横向绕行「换路」重试，
        §17.8——客户端 LocalPathfinder 在各段内局部绕障）。
        返回 goto_status 终态（arrived/stuck/blocked_*）或 "reach"（提前够到
        stop_near）或 "timeout"。
        """
        budget = budget or self.GOTO_BUDGET
        pos = self._pos()
        feet = [int(math.floor(v)) for v in pos]
        goal = [int(v) for v in goal]
        found = False
        if via is not None:
            waypoints = [feet, [int(v) for v in via], goal]
        else:
            try:
                waypoints, _details = self.api.grpc.compute_path(
                    player=self.api.player, start=feet, goal=goal)
            except Exception:  # noqa: BLE001 —— 路径服务失败退化直连
                waypoints = []
            found = bool(waypoints)
            if not found:
                waypoints = [feet, goal]   # 客户端 LocalPathfinder 局部兜底
        self.api.ws.send_goto_path(
            [[int(w[0]), int(w[1]), int(w[2])] for w in waypoints], dig=digs or None)
        self._semantic("goto_path", goal=list(goal), waypoints=len(waypoints),
                       digs=len(digs or []), server_found=bool(found))

        for _ in range(budget):
            self._pump(1)
            if self.progress >= 1.0:
                self.api.ws.send_goto_cancel()
                return "done"
            for e in self._events():
                if e.get("type") == "goto_status":
                    return str(e.get("state"))
            if stop_near is not None:
                if self._dist3(self._pos(), self._center(stop_near)) <= arrive_dist:
                    self.api.ws.send_goto_cancel()
                    return "reach"
        self.api.ws.send_goto_cancel()
        return "timeout"

    def _pillar(self, target_y: Optional[int] = None, max_blocks: int = 8) -> bool:
        """客户端 pillar_up 技能；返回是否至少垫上一格（done）。"""
        self.api.ws.send_goto_cancel()
        self.api.ws.send_pillar_up(target_y=target_y, max_blocks=max_blocks,
                                   item="minecraft:dirt")
        self._semantic("pillar_up", target_y=target_y, max_blocks=max_blocks)
        for _ in range(self.PILLAR_BUDGET):
            self._pump(1)
            for e in self._events():
                if e.get("type") == "pillar_status":
                    state = str(e.get("state"))
                    if state in ("done", "arrived"):
                        return True
                    if state != "progress":
                        self.log(f"  [pillar] 终态 {state}"
                                 f"（reason={e.get('reason')}）")
                        return False
        self.api.ws.send_pillar_cancel()
        return False

    def _dig_at(self, target: Tuple[int, int, int]) -> bool:
        """近身补挖：选工具 + look_at 块中心 + attack 电平，至块变空气。

        目标块本身由本层挖（语义标签 dig_target 与 nav 的 dig_obstacle 区分开）；
        工具按方块查表（难点④），不在 kit 内则用任务主工具。
        """
        block_name = self._name_at(target) or ""
        tool = tool_for_block(block_name)
        slot = kit_slot_for_tool(tool)
        if slot is None:
            slot = self.profile.tool_slot
        self.api.ws.send_goto_cancel()
        self._pump(1)   # 释放导航按键
        self._select_slot(slot)
        cx, cy, cz = self._center(target)
        self.api.ws.send({"cmd": "look_at", "x": cx, "y": cy, "z": cz})
        self._semantic("dig_at", target=list(target), block=block_name, slot=slot)
        self._pump(2)   # 瞄准收敛（12°/tick × 2 step = 48°，常规角度足够）
        for i in range(self.DIG_BUDGET):
            a = self._idle()
            a["attack"] = True
            self._step(a, 1)
            if self.progress >= 1.0:
                break
            if i % 3 == 2 and self._name_at(target) in (None, "minecraft:air"):
                break
        self._pump(1)   # 收手
        return self.progress >= 1.0 or self._name_at(target) in (None, "minecraft:air")

    def _wander(self, budget: Optional[int] = None) -> None:
        """随机方向短距游走（换视野/换角度）。"""
        st = self._state()
        px, py, pz = (float(v) for v in st["player"]["pos"])
        ang = self.rng.uniform(0, 2 * math.pi)
        d = self.rng.uniform(4, 8)
        goal = [int(px + math.cos(ang) * d), int(py), int(pz + math.sin(ang) * d)]
        self._semantic("wander", goal=goal)
        self._goto(goal, [], budget=budget or self.WANDER_BUDGET, arrive_dist=1.5,
                   stop_near=goal)

    # ---- 目标选择 / 计划 ----

    def _highlight(self, target: Optional[Tuple[int, int, int]]) -> None:
        """M11.6 debug：服务端粒子高亮当前目标方块（ShowPath 红色 Dust + END_ROD，
        demo_task 路径可视化同款机制——粒子在世界中渲染，**录制画面也可见**）。

        目标方块每轮重选后重新下发（lifetime 600 tick = 30s，覆盖 approach+dig 时长）；
        None = 清除该玩家高亮。
        """
        if target is None:
            self.api.grpc.show_path(player=self.api.player, clear=True)
        else:
            self.api.grpc.show_path(player=self.api.player, waypoints=[],
                                    goal=target, lifetime_ticks=600)

    def _select_target(self) -> Optional[Tuple[int, int, int]]:
        """体素扫描目标块：排除黑名单/脚下正下方，**优先选 nav 可直接到达的目标**。

        M11.6 振荡修复：客户端 LocalPathfinder 只能 fall≤3 / step_up≤1——若选中 3-5 格
        以下（谷底/悬崖下）的目标，approach 永远走不过去：每轮 STUCK → 横向绕行（同高度
        无效）→ 换目标，在同一小区域来回跳（实测 dig_stone progress=0、轨迹玩家全程在
        高地 y≈70、目标在 y=65-67）。故：候选分两档——「可达高度 |dy|<=2」优先，全部
        不可达才放宽到 |dy|<=6；排序加高高度惩罚，同高度目标优先。
        """
        st = self._state()
        px, py, pz = (float(v) for v in st["player"]["pos"])
        palette, data, origin, size = self.api.grpc.get_voxels(
            player=self.api.player, half_extent=self.half_extent)
        idx_of = {b: i for i, b in enumerate(palette)}
        wanted = set()
        for b in self.profile.target_blocks:
            for pal_name, i in idx_of.items():
                if pal_name.split("[")[0] == b:
                    wanted.add(i)
        if not wanted:
            return None
        ox, oy, oz = origin
        out: List[Tuple[int, int, int]] = []
        for iy in range(size):
            for iz in range(size):
                for ix in range(size):
                    if int(data[iy, iz, ix]) not in wanted:
                        continue
                    b = (ox + ix, oy + iy, oz + iz)
                    if b in self._blacklist:
                        continue
                    if b[0] == int(px) and b[2] == int(pz) and b[1] == int(py) - 1:
                        continue   # 脚下正下方（挖了会掉）
                    if abs(b[1] - py) > 6:
                        continue   # 高差过大（树冠/深层）
                    out.append(b)
        if not out:
            return None
        reachable = [b for b in out if abs(b[1] - py) <= 2]
        pool = reachable if reachable else out
        pool.sort(key=lambda b: (b[0] - px) ** 2 + (b[2] - pz) ** 2 + 4 * abs(b[1] - py))
        top = pool[:3]
        weights = [3, 2, 1][: len(top)]
        return self.rng.choices(top, weights=weights, k=1)[0]

    def _select_place_spot(self) -> Optional[Tuple[int, int, int]]:
        """place 目标格选择（M11.6）：体素扫描找"较远"的可放置地面格。

        返回 (gx, gy, gz)：gy = 实心地面格 Y，放置格在其上方 (gx, gy+1, gz)。
        候选 = 实心地面（非 air）、上方 1 格 air（可放置）、水平距 [PLACE_MIN, MAX]、
        高差 |dy|<=3、不在黑名单；按「水平距偏好 PLACE_PREF_DIST（~6 格）」取最优。
        无候选返回 None → 调用方游走换视野。
        """
        st = self._state()
        px, py, pz = (float(v) for v in st["player"]["pos"])
        palette, data, origin, size = self.api.grpc.get_voxels(
            player=self.api.player, half_extent=self.half_extent)
        air_idx = {i for i, b in enumerate(palette) if b.split("[")[0] == "minecraft:air"}
        if not air_idx:
            return None
        best, best_score = None, float("inf")
        for iy in range(size - 1):
            by = origin[1] + iy
            if abs(by - py) > 3:
                continue
            row, row_up = data[iy], data[iy + 1]
            for iz in range(size):
                for ix in range(size):
                    if int(row[iz, ix]) in air_idx:
                        continue                     # 该格不是实心地面
                    if int(row_up[iz, ix]) not in air_idx:
                        continue                     # 上方 1 格已被占 → 不可放置
                    x, z = origin[0] + ix, origin[2] + iz
                    if abs(x - int(px)) < 3 and abs(z - int(pz)) < 3:
                        continue                     # 脚下近处不选
                    d = math.hypot(x + 0.5 - px, z + 0.5 - pz)
                    if d < PLACE_MIN_DIST or d > PLACE_MAX_DIST:
                        continue
                    if (x, by + 1, z) in self._blacklist:
                        continue
                    score = abs(d - PLACE_PREF_DIST)
                    if score < best_score:
                        best_score, best = score, (x, by, z)
        return best

    def _dig_plan(self, target: Tuple[int, int, int]) -> List[Dict[str, Any]]:
        """挖块计划 = 只有目标块本身（带工具标注）。

        路径阻挡块由客户端 LocalPathfinder 按需规划（dig-through/dig_step_up），
        不预挖 6 邻域——过度挖掘会吃服务端 dig_penalty（§17.3 难点③）。
        """
        name = self._name_at(target) or ""
        tool = tool_for_block(name)
        plan = {"x": target[0], "y": target[1], "z": target[2], "block": name}
        if tool:
            plan["tool"] = tool
        return [plan]

    def _standable_near(self, target: Tuple[int, int, int]) -> Optional[Tuple[int, int, int]]:
        """目标（实心块）相邻的可站脚位：水平 4 邻实心块上方，或目标顶上。"""
        x, y, z = target
        for nx, nz in ((x + 1, z), (x - 1, z), (x, z + 1), (x, z - 1)):
            if self._solid(nx, y, nz) and not self._solid(nx, y + 1, nz):
                return (nx, y + 1, nz)
        if not self._solid(x, y + 1, z):
            return (x, y + 1, z)
        return None

    def _find_entity(self, entity_type: Optional[str]) -> Optional[Tuple[float, float, float]]:
        st = self._state()
        px, py, pz = (float(v) for v in st["player"]["pos"])
        best, best_d = None, float("inf")
        for e in st.get("entities", []):
            if e.get("type") != entity_type:
                continue
            ex, ey, ez = float(e["x"]), float(e["y"]), float(e["z"])
            d = (ex - px) ** 2 + (ey - py) ** 2 + (ez - pz) ** 2
            if d < best_d:
                best_d, best = d, (ex, ey, ez)
        return best

    def _aimed_entity(self) -> Optional[Tuple[str, float]]:
        """客户端准星实体（crosshairTarget，M11.5 出剑门控）：(类型, 距离)。

        准星没套住实体（瞄着方块/空气）返回 None。经 WS `state` 请求 + 事件轮询
        （录制器活跃时事件由其转发）。
        """
        self.api.ws.send({"cmd": "state"})
        for _ in range(3):
            self._pump(1)
            for e in self._events():
                if e.get("type") == "state":
                    ent = e.get("aimed_entity")
                    if ent:
                        return str(ent), float(e.get("aimed_entity_dist", 99.0))
                    return None
        return None

    # ---- 世界查询（小体素窗口，按需拉取） ----

    def _name_at(self, pos: Sequence[int]) -> Optional[str]:
        try:
            palette, data, origin, size = self.api.grpc.get_voxels(
                player=self.api.player, half_extent=1,
                center=(int(pos[0]), int(pos[1]), int(pos[2])))
        except Exception:  # noqa: BLE001
            return None
        ix, iy, iz = (int(pos[0]) - origin[0], int(pos[1]) - origin[1],
                      int(pos[2]) - origin[2])
        if not (0 <= ix < size and 0 <= iy < size and 0 <= iz < size):
            return None
        return palette[int(data[iy, iz, ix])].split("[")[0]

    def _solid(self, x: int, y: int, z: int) -> bool:
        n = self._name_at((x, y, z))
        return n is not None and n not in NON_SOLID

    def _ground_y(self, x: int, y_hint: int, z: int) -> Optional[int]:
        for y in range(y_hint + 1, y_hint - 4, -1):
            if self._solid(x, y, z):
                return y
        return None

    # ---- 泵 / 基础动作 ----

    def _idle(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {name: False for name in _BUTTONS}
        d["hotbar"] = -1
        d["camera"] = [0.0, 0.0]
        return d

    def _step(self, action: Dict[str, Any], ticks: int = 1) -> None:
        st = self._step_fn(action, ticks)
        self.steps += 1
        if st is not None:
            self.progress = max(self.progress, float(st.get("progress", 0.0)))

    def _pump(self, n: int) -> None:
        for _ in range(n):
            self._step(self._idle(), 1)

    def _select_slot(self, slot: int) -> None:
        a = self._idle()
        a["hotbar"] = int(slot)
        self._step(a, 1)

    def _semantic(self, kind: str, **args: Any) -> None:
        if self.recorder is not None and hasattr(self.recorder, "add_semantic"):
            self.recorder.add_semantic({"step": self.steps, "kind": kind, **args})

    def _state(self) -> Dict[str, Any]:
        return self.api.grpc.get_state(player=self.api.player)

    def _pos(self) -> List[float]:
        return [float(v) for v in self._state()["player"]["pos"]]

    @staticmethod
    def _center(b: Sequence[float]) -> Tuple[float, float, float]:
        return (float(b[0]) + 0.5, float(b[1]) + 0.5, float(b[2]) + 0.5)

    @staticmethod
    def _dist3(a: Sequence[float], b: Sequence[float]) -> float:
        return math.dist((float(a[0]), float(a[1]), float(a[2])),
                         (float(b[0]), float(b[1]), float(b[2])))
