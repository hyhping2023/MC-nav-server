#!/usr/bin/env python3
"""M3-B 脚本化成功 agent（docs/m3m4_protocol.md §2，无推理）。

产出**确定性成功轨迹**（模仿学习正样本），不依赖随机探索：
  - craft_planks（必成功）：reset 已给 3 木头 → craft(wood_oak x4) → 轮询 success。
  - collect_wood（尽力）：从 observe 的 world.nearby_blocks 找 tree_oak →
    goto → dig → 轮询 success；无目标方块时 collect_nearby（走向地面掉落物）。
  - 通用：look_at → goto → dig/place/craft → collect_nearby（按任务 type 分发）。

复用 docs/m2_protocol.md §1 的请求式采样对齐：每次 observe → Lua 写一行
states → Python 从渲染器取帧按该行 frame 号写 PNG → end_episode 后断言
states==actions==rewards==PNG 数、image 引用全存在、meta.json env 字段。

用法：
  python mcl2_env/mcl2_env/scripts/scripted_agent.py --repo <repo> --world m0world \
      --renderer engine_fork --spawn-client --steps 60 --task craft_planks
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# ---- 包导入引导：兼容 `python -m`、直接运行、以及轻依赖环境 ----
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.scripts._common import (  # noqa: E402
    PLAYER,
    RUN_ID,
    build_begin_episode_spec,
    build_renderer,
    print_log_tail,
    read_world_seed,
    render_frame_to_episode,
    start_proc,
    stop_proc,
    verify_alignment,
)

DEFAULT_REPO = "/Users/hyhpinggongzuoban/Code/fake-mc"

# ---------------------------------------------------------------- 物品/方块常量
TREE_BLOCK = "mcl_trees:tree_oak"   # 原木方块（world.nearby_blocks 里的 name）
PLANK_ITEM = "mcl_trees:wood_oak"   # 木板物品（craft 产物 / 背包判定）

# 已知任务 → 目标物品 + 动作类型（供通用策略推导，避免猜错物品名）
# 值与 mcl2_agent/tasks/*.lua 的 success_args 对齐
TASK_ITEM_MAP: dict[str, tuple[str, int, str]] = {
    "craft_planks": ("mcl_trees:wood_oak", 4, "craft"),
    "craft_workbench": ("mcl_crafting_table:crafting_table", 1, "craft"),
    "smelt_iron": ("mcl_core:iron_ingot", 1, "craft"),
    "collect_wood": ("mcl_trees:tree_oak", 3, "collect"),
    "collect_stone": ("mcl_core:stone", 5, "collect"),
    "collect_iron_ore": ("mcl_core:iron_lump", 2, "collect"),
    "place_torch": ("mcl_torches:torch", 1, "place"),
}


def _pos_key(pos: dict[str, Any] | None) -> str | None:
    """pos 的去重键（x,y,z 取整）。"""
    if not pos:
        return None
    return f"{int(round(pos.get('x', 0)))},{int(round(pos.get('y', 0)))},{int(round(pos.get('z', 0)))}"


class ScriptedPolicy:
    """按任务类型生成确定性动作序列（语义动作，经 bridge.execute 下发）。

    策略是无状态的计划器，但内部保留已下发动作的指纹（craft 标记 / pos 去重），
    避免每步 observe 重复下发同一动作把队列灌满。
    """

    def __init__(self, bridge, player: str = PLAYER, task_id: str = ""):
        self.bridge = bridge
        self.player = player
        self.task_id = task_id
        self._craft_issued = False
        self._issued_pos: set[str] = set()

    # ------------------------------------------------------------ 入口

    def plan(self, obs: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """根据观测生成下一步动作序列 [(action, args), ...]；空列表表示轮询等待。"""
        task = obs.get("task") or {}
        tid = task.get("id") or self.task_id
        ttype = task.get("type")

        if tid == "craft_planks":
            return self._plan_craft_planks()
        if ttype == "craft":
            return self._plan_craft(obs)
        if ttype == "collect":
            return self._plan_collect(obs)
        if ttype == "build":
            return self._plan_build(obs)
        return self._plan_generic(obs)

    # ------------------------------------------------------------ craft

    def _plan_craft_planks(self) -> list[tuple[str, dict[str, Any]]]:
        """必成功路径：reset 已给 3 木头，craft 4 木板只发一次，之后轮询。"""
        if self._craft_issued:
            return []
        self._craft_issued = True
        return [("craft", {"item": PLANK_ITEM, "count": 4})]

    def _plan_craft(self, obs: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """通用 craft：从任务表/指令推导目标物品，发一次 craft。"""
        if self._craft_issued:
            return []
        item = self._guess_craft_item(obs)
        if item is None:
            return []
        self._craft_issued = True
        return [("craft", {"item": item, "count": 1})]

    def _guess_craft_item(self, obs: dict[str, Any]) -> str | None:
        tid = (obs.get("task") or {}).get("id") or self.task_id
        if tid in TASK_ITEM_MAP:
            item, _, _ = TASK_ITEM_MAP[tid]
            return item
        # 兜底：从 instruction 文本里找已知物品名
        inst = (obs.get("task") or {}).get("instruction") or ""
        known = sorted({v[0] for v in TASK_ITEM_MAP.values()})
        for name in known:
            if name in inst:
                return name
        return None

    # ---------------------------------------------------------------- collect

    def _plan_collect(self, obs: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """尽力而为：按 (x,z) 列分组找最近的一整棵树 → goto → 自下而上 dig 整列原木。

        修复：不再每次只挑最近的一个原木方块（那会总挑到眼平线附近、在不同树之间
        横向跳着挖，永远不沿纵向挖完整棵树）。
        """
        world = obs.get("world") or {}
        player_pos = (obs.get("player") or {}).get("pos")
        logs = [b["pos"] for b in (world.get("nearby_blocks") or [])
                if (b.get("name") or "") == TREE_BLOCK and b.get("pos")]

        col = self._nearest_tree_column(logs, player_pos)
        if col is None:
            return self._plan_collect_nearby(obs)

        # 该列已下发过（含整列原木），避免重复；转而处理掉落物/其他列
        bottom_key = _pos_key(col["bottom"])
        if bottom_key in self._issued_pos:
            return self._plan_collect_nearby(obs)

        self._issued_pos.add(bottom_key)
        for p in col["logs"]:
            self._issued_pos.add(_pos_key(p))

        actions: list[tuple[str, dict[str, Any]]] = [("goto", {"pos": col["bottom"]})]
        for p in col["logs"]:  # logs 已按 y 升序：自下而上挖，抬头即可够到整列
            actions.append(("dig", {"pos": p}))
        return actions

    @staticmethod
    def _nearest_tree_column(logs: list[dict[str, Any]], player_pos: dict[str, Any] | None):
        """把原木按 (x,z) 列分组，返回离玩家最近的列 {bottom, logs(按y升序)}；无则 None。"""
        if not logs:
            return None
        rx, rz = (player_pos or {}).get("x", 0), (player_pos or {}).get("z", 0)
        cols: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for p in logs:
            k = (int(round(p.get("x", 0))), int(round(p.get("z", 0))))
            cols.setdefault(k, []).append(p)
        best, best_d = None, None
        for (cx, cz), lst in cols.items():
            lst.sort(key=lambda p: p.get("y", 0))
            d = (cx - rx) ** 2 + (cz - rz) ** 2
            if best_d is None or d < best_d:
                best_d = d
                best = {"bottom": lst[0], "logs": lst}
        return best

    def _plan_collect_nearby(self, obs: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """走向地面掉落物（Mineclonia 玩家靠近自动拾取）。"""
        world = obs.get("world") or {}
        for it in world.get("items_on_ground") or []:
            pos = it.get("pos")
            key = _pos_key(pos)
            if pos and key is not None and key not in self._issued_pos:
                self._issued_pos.add(key)
                return [("goto", {"pos": pos})]
        return []

    # ---------------------------------------------------------------- build / generic

    def _plan_build(self, obs: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """放置类任务：玩家面向目标点（相邻地面）→ 放置。"""
        item = self._guess_craft_item(obs)
        if item is None:
            return []
        target = self._place_target(obs)
        if target is None:
            return []
        key = _pos_key(target)
        if key in self._issued_pos:
            return []
        self._issued_pos.add(key)
        return [("look_at", {"pos": target}), ("place", {"item": item, "pos": target})]

    def _plan_generic(self, obs: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """通用策略：aimed_block 命中则 look_at→goto→dig，否则 collect_nearby。"""
        world = obs.get("world") or {}
        aimed = world.get("aimed_block") or {}
        if aimed.get("pos") and aimed.get("name") and aimed["name"] != "air":
            key = _pos_key(aimed["pos"])
            if key not in self._issued_pos:
                self._issued_pos.add(key)
                return [("look_at", {"pos": aimed["pos"]}),
                        ("goto", {"pos": aimed["pos"]}),
                        ("dig", {"pos": aimed["pos"]})]
        return self._plan_collect_nearby(obs)

    @staticmethod
    def _place_target(obs: dict[str, Any]) -> dict[str, Any] | None:
        """放置位置：玩家相邻地面（x+1, y-1, z），在 place_torch 判定区域内。"""
        pl = obs.get("player") or {}
        pos = pl.get("pos")
        if not pos:
            return None
        return {"x": round(pos.get("x", 0)) + 1, "y": round(pos.get("y", 0)) - 1, "z": round(pos.get("z", 0))}


# ---------------------------------------------------------------- episode 循环

def run_scripted_episode(
    bridge,
    renderer: Any,
    episode_id: str,
    task_id: str,
    data_root: Path,
    world_seed: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """跑一个脚本化 episode：begin → 策略循环（执行+observe 对齐）→ end → 断言。

    每步：observe（Lua 写一行 states）→ 渲染器写该 frame 的 PNG → 策略下发动作。
    成功判定来自 observe 的 task.success（Lua 侧 task.evaluate 全局步驱动）。
    """
    from mcl2_env.dataset.episode_writer import EpisodeWriter

    spec = build_begin_episode_spec(
        player=PLAYER, task_id=task_id, episode_id=episode_id,
        run_id=RUN_ID, world_seed=world_seed, task_seed=args.seed,
    )
    bridge.begin_episode(spec)

    writer = EpisodeWriter(str(data_root), RUN_ID, episode_id, images_only=True)
    episode_dir = data_root / "episodes" / episode_id
    policy = ScriptedPolicy(bridge, player=PLAYER, task_id=task_id)

    frames_written = 0
    steps = 0
    success = False
    deadline = time.monotonic() + getattr(args, "timeout", 120.0)

    def frame_step(obs: dict[str, Any] | None) -> None:
        nonlocal frames_written
        if obs is None:
            return
        # 任务成功后 Lua 侧 flush episode（sess.episode=nil），observe 不再采样：
        # 此时 obs 无 episode 段，不写帧，避免旧 frame 号被覆盖导致帧/状态错位。
        if obs.get("episode") is None:
            return
        written, _ = render_frame_to_episode(writer, renderer, obs, episode_dir)
        if written:
            frames_written += 1

    # 初始观察（写第 0 行 states + 帧 0）
    obs = bridge.observe(player=PLAYER)
    frame_step(obs)

    while steps < args.steps and time.monotonic() < deadline:
        task = obs.get("task") or {}
        if task.get("success"):
            success = True
            print(f"      task.success at step {steps}")
            break

        actions = policy.plan(obs)
        for name, aargs in actions:
            print(f"      execute {name} {json.dumps(aargs, ensure_ascii=False)}")
            bridge.execute(name, aargs, player=PLAYER)

        obs = bridge.observe(player=PLAYER)
        steps += 1
        frame_step(obs)
        for ev in bridge.poll_events():
            print(f"      event: {json.dumps(ev, ensure_ascii=False)}")

    task = obs.get("task") or {}
    if task.get("success"):
        success = True
    if not success:
        print(f"      task not completed after {steps} steps (best-effort: OK for collect_wood)")

    bridge.end_episode(success=success, player=PLAYER)
    align_ok, checks = verify_alignment(episode_dir)
    return {
        "episode_id": episode_id,
        "task": task_id,
        "success": success,
        "steps": steps,
        "frames": frames_written,
        "align_ok": align_ok,
        "checks": checks,
    }


# ---------------------------------------------------------------- CLI

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M3-B scripted success agent (file IPC + renderer + alignment)")
    p.add_argument("--repo", default=DEFAULT_REPO, help="repo root (default: %(default)s)")
    p.add_argument("--world", default="m0world", help="world name under <repo>/luanti/worlds")
    p.add_argument("--task", default="craft_planks",
                   help="task id（默认 %(default)s；craft_planks 必成功，collect_wood 尽力）")
    p.add_argument("--steps", type=int, default=60, help="max policy steps (craft_planks 需 <60) (default: %(default)s)")
    p.add_argument("--renderer", choices=["engine_fork", "voxel", "none"],
                   default="engine_fork", help="renderer; engine_fork 无帧时回退 voxel (default: %(default)s)")
    p.add_argument("--spawn-client", action="store_true",
                   help="额外拉起 luanti 客户端以 bot1 连接（独立验证用）")
    p.add_argument("--external-server", action="store_true",
                   help="复用外部已启动的服务器和客户端（demo 包装器使用）")
    p.add_argument("--fps", type=int, default=5, help="renderer 降采样帧率 (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=120.0, help="episode timeout in seconds")
    p.add_argument("--seed", type=int, default=42, help="task_seed (default: %(default)s)")
    p.add_argument("--out", default="", help="可选：导出 webdataset 目录（缺省不导出）")
    p.add_argument("--demo-taskgen", action="store_true",
                   help="连上服务器后运行 M3-C 任务生成器演示（课程/procedural/llm mock）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).resolve()
    world_dir = repo / "luanti" / "worlds" / args.world
    server_bin = repo / "luanti" / "bin" / "luantiserver"
    client_bin = repo / "luanti" / "bin" / "luanti"
    server_conf = world_dir / "server.conf"
    logfile = world_dir / "server.log"
    data_root = world_dir / "mcl2_agent" / "data"

    if not server_bin.exists():
        print(f"FAIL: luantiserver not found: {server_bin}")
        return 1

    from mcl2_env.bridge import BridgeError, FileBridgeClient

    server = client = None
    out_d = err_d = None
    renderer = build_renderer(args.renderer, args.fps)

    try:
        # ---- 1) 启动服务器 ----
        if args.external_server:
            if args.spawn_client:
                print("FAIL: --external-server cannot be combined with --spawn-client")
                return 1
            print(f"[1/6] using external server (world={args.world}, task={args.task}, steps={args.steps})")
        else:
            print(f"[1/6] starting luantiserver (world={args.world}, task={args.task}, steps={args.steps})")
            server, out_d, err_d = start_proc(server_bin, [
                str(server_bin), "--world", str(world_dir),
                "--config", str(server_conf), "--logfile", str(logfile),
            ], repo / "luanti")

        # ---- 2) 可选：拉起客户端 ----
        if args.spawn_client:
            if not client_bin.exists():
                print(f"FAIL: luanti client not found: {client_bin}")
                return 1
            print(f"[2/6] spawning client: luanti --go --address 127.0.0.1 --port 30000 --name {PLAYER}")
            client_cfg = repo / "luanti" / "mcl2_client.conf"
            client_cmd = [str(client_bin), "--go", "--address", "127.0.0.1",
                          "--port", "30000", "--name", PLAYER]
            if client_cfg.exists():
                client_cmd += ["--config", str(client_cfg)]
            client, _, _ = start_proc(client_bin, client_cmd, repo / "luanti")

        # ---- 3) 渲染器 + 等 ready.json ----
        if renderer:
            renderer.start()
            print(f"[3/6] renderer={type(renderer).__name__} started")
        print("[3/6] waiting for ready.json ...")
        bridge = FileBridgeClient(world_dir, timeout=args.timeout)
        try:
            ready = bridge.wait_ready(timeout=args.timeout)
        except BridgeError as e:
            print(f"FAIL: {e}")
            if server is not None and server.poll() is not None:
                print(f"      server exited early (rc={server.returncode})")
            print_log_tail(logfile)
            return 1
        print(f"      ready = {json.dumps(ready, ensure_ascii=False)}")

        # ---- 3.5) 等 bot1 会话 ----
        print("[3.5/6] waiting for player session ...")
        player_ready = False
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                probe = bridge.observe(player=PLAYER)
            except BridgeError:
                probe = None
            if isinstance(probe, dict) and probe.get("player"):
                player_ready = True
                break
            time.sleep(0.5)
        if not player_ready:
            print(f"FAIL: player {PLAYER} did not connect within {args.timeout:.0f}s")
            print_log_tail(logfile)
            return 1
        print(f"      player {PLAYER} connected")

        # ---- M3-C：任务生成器演示 ----
        if args.demo_taskgen:
            from mcl2_env.taskgen import TaskGenerator

            print("[3.7/6] --demo-taskgen: 列出课程 / procedural 生成 / llm mock")
            demo_results = TaskGenerator.demo(bridge, max_difficulty=3, items=[
                {"item": "mcl_core:stick", "count": 4, "difficulty": 1},
                {"item": "mcl_tools:pick_wood", "count": 1, "difficulty": 2},
            ])
            cur = demo_results["curriculum"]
            print(f"      curriculum({len(cur)} 条，difficulty<={3}): "
                  f"{json.dumps([t.get('id') for t in cur], ensure_ascii=False)}")
            proc = demo_results["procedural"]
            print(f"      procedural registered={json.dumps(proc['registered'], ensure_ascii=False)} "
                  f"skipped={json.dumps(proc['skipped'], ensure_ascii=False)}")
            llm = demo_results["llm"]
            print(f"      llm_hook source={llm.get('source', 'op')} "
                  f"registered={json.dumps(llm['registered'], ensure_ascii=False)}")
            print("      (demo taskgen 完成，继续跑 episode 或结束)")

        # ---- 4) begin_episode + 脚本化策略循环 ----
        episode_id = f"ep-{int(time.time()) % 1000000:06d}"
        print(f"[4/6] begin_episode task={args.task} episode={episode_id}")
        result = run_scripted_episode(
            bridge, renderer, episode_id, args.task, data_root,
            read_world_seed(world_dir), args,
        )
        print(f"[5/6] episode done: steps={result['steps']} frames={result['frames']} "
              f"success={result['success']} align={'OK' if result['align_ok'] else 'FAIL'}")

        for name, ok, detail in result["checks"]:
            print(f"      [{'OK' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

        ok = result["align_ok"] and (result["success"] or args.task != "craft_planks")
        if not ok:
            print("FAIL: episode 未通过（craft_planks 必须 success，其余尽力）")
            print_log_tail(logfile)
            return 1

        # ---- 6) 可选导出 ----
        if args.out:
            from mcl2_env.dataset.export import ExportConfig, export_webdataset

            out_dir = Path(args.out).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            export_webdataset(ExportConfig(source_root=str(data_root), out_dir=str(out_dir),
                                           shard_size=1000, only=(episode_id,)))
            print(f"[6/6] exported -> {out_dir}")

        print(f"PASS: scripted_agent episode={episode_id} task={args.task} success={result['success']}")
        return 0

    except BridgeError as e:
        print(f"FAIL: bridge error: {e}")
        print_log_tail(logfile)
        return 1
    finally:
        if renderer:
            renderer.stop()
        stop_proc(client)
        stop_proc(server)
        if out_d:
            out_d.join(timeout=2)
        if err_d:
            err_d.join(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
