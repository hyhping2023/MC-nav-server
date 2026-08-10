#!/usr/bin/env python3
"""M11.5 人类式按键录制 demo（DESIGN.md §17，编排器版）。

按 --task 用固定生存工具包（镐/剑/铲/泥土，hotbar 0-3）**模拟人类执行**操作，
录制 frame↔按键对齐的数据。架构（§17.2）：客户端为手（NavExecutor/PillarExecutor
+ Humanizer 人类化整形逐 tick 合成按键），Python KitAgent 为脑（选目标/粗航点/
技能派发/脱困决策），服务端为世界与裁判（任务判定/奖励/粗航点/确定性重置）。

用法（在 vla_env/ 目录内，避免 namespace 遮蔽）：
    .venv/bin/python -u scripts/demo_human.py [outdir] --task dig_stone --seed 42
    .venv/bin/python -u scripts/demo_human.py [outdir] --task kill_animal --seed 11 --tail-seconds 12
    .venv/bin/python -u scripts/demo_human.py --replay <episode_dir>   # 种子回放

任务（kit 局限：镐/剑/铲/泥土）：
    dig_stone   → collect_stone（挖 8 石头，镐）
    dig_dirt    → dig_dirt（挖 6 泥土，铲）
    kill_animal → kill_animal（杀 2 猪，剑）
    place_dirt  → place_dirt（放置 3 泥土）

输出（outdir + 同名 mp4，契约见 §17.4）：
    meta.json / trajectory.jsonl（逐帧按键状态=训练对）/ frames/*.jpg /
    keys.jsonl（离散按/抬事件）/ actions.jsonl（语义编排流）/ state.jsonl /
    align_assertions.jsonl / episode_summary.json
结尾打印 `HUMAN_DEMO_OK task=... seed=... steps=... progress=... align_ok=...`。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

from vla_env.interact import SeedReplayApi
from vla_env.dataset.human_recorder import HumanRecorder
from vla_env.orchestrator import KitAgent
from vla_env.tasks import PROFILES, SURVIVAL_KIT, get_profile
from vla_env.action_space import BUTTONS

# 本脚本以 `python scripts/demo_human.py` 运行，sys.path[0]=scripts/，
# 故 demo_task / collect_wood_agent 是裸导入（scripts/ 不是包）。
from demo_task import _ground_y, compose_mp4  # noqa: E402
from collect_wood_agent import blocks_3d, name_at  # noqa: E402

SETUP_OFFSETS = [(9, 0), (0, -9), (-9, 0), (0, 9), (-8, 8), (8, -8), (7, 7), (-7, -7)]
MAX_GROUND_DROP = 2


def _count_reachable(api, block_id: str, half_extent: int) -> int:
    """玩家 nav 可达高差窗口内的指定方块数（供给判断用）。

    M11.6：窗口从 |dy|<=6 收紧到 |dy|<=3——与客户端 nav 能力（fall<=3 / step_up<=1）
    对齐。否则谷底/悬崖下的石头会被算成"可达"→ 不触发人工供给 → `_select_target`
    只能选走不过去的高度 → 每轮 STUCK 原地来回跳（dig_stone progress=0 实测根因）。
    """
    st = api.grpc.get_state(player=api.player)
    py = float(st["player"]["pos"][1])
    palette, data, origin, size = api.grpc.get_voxels(
        player=api.player, half_extent=half_extent)
    wanted = {i for i, b in enumerate(palette) if b.split("[")[0] == block_id}
    if not wanted:
        return 0
    n = 0
    for iy in range(size):
        by = origin[1] + iy
        if abs(by - py) > 3:
            continue
        plane = data[iy]
        for v in plane.flat:
            if int(v) in wanted:
                n += 1
    return n


def _ensure_pigs(api, rng, half_extent: int, count: int = 2) -> int:
    """确定性 spawn 猪（reset 冻结 doMobSpawning，自然不生成；seed 决定偏移顺序）。"""
    st = api.grpc.get_state(player=api.player)
    px, py, pz = (float(v) for v in st["player"]["pos"])
    palette, data, origin, size = api.grpc.get_voxels(
        player=api.player, half_extent=half_extent)
    blocks3 = blocks_3d(palette, data, size)
    px, pz, py = int(px), int(pz), int(py)
    offsets = list(SETUP_OFFSETS)
    rng.shuffle(offsets)
    spawned = 0
    for dx, dz in offsets:
        if spawned >= count:
            break
        x, z = px + dx, pz + dz
        gy = _ground_y(blocks3, origin, x, z, py)
        if gy is None or abs(gy - py) > MAX_GROUND_DROP:
            continue
        if name_at(blocks3, origin, (x, gy + 1, z)) not in (None, "minecraft:air"):
            continue
        api.grpc.spawn_entity(player=api.player, entity_type="minecraft:pig",
                              pos=(x + 0.5, gy + 1, z + 0.5), count=1)
        spawned += 1
        print(f"  [provision] pig#{spawned} at ({x}, {gy + 1}, {z})", flush=True)
    return spawned


def _ensure_stone(api, rng, half_extent: int, need: int = 8) -> int:
    """确定性放置石头 slab（自然可挖石头不足 need 时；seed 决定偏移顺序）。"""
    st = api.grpc.get_state(player=api.player)
    px, py, pz = (float(v) for v in st["player"]["pos"])
    palette, data, origin, size = api.grpc.get_voxels(
        player=api.player, half_extent=half_extent)
    blocks3 = blocks_3d(palette, data, size)
    px, pz, py = int(px), int(pz), int(py)
    offsets = list(SETUP_OFFSETS)
    rng.shuffle(offsets)
    placed = 0
    for dx, dz in offsets:
        if placed >= need:
            break
        x, z = px + dx, pz + dz
        gy = _ground_y(blocks3, origin, x, z, py)
        if gy is None or abs(gy - py) > MAX_GROUND_DROP:
            continue
        if name_at(blocks3, origin, (x, gy + 1, z)) not in (None, "minecraft:air"):
            continue
        for sx in (-1, 0, 1):
            for sz in (-1, 0, 1):
                api.grpc.set_block(player=api.player, pos=(x + sx, gy + 1, z + sz),
                                   block="minecraft:stone")
        placed += 9
        print(f"  [provision] stone slab at ({x}, {gy + 1}, {z})", flush=True)
    return placed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M11.5 人类式按键录制 demo（编排器版）")
    p.add_argument("outdir", nargs="?", default=None,
                   help="输出目录（默认 datasets/demo_human/<task>_<seed>_<ts>）")
    p.add_argument("--task", choices=list(PROFILES), default="dig_stone",
                   help="dig_stone（镐挖石）/ dig_dirt（铲挖土）/ kill_animal（剑杀猪）/ "
                        "place_dirt（放置泥土）")
    p.add_argument("--seed", type=int, default=42, help="episode 种子（同 seed 确定性可回放）")
    p.add_argument("--max-steps", type=int, default=600)
    p.add_argument("--tail-seconds", type=float, default=10.0,
                   help="任务完成后额外录制 N 秒收尾环视（慢速转镜，加长 demo 视频）；0 关闭")
    p.add_argument("--ticks", type=int, default=2)
    p.add_argument("--half-extent", type=int, default=16)
    p.add_argument("--player", default="agent0")
    p.add_argument("--spawn", default=None,
                   help="自定义出生点 x,y,z[,yaw]（M11.5 难点③），如 --spawn -20,68,-140")
    p.add_argument("--capture", default="native",
                   help="抓帧分辨率：native=游戏 framebuffer 原始分辨率（推荐）或 WxH")
    p.add_argument("--no-hud", action="store_true", help="关闭 HUD 抓帧")
    p.add_argument("--no-humanize", action="store_true",
                   help="关闭客户端人类化整形（纯执行器节奏）")
    p.add_argument("--no-provision", action="store_true",
                   help="不人工放置目标（纯自然资源，不保证任务可完成）")
    p.add_argument("--replay", metavar="EPISODE_DIR", default=None,
                   help="种子回放：从 keys.jsonl 重建 tick 级按键序列并重放（同 seed 确定性）")
    return p.parse_args()


# ---- 种子回放：keys.jsonl（按/抬事件）+ trajectory（camera Δ）→ tick 级动作序列 ----

def reconstruct_tick_actions(episode_dir: Path) -> list:
    """从录制数据重建 tick 级动作序列（§17.4 回放契约）。

    按键电平：keys.jsonl 的 down/up 事件按 tick 展开为电平；
    相机增量：trajectory.jsonl 帧级 yaw/pitch Δ 按 server_tick 聚合；
    hotbar：`hotbar:N` 事件在对应 tick 发一次。
    """
    key_rows = [json.loads(l) for l in
                (episode_dir / "keys.jsonl").read_text(encoding="utf-8").splitlines() if l]
    traj_rows = [json.loads(l) for l in
                 (episode_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines() if l]
    if not traj_rows:
        return []
    cam_by_tick = defaultdict(lambda: [0.0, 0.0])
    for r in traj_rows:
        cam = r.get("keys", {}).get("camera", [0.0, 0.0])
        t = int(r.get("server_tick", 0))
        cam_by_tick[t][0] += float(cam[0])
        cam_by_tick[t][1] += float(cam[1])
    events_by_tick = defaultdict(list)
    for e in key_rows:
        events_by_tick[int(e.get("tick", 0))].append(e)
    t0 = int(traj_rows[0]["server_tick"])
    t1 = int(traj_rows[-1]["server_tick"])
    level = {k: False for k in ("forward", "back", "left", "right", "jump", "sneak",
                                "sprint", "attack", "use", "drop", "inventory")}
    actions = []
    for t in range(t0, t1 + 1):
        hotbar = -1
        for e in events_by_tick.get(t, ()):
            key = str(e.get("key", ""))
            if key.startswith("hotbar:"):
                hotbar = int(key.split(":", 1)[1])
            elif key in level:
                level[key] = bool(e.get("down"))
        a = dict(level)
        a["hotbar"] = hotbar
        a["camera"] = cam_by_tick.get(t, [0.0, 0.0])
        actions.append(a)
    return actions


def run_replay(episode_dir: str, args: argparse.Namespace) -> int:
    """种子回放：同 seed 重置世界 → tick 级重放录制按键 → 报告进度。"""
    ed = Path(episode_dir)
    if not (ed / "meta.json").exists():
        print(f"REPLAY_FAIL: {ed}/meta.json 不存在", file=sys.stderr)
        return 1
    meta = json.loads((ed / "meta.json").read_text(encoding="utf-8"))
    actions = reconstruct_tick_actions(ed)
    if not actions:
        print("REPLAY_FAIL: 无法从 keys/trajectory 重建动作序列", file=sys.stderr)
        return 1
    seed = meta.get("seed", 0)
    task_id = meta.get("task_id", "collect_stone")
    spawn = meta.get("spawn")

    api = SeedReplayApi(player=args.player, ticks_per_step=1)
    try:
        frame, obs = api.reset(seed=seed, task=task_id, items=SURVIVAL_KIT, spawn=spawn)
        n = 0
        progress = 0.0
        frames = 0
        for action in actions:
            frame, obs, reward, terminated, truncated, info = api.step(action, ticks=1)
            progress = float(info.get("progress", progress))
            n += 1
            if frame is not None:
                frames += 1
            if terminated or truncated:
                break
        print(f"REPLAY_OK episode={episode_dir} seed={seed} task={task_id} "
              f"actions={len(actions)} replayed={n} frames={frames} "
              f"progress={progress:.2f}", flush=True)
        return 0
    finally:
        api.close()


def main() -> int:
    args = parse_args()
    if args.replay:
        return run_replay(args.replay, args)

    profile = get_profile(args.task)
    task = profile.task_id
    spawn = None
    if args.spawn:
        spawn = [float(v) for v in args.spawn.split(",")]
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                        "datasets", "demo_human"))
    outdir = args.outdir or os.path.join(
        root, f"{args.task}_{args.seed}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(outdir, exist_ok=True)
    mp4_path = outdir + ".mp4"

    api = SeedReplayApi(player=args.player, ticks_per_step=args.ticks)
    try:
        # 1. 确定性重置（重试）+ 人类化整形
        obs = None
        for attempt in range(30):
            try:
                frame, obs = api.reset(seed=args.seed, task=task, items=SURVIVAL_KIT,
                                       spawn=spawn, humanize=not args.no_humanize)
                break
            except Exception as e:  # noqa: BLE001
                print(f"[reset] attempt {attempt + 1} failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
                time.sleep(2)
        if obs is None:
            print("HUMAN_DEMO_FAIL: env.reset 未成功", file=sys.stderr)
            return 1
        print(f"[reset] OK seed={args.seed} pos={obs['player']['pos']} "
              f"checksum={api.last_checksum}", flush=True)

        # 2. 目标供给（seed 确定性；真实世界资源优先，不足才人工放置）
        rng = random.Random(args.seed)
        if profile.kind == "kill":
            if not args.no_provision and _ensure_pigs(api, rng, args.half_extent,
                                                      profile.count) == 0:
                print("HUMAN_DEMO_FAIL: 未放置任何猪（周围地形不合适）", file=sys.stderr)
                return 1
        elif args.task == "dig_stone" and not args.no_provision:
            natural = _count_reachable(api, "minecraft:stone", args.half_extent)
            print(f"  [provision] natural reachable stone = {natural}", flush=True)
            if natural < profile.count:
                _ensure_stone(api, rng, args.half_extent, need=profile.count)
        elif args.task == "dig_dirt":
            natural = _count_reachable(api, "minecraft:dirt", args.half_extent)
            print(f"  [provision] natural reachable dirt = {natural}", flush=True)

        # 3. 抓帧分辨率 + HUD（demo 视频完整 UI）
        if args.capture.lower() == "native":
            api.ws.send({"cmd": "set_capture", "width": 0, "height": 0})
        else:
            w, h = (int(x) for x in args.capture.lower().split("x"))
            api.ws.send({"cmd": "set_capture", "width": w, "height": h})
        time.sleep(0.5)
        api.ws.send({"cmd": "set_capture_ui", "hud": not args.no_hud})
        time.sleep(0.3)

        # 排空 set_capture 前积压的旧分辨率帧（惰性 FBO 重建，见 native 帧宽）
        drained = 0
        for _ in range(120):
            f = api.ws.recv_frame(timeout=0.5)
            if f is None:
                break
            drained += 1
            if f.rgb.shape[1] != 224:
                break
        print(f"[capture] drained={drained} capture={args.capture}", flush=True)

        # 4. 录制器（后台录帧 + 排空事件；goto/pillar 状态经 poll_events 转发编排器）
        ping = api.grpc.ping()
        meta = {
            "format": "m11_human_demo_v2",
            "task": args.task,
            "task_id": task,
            "seed": args.seed,
            "kit": SURVIVAL_KIT,
            "player": args.player,
            "spawn": spawn,
            "humanize": not args.no_humanize,
            "ticks_per_step": args.ticks,
            "capture": args.capture,
            "hud": not args.no_hud,
            "world_name": ping["world_name"],
            "mc_version": ping["version"],
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        recorder = HumanRecorder(outdir, meta)
        recorder.start(api.ws)
        api.ws.send_set_key_log(True)

        # 5. 编排执行（客户端为手：逐 tick 按键由 NavExecutor/PillarExecutor+Humanizer 合成）
        def step_fn(action, ticks):
            api.ws.send_action(action)
            step = api.grpc.get_step_result(player=api.player, await_ticks=ticks)
            try:
                state = api.grpc.get_state(player=api.player)
                recorder.add_step(state, step["server_tick"], step["progress"])
            except Exception:  # noqa: BLE001 —— 状态记录失败不阻塞策略
                pass
            return {"progress": float(step["progress"]), "terminated": step["terminated"],
                    "truncated": step["truncated"], "reward": step["reward"]}

        agent = KitAgent(api, profile, rng=random.Random(args.seed),
                         half_extent=args.half_extent, recorder=recorder,
                         on_no_target=(
                             (lambda: _ensure_pigs(api, rng, args.half_extent, 1) > 0)
                             if profile.kind == "kill" and not args.no_provision else None))
        ok, steps, max_progress = agent.run(step_fn, max_steps=args.max_steps)

        # M11.6 加长 demo：任务完成后继续录制一段"收尾环视"（慢速 360° 转镜，纯演示
        # 画面，不推进任务/不干扰判定）。录帧由 recorder 后台线程持续进行，这里只喂
        # 动作并同步状态行，保证帧↔tick 对齐断言持续有效。
        if args.tail_seconds > 0:
            tail_ticks = int(args.tail_seconds * 20)     # 20 tps
            yaw_delta = 360.0 / max(1, tail_ticks)       # 全程一圈
            for _ in range(tail_ticks):
                idle = {name: False for name in BUTTONS}
                idle["hotbar"] = -1
                idle["camera"] = [0.0, yaw_delta]
                api.ws.send_action(idle)
                step = api.grpc.get_step_result(player=api.player, await_ticks=1)
                try:
                    state = api.grpc.get_state(player=api.player)
                    recorder.add_step(state, step["server_tick"], float(step["progress"]))
                except Exception:  # noqa: BLE001
                    pass
            print(f"[tail] post-completion pan {tail_ticks} ticks ({args.tail_seconds}s)",
                  flush=True)

        # 6. 收尾
        api.ws.send_set_key_log(False)
        time.sleep(0.5)
        summary = recorder.finalize({
            "success": ok, "steps": steps, "progress": max_progress, "seed": args.seed})
        align_ok = bool(summary["align_ok"]) and summary["frames"] > 0

        if not os.listdir(os.path.join(outdir, "frames")):
            print("HUMAN_DEMO_FAIL: 无帧产出", file=sys.stderr)
            return 1
        if not compose_mp4(os.path.join(outdir, "frames"), mp4_path):
            print("HUMAN_DEMO_FAIL: ffmpeg 合成失败", file=sys.stderr)
            return 1

        print(f"HUMAN_DEMO_OK task={args.task} task_id={task} seed={args.seed} "
              f"steps={steps} progress={max_progress:.2f} frames={summary['frames']} "
              f"key_events={summary['key_events']} semantic={summary['semantic_actions']} "
              f"align_ok={align_ok} dir={outdir}", flush=True)
        return 0 if ok and align_ok else 1
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
