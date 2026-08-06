#!/usr/bin/env python3
"""M7 验收 + 3D 导航升级：collect_wood_agent —— 脚本策略完成 collect_wood。

用法（在 vla_env/ 目录内，避免 namespace 遮蔽）：
    .venv/bin/python scripts/collect_wood_agent.py [--max-steps 600] [--ticks 2]

策略（V2，A* + 跳跃 3D 导航，DESIGN.md §7.3 语义 goto 的脚本版）：
  1. **A* 3D 导航**：approach 阶段调 `ComputePath`（服务端 A*，含跳跃/下落节点）到目标
     原木，逐航点跟随；航点 y 比玩家高 >0.5 时**跳跃**（翻 1 格台阶/爬坡）。
  2. **挖木期不动、不跳、不重瞄**：进入 attack 先"站稳"（settle 只发相机），再锁定目标
     方块连续按住 attack。任何移动/跳跃都会让准星漂移 → 方块破坏进度清零。
  3. **目标粘滞**：不频繁重扫换目标；attack 期间每 N 步确认目标是否还在（被挖掉换下一个）。
  4. **挖完停顿**：方块破坏后掉落物从眼前下落会挡射线，给 2 步间隔再继续。
  5. **受阻换树**：A* 无路 / 卡死时游走换下一棵，不硬钻树冠。

成功判定：terminated → `M7_COLLECT_WOOD_OK steps=N` exit 0。

可复用核心：`collect_wood_policy(env, step_fn, ...)` 供 demo_record.py 录制
第一视角视频时调用（传入不同 step_fn 即可：env.step 或 录帧版 send+grpc）。
"""

from __future__ import annotations

import argparse
import math
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
WANDER_STEPS = 14
SETTLE_STEPS = 3           # 进入 attack 后先停几拍（只相机不动不挖）
DIG_SCAN_EVERY = 12        # attack 期间每隔 N 步确认目标是否还在
MAX_DIG_TRY = 90           # 同一目标连续挖 N 步未破坏则放弃
APPROACH_RESCAN = 5        # approach 期间每隔 N 步重扫（检测转为 attack）
STUCK_STEPS = 20           # 位置基本不动 N 步 → 卡死
APPROACH_STUCK_STEPS = 6   # approach 朝当前航点连续 N 步几乎没动 → 重算路径
STUCK_MOVED = 0.02         # 水平位移 < 此值视为"没动"
WP_ARRIVE_DIST = 1.5       # 距航点 < 此值视为到达
JUMP_Y_THRESH = 0.5        # 下一航点 y 高于玩家此值 → 跳跃（翻 1 格台阶）


def log(*a, **kw) -> None:
    """打印并立即 flush；支持 print 的 kwargs（如 file=sys.stderr）。"""
    print(*a, flush=True, **kw)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M7 collect_wood 脚本策略验收（A*+跳跃 3D 导航）")
    p.add_argument("--max-steps", type=int, default=600)
    p.add_argument("--ticks", type=int, default=2, help="每步服务端 tick 数")
    p.add_argument("--half-extent", type=int, default=16, help="体素半宽")
    p.add_argument("--player", default="agent0")
    return p.parse_args()


def find_logs(
    palette: List[str],
    data: np.ndarray,
    origin: Tuple[int, int, int],
) -> List[Tuple[int, int, int]]:
    """palette+data 还原成 3D 块名数组，返回所有 oak_log 的世界坐标。

    data 索引顺序（proto VoxelReply）：y 外层 → z → x 最内层，即 arr[y][z][x]。
    """
    names = [p.split("[")[0] for p in palette]  # 去掉块状态（如 [axis=y]）
    matches: List[Tuple[int, int, int]] = []
    size = data.shape[0]
    ox, oy, oz = origin
    for y in range(size):
        for z in range(size):
            for x in range(size):
                if names[int(data[y, z, x])] == OAK_LOG:
                    matches.append((ox + x, oy + y, oz + z))
    return matches


