#!/usr/bin/env python3
"""Oracle 轨迹批量生成器（DESIGN.md §11.5，2026-08-08）。

去服务端全局 A* 的 Oracle 数据生成：每 episode reset → （可选场景摆放）→
oracle_wood_policy（双航点 goto + 局部绕障 + 语义标签）→ 录帧 → 四者计数
+ 对齐断言 → 落盘 canonical episode 目录。

用法（在 vla_env/ 目录内，避免 namespace 遮蔽）：
    .venv/bin/python -u scripts/generate_oracle_dataset.py --episodes 3 --out-dir /tmp/oracle_test
    .venv/bin/python -u scripts/generate_oracle_dataset.py --task collect_stone --episodes 5 --budget-per-target 2

输出（每个 episode 一个目录，结构见 vla_env/dataset/oracle_recorder.py）：
    <out_dir>/episodes/ep-000001/{meta.json, trajectory.jsonl, frames/*.jpg,
                                  align_assertions.jsonl, episode_summary.json}
    <out_dir>/runs.jsonl            # 每 episode 一行摘要
    <out_dir>/summary.json          # 全 run 汇总

结束打印 `ORACLE_GEN_OK episodes=N success=M align_rate=1.00` exit 0；
失败如实上报 exit 1。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

from PIL import Image

from vla_env.dataset import oracle_recorder, schema
from vla_env.env import MinecraftEnv
# 本脚本以 `python scripts/generate_oracle_dataset.py` 运行，sys.path[0]=scripts/，
# collect_wood_agent 裸导入（scripts/ 不是包）。
from collect_wood_agent import oracle_wood_policy  # noqa: E402

# 场景摆放（复用 demo_task 的 place_trees/place_stones 先例；玩家水底时先落地）
from demo_task import place_trees, place_stones  # noqa: E402

# 支持的任务（TASK_CONFIG 已参数化；collect_wood 默认）。
TASKS = ("collect_wood", "collect_stone", "kill_animal")

# 默认场景坐标池（世界 level-seed=20260808，预选地形中心；P1 场景多样性注入点）。
DEFAULT_REGIONS = [
    None,  # 缺省：玩家当前位置（reset 默认区域）
]

# 录帧线程（demo_record.recorder 模式：消费 WS 帧流，缓存最新帧供 recorder 对齐）。
def _recorder_thread(env, recorder, stop_flag) -> None:
    from vla_env.client_ws import Frame  # noqa: F401  # type: ignore[attr-defined]

    n = 0
    while not stop_flag.is_set():
        try:
            frame = env.ws.recv_frame(timeout=0.5)
        except Exception:  # noqa: BLE001 —— 断线/超时继续
            continue
        if frame is None:
            continue
        recorder.on_frame(frame)
        n += 1
    print(f"[rec] frames={n}", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Oracle 轨迹批量生成器（去 A*）")
    p.add_argument("--task", choices=list(TASKS), default="collect_wood")
    p.add_argument("--episodes", type=int, default=3, help="episode 数（默认 3）")
    p.add_argument("--out-dir", default="/tmp/oracle_gen", help="输出根目录")
    p.add_argument("--max-steps", type=int, default=600)
    p.add_argument("--ticks", type=int, default=2, help="每步服务端 tick 数")
    p.add_argument("--half-extent", type=int, default=16)
    p.add_argument("--player", default="agent0")
    p.add_argument("--capture", default="224x224",
                   help="抓帧分辨率：224x224（VLA 观测，默认）或 native（原生 framebuffer）")
    p.add_argument("--no-hud", action="store_true",
                   help="关闭 HUD 抓帧（VLA 观测默认纯净；demo 视频才开 HUD）")
    # Oracle 行为参数（P1 行为参数化注入点）
    p.add_argument("--behavior", choices=("efficient", "cautious", "aggressive"),
                   default="efficient", help="行为档位（P1：efficient=基线/现状一致，"
                                             "cautious/aggressive 放大非最优行为）")
    p.add_argument("--noise-seed", type=int, default=None, help="目标选择噪声种子（None=系统随机）")
    p.add_argument("--target-noise", type=float, default=0.3, help="目标选择噪声权重（0=确定性）")
    p.add_argument("--budget-per-target", type=int, default=3, help="同目标挖块上限（>0）")
    p.add_argument("--detour-retries", type=int, default=1, help="blocked_wall/stuck 本地绕行次数")
    p.add_argument("--dry-run", action="store_true", help="只打印 episode 计划，不采集")
    p.add_argument("--no-setup", action="store_true",
                   help="不摆树/石板（靠自然资源，explore 更长，轨迹更长）")
    return p.parse_args()


def reset_with_retry(env: MinecraftEnv, player: str, max_tries: int = 30) -> Any:
    """env.reset() 带重试（客户端/服务端刚启动可能未就绪）。"""
    for attempt in range(max_tries):
        try:
            obs, reset_info = env.reset()
            return obs
        except Exception as e:  # noqa: BLE001 —— 采集脚本需如实上报
            print(f"[reset] attempt {attempt + 1} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            time.sleep(2)
    raise RuntimeError(f"env.reset 连续 {max_tries} 次失败（客户端未就绪？）")


def _goto_nearest_tree(env: MinecraftEnv, half_extent: int) -> None:
    """玩家周围摆树失败时：找最近自然 oak_log，teleport 玩家到其旁平地。

    place_trees 依赖玩家周围有合适平地（MAX_GROUND_DROP=2），玩家在坡/树根
    密集区可能摆不出树——此时把玩家 teleport 到最近自然树旁，策略即可到达。
    """
    from collect_wood_agent import blocks_3d, find_blocks, name_at

    palette, data, origin, size = env.grpc.get_voxels(
        player=env.player, half_extent=half_extent)
    b3 = blocks_3d(palette, data, size)
    logs = find_blocks(palette, data, origin, {"minecraft:oak_log"})
    st = env.grpc.get_state(player=env.player)
    px, py, pz = (float(v) for v in st["player"]["pos"])
    if not logs:
        print("[tree-nav] 32³ 内无自然树，保持原地", flush=True)
        return
    # 最近原木
    log = min(logs, key=lambda b: (b[0] - px) ** 2 + (b[1] - py) ** 2 + (b[2] - pz) ** 2)
    lx, ly, lz = log
    # 找该树旁平地：原木下方地面的相邻水平格（脚格=空气、脚下=实心）
    best = None
    best_d = float("inf")
    for dx in range(-3, 4):
        for dz in range(-3, 4):
            if dx == 0 and dz == 0:
                continue
            x, z = lx + dx, lz + dz
            # 从树底往下找地面
            for y in range(ly - 1, ly - 8, -1):
                foot = name_at(b3, origin, (x, y, z))
                below = name_at(b3, origin, (x, y - 1, z))
                if foot == "minecraft:air" and below not in (None, "minecraft:air",
                                                             "minecraft:water",
                                                             "minecraft:lava",
                                                             "minecraft:oak_leaves",
                                                             "minecraft:oak_log"):
                    d = dx * dx + dz * dz
                    if d < best_d:
                        best_d = d
                        best = (x + 0.5, y, z + 0.5)
                    break
    if best is None:
        print(f"[tree-nav] 树 {log} 旁找不到平地，保持原地", flush=True)
        return
    env.grpc.teleport(player=env.player, pos=best)
    time.sleep(0.8)
    fresh = env.grpc.get_state(player=env.player)
    print(f"[tree-nav] teleport 到最近树 {log} 旁平地 {best}（新位置 {fresh['player']['pos']}）",
          flush=True)


def _ensure_grounded(env: MinecraftEnv, half_extent: int) -> None:
    """出生点校准：玩家在坑里/低洼（y 显著低于周围地面）时，teleport 到附近平地最高点。

    Oracle 生成器需要玩家在平地出发（树在可达范围）。玩家掉坑里（y 低 3+ 格）
    时 goto 无法爬出，会卡死空挥。此函数扫描 32³ 找「周围地面的代表高度」，
    若玩家 y 低于代表高度 3+ 格 → teleport 到最近平地格。
    水/岩浆不做处理（策略溺水自救覆盖）。
    """
    st = env.grpc.get_state(player=env.player)
    px, py, pz = (float(v) for v in st["player"]["pos"])
    palette, data, origin, size = env.grpc.get_voxels(
        player=env.player, half_extent=half_extent)
    from collect_wood_agent import blocks_3d, name_at

    b3 = blocks_3d(palette, data, size)
    ipx, ipy, ipz = int(px), int(py), int(pz)
    # 玩家脚格若是水/岩浆 → 不校准（溺水自救处理）
    feet_name = name_at(b3, origin, (ipx, ipy, ipz))
    if feet_name in ("minecraft:water", "minecraft:lava"):
        print(f"[ground] 脚格={feet_name}，溺水自救覆盖，不校准", flush=True)
        return

    # 收集周围（水平 ±8）地面高度（最高实心格 y，非空气/水/树叶/原木）
    ground_ys = []
    for dx in range(-8, 9, 4):
        for dz in range(-8, 9, 4):
            for y in range(ipy + 6, ipy - 8, -1):
                n = name_at(b3, origin, (ipx + dx, y, ipz + dz))
                if n not in (None, "minecraft:air", "minecraft:water", "minecraft:lava",
                             "minecraft:oak_leaves", "minecraft:oak_log"):
                    ground_ys.append(y)
                    break
    if not ground_ys:
        print("[ground] 找不到周围地面，跳过校准", flush=True)
        return
    ref = max(set(ground_ys), key=ground_ys.count)  # 代表高度（众数）
    if ref - py < 3.0:
        print(f"[ground] 玩家 y={py:.0f} 接近地面 {ref}，无需校准", flush=True)
        return

    # 找最近平地格：脚格=空气、脚下=实心、y ≈ ref
    best = None
    best_d = float("inf")
    for dx in range(-12, 13):
        for dz in range(-12, 13):
            x, z = ipx + dx, ipz + dz
            foot = name_at(b3, origin, (x, ref, z))
            below = name_at(b3, origin, (x, ref - 1, z))
            if foot == "minecraft:air" and below not in (None, "minecraft:air",
                                                         "minecraft:water", "minecraft:lava"):
                d = dx * dx + dz * dz
                if d < best_d:
                    best_d = d
                    best = (x + 0.5, ref, z + 0.5)
    if best is None:
        print("[ground] 找不到平地格，跳过校准", flush=True)
        return
    env.grpc.teleport(player=env.player, pos=best)
    time.sleep(0.8)
    fresh = env.grpc.get_state(player=env.player)
    print(f"[ground] 坑里(y={py:.0f} < 地面{ref}) → teleport 到 {best}（新位置 {fresh['player']['pos']}）",
          flush=True)


def main() -> int:
    args = parse_args()
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    runs_path = os.path.join(out_dir, "runs.jsonl")

    if args.dry_run:
        print(f"ORACLE_DRY_RUN task={args.task} episodes={args.episodes} "
              f"out={out_dir} budget={args.budget_per_target} noise={args.target_noise}")
        return 0

    env = MinecraftEnv(player=args.player, task=args.task, ticks_per_step=args.ticks)
    totals = {"episodes": 0, "success": 0, "failed": 0, "frames": 0,
              "align_ok": 0, "align_fail": 0}
    try:
        for ep in range(1, args.episodes + 1):
            episode_id = f"ep-{ep:06d}"
            ep_dir = os.path.join(out_dir, "episodes", episode_id)
            print(f"\n===== {episode_id} task={args.task} =====", flush=True)

            obs = reset_with_retry(env, args.player)
            spawn_pos = [float(v) for v in obs["player"]["pos"]]

            # 出生点校准：玩家在坑里/低洼（y 显著低于周围地面）时 teleport 到附近平地
            _ensure_grounded(env, args.half_extent)

            # 摆场景：collect_wood 摆树、collect_stone 摆石板（确定性目标；P1 场景池注入点）
            # --no-setup 时不摆（自然资源，轨迹更长）
            if not args.no_setup:
                if args.task == "collect_wood":
                    n = place_trees(env, args.half_extent)
                    if n < 3:
                        # place_trees 找地面失败（玩家在坡/树根，周围无平地）→
                        # 不强摆（周围没地方），改为 teleport 玩家到最近自然树旁，
                        # 保证玩家可达目标（比原地摆树可靠）
                        print(f"[setup] placed {n} trees（不足，teleport 到最近自然树）", flush=True)
                        _goto_nearest_tree(env, args.half_extent)
                        n2 = place_trees(env, args.half_extent)  # 新位置再摆一轮
                        print(f"[setup] 新位置补摆 {n2} trees", flush=True)
                    else:
                        print(f"[setup] placed {n} trees", flush=True)
                elif args.task == "collect_stone":
                    n = place_stones(env, args.half_extent)
                    print(f"[setup] placed {n} stone slabs", flush=True)

            # kill_animal：reset 后生成猪（只生成一次）
            if args.task == "kill_animal":
                env.grpc.spawn_entity(player=args.player, entity_type="minecraft:pig", count=2)
                time.sleep(0.5)

            # 抓帧分辨率 + HUD（VLA 观测：224×224 纯净画面）
            if args.capture.lower() == "native":
                env.ws.send({"cmd": "set_capture", "width": 0, "height": 0})
            else:
                w, h = (int(x) for x in args.capture.lower().split("x"))
                env.ws.send({"cmd": "set_capture", "width": w, "height": h})
            time.sleep(0.5)
            env.ws.send({"cmd": "set_capture_ui", "hud": not args.no_hud})
            time.sleep(0.3)
            # 排空旧分辨率帧
            for _ in range(20):
                if env.ws.recv_frame(timeout=0.5) is None:
                    break

            # 录帧线程 + recorder
            recorder = oracle_recorder.StepRecorder(
                ep_dir, ticks_per_step=args.ticks, tol=2)
            stop_flag = threading.Event()
            rec_thread = threading.Thread(
                target=_recorder_thread, args=(env, recorder, stop_flag), daemon=True)
            rec_thread.start()

            def step_fn(action, ticks):
                """录帧版 step：发动作 + gRPC 结算，不读 WS 帧（录帧线程在消费）。"""
                env.ws.send_action(action)
                step = env.grpc.get_step_result(player=env.player, await_ticks=ticks)
                return {"progress": float(step["progress"]), "terminated": step["terminated"],
                        "truncated": step["truncated"], "reward": step["reward"],
                        "server_tick": int(step["server_tick"])}

            oracle_cfg = {
                "policy_version": "oracle_v1",
                "behavior": args.behavior,
                "ticks_per_step": args.ticks,
                "half_extent": args.half_extent,
                "max_steps": args.max_steps,
                "noise_seed": args.noise_seed,
                "target_noise": args.target_noise,
                "budget_per_target": args.budget_per_target,
                "detour_retries": args.detour_retries,
                "engine": {"client_goto": True, "local_detour": True,
                           "python_dig": True, "wander": True, "swim_escape": True},
            }

            ok, steps, max_progress = oracle_wood_policy(
                env, step_fn, max_steps=args.max_steps, ticks=args.ticks,
                half_extent=args.half_extent, task=args.task,
                recorder=recorder, oracle_cfg=oracle_cfg, spawn_pos=tuple(spawn_pos))

            # 收尾：停录帧，稍等帧流排空
            time.sleep(0.5)
            stop_flag.set()
            rec_thread.join(timeout=3)

            # meta.json（种子/配置/版本）
            meta = schema.oracle_meta(
                episode_id=episode_id,
                task=args.task,
                world_seed=20260808,
                task_seed=ep,
                reset_seed=ep,
                oracle_cfg=oracle_cfg,
                spawn_pos=spawn_pos,
                frame_count=recorder.counters["frames"],
                server_tick_start=0,
                server_tick_end=0,
                versions={"vla_env": "0.1.0", "oracle": "oracle_v1"},
                render={"res": 224 if args.capture == "224x224" else -1,
                        "fov": 70, "fps": 20, "hud": not args.no_hud},
                extra={"success": ok, "steps": steps, "max_progress": round(max_progress, 4)},
            )
            summary = recorder.finalize(meta, success=ok)

            # 四者计数 + 对齐断言（M8 口径）
            counts_ok = summary["counts_consistent"]
            align_ok = summary["align_rate"] == 1.0 and summary["mismatch"] == 0
            totals["episodes"] += 1
            totals["success"] += 1 if ok else 0
            totals["failed"] += 0 if ok else 1
            totals["frames"] += summary["frames"]
            totals["align_ok"] += 1 if align_ok else 0
            totals["align_fail"] += 0 if align_ok else 1

            run_row = {
                "episode_id": episode_id,
                "task": args.task,
                "success": ok,
                "steps": steps,
                "max_progress": round(max_progress, 4),
                "frames": summary["frames"],
                "actions": summary["actions"],
                "rewards": summary["rewards"],
                "states": summary["states"],
                "counts_consistent": counts_ok,
                "align_rate": summary["align_rate"],
                "mismatch": summary["mismatch"],
                "intent_counts": summary["intent_counts"],
            }
            with open(runs_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(run_row, ensure_ascii=False) + "\n")

            print(f"[ep{ep}] success={ok} steps={steps} frames={summary['frames']} "
                  f"counts={counts_ok} align_rate={summary['align_rate']} "
                  f"intents={sorted(summary['intent_counts'].items())}",
                  flush=True)
            if not ok:
                print(f"[ep{ep}] 未完成（progress={max_progress:.2f}）——失败轨迹保留（反事实数据）",
                      flush=True)
    except Exception as e:  # noqa: BLE001 —— 如实上报
        import traceback
        traceback.print_exc()
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        print("ORACLE_GEN_FAIL (exception)", file=sys.stderr)
        return 1
    finally:
        env.close()

    # 汇总
    summary_path = os.path.join(out_dir, "summary.json")
    agg = {
        "task": args.task,
        "episodes": totals["episodes"],
        "success": totals["success"],
        "failed": totals["failed"],
        "frames": totals["frames"],
        "align_ok": totals["align_ok"],
        "align_fail": totals["align_fail"],
        "success_rate": round(totals["success"] / totals["episodes"], 4) if totals["episodes"] else 0.0,
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=2)
    print("---- ORACLE summary ----")
    print(json.dumps(agg, ensure_ascii=False, indent=2))

    all_ok = totals["align_fail"] == 0 and totals["episodes"] > 0
    if all_ok:
        print(f"ORACLE_GEN_OK episodes={totals['episodes']} success={totals['success']} "
              f"align_rate=1.00")
        return 0
    print(f"ORACLE_GEN_PARTIAL episodes={totals['episodes']} success={totals['success']} "
          f"align_fail={totals['align_fail']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
