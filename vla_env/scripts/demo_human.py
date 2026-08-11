#!/usr/bin/env python3
"""M11.5 人类式按键录制 demo（DESIGN.md §17，编排器版）。

按 --task 用固定生存工具包（镐/剑/铲/泥土/斧，hotbar 0-4）**模拟人类执行**操作，
录制 frame↔按键对齐的数据。架构（§17.2）：客户端为手（NavExecutor/PillarExecutor
+ Humanizer 人类化整形逐 tick 合成按键），Python KitAgent 为脑（选目标/粗航点/
技能派发/脱困决策），服务端为世界与裁判（任务判定/奖励/粗航点/确定性重置）。

用法（在 vla_env/ 目录内，避免 namespace 遮蔽）：
    .venv/bin/python -u scripts/demo_human.py [outdir] --task dig_stone --seed 42
    .venv/bin/python -u scripts/demo_human.py [outdir] --task kill_animal --seed 11 --tail-seconds 12
    .venv/bin/python -u scripts/demo_human.py --replay <episode_dir>   # 种子回放

任务（kit：镐/剑/铲/泥土/斧，hotbar 0-4）：
    dig_stone    → collect_stone（挖 4 格石柱，镐）
    dig_dirt     → dig_dirt（挖 4 格泥土柱，铲）
    kill_animal  → kill_animal（杀 2 猪，剑）
    place_dirt   → place_dirt（放置 3 泥土）
    collect_wood → collect_wood（砍 4 原木，斧）

受控平原由服务端插件启动时自动初始化；每次 SetTask 只投放当前任务的一根石柱、
泥土柱或一棵树，无需再手工 flatten / place_objects。

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
import sys
from collections import defaultdict
from pathlib import Path

from vla_env.interact import SeedReplayApi
from vla_env.dataset.human_episode import EpisodeConfig, record_human_episode
from vla_env.tasks import PROFILES, SURVIVAL_KIT, get_profile

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
                        "place_dirt（放置泥土）/ collect_wood（斧砍树）")
    p.add_argument("--seed", type=int, default=42, help="episode 种子（同 seed 确定性可回放）")
    p.add_argument("--surface", default="grass_block",
                   help="单材质元世界：grass_block/dirt/coarse_dirt/sand/red_sand/stone/"
                        "granite/diorite/andesite/clay（默认 grass_block）")
    p.add_argument("--max-steps", type=int, default=600)
    p.add_argument("--tail-seconds", type=float, default=0.0,
                   help="任务完成后额外录制 N 秒收尾环视（慢速转镜，加长 demo 视频）；0 关闭")
    p.add_argument("--ticks", type=int, default=2)
    p.add_argument("--half-extent", type=int, default=16,
                   help="体素扫描/重置区域半宽（默认 16：覆盖任务目标环 6-12 格）")
    p.add_argument("--player", default="agent0")
    p.add_argument("--ws-url", default="ws://127.0.0.1:30001",
                   help="Fabric client WS 地址（多 worker 时每个客户端不同）")
    p.add_argument("--grpc-host", default="127.0.0.1")
    p.add_argument("--grpc-port", type=int, default=50051)
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
    surface = meta.get("surface")
    spawn = meta.get("spawn")

    api = SeedReplayApi(player=args.player, ws_url=args.ws_url,
                        grpc_host=args.grpc_host, grpc_port=args.grpc_port,
                        ticks_per_step=1)
    try:
        frame, obs = api.reset(seed=seed, task=task_id, surface=surface,
                               items=SURVIVAL_KIT, spawn=spawn)
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

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                        "datasets", "demo_human"))
    outdir = args.outdir or os.path.join(
        root, f"{args.task}_{args.seed}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(outdir, exist_ok=True)
    mp4_path = outdir + ".mp4"

    api = SeedReplayApi(player=args.player, ws_url=args.ws_url,
                        grpc_host=args.grpc_host, grpc_port=args.grpc_port,
                        ticks_per_step=args.ticks)
    try:
        result = record_human_episode(
            api,
            EpisodeConfig(
                outdir=outdir,
                task=args.task,
                seed=args.seed,
                surface=args.surface,
                max_steps=args.max_steps,
                ticks=args.ticks,
                half_extent=args.half_extent,
                capture=args.capture,
                hud=not args.no_hud,
                humanize=not args.no_humanize,
                provision=not args.no_provision,
                spawn=args.spawn,
                tail_seconds=args.tail_seconds,
            ),
            compose_mp4=compose_mp4,
        )
        summary = result["summary"]
        print(f"HUMAN_DEMO_OK task={args.task} task_id={get_profile(args.task).task_id} "
              f"seed={args.seed} steps={result['steps']} progress={result['progress']:.2f} "
              f"frames={summary['frames']} key_events={summary['key_events']} "
              f"semantic={summary['semantic_actions']} align_ok={result['align_ok']} "
              f"dir={outdir}", flush=True)
        return 0 if result["ok"] else 1
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