def select_target(
    logs: List[Tuple[int, int, int]],
    px: float, py: float, pz: float,
    exclude: Optional[Dict[Tuple[int, int, int], int]] = None,
) -> Tuple[str, Optional[Tuple[int, int, int]]]:
    """目标选择：优先可达 log（attack），否则最近 log（approach），无 log → none。

    可达判定：3D 距离 ≤REACH 且水平距离 ≥REACH_MIN_H、高度差 ≤2.6。
    `exclude`（黑名单 {pos: expire_step}）内的目标跳过（挖不动换目标用）。
    """
    skip = set(exclude.keys()) if exclude else set()
    eye_y = py + EYE_HEIGHT
    reachable: List[Tuple[float, int, int, int]] = []
    for (bx, by, bz) in logs:
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
        _, bx, by, bz = reachable[0]
        return "attack", (bx, by, bz)
    if logs:
        n = min(
            (b for b in logs if b not in skip),
            key=lambda b: (b[0] + 0.5 - px) ** 2
            + (b[1] + 0.5 - py) ** 2
            + (b[2] + 0.5 - pz) ** 2,
            default=None,
        )
        if n is not None:
            return "approach", n
    return "none", None


def look_at(
    px: float, py: float, pz: float,
    bx: int, by: int, bz: int,
    pitch_clamp: Optional[float] = None,
) -> Tuple[float, float]:
    """玩家 pos 与目标点（块中心/航点），返回 (yaw, pitch)。pitch_clamp 夹紧 |pitch|。"""
    dx, dy, dz = bx - px, by - py, bz - pz
    h = math.hypot(dx, dz)
    yaw = math.degrees(math.atan2(-dx, dz))
    pitch = math.degrees(math.atan2(-dy, h)) if h > 1e-6 else 0.0
    if pitch_clamp is not None:
        pitch = max(-pitch_clamp, min(pitch_clamp, pitch))
    return yaw, pitch


def dist3(px, py, pz, bx, by, bz) -> float:
    return math.sqrt((bx - px) ** 2 + (by - py) ** 2 + (bz - pz) ** 2)


