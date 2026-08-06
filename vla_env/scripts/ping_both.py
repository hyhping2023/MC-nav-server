#!/usr/bin/env python3
"""M1 验收：双端 ping 打通（WS 客户端 pong + gRPC 服务端 server_tick）。

用法：
    .venv/bin/python scripts/ping_both.py
    .venv/bin/python scripts/ping_both.py --ws-url ws://127.0.0.1:30001 \
        --grpc-host 127.0.0.1 --grpc-port 50051

流程：WS ping → 打印 pong（ts/api_mode）；gRPC ping → 打印
server_tick/tps/version/world_name；两者都成功打印 `M1_PING_BOTH_OK` 并
exit 0，任一失败打印错误信息并 exit 1。
"""

from __future__ import annotations

import argparse
import sys


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M1 双端 ping 验收（WS + gRPC）")
    p.add_argument("--ws-url", default="ws://127.0.0.1:30001",
                   help="WS harness 地址（默认 ws://127.0.0.1:30001）")
    p.add_argument("--grpc-host", default="127.0.0.1",
                   help="gRPC 服务端 host（默认 127.0.0.1）")
    p.add_argument("--grpc-port", type=int, default=50051,
                   help="gRPC 服务端 port（默认 50051）")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # ---- WS ping ----
    pong = None
    try:
        from vla_env.client_ws import ClientWs
        with ClientWs(url=args.ws_url) as ws:
            pong = ws.ping()
        print(f"WS pong: ts={pong.get('ts')} api_mode={pong.get('api_mode')} "
              f"type={pong.get('type')}")
    except Exception as e:  # noqa: BLE001 —— 验收脚本需捕获并如实上报
        print(f"WS ping 失败: {type(e).__name__}: {e}", file=sys.stderr)

    # ---- gRPC ping ----
    reply = None
    try:
        from vla_env.server_grpc import ServerGrpc
        sg = ServerGrpc(host=args.grpc_host, port=args.grpc_port)
        try:
            reply = sg.ping()
        finally:
            sg.close()
        print(f"gRPC PingReply: server_tick={reply.get('server_tick')} "
              f"tps={reply.get('tps'):.2f} version={reply.get('version')} "
              f"world_name={reply.get('world_name')}")
    except Exception as e:  # noqa: BLE001
        print(f"gRPC ping 失败: {type(e).__name__}: {e}", file=sys.stderr)

    if pong is not None and reply is not None:
        print("M1_PING_BOTH_OK")
        return 0

    print("M1_PING_BOTH_FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
