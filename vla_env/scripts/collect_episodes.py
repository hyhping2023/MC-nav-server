#!/usr/bin/env python3
"""M8 验收：collect_episodes —— 全栈 episode 采集 + tick/frame 对齐断言。

用法（在 vla_env/ 目录内，避免 namespace 遮蔽）：
    .venv/bin/python scripts/collect_episodes.py --episodes 10 --steps 60 --ticks 2

每 episode：
    1. env.reset(task="collect_wood")（ResetWorld + SetTask + mode=api + 收首帧）
    2. 循环 S 步随机动作（lockstep，§9.3）：
         ws.send_action → frame=ws.recv_frame() → gRPC get_step_result →
         gRPC get_state
    3. 逐 step 用 lockstep.Aligner 断言（窗口 0<=server_tick-frame_tick
       <=ticks_per_step+tol + frame_id/server_tick 单调不减）
    4. 记录一行 JSONL（写 /tmp，完整落盘属 M10）：
       {ep, step, frame_id, frame_tick, server_tick, wall_nanos, reward, terminated}
    5. episode 结束统计 frames==actions==rewards==states 四者计数

汇总：全部 episode 计数一致 + 对齐率 100% → 打印
`M8_ALIGN_OK episodes=N align_rate=1.00` exit 0；
否则如实打印实际数据（各 episode 计数 / mismatch / max_diff / 对齐率）exit 1。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List

from vla_env.action_space import random_action
from vla_env.env import MinecraftEnv
from vla_env.lockstep import Aligner


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M8 全栈 episode 采集 + tick/frame 对齐验收")
    p.add_argument("--episodes", type=int, default=10, help="episode 数（默认 10）")
    p.add_argument("--steps", type=int, default=60, help="每 episode 步数（默认 60）")
    p.add_argument("--ticks", type=int, default=2, help="每步服务端 tick 数（默认 2）")
    p.add_argument("--tol", type=int, default=2, help="对齐容差 tick（默认 2）")
    p.add_argument("--player", default="agent0")
    p.add_argument("--out", default="/tmp/m8_episodes.jsonl", help="JSONL 输出路径")
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


def main() -> int:
    args = parse_args()
    out_path = args.out

    env = MinecraftEnv(player=args.player, task="collect_wood", ticks_per_step=args.ticks)
    aligner = Aligner(ticks_per_step=args.ticks, tol=args.tol)

    total = {"frames": 0, "actions": 0, "rewards": 0, "states": 0}
    ep_counts: List[Dict[str, Any]] = []
    rows_written = 0

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            for ep in range(1, args.episodes + 1):
                reset_with_retry(env, args.player)
                counters = {"frames": 0, "actions": 0, "rewards": 0, "states": 0}

                for step_idx in range(1, args.steps + 1):
                    action = random_action()
                    env.ws.send_action(action)
                    counters["actions"] += 1

                    frame = env.ws.recv_frame_latest(timeout=2.0)
                    if frame is None:
                        # 帧超时：如实记录（counters 天然不一致，汇总阶段会暴露）
                        print(f"[ep{ep}] step={step_idx} FRAME_TIMEOUT", file=sys.stderr)
                        continue

                    step = env.grpc.get_step_result(
                        player=args.player, await_ticks=args.ticks
                    )
                    counters["rewards"] += 1

                    env.grpc.get_state(player=args.player)
                    counters["states"] += 1  # get_state 计数（四者之一）

                    rec = aligner.check(frame, step)
                    if not rec["ok"]:
                        print(
                            f"[ep{ep}] step={step_idx} MISALIGN {rec}",
                            file=sys.stderr,
                        )

                    row = {
                        "ep": ep,
                        "step": step_idx,
                        "frame_id": frame.frame_id,
                        "frame_tick": frame.server_tick,
                        "server_tick": step["server_tick"],
                        "wall_nanos": frame.wall_nanos,
                        "reward": step["reward"],
                        "terminated": step["terminated"],
                    }
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    rows_written += 1
                    counters["frames"] += 1

                    if step["terminated"]:
                        break

                for k in total:
                    total[k] += counters[k]
                ep_counts.append({"ep": ep, **counters})
                consistent = len(set(counters.values())) == 1
                print(
                    f"[ep{ep}] frames={counters['frames']} actions={counters['actions']} "
                    f"rewards={counters['rewards']} states={counters['states']} "
                    f"consistent={consistent}"
                )
    except Exception as e:  # noqa: BLE001 —— 如实上报
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        print("M8_ALIGN_FAIL (exception)", file=sys.stderr)
        return 1
    finally:
        env.close()

    report = aligner.report()
    counts_ok = len(set(total.values())) == 1 and total["frames"] > 0
    align_ok = report["align_rate"] == 1.0 and report["mismatch"] == 0

    print("---- M8 summary ----")
    print(f"rows_written={rows_written} (-> {out_path})")
    for c in ep_counts:
        print(f"  ep{c['ep']}: frames={c['frames']} actions={c['actions']} "
              f"rewards={c['rewards']} states={c['states']}")
    print(f"total: frames={total['frames']} actions={total['actions']} "
          f"rewards={total['rewards']} states={total['states']} "
          f"counts_consistent={counts_ok}")
    print(f"align: steps={report['steps']} mismatch={report['mismatch']} "
          f"align_rate={report['align_rate']:.2f} max_diff={report['max_diff']} "
          f"(ticks_per_step={report['ticks_per_step']} tol={report['tol']})")

    if counts_ok and align_ok:
        print(f"M8_ALIGN_OK episodes={args.episodes} align_rate={report['align_rate']:.2f}")
        return 0

    print(
        f"M8_ALIGN_FAIL counts_consistent={counts_ok} align_rate={report['align_rate']:.2f} "
        f"mismatch={report['mismatch']} max_diff={report['max_diff']}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
