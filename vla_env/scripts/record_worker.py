#!/usr/bin/env python3
"""长期存活的单 worker 并发录制进程。

一个实例严格绑定：

    一个 Fabric client WS endpoint + 一个离线 player + 一张 player 专属 surface world

它在启动时只选择一次地图，此后连续执行 ``reset -> record episode -> reset``；
不会为每条数据重启 Minecraft 客户端。多个 worker 由不同 player / WS port 并发运行，
服务端会将它们映射到 ``vla_surface_<surface>__<player>/`` 独立世界。

用法（必须从 vla_env/ 目录执行）：

    .venv/bin/python -u scripts/record_worker.py \
      --worker-id worker-00 --player agent00 --ws-url ws://127.0.0.1:30001 \
      --surface sand --map-seed 10000 \
      --jobs ../runtime/worker-00/jobs.jsonl \
      --out-root ../datasets/demo_human
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable

from vla_env.dataset.human_episode import EpisodeConfig, record_human_episode
from vla_env.interact import SeedReplayApi

# 本脚本同 demo_human 一样以裸脚本运行；scripts 不是 Python package。
from demo_task import compose_mp4  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="持久 Fabric client 的并发录制 worker")
    parser.add_argument("--worker-id", required=True, help="worker 标识，如 worker-00")
    parser.add_argument("--player", required=True, help="该 worker 的独占离线玩家名，如 agent00")
    parser.add_argument("--ws-url", required=True, help="该 Fabric client 的 WS，例如 ws://127.0.0.1:30001")
    parser.add_argument("--grpc-host", default="127.0.0.1")
    parser.add_argument("--grpc-port", type=int, default=50051)
    parser.add_argument("--surface", required=True, help="该 worker 固定维护的单材质地图")
    parser.add_argument("--map-seed", type=int, required=True,
                        help="首次创建该 worker 专属地图时使用的稳定 seed")
    parser.add_argument("--jobs", required=True, help="JSONL job 队列（该 worker 独占）")
    parser.add_argument("--out-root", required=True, help="最终 episode 输出根目录")
    parser.add_argument("--results", default=None,
                        help="结果 JSONL；默认 jobs 同目录/results.jsonl")
    parser.add_argument("--ticks", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=600)
    parser.add_argument("--half-extent", type=int, default=16)
    parser.add_argument("--capture", default="native")
    parser.add_argument("--no-hud", action="store_true")
    parser.add_argument("--no-humanize", action="store_true")
    parser.add_argument("--no-provision", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=0, help="最多录制 N 个 job；0=队列全部")
    parser.add_argument("--retry-failed", action="store_true",
                        help="重跑 results.jsonl 中 status=failed 的 job（默认跳过已有结果）")
    parser.add_argument("--ready-timeout", type=float, default=180.0,
                        help="等待服务端、client WS、player join 的超时秒数")
    return parser.parse_args()


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{line_no}: job must be a JSON object")
            yield item


def result_index(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    indexed: Dict[str, Dict[str, Any]] = {}
    for row in load_jsonl(path):
        job_id = row.get("job_id")
        if job_id:
            indexed[str(job_id)] = row
    return indexed


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def normalize_job(job: Dict[str, Any], sequence: int, surface: str) -> Dict[str, Any]:
    """校验 queue entry 并补充稳定 job_id。"""
    if "task" not in job or "seed" not in job:
        raise ValueError("each job requires task and seed")
    actual_surface = str(job.get("surface", surface))
    if actual_surface != surface:
        raise ValueError(
            f"job {job.get('job_id', sequence)!r} surface={actual_surface!r}, "
            f"but worker is fixed to {surface!r}"
        )
    item = dict(job)
    item["surface"] = surface
    item.setdefault("job_id", f"{surface}_{item['task']}_{int(item['seed'])}_{sequence:06d}")
    return item


def wait_ready(api: SeedReplayApi, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            grpc = api.grpc.ping()
            ws = api.ws.ping()
            state = api.grpc.get_state(player=api.player)
            print(f"[ready] grpc_tick={grpc['server_tick']} ws_api={ws.get('api_mode')} "
                  f"player={api.player} pos={state['player']['pos']}", flush=True)
            return
        except Exception as exc:  # noqa: BLE001 -- wait until external processes are healthy
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
    raise TimeoutError(f"worker readiness timed out after {timeout}s: {last_error}")


def final_paths(out_root: Path, surface: str, job_id: str) -> tuple[Path, Path]:
    episode_dir = out_root / surface / job_id
    return episode_dir, Path(f"{episode_dir}.mp4")


def publish_episode(temp_dir: Path, final_dir: Path) -> None:
    """先录到 .partial，再原子发布 episode 目录与同名 mp4。"""
    temp_mp4 = Path(f"{temp_dir}.mp4")
    final_mp4 = Path(f"{final_dir}.mp4")
    if final_dir.exists() or final_mp4.exists():
        raise FileExistsError(f"final output already exists: {final_dir}")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_dir, final_dir)
    if temp_mp4.exists():
        os.replace(temp_mp4, final_mp4)


def main() -> int:
    args = parse_args()
    jobs_path = Path(args.jobs).resolve()
    results_path = Path(args.results).resolve() if args.results else jobs_path.with_name("results.jsonl")
    out_root = Path(args.out_root).resolve()
    partial_root = out_root / ".partial" / args.worker_id
    completed = result_index(results_path)

    if not jobs_path.is_file():
        print(f"WORKER_FAIL: jobs file not found: {jobs_path}", file=sys.stderr)
        return 2

    api = SeedReplayApi(
        player=args.player,
        ws_url=args.ws_url,
        grpc_host=args.grpc_host,
        grpc_port=args.grpc_port,
        ticks_per_step=args.ticks,
    )
    processed = 0
    succeeded = 0
    failed = 0
    try:
        wait_ready(api, args.ready_timeout)
        selected = api.grpc.select_surface_world(
            player=args.player,
            surface=args.surface,
            seed=args.map_seed,
        )
        expected_worker = args.player.lower().replace(" ", "_")
        print(f"[map] world={selected['world_name']} worker={selected['worker_id']} "
              f"surface={selected['surface_id']} map_seed={selected['map_seed']} "
              f"created={selected['created']}", flush=True)
        if selected["worker_id"] != expected_worker:
            raise RuntimeError(
                f"server worker scope mismatch: expected={expected_worker} "
                f"actual={selected['worker_id']}"
            )

        for sequence, raw_job in enumerate(load_jsonl(jobs_path), start=1):
            job = normalize_job(raw_job, sequence, args.surface)
            job_id = str(job["job_id"])
            previous = completed.get(job_id)
            if previous and (previous.get("status") == "ok" or not args.retry_failed):
                print(f"[skip] job={job_id} previous_status={previous.get('status')}", flush=True)
                continue
            final_dir, final_mp4 = final_paths(out_root, args.surface, job_id)
            if final_dir.exists() and (final_dir / "episode_summary.json").exists() and final_mp4.exists():
                print(f"[skip] job={job_id} final artifacts already exist", flush=True)
                append_jsonl(results_path, {
                    "job_id": job_id, "status": "ok", "reason": "artifacts_preexisting",
                    "worker_id": args.worker_id, "player": args.player,
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
                continue
            if args.max_jobs and processed >= args.max_jobs:
                break

            processed += 1
            temp_dir = partial_root / f"{job_id}.{int(time.time() * 1000)}"
            config = EpisodeConfig.from_mapping(
                job,
                outdir=str(temp_dir),
                default_surface=args.surface,
                default_max_steps=args.max_steps,
                default_ticks=args.ticks,
                default_half_extent=args.half_extent,
                default_capture=args.capture,
                default_hud=not args.no_hud,
                default_humanize=not args.no_humanize,
                default_provision=not args.no_provision,
                # 关键：此 worker 已经在启动时选择并固定其专属地图。
                select_surface=False,
                worker_id=args.worker_id,
                map_seed=selected["map_seed"],
            )
            # job 文件不得把持久 worker 切回共享/重新选择地图的模式。
            config = replace(config, select_surface=False)
            print(f"[job] begin id={job_id} task={config.task} seed={config.seed}", flush=True)
            try:
                result = record_human_episode(api, config, compose_mp4=compose_mp4)
                if not result["ok"]:
                    raise RuntimeError(
                        f"episode incomplete success={result['success']} "
                        f"progress={result['progress']:.2f} align_ok={result['align_ok']} "
                        f"mp4_ok={result['mp4_ok']}"
                    )
                publish_episode(temp_dir, final_dir)
                succeeded += 1
                row = {
                    "job_id": job_id,
                    "status": "ok",
                    "worker_id": args.worker_id,
                    "player": args.player,
                    "surface": args.surface,
                    "map_seed": selected["map_seed"],
                    "world_name": selected["world_name"],
                    "outdir": str(final_dir),
                    "mp4_path": str(final_mp4),
                    "result": result,
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                append_jsonl(results_path, row)
                print(f"WORKER_EPISODE_OK id={job_id} progress={result['progress']:.2f} "
                      f"dir={final_dir}", flush=True)
            except Exception as exc:  # noqa: BLE001 -- isolate a failed episode
                failed += 1
                append_jsonl(results_path, {
                    "job_id": job_id,
                    "status": "failed",
                    "worker_id": args.worker_id,
                    "player": args.player,
                    "surface": args.surface,
                    "map_seed": selected["map_seed"],
                    "temp_outdir": str(temp_dir),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
                print(f"WORKER_EPISODE_FAIL id={job_id}: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
                # 失败目录保留以便诊断；下一 job 仍复用同一个客户端继续工作。

        print(f"WORKER_DONE worker={args.worker_id} processed={processed} "
              f"success={succeeded} failed={failed}", flush=True)
        return 0 if failed == 0 else 1
    except Exception as exc:  # noqa: BLE001
        print(f"WORKER_FAIL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
