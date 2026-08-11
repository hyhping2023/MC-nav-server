#!/usr/bin/env python3
"""把一个 surface 的 episode 清单均分给多个独占 worker。

这不是在线抢占队列：它会生成每个 worker 独占的 JSONL，避免多个进程争抢同一条任务。
每个 worker 只绑定一个 surface map；要同时录制多种材质，请为每种材质分别运行一次
coordinator（可使用不同 --start-index）。

输入 JSONL 每行至少：

    {"job_id": "sand_stone_000001", "task": "dig_stone", "seed": 1}

可选字段会原样透传至 record_worker，例如 max_steps/capture/hud/humanize/spawn。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="为并发持久录制 worker 分配静态 JSONL 队列")
    parser.add_argument("--jobs", required=True, help="输入 job JSONL")
    parser.add_argument("--surface", required=True, help="本批任务的固定地表材质")
    parser.add_argument("--workers", type=int, required=True, help="worker 数量")
    parser.add_argument("--runtime-root", default="../runtime")
    parser.add_argument("--out-root", default="../datasets/demo_human")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--base-ws-port", type=int, default=30001)
    parser.add_argument("--player-prefix", default="agent")
    parser.add_argument("--map-seed-base", type=int, default=10000,
                        help="worker i 的 map seed = base + i")
    parser.add_argument("--overwrite-queues", action="store_true",
                        help="允许覆盖已有 worker jobs.jsonl；默认拒绝，防误丢队列")
    return parser.parse_args()


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{line_no}: job must be object")
            yield item


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        print("--workers must be >= 1", file=sys.stderr)
        return 2
    jobs_path = Path(args.jobs).resolve()
    runtime_root = Path(args.runtime_root).resolve()
    out_root = Path(args.out_root).resolve()
    jobs: List[Dict[str, Any]] = []
    try:
        for sequence, raw in enumerate(load_jsonl(jobs_path), start=1):
            job = dict(raw)
            job_surface = str(job.get("surface", args.surface))
            if job_surface != args.surface:
                raise ValueError(
                    f"job {job.get('job_id', sequence)!r} has surface={job_surface!r}; "
                    f"coordinator surface={args.surface!r}"
                )
            job["surface"] = args.surface
            job.setdefault("job_id", f"{args.surface}_{job['task']}_{int(job['seed'])}_{sequence:06d}")
            jobs.append(job)
    except Exception as exc:  # noqa: BLE001
        print(f"COORDINATOR_FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    queues: List[List[Dict[str, Any]]] = [[] for _ in range(args.workers)]
    for index, job in enumerate(jobs):
        queues[index % args.workers].append(job)

    for offset, queue in enumerate(queues):
        worker_index = args.start_index + offset
        worker_id = f"worker-{worker_index:02d}"
        player = f"{args.player_prefix}{worker_index:02d}"
        worker_dir = runtime_root / worker_id
        queue_path = worker_dir / "jobs.jsonl"
        if queue_path.exists() and not args.overwrite_queues:
            print(f"COORDINATOR_FAIL: existing queue: {queue_path} "
                  f"(pass --overwrite-queues to replace)", file=sys.stderr)
            return 2
        worker_dir.mkdir(parents=True, exist_ok=True)
        with queue_path.open("w", encoding="utf-8") as handle:
            for job in queue:
                handle.write(json.dumps(job, ensure_ascii=False, sort_keys=True) + "\n")

        ws_port = args.base_ws_port + worker_index
        map_seed = args.map_seed_base + worker_index
        print(f"[queue] {worker_id}: jobs={len(queue)} player={player} ws={ws_port} "
              f"map_seed={map_seed} file={queue_path}")
        print(
            "  cd vla_env && .venv/bin/python -u scripts/record_worker.py"
            f" --worker-id {worker_id} --player {player} --ws-url ws://127.0.0.1:{ws_port}"
            f" --surface {args.surface} --map-seed {map_seed}"
            f" --jobs {queue_path} --out-root {out_root}"
        )

    print(f"COORDINATOR_OK surface={args.surface} jobs={len(jobs)} workers={args.workers}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