def collect_wood_policy(
    env: MinecraftEnv,
    step_fn: Callable[[Dict, int], Dict],
    max_steps: int = 600,
    ticks: int = 2,
    half_extent: int = 16,
) -> Tuple[bool, int, float]:
    """collect_wood 策略核心（V2：A* + 跳跃 3D 导航）。

    - `env`：已 reset 的 MinecraftEnv
    - `step_fn(action, ticks) -> dict{progress, terminated, truncated}`：
      普通版传 `lambda a, t: env.step(a, ticks=t)[2:] 对应字段`；录帧版传
      send_action + get_step_result（帧由独立线程消费）
    返回 (success, steps, max_progress)。假设 env.reset 已由调用方完成。
    """
    # 策略状态
    mode = "none"
    target: Optional[Tuple[int, int, int]] = None
    waypoints: List[Tuple[float, float, float]] = []
    wp: Optional[Tuple[float, float, float]] = None
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
    logs_found = 0
    aim_cache: Optional[Tuple[float, float]] = None
    scan_timer = 0

    for step in range(1, max_steps + 1):
        state = env.grpc.get_state(player=env.player)
        px, py, pz = (float(v) for v in state["player"]["pos"])
        if last_pos is None:
            last_pos = (px, py, pz)
        moved = math.hypot(px - last_pos[0], pz - last_pos[2])
        last_pos = (px, py, pz)
        dist = -1.0
        jump = False

        # ---- 1) 挖后后退：清掉落物挡视线，不瞄准新目标 ----
        if reposition > 0:
            reposition -= 1
            mode, target = "none", None
            waypoints, wp = [], None
            wp_stuck = 0
            action = {"back": True, "camera": [0.0, 0.0]}

        # ---- 2) 游走 ----
        elif wander_left > 0:
            wander_left -= 1
            env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
            if wander_jump > 0:
                wander_jump -= 1
                action = {"forward": True, "jump": True, "camera": [0.0, 0.0]}
            else:
                action = {"forward": True, "camera": [0.0, 0.0]}

        # ---- 3) attack 挖掘：不动不跳；每 20 步重瞄一次 ----
        elif mode == "attack" and target is not None:
            bx, by, bz = target
            dist = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
            dig_try += 1
            if dig_try % DIG_SCAN_EVERY == 0:
                palette, data, origin, _ = env.grpc.get_voxels(
                    player=env.player, half_extent=half_extent)
                logs = find_logs(palette, data, origin)
                if target not in logs:
                    log(f"  [dig] target {target} gone after {dig_try} tries")
                    reposition = 2
                    mode, target = "none", None
                    waypoints, wp = [], None
                    wp_stuck = 0
                    aim_cache = None
                    action = {"camera": [0.0, 0.0]}
                else:
                    action = {"attack": True, "camera": [0.0, 0.0]}
            elif dig_try > MAX_DIG_TRY:
                # 挖不动：黑名单 + 游走换树（防无限重选同一目标）
                log(f"  [dig] give up target {target} after {MAX_DIG_TRY} tries, blacklist+wander")
                failed_targets[target] = step + 60
                wander_left = WANDER_STEPS + 6
                wander_jump = 6
                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                mode, target = "none", None
                waypoints, wp = [], None
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
                aim_cache = (yaw, pitch)
                action = {"attack": True, "camera": [0.0, 0.0]}
            else:
                action = {"attack": True, "camera": [0.0, 0.0]}

        # ---- 4) approach：A* 3D 导航（逐航点 + 台阶跳跃） ----
        elif mode == "approach" and target is not None:
            bx, by, bz = target
            dist = dist3(px, py, pz, bx + 0.5, by + 0.5, bz + 0.5)
            scan_timer += 1

            # 周期重扫：目标可能已可达（转 attack）或已被破坏（换目标）
            if scan_timer % APPROACH_RESCAN == 0:
                palette, data, origin, _ = env.grpc.get_voxels(
                    player=env.player, half_extent=half_extent)
                logs = find_logs(palette, data, origin)
                logs_found = len(logs)
                new_mode, new_target = select_target(logs, px, py, pz, exclude=failed_targets)
                if new_mode == "attack":
                    # 已可达 → 切 attack
                    mode, target = "attack", new_target
                    settle_steps, dig_try = 0, 0
                    aim_cache = None
                    wp_stuck = 0
                    bx, by, bz = target
                    env.ws.send({"cmd": "look_at", "x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5})
                    settle_steps = 1
                    action = {"camera": [0.0, 0.0]}
                    log(f"  [attack] target={target} dist={dist:.2f}")
                elif target not in logs:
                    # 目标被破坏/消失 → 后退清视线再重选
                    reposition = 2
                    mode, target = "none", None
                    waypoints, wp = [], None
                    wp_stuck = 0
                    action = {"camera": [0.0, 0.0]}
                # 否则保持当前 approach 目标

            # 航点跟随
            if mode == "approach":
                if wp is None:
                    if not waypoints:
                        path = env.grpc.compute_path(player=env.player,
                                                     goal=(bx, by, bz))
                        if path:
                            waypoints = list(path)
                            log(f"  [path] target={target} waypoints={len(path)}")
                        else:
                            # A* 无路（目标在树冠/悬崖）→ 游走换树
                            log(f"  [path] no path to {target}, wander")
                            wander_left = WANDER_STEPS
                            wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                            mode, target = "none", None
                            waypoints, wp = [], None
                            wp_stuck = 0
                            action = {"forward": True, "camera": [0.0, 0.0]}
                    wp = waypoints.pop(0)
                wx, wy, wz = wp
                # 朝向下一航点；航点更高 → 跳跃（3D 导航）
                yaw, pitch = look_at(px, py, pz, wx, wy, wz, pitch_clamp=20.0)
                env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                jump = (wy - py) > JUMP_Y_THRESH
                action = {"forward": True, "jump": jump, "camera": [0.0, 0.0]}
                if dist3(px, py, pz, wx, wy, wz) < WP_ARRIVE_DIST:
                    wp = None  # 到达 → 取下一航点（下个循环）

                # 航点卡死：朝当前航点连续 APPROACH_STUCK_STEPS 步几乎没动。
                # 首次：后退 3 步解卡（撞墙/贴树干时远离障碍）；同一目标二次卡死：
                # 黑名单 + 游走换树（防对不可达目标无限重试）。
                if wp is not None:
                    if moved < STUCK_MOVED:
                        wp_stuck += 1
                    else:
                        wp_stuck = 0
                else:
                    wp_stuck = 0

                if wp_stuck >= APPROACH_STUCK_STEPS:
                    wp_stuck = 0
                    waypoints, wp = [], None
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

        # ---- 5) 无目标：重扫/选择 ----
        else:
            palette, data, origin, size = env.grpc.get_voxels(
                player=env.player, half_extent=half_extent)
            logs = find_logs(palette, data, origin)
            logs_found = len(logs)
            new_mode, new_target = select_target(logs, px, py, pz, exclude=failed_targets)
            if new_mode == "none":
                wander_left = WANDER_STEPS
                wander_yaw = float(state["player"].get("yaw", 0.0)) + random.uniform(-30.0, 30.0)
                env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                action = {"forward": True, "camera": [0.0, 0.0]}
                log(f"  [explore] no logs in voxels, wander {WANDER_STEPS} steps")
            else:
                mode, target = new_mode, new_target
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
                else:  # approach：立刻算 A* 路径
                    path = env.grpc.compute_path(player=env.player, goal=(bx, by, bz))
                    waypoints = list(path) if path else []
                    wp = None
                    log(f"  [approach] target={target} dist={dist:.2f} path={len(waypoints)}")
                    if not waypoints:
                        wander_left = WANDER_STEPS
                        wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                        mode, target = "none", None
                        action = {"forward": True, "camera": [0.0, 0.0]}
                    else:
                        wx, wy, wz = waypoints.pop(0)
                        yaw, pitch = look_at(px, py, pz, wx, wy, wz, pitch_clamp=20.0)
                        env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                        jump = (wy - py) > JUMP_Y_THRESH
                        action = {"forward": True, "jump": jump, "camera": [0.0, 0.0]}

        # ---- 统一 step ----
        res = step_fn(action, ticks)
        progress = float(res["progress"])
        max_progress = max(max_progress, progress)

        # 卡死检测：非 attack 且基本没动 → 游走/重算
        if moved < 0.02 and mode != "attack" and reposition == 0:
            stuck_count += 1
        else:
            stuck_count = 0

        # 过期黑名单清理（每 30 步一次）
        if step % 30 == 0 and failed_targets:
            failed_targets = {k: v for k, v in failed_targets.items() if v > step}
        if stuck_count >= STUCK_STEPS:
            log(f"  [stuck] {STUCK_STEPS} 步未移动（mode={mode}），游走")
            wander_left = WANDER_STEPS
            wander_jump = 6
            wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
            mode, target = "none", None
            waypoints, wp = [], None
            wp_stuck = 0
            aim_cache = None
            stuck_count = 0

        if step % 20 == 0 or res.get("terminated"):
            dist_str = f"{dist:.2f}" if dist >= 0 else "-"
            log(f"step={step} progress={progress:.2f} max={max_progress:.2f} "
                f"dist={dist_str} mode={mode} logs={logs_found} jump={jump}")
        if res.get("terminated"):
            return True, step, progress
        if res.get("truncated"):
            log("M7_COLLECT_WOOD_TIMEOUT: truncated（steps 超时）", file=sys.stderr)
            return False, step, max_progress

    log(f"M7_COLLECT_WOOD_FAIL: {max_steps} 步未完成（progress={max_progress:.2f}）",
        file=sys.stderr)
    return False, max_steps, max_progress


def main() -> int:
    args = parse_args()
    env = MinecraftEnv(player=args.player, task="collect_wood", ticks_per_step=args.ticks)
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
            half_extent=args.half_extent)
        if ok:
            log(f"M7_COLLECT_WOOD_OK steps={steps} progress={max_progress:.2f}")
            return 0
        return 1
    finally:
        env.close()


if __name__ == "__main__":
    sys.exit(main())
