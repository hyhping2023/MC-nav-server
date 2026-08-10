"""脱困测试：验证 NavExecutor 卡死恢复链（挖穿 / 绕行 / STUCK 上报）。

场景（均用 SetBlock 搭建在玩家当前位置走廊，测试后还原）：
  S1 墙挖穿：6 格石头直墙（2 厚 × 3 宽，脚层）+ 目标在墙后，
             goto_path 带 dig=[墙格] → 先挖穿再走到达（arrived）
  S2 绕行   ：基岩竖墙（不可挖）两侧留缝 → 本地重规划绕行到达（arrived）
  S3 彻底堵死：玩家脚下 3×3 石坑 + 四面基岩墙 + 头顶封死，
             恢复链用尽（replan→站挖→仍卡）后上报 STUCK

用法（必须在 vla_env/ 内运行）：
  .venv/bin/python scripts/unstick_test.py [--scene 1|2|3|all] [--steps N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, ".")

import grpc  # noqa: E402

import vla_env.proto.vla_pb2 as pb  # noqa: E402
import vla_env.proto.vla_pb2_grpc as pbgrpc  # noqa: E402
from vla_env.client_ws import ClientWs  # noqa: E402

WS_URL = "ws://127.0.0.1:30001"
GRPC = "127.0.0.1:50051"
PLAYER = "agent0"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default="all", choices=["1", "2", "3", "all"])
    ap.add_argument("--steps", type=int, default=800, help="每场景最长等待（0.1s/次轮询）")
    args = ap.parse_args()

    ch = grpc.insecure_channel(GRPC)
    stub = pbgrpc.VlaServerStub(ch)

    def setb(x: int, y: int, z: int, block: str) -> None:
        stub.SetBlock(pb.SetBlockRequest(
            player=PLAYER, pos=pb.Vec3(x=x, y=y, z=z), block=block,
            apply_physics=False), timeout=5)

    def get_pos() -> tuple[int, int, int]:
        st = json.loads(stub.GetState(pb.StateRequest(player=PLAYER), timeout=5).json)
        p = st["player"]["pos"]
        return int(p[0]), int(p[1]), int(p[2])

    def in_corr(x: int, z: int, px: int, pz: int) -> bool:
        return px - 1 <= x <= px + 9 and pz - 3 <= z <= pz + 3

    def build_corridor(px: int, py: int, pz: int) -> None:
        """玩家脚下起：地板 y=py-1 石头，y=py/py+1 清空，走廊 z=pz-3..pz+3，外墙。"""
        for x in range(px - 2, px + 11):
            for z in range(pz - 4, pz + 5):
                for y in (py - 1, py, py + 1):
                    if y == py - 1:
                        setb(x, y, z, "minecraft:stone")
                    elif in_corr(x, z, px, pz):
                        setb(x, y, z, "minecraft:air")
                    else:
                        setb(x, y, z, "minecraft:stone")

    def build_scene(scene: int, px: int, py: int, pz: int) -> tuple[tuple, list | None]:
        """搭障碍，返回 (目标格, dig 列表)。目标 = 走廊远端 (px+9, py, pz)。"""
        goal = (px + 9, py, pz)
        if scene == 1:                      # 直墙 2 厚 × 3 宽（脚层），目标墙后
            wall = []
            for x in (px + 4, px + 5):
                for z in (pz - 1, pz, pz + 1):
                    setb(x, py, z, "minecraft:stone")
                    setb(x, py + 1, z, "minecraft:air")
                    wall.append((x, py, z))
            return goal, wall               # dig = 墙脚层 6 格
        if scene == 2:                      # 基岩竖墙，两侧留缝（z=pz±2/±3 可绕）
            for z in (pz - 1, pz, pz + 1):
                setb(px + 4, py, z, "minecraft:bedrock")
                setb(px + 4, py + 1, z, "minecraft:bedrock")
            return goal, None
        # scene 3：脚下 3×3 坑 + 四面基岩墙 + 头顶封死
        for dz in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for y in (py, py + 1, py + 2):
                    setb(px + dx, y, pz + dz, "minecraft:air")
        for dz in (-2, 2):
            for x in range(px - 1, px + 2):
                for y in (py, py + 1, py + 2, py + 3):
                    setb(x, y, pz + dz, "minecraft:bedrock")
        for dx in (-2, 2):
            for z in range(pz - 1, pz + 2):
                for y in (py, py + 1, py + 2, py + 3):
                    setb(px + dx, y, z, "minecraft:bedrock")
        for x in range(px - 1, px + 2):
            for z in range(pz - 1, pz + 2):
                setb(x, py + 3, z, "minecraft:bedrock")
        return goal, None

    def run_scene(scene: int) -> bool:
        px, py, pz = get_pos()
        build_corridor(px, py, pz)
        goal, digs = build_scene(scene, px, py, pz)
        time.sleep(1.5)                     # 等客户端世界同步

        start = (px, py, pz)
        print(f"scene{scene}: start={start} goal={goal} dig={len(digs) if digs else 0}块")
        ws.send_goto_path([start, goal], dig=digs)

        deadline = time.time() + args.steps * 0.1
        terminal = None
        while time.time() < deadline:
            ws.recv_frame_latest(timeout=0.3)   # 消费 socket（JSON 文本经 recv 路由进 _text_q）
            evs = ws.drain_json(0.0, idle=0.01)
            for e in evs:
                if e.get("type") == "goto_status":
                    terminal = e
                    break
            if terminal:
                break
        ws.send_goto_cancel()

        if terminal is None:
            print(f"scene{scene}: TIMEOUT（{args.steps*0.1:.0f}s 内无 goto_status）")
            return False
        st = terminal.get("state")
        print(f"scene{scene}: goto_status={st} detail={terminal.get('detail','')}")
        # 场景 3（彻底堵死）预期上报 STUCK——恢复链用尽后正确上报即通过
        expect = "stuck" if scene == 3 else "arrived"
        return st == expect

    ws = ClientWs(WS_URL)
    ws.connect()
    r = ws.send_mode("api")
    print("mode:", r)

    ok = True
    results = {}
    scenes = ["1", "2", "3"] if args.scene == "all" else [args.scene]
    for s in scenes:
        results[s] = run_scene(int(s))
        ok = ok and results[s]

    # 还原：重建走廊（清障碍）
    px, py, pz = get_pos()
    build_corridor(px, py, pz)
    print("terrain restored")

    print("\n=== 脱困测试结果 ===")
    for s, o in results.items():
        print(f"  scene{s}: {'PASS' if o else 'FAIL'}")
    print("总结:", "全部通过" if ok else "有失败")
    ws.close()


if __name__ == "__main__":
    main()
