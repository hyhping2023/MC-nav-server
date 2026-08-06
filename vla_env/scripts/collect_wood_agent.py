#!/usr/bin/env python3
"""M7 验收：collect_wood_agent —— 脚本策略完成 collect_wood（gymnasium 端到端）。

用法（在 vla_env/ 目录内，避免 namespace 遮蔽）：
    .venv/bin/python scripts/collect_wood_agent.py [--max-steps 600] [--ticks 2]

策略（吸取"跳挖卡住"教训，核心原则）：
  1. **挖木期不动、不跳、不重瞄**：进入 attack 后先"站稳"（settle，只发相机不动）
     若干步，再锁定目标方块，连续按住 attack。任何移动/跳跃都会让准星漂移导致
     方块破坏进度清零（crosshair 换目标 → stopDestroyBlock）。
  2. **目标粘滞**：不每 2 步重扫换目标；attack 期间每 N 步才确认目标是否还在
     （被挖掉就换下一个），避免两个相邻原木之间来回切换永远挖不掉。
  3. **挖完停顿**：方块破坏后掉落物从眼前下落会挡住射线（crosshair 命中物品实体
     暂停破坏），给 2 步间隔再继续。
  4. **逼近受阻即换树**：approach 撞树/卡住时游走换下一棵，不硬钻树冠。

成功判定：terminated → `M7_COLLECT_WOOD_OK steps=N` exit 0。
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from typing import List, Optional, Tuple

import numpy as np

from vla_env.env import MinecraftEnv

OAK_LOG = "minecraft:oak_log"
EYE_HEIGHT = 1.62  # 站立眼睛高度
RES = 224
REACH = 4.0       # 挖掘可达阈值（原版 4.5，留余量；太近也不行）
REACH_MIN_H = 0.6 # 水平距离过近时准星顶在脸上，视为需后退/换目标
WANDER_STEPS = 14
SETTLE_STEPS = 3  # 进入 attack 后先停几拍（只相机，不动不挖），让位移惯性消失
DIG_SCAN_EVERY = 12   # attack 期间每隔 N 步确认目标是否仍在
MAX_DIG_TRY = 90      # 同一目标连续挖 N 步仍未被破坏则放弃（可能被掉落物挡/准星偏）


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M7 collect_wood 脚本策略验收")
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
) -> Tuple[str, Optional[Tuple[int, int, int]]]:
    """目标选择：优先可达 log（attack），否则最近可接近 log（approach），无 → none。

    可达判定：3D 距离 ≤REACH 且水平距离 ∈[REACH_MIN_H, 3.0]（太近准星贴脸）、
    高度差 ≤2.6（眼位起算）。

    approach 候选**必须高度差 ≤2.6**：只有这类 log 玩家水平走近后能进入可达集。
    只挑"最近 3D"会把目标定在树顶/树冠 log（dy 远超 2.6，怎么走都进不了可达集），
    导致永远 approach、卡死（M7 实测 target=(68,73,-46) 距地面 5 格反复卡住）。
    """
    eye_y = py + EYE_HEIGHT
    reachable: List[Tuple[float, int, int, int]] = []
    for (bx, by, bz) in logs:
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
        # approach：先过滤"走近后有望可达"（|dy| ≤ 2.6），再按水平距离排序；
        # 全都不满足（罕见）退化为水平最近。
        candidates = [b for b in logs if abs(b[1] + 0.5 - eye_y) <= 2.6]
        if not candidates:
            candidates = logs
        n = min(
            candidates,
            key=lambda b: (b[0] + 0.5 - px) ** 2 + (b[2] + 0.5 - pz) ** 2,
        )
        return "approach", n
    return "none", None


def look_at(
    px: float, py: float, pz: float,
    bx: int, by: int, bz: int,
    pitch_clamp: Optional[float] = None,
) -> Tuple[float, float]:
    """玩家 pos 与目标块中心，返回 (yaw, pitch)（MC 约定：yaw=0 南方(+Z)，
    pitch 正为向下）。pitch_clamp 非空时夹紧 |pitch| ≤ 该值（approach 用）。

    M7 修：pitch 必须从**眼位**起算（py + EYE_HEIGHT）。crosshair 射线从眼睛
    发出，从脚底起算会整体偏高一截（≈atan2(1.62, h)），目标在眼位上方时准星
    打在目标块上方的相邻块/树叶上，挖掘永不命中（客户端 crosshair 实测打在
    target 上方）。
    """
    cx, cy, cz = bx + 0.5, by + 0.5, bz + 0.5
    dx, dy, dz = cx - px, cy - (py + EYE_HEIGHT), cz - pz
    h = math.hypot(dx, dz)
    yaw = math.degrees(math.atan2(-dx, dz))
    pitch = math.degrees(math.atan2(-dy, h)) if h > 1e-6 else 0.0
    if pitch_clamp is not None:
        pitch = max(-pitch_clamp, min(pitch_clamp, pitch))
    return yaw, pitch


def dist3(px, py, pz, bx, by, bz) -> float:
    """玩家 pos 到目标块中心的 3D 距离。"""
    return math.sqrt(
        (bx + 0.5 - px) ** 2 + (by + 0.5 - py) ** 2 + (bz + 0.5 - pz) ** 2
    )


def main() -> int:
    args = parse_args()

    env = MinecraftEnv(player=args.player, task="collect_wood", ticks_per_step=args.ticks)
    try:
        obs = None
        for attempt in range(30):
            try:
                obs, reset_info = env.reset()
                break
            except Exception as e:  # noqa: BLE001
                print(f"[reset] attempt {attempt + 1} failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
                time.sleep(2)
        if obs is None:
            print("M7_FAIL: env.reset 30 次未成功（客户端未就绪？）", file=sys.stderr)
            return 1

        pov = obs["pov"]
        assert pov.shape == (RES, RES, 3), f"pov shape 应为 (224,224,3)，实为 {pov.shape}"
        player_pos = obs["player"]["pos"]
        assert len(player_pos) == 3, f"player.pos 应为 [x,y,z]，实为 {player_pos}"
        print(f"[reset] OK ep={obs['agent']['episode_id']} pov={pov.shape} "
              f"pos=({player_pos[0]:.1f},{player_pos[1]:.1f},{player_pos[2]:.1f})")

        # 策略状态
        mode = "none"                       # none / approach / attack
        target: Optional[Tuple[int, int, int]] = None
        target_age = 0
        settle_steps = 0                    # attack 前的站稳倒计时
        dig_try = 0                         # 同一目标连续挖的步数
        after_break_pause = 0               # 挖掉后的停顿（让掉落物落下）
        wander_left = 0
        wander_yaw = 0.0
        wander_jump = 0                     # 卡死逃逸时的短暂跳跃步数
        stuck_count = 0
        last_pos = tuple(player_pos)
        max_progress = 0.0
        logs_found = 0
        aim_cache: Optional[Tuple[float, float]] = None   # attack 期锁定一次瞄准

        for step in range(1, args.max_steps + 1):
            state = env.grpc.get_state(player=args.player)
            px, py, pz = (float(v) for v in state["player"]["pos"])
            player_pos = (px, py, pz)
            dist = -1.0

            # ---- 停顿阶段：不挖不动，等掉落物下落 ----
            if after_break_pause > 0:
                after_break_pause -= 1
                action = {"camera": [0.0, 0.0]}
                mode = "none"
                target = None

            # ---- 游走 ----
            elif wander_left > 0:
                wander_left -= 1
                env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                # 仅卡死逃逸时短暂跳跃（前 wander_jump 步），正常游走不跳防爬树冠
                if wander_jump > 0:
                    wander_jump -= 1
                    action = {"forward": True, "jump": True, "camera": [0.0, 0.0]}
                else:
                    action = {"forward": True, "camera": [0.0, 0.0]}
                mode = "none"

            # ---- attack 挖掘 ----
            elif mode == "attack" and target is not None:
                bx, by, bz = target
                dist = dist3(px, py, pz, bx, by, bz)
                dig_try += 1
                target_age += 1

                # 站稳后若玩家漂移出可达距离（斜坡下滑/惯性），放弃攻击退回 approach，
                # 而不是空挖 90 步浪费（M7 实测 attack 中 dist 3.93→4.58 全程 0 进度）。
                if settle_steps >= SETTLE_STEPS and dist > REACH + 0.2:
                    print(f"  [dig] drifted out of reach dist={dist:.2f}, re-approach")
                    mode = "approach"
                    dig_try = 0
                    settle_steps = 0
                    aim_cache = None
                    yaw, pitch = look_at(px, py, pz, bx, by, bz, pitch_clamp=30.0)
                    env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                    action = {"forward": True, "camera": [0.0, 0.0]}

                # 周期性确认目标还在（被挖掉/被其他玩家破坏则换）
                elif dig_try % DIG_SCAN_EVERY == 0:
                    palette, data, origin, size = env.grpc.get_voxels(
                        player=args.player, half_extent=args.half_extent)
                    logs = find_logs(palette, data, origin)
                    if target not in logs:
                        # 目标消失 = 已挖掉（或异常），停顿让掉落物落下再继续
                        print(f"  [dig] target {target} gone after {dig_try} tries")
                        after_break_pause = 2
                        mode = "none"
                        target = None
                        aim_cache = None
                        action = {"camera": [0.0, 0.0]}
                        env.ws.send({"cmd": "reset_camera", "yaw": 0.0, "pitch": 0.0})
                    else:
                        action = {"attack": True, "camera": [0.0, 0.0]}

                elif dig_try > MAX_DIG_TRY:
                    # 长时间没挖掉（可能准星被物品挡/角度歪），换个目标
                    print(f"  [dig] give up target {target} after {MAX_DIG_TRY} tries")
                    mode = "none"
                    target = None
                    aim_cache = None
                    action = {"camera": [0.0, 0.0]}

                elif settle_steps < SETTLE_STEPS:
                    # 站稳：只发相机（瞄准一次），不动不挖
                    if settle_steps == 0:
                        yaw, pitch = look_at(px, py, pz, bx, by, bz)
                        env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                        aim_cache = (yaw, pitch)
                    settle_steps += 1
                    action = {"camera": [0.0, 0.0]}

                else:
                    # 站稳完成：锁定瞄准，连续按住 attack，不重瞄不动不跳
                    action = {"attack": True, "camera": [0.0, 0.0]}

            # ---- 无目标：重扫/选择 ----
            else:
                palette, data, origin, size = env.grpc.get_voxels(
                    player=args.player, half_extent=args.half_extent)
                logs = find_logs(palette, data, origin)
                logs_found = len(logs)
                if step % 10 == 1:
                    print(f"[voxels] size={size} origin={origin} logs_found={logs_found}")
                new_mode, new_target = select_target(logs, px, py, pz)
                if new_mode == "none":
                    wander_left = WANDER_STEPS
                    wander_yaw = float(state["player"].get("yaw", 0.0)) + random.uniform(-30.0, 30.0)
                    env.ws.send({"cmd": "reset_camera", "yaw": float(wander_yaw), "pitch": 0.0})
                    action = {"forward": True, "camera": [0.0, 0.0]}
                    mode = "none"
                    target = None
                    print(f"  [explore] no logs in voxels, wander {WANDER_STEPS} steps")
                else:
                    mode, target = new_mode, new_target
                    bx, by, bz = target
                    dist = dist3(px, py, pz, bx, by, bz)
                    if mode == "attack":
                        settle_steps = 0
                        dig_try = 0
                        aim_cache = None
                        yaw, pitch = look_at(px, py, pz, bx, by, bz)
                        env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                        aim_cache = (yaw, pitch)
                        settle_steps = 1  # 本步算站稳第 1 拍
                        action = {"camera": [0.0, 0.0]}
                        print(f"  [attack] target={target} dist={dist:.2f}")
                    else:  # approach —— 只走不跳，防止跳进树冠爬高
                        yaw, pitch = look_at(px, py, pz, bx, by, bz, pitch_clamp=30.0)
                        env.ws.send({"cmd": "reset_camera", "yaw": float(yaw), "pitch": float(pitch)})
                        action = {"forward": True, "camera": [0.0, 0.0]}
                        print(f"  [approach] target={target} dist={dist:.2f}")

            obs, reward, terminated, truncated, info = env.step(action, ticks=args.ticks)
            progress = float(info.get("progress", 0.0))
            max_progress = max(max_progress, progress)

            assert isinstance(reward, float), f"reward 应为 float，实为 {type(reward)}"
            assert isinstance(terminated, bool), f"terminated 应为 bool，实为 {type(terminated)}"
            assert isinstance(truncated, bool), f"truncated 应为 bool，实为 {type(truncated)}"
            assert isinstance(info, dict), f"info 应为 dict，实为 {type(info)}"
            assert "progress" in info and "server_tick" in info, "info 缺 progress/server_tick"

            dist_str = f"{dist:.2f}" if dist >= 0 else "-"
            print(f"step={step} progress={progress:.2f} max={max_progress:.2f} "
                  f"dist={dist_str} mode={mode} logs={logs_found} reward={reward:.1f} "
                  f"tick={info['server_tick']}")

            if progress < max_progress - 1e-9:
                print(f"  [warn] progress 回退 {max_progress:.2f} -> {progress:.2f}")

            # 卡死检测：位置基本没动 → 游走
            moved = math.hypot(px - last_pos[0], pz - last_pos[2])
            last_pos = (px, py, pz)
            if moved < 0.02 and mode != "attack" and after_break_pause == 0:
                stuck_count += 1
            else:
                stuck_count = 0
            if stuck_count >= 20:
                print(f"  [stuck] 20 步未移动（mode={mode} dist={dist_str}），游走")
                wander_left = WANDER_STEPS
                wander_jump = 6
                wander_yaw = float(state["player"].get("yaw", 0.0)) + 90.0
                mode = "none"
                target = None
                aim_cache = None
                stuck_count = 0

            if terminated:
                print(f"M7_COLLECT_WOOD_OK steps={step} progress={progress:.2f}")
                return 0
            if truncated:
                print("M7_COLLECT_WOOD_TIMEOUT: truncated（steps 超时）", file=sys.stderr)
                return 1

        print(f"M7_COLLECT_WOOD_FAIL: {args.max_steps} 步未完成（progress={max_progress:.2f}）",
              file=sys.stderr)
        return 1
    finally:
        env.close()


if __name__ == "__main__":
    sys.exit(main())
