"""任务规划器（规划-执行分层重构，2026-08-09）。

Python 只做两件事：
  1. 选目标（体素扫描任务块）；
  2. 建「挖块计划」：每个 dig 带 {x,y,z,block,tool}（按方块查表选工具）。

执行全部交给客户端技能：
  - goto_path（带 dig 计划）→ NavExecutor 跟路/绕障/按计划挖块（工具自切）
  - pillar_up → 垫方块爬高（露天坑脱困）
  - 卡死终态（stuck/blocked_wall）→ 换目标或 pillar

流程（每目标）：
  scan → 选最近可达任务块 → goto_path([玩家脚格, 目标块], dig=计划)
        → 等 goto_status → arrived: 下个目标 / stuck|blocked: 换目标或 pillar
循环至任务完成（进度 1.0）或 max_episodes 集。

用法（vla_env/ 内）：
  .venv/bin/python -u scripts/task_runner.py --task collect_wood --episodes 5
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, ".")

import grpc  # noqa: E402

import vla_env.proto.vla_pb2 as pb  # noqa: E402
import vla_env.proto.vla_pb2_grpc as pbgrpc  # noqa: E402
from vla_env.client_ws import ClientWs  # noqa: E402

WS_URL = "ws://127.0.0.1:30001"
GRPC = "127.0.0.1:50051"
PLAYER = "agent0"
HALF_EXTENT = 12

# 方块 → 工具（与 collect_wood_agent._TOOL_FOR_BLOCK 同表，规划器写进 dig 计划）
TOOL_FOR_BLOCK = {
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
    "minecraft:grass": "minecraft:diamond_shovel",
    "minecraft:short_grass": "minecraft:diamond_shovel",
    "minecraft:tall_grass": "minecraft:diamond_shovel",
    "minecraft:vine": "minecraft:diamond_shovel",
    "minecraft:dead_bush": "minecraft:diamond_shovel",
    "minecraft:netherrack": "minecraft:diamond_pickaxe",
    "minecraft:coal_ore": "minecraft:diamond_pickaxe",
    "minecraft:iron_ore": "minecraft:diamond_pickaxe",
    "minecraft:copper_ore": "minecraft:diamond_pickaxe",
    "minecraft:gold_ore": "minecraft:diamond_pickaxe",
}
TASK_TARGETS = {
    # 只选真正掉落任务品的块：原木（不选树叶——树叶不掉木头，且选树叶会
    # 把树冠当目标导致永远挖不到）
    "collect_wood": {"minecraft:oak_log", "minecraft:oak_planks"},
    "collect_stone": {"minecraft:stone", "minecraft:cobblestone", "minecraft:deepslate"},
}


def log(*a: Any) -> None:
    print(*a, flush=True)


class Planner:
    def __init__(self) -> None:
        self.ch = grpc.insecure_channel(GRPC)
        self.stub = pbgrpc.VlaServerStub(self.ch)
        self.ws = ClientWs(WS_URL)
        self.ws.connect()
        self.ws.send_mode("api")

    # ---- 世界/状态 ----
    def get_state(self) -> Dict:
        return json.loads(self.stub.GetState(pb.StateRequest(player=PLAYER), timeout=5).json)

    def get_voxels(self, cx: int, cy: int, cz: int) -> Tuple[List[str], List[int], Tuple[int, int, int]]:
        r = self.stub.GetVoxels(pb.VoxelRequest(
            player=PLAYER, center_x=cx, center_y=cy, center_z=cz,
            half_extent=HALF_EXTENT), timeout=5)
        return (list(r.palette), list(r.data), (r.origin_x, r.origin_y, r.origin_z))

    def name_at(self, pal: List[str], data: List[int], origin: Tuple[int, int, int],
                x: int, y: int, z: int) -> Optional[str]:
        ox, oy, oz = origin
        size = HALF_EXTENT * 2 + 1
        lx, ly, lz = x - ox, y - oy, z - oz
        if not (0 <= lx < size and 0 <= ly < size and 0 <= lz < size):
            return None
        idx = (ly * size + lz) * size + lx   # [y][z][x] 与服务端序列一致
        if 0 <= idx < len(data) and 0 <= data[idx] < len(pal):
            return pal[data[idx]].split("[")[0]
        return None

    def find_targets(self, targets: set) -> List[Tuple[int, int, int]]:
        """扫描玩家周围，返回所有任务块世界坐标。"""
        st = self.get_state()
        px, py, pz = (int(math.floor(float(v))) for v in st["player"]["pos"])
        pal, data, origin = self.get_voxels(px, py, pz)
        out = []
        for y in range(origin[1], origin[1] + HALF_EXTENT * 2 + 1):
            for z in range(origin[2] - HALF_EXTENT, origin[2] + HALF_EXTENT + 1):
                for x in range(origin[0] - HALF_EXTENT, origin[0] + HALF_EXTENT + 1):
                    if self.name_at(pal, data, origin, x, y, z) in targets:
                        out.append((x, y, z))
        return out

    def nearest(self, px: float, py: float, pz: float,
                blocks: List[Tuple[int, int, int]]) -> Optional[Tuple[int, int, int]]:
        """最近可达目标：水平距离近 + 高度差小（|dy|≤4，且目标不高过玩家太多）。

        树冠 log（y 高出玩家 5+）不可达（玩家站地面够不着），不选；
        高度差小的（玩家脚下/眼前 1-4 格）可达。
        """
        if not blocks:
            return None
        candidates = [b for b in blocks if abs(b[1] - py) <= 4]
        if not candidates:
            # 全部太高/太低 → 返回最低的（宁可挖低处的，也不选树冠）
            candidates = sorted(blocks, key=lambda b: b[1])[:3]
        return min(candidates,
                   key=lambda b: (b[0] - px) ** 2 + (b[2] - pz) ** 2 + abs(b[1] - py) * 2)

    # ---- 计划构建 ----
    def build_dig_plan(self, target: Tuple[int, int, int]) -> List[Dict]:
        """目标块：挖它及它 6 邻域里的可挖块（暴露/挖穿），每个带 block+tool。"""
        st = self.get_state()
        px, py, pz = (int(math.floor(float(v))) for v in st["player"]["pos"])
        pal, data, origin = self.get_voxels(px, py, pz)
        bx, by, bz = target
        plan: List[Dict] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if abs(dx) + abs(dy) + abs(dz) > 1:
                        continue   # 只 6 邻域
                    x, y, z = bx + dx, by + dy, bz + dz
                    nm = self.name_at(pal, data, origin, x, y, z)
                    if nm is None or nm in ("minecraft:air", "minecraft:water"):
                        continue
                    tool = TOOL_FOR_BLOCK.get(nm)
                    if tool:
                        plan.append({"x": x, "y": y, "z": z, "block": nm, "tool": tool})
        return plan

    # ---- 执行（纯客户端） ----
    def goto(self, goal: Tuple[int, int, int], digs: List[Dict],
             wait: float = 60.0) -> Optional[str]:
        st = self.get_state()
        px, py, pz = (int(math.floor(float(v))) for v in st["player"]["pos"])
        start = (px, py, pz)
        msg = {"cmd": "goto_path", "waypoints": [list(start), list(goal)]}
        if digs:
            msg["dig"] = digs
        self.ws._send_json(msg)
        log(f"  goto {start} -> {goal} dig={len(digs)}")

        deadline = time.time() + wait
        while time.time() < deadline:
            self.ws.recv_frame_latest(timeout=0.3)   # 消费帧流（事件才进队列）
            for e in self.ws.drain_json(timeout=0.0):
                if e.get("type") == "goto_status":
                    return e.get("state")
            time.sleep(0.1)
        return None   # 超时

    def pillar(self, target_y: Optional[int] = None, max_blocks: int = 10) -> str:
        self.ws.send_pillar_up(target_y=target_y, max_blocks=max_blocks, item="minecraft:dirt")
        deadline = time.time() + 30
        state = "pending"
        while time.time() < deadline:
            self.ws.recv_frame_latest(timeout=0.3)
            for e in self.ws.drain_json(timeout=0.0):
                if e.get("type") == "pillar_status":
                    st = e.get("state")
                    if st != "progress":
                        state = st
                        return state
            time.sleep(0.1)
        return state

    # ---- 主循环 ----
    def run_episode(self, task: str, max_targets: int = 20) -> bool:
        targets_set = TASK_TARGETS[task]
        done_blocks = 0
        failed: set = set()
        for t in range(max_targets):
            st = self.get_state()
            px, py, pz = (float(v) for v in st["player"]["pos"])
            blocks = [b for b in self.find_targets(targets_set) if b not in failed]
            target = self.nearest(px, py, pz, blocks)
            if target is None:
                log(f"  [ep] 无目标（done={done_blocks}），尝试 pillar 垫高再扫")
                self.pillar(target_y=None, max_blocks=8)
                time.sleep(1)
                blocks = [b for b in self.find_targets(targets_set) if b not in failed]
                target = self.nearest(px, py, pz, blocks)
                if target is None:
                    log(f"  [ep] 仍无目标 → 本集失败")
                    return False
            digs = self.build_dig_plan(target)
            state = self.goto(target, digs)
            if state == "arrived":
                done_blocks += 1
                log(f"  [ep] 到达目标 {target}（done={done_blocks}）")
            elif state in ("stuck", "blocked_wall", "blocked_breakable"):
                log(f"  [ep] {state} 目标 {target} → 先 pillar 垫高再换目标")
                # 垫高（露天/头顶可挖时 pillar 一步到位，垫到坑沿）
                self.pillar(target_y=None, max_blocks=8)
                time.sleep(0.5)
                failed.add(target)
            else:
                log(f"  [ep] 无终态（{state}）→ 换目标")
                failed.add(target)
            # 进度检查（挖到任务块数量是否达标）
            if done_blocks >= 4:   # collect_wood 4 原木 / collect_stone 8 石头（粗略）
                return True
        return False

    def run(self, task: str, episodes: int) -> None:
        ok = 0
        for ep in range(1, episodes + 1):
            log(f"===== episode {ep} task={task} =====")
            # reset：重置世界 + 摆场景（客户端/服务端已有 reset_world 支持）
            try:
                self.stub.ResetWorld(pb.ResetRequest(player=PLAYER), timeout=5)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
            # 摆目标（树/石）：尝试在玩家附近生成（若服务端有 spawn 能力）
            if task == "collect_wood":
                pass   # 自然树/服务端摆树由 reset 后环境决定
            if self.run_episode(task):
                ok += 1
                log(f"episode {ep}: PASS")
            else:
                log(f"episode {ep}: FAIL")
        log(f"=== 完成 {ok}/{episodes} ===")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=list(TASK_TARGETS), default="collect_wood")
    ap.add_argument("--episodes", type=int, default=5)
    args = ap.parse_args()
    p = Planner()
    try:
        p.run(args.task, args.episodes)
    finally:
        p.ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
