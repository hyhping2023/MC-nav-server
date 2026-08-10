#!/usr/bin/env python3
"""任务 demo（寻路可视化）：按 --task 生成 collect_wood / collect_stone / kill_animal 的第一视角 demo。

与 demo_record.py 的区别：
- 驱动 collect_wood_policy 时传 on_path 回调：每次 ComputePath 返回后调服务端
  ShowPath RPC，在 A* 航点上刷 END_ROD 粒子 → 视频里能看到一条"发光路径"，
  直观展示寻路算法（绕障碍/跳台阶/追目标，目标红色高亮）。
- **真实世界**：默认不人工放置方块——树/石头取自种子生成的自然世界（模拟真实伐木/
  采矿）；只有 kill_animal 因 reset 冻结 doMobSpawning 而必须 gRPC spawn 猪（实体）。
  `--setup` 可显式 opt-in 人工放置目标（无资源种子/调试用）。
- 任务结束/失败后自动清除路径特效，并用 ffmpeg 把帧目录合成 mp4。

用法（在 vla_env/ 目录内，避免 namespace 遮蔽）：
    .venv/bin/python -u scripts/demo_task.py [outdir] --task collect_wood|collect_stone|kill_animal

输出：
- outdir/（JPEG 帧，f_%06d.jpg）
- outdir 同名 .mp4（ffmpeg 合成，20fps）
- 结尾打印 `DEMO_OK steps=N progress=P`（或 DEMO_NOT_COMPLETE）并 exit 0/1。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

from PIL import Image

from vla_env.env import MinecraftEnv
# 本脚本以 `python scripts/demo_task.py` 运行，sys.path[0]=scripts/，
# 故 collect_wood_agent 是裸导入（scripts/ 不是包，vla_env.scripts 不可导入）。
from collect_wood_agent import collect_wood_policy, blocks_3d, name_at  # noqa: E402

TASKS = ("collect_wood", "collect_stone", "kill_animal")

# 候选位置（相对玩家脚格的水平偏移；会按地面高度过滤）
SETUP_OFFSETS = [(9, 0), (0, -9), (-9, 0), (0, 9), (-8, 8), (8, -8), (7, 7), (-7, -7)]
MAX_GROUND_DROP = 2          # 地面偏离玩家脚 y 超过此值则跳过（防找路卡死）
N_TREES = 3
# 石头 slab 大小（3×3×1 = 9 块，任务需 8）
STONE_SLAB = 3


def _ground_y(blocks3, origin, x, z, py, max_drop=MAX_GROUND_DROP + 3):
    """(x,z) 列从 py 向下找最高实心格（非空气/水/岩浆/树叶/原木）y；找不到返回 None。"""
    for y in range(py, py - max_drop - 1, -1):
        n = name_at(blocks3, origin, (x, y, z))
        if n not in (None, "minecraft:air", "minecraft:water", "minecraft:lava",
                     "minecraft:oak_leaves", "minecraft:oak_log"):
            return y
    return None


def place_trees(env, half_extent: int) -> int:
    """在玩家周围平地上放置 N_TREES 棵小树（保证 collect_wood 有确定性目标）。

    每棵树：2 根 oak_log（地面+1、地面+2）+ 5 片 oak_leaves 树冠（地面+3）。
    返回实际放置的棵数。仅在 env.reset() 之后调用。
    """
    st = env.grpc.get_state(player=env.player)
    px, py, pz = (float(v) for v in st["player"]["pos"])
    palette, data, origin, size = env.grpc.get_voxels(
        player=env.player, half_extent=half_extent)
    blocks3 = blocks_3d(palette, data, size)
    px, pz, py = int(px), int(pz), int(py)
    placed = 0
    for dx, dz in SETUP_OFFSETS:
        if placed >= N_TREES:
            break
        x, z = px + dx, pz + dz
        gy = _ground_y(blocks3, origin, x, z, py)
        if gy is None or abs(gy - py) > MAX_GROUND_DROP:
            continue
        base = (x, gy + 1, z)
        if name_at(blocks3, origin, base) not in (None, "minecraft:air"):
            continue
        # 2 根原木 + 5 片树叶
        for bx, by, bz in (base, (x, gy + 2, z)):
            env.grpc.set_block(player=env.player, pos=(bx, by, bz),
                               block="minecraft:oak_log")
        for bx, by, bz in ((x, gy + 3, z), (x + 1, gy + 3, z), (x - 1, gy + 3, z),
                           (x, gy + 3, z + 1), (x, gy + 3, z - 1)):
            env.grpc.set_block(player=env.player, pos=(bx, by, bz),
                               block="minecraft:oak_leaves")
        placed += 1
        print(f"  [place] tree#{placed} base={base} ground_y={gy}", flush=True)
    return placed


def place_stones(env, half_extent: int) -> int:
    """在玩家附近平地上放置 1-2 块 3×3×1 石头石板（保证 collect_stone 有确定性目标）。

    石板放不出（周围无合适平地）时返回 0——任务可依赖自然石头平台兜底。
    返回实际放置的石头块数。仅在 env.reset() 之后调用。
    """
    st = env.grpc.get_state(player=env.player)
    px, py, pz = (float(v) for v in st["player"]["pos"])
    palette, data, origin, size = env.grpc.get_voxels(
        player=env.player, half_extent=half_extent)
    blocks3 = blocks_3d(palette, data, size)
    px, pz, py = int(px), int(pz), int(py)
    total = 0
    for dx, dz in SETUP_OFFSETS:
        if total >= 9:
            break
        x, z = px + dx, pz + dz
        gy = _ground_y(blocks3, origin, x, z, py)
        if gy is None or abs(gy - py) > MAX_GROUND_DROP:
            continue
        if name_at(blocks3, origin, (x, gy + 1, z)) not in (None, "minecraft:air"):
            continue
        base_y = gy + 1
        placed = 0
        half = STONE_SLAB // 2
        for sx in range(-half, half + 1):
            for sz in range(-half, half + 1):
                env.grpc.set_block(player=env.player,
                                   pos=(x + sx, base_y, z + sz),
                                   block="minecraft:stone")
                placed += 1
        total += placed
        print(f"  [place] stone slab at ({x}, {base_y}, {z}) {STONE_SLAB}x{STONE_SLAB} "
              f"({placed} blocks)", flush=True)
    return total


def spawn_pigs(env, half_extent: int) -> int:
    """在玩家附近平地放置 2 头猪（保证 kill_animal 有目标；猪会跑，展示追猎寻路）。

    返回实际放置的头数。仅在 env.reset() 之后调用。
    """
    st = env.grpc.get_state(player=env.player)
    px, py, pz = (float(v) for v in st["player"]["pos"])
    palette, data, origin, size = env.grpc.get_voxels(
        player=env.player, half_extent=half_extent)
    blocks3 = blocks_3d(palette, data, size)
    px, pz, py = int(px), int(pz), int(py)
    spawned = 0
    for dx, dz in SETUP_OFFSETS:
        if spawned >= 2:
            break
        x, z = px + dx, pz + dz
        gy = _ground_y(blocks3, origin, x, z, py)
        if gy is None or abs(gy - py) > MAX_GROUND_DROP:
            continue
        if name_at(blocks3, origin, (x, gy + 1, z)) not in (None, "minecraft:air"):
            continue
        env.grpc.spawn_entity(player=env.player, entity_type="minecraft:pig",
                              pos=(x + 0.5, gy + 1, z + 0.5), count=1)
        spawned += 1
        print(f"  [spawn] pig#{spawned} at ({x}, {gy + 1}, {z})", flush=True)
    return spawned


def recorder(ws, outdir, stop_flag) -> None:
    """录帧线程：消费全部 WS 帧，存 JPEG（f_%06d.jpg）。"""
    n = 0
    while not stop_flag.is_set():
        try:
            frame = ws.recv_frame(timeout=0.5)
        except Exception:  # noqa: BLE001
            continue
        if frame is None:
            continue
        Image.fromarray(frame.rgb).save(f"{outdir}/f_{n:06d}.jpg", quality=90)
        n += 1
        if n % 150 == 0:
            print(f"[rec] frames={n}", flush=True)
    print(f"[rec] done, total={n}", flush=True)


def compose_mp4(outdir: str, mp4_path: str, fps: int = 20) -> bool:
    """ffmpeg 把 outdir/f_%06d.jpg 合成 mp4；失败打印 stderr 并返回 False。"""
    cmd = [
        "ffmpeg", "-y", "-framerate", str(fps), "-start_number", "1",
        "-i", f"{outdir}/f_%06d.jpg",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        mp4_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[ffmpeg] FAILED:\n{proc.stderr}", file=sys.stderr, flush=True)
        return False
    print(f"[ffmpeg] -> {mp4_path}", flush=True)
    return True


def parse_args(default_task: str = "collect_wood") -> argparse.Namespace:
    p = argparse.ArgumentParser(description="任务 demo（A* 路径粒子可视化）")
    p.add_argument("outdir", nargs="?", default=None,
                   help="帧输出目录（默认 datasets/demo/<task>_<ts>）")
    p.add_argument("--task", choices=TASKS, default=default_task,
                   help="任务：collect_wood（挖 4 原木）/ collect_stone（挖 8 石头）/ "
                        "kill_animal（杀 2 猪）")
    p.add_argument("--max-steps", type=int, default=600)
    p.add_argument("--ticks", type=int, default=2)
    p.add_argument("--half-extent", type=int, default=16)
    p.add_argument("--player", default="agent0")
    p.add_argument("--capture", default="native",
                   help="抓帧分辨率：native=游戏 framebuffer 原始分辨率（推荐）或 WxH")
    p.add_argument("--no-hud", action="store_true",
                   help="关闭 HUD 抓帧（默认开启：demo 视频含物品栏/血条/手/准星）")
    p.add_argument("--setup", action="store_true",
                   help="人工放置演示目标（真实世界默认不放置——树/石头取自种子生成的自然世界，"
                        "模拟真实伐木/采矿；无资源种子或调试时才用 --setup）")
    p.add_argument("--no-spawn", action="store_true",
                   help="kill_animal 不 spawn 猪（reset 冻结 doMobSpawning，自然不生成猪；"
                        "默认 gRPC spawn 2 头猪，属实体而非填充方块）")
    return p.parse_args()


def main(default_task: str = "collect_wood") -> int:
    args = parse_args(default_task)
    task = args.task
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "datasets", "demo"))
    prefix = {"collect_wood": "dig_tree", "collect_stone": "collect_stone",
              "kill_animal": "kill_animal"}[task]
    outdir = args.outdir or os.path.join(root, f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(outdir, exist_ok=True)
    mp4_path = outdir + ".mp4"

    env = MinecraftEnv(player=args.player, task=task, ticks_per_step=args.ticks)
    try:
        obs = None
        for attempt in range(30):
            try:
                obs, _ = env.reset()
                break
            except Exception as e:  # noqa: BLE001
                print(f"[reset] attempt {attempt + 1} failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
                time.sleep(2)
        if obs is None:
            print("DEMO_FAIL: env.reset 未成功", file=sys.stderr)
            return 1
        print(f"[reset] OK pos={obs['player']['pos']}", flush=True)

        # 任务 setup：真实世界默认不人工放置方块（树/石头取自种子生成的自然世界，
        # 模拟真实伐木/采矿）。kill_animal 因 reset 冻结 doMobSpawning、猪不会自然生成，
        # 必须 gRPC spawn（实体，非填充方块；--no-spawn 可关闭）。
        if task == "kill_animal":
            if not args.no_spawn:
                if spawn_pigs(env, args.half_extent) == 0:
                    print("DEMO_FAIL: 未放置任何猪（周围地形不合适）", file=sys.stderr)
                    return 1
        elif args.setup:
            # 显式 opt-in：人工放置目标（无资源种子/调试用）
            if task == "collect_wood":
                if place_trees(env, args.half_extent) == 0:
                    print("DEMO_FAIL: 未放置任何树（周围地形不合适）", file=sys.stderr)
                    return 1
            elif task == "collect_stone":
                n = place_stones(env, args.half_extent)
                print(f"  [setup] placed stone blocks: {n}（不足则依赖自然石头兜底）", flush=True)

        # 切换抓帧分辨率（native → 游戏 framebuffer 原始分辨率，保真+保留比例）
        if args.capture.lower() == "native":
            env.ws.send({"cmd": "set_capture", "width": 0, "height": 0})
        else:
            w, h = (int(x) for x in args.capture.lower().split("x"))
            env.ws.send({"cmd": "set_capture", "width": w, "height": h})
        time.sleep(0.5)  # 等客户端渲染线程重建 FBO

        # demo 视频需要完整 UI（HUD+手+准星）——切到 GameRenderer TAIL 抓帧
        env.ws.send({"cmd": "set_capture_ui", "hud": not args.no_hud})
        time.sleep(0.3)

        # 排空 set_capture 前积压的旧分辨率帧，并等 native 帧真正生效
        # （set_capture 是惰性 FBO 重建：命令先入队，渲染线程下一次抓帧才切尺寸，
        #  直接排空 N 帧可能不够——首帧尺寸仍可能是默认 224，会让 ffmpeg 按 224 定
        #  视频大小并缩放全部帧）。看到非 224 宽帧即视为 native 已生效，丢弃该帧。
        drained = 0
        for _ in range(120):
            f = env.ws.recv_frame(timeout=0.5)
            if f is None:
                break
            drained += 1
            if f.rgb.shape[1] != 224:
                break
        print(f"[capture] drained={drained} capture={args.capture}", flush=True)

        # 录帧线程（消费全部 WS 帧；主线程只发动作/调 gRPC，不读 WS）
        stop_flag = threading.Event()
        rec_thread = threading.Thread(target=recorder, args=(env.ws, outdir, stop_flag),
                                      daemon=True)
        rec_thread.start()

        def step_fn(action, ticks):
            """录帧版 step：发动作 + gRPC 结算，不读 WS 帧（录帧线程在消费）。"""
            env.ws.send_action(action)
            step = env.grpc.get_step_result(player=env.player, await_ticks=ticks)
            return {"progress": float(step["progress"]), "terminated": step["terminated"],
                    "truncated": step["truncated"], "reward": step["reward"]}

        def on_path(waypoints, details, goal):
            """每次 ComputePath 返回后刷新路径粒子：waypoints 空 → 清除特效。"""
            try:
                if not waypoints:
                    env.grpc.show_path(player=env.player, clear=True)
                else:
                    pts = [tuple(d["pos"]) for d in details] or list(waypoints)
                    env.grpc.show_path(player=env.player, waypoints=pts, goal=goal)
            except Exception as e:  # noqa: BLE001 —— 可视化失败不阻塞策略
                print(f"[show_path] WARN: {type(e).__name__}: {e}", file=sys.stderr)

        ok, steps, max_progress = collect_wood_policy(
            env, step_fn, max_steps=args.max_steps, ticks=args.ticks,
            half_extent=args.half_extent, task=task, on_path=on_path)

        # 收尾：清路径特效、停录帧、稍等帧流排空
        try:
            env.grpc.show_path(player=env.player, clear=True)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
        stop_flag.set()
        rec_thread.join(timeout=3)

        # ffmpeg 合成 mp4（帧数太少不合成，视为异常）
        if not os.listdir(outdir):
            print("DEMO_FAIL: 无帧产出", file=sys.stderr)
            return 1
        if not compose_mp4(outdir, mp4_path):
            print("DEMO_FAIL: ffmpeg 合成失败", file=sys.stderr)
            return 1

        if ok:
            print(f"DEMO_OK task={task} steps={steps} progress={max_progress:.2f} "
                  f"frames_dir={outdir}", flush=True)
            return 0
        print("DEMO_NOT_COMPLETE", file=sys.stderr, flush=True)
        return 1
    finally:
        env.close()


if __name__ == "__main__":
    sys.exit(main())
