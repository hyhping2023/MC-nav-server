"""读玩家状态 + 脚下/周围地形（调试辅助）。"""
import json
import sys

sys.path.insert(0, ".")
import grpc  # noqa: E402

import vla_env.proto.vla_pb2 as pb  # noqa: E402
import vla_env.proto.vla_pb2_grpc as pbgrpc  # noqa: E402

ch = grpc.insecure_channel("127.0.0.1:50051")
stub = pbgrpc.VlaServerStub(ch)
st = json.loads(stub.GetState(pb.StateRequest(player="agent0"), timeout=5).json)
p = st["player"]["pos"]
print("pos:", [round(v, 1) for v in p], "on_ground:", st["player"].get("on_ground"),
      "held:", st["inventory"].get("held_item"))
r = stub.GetVoxels(pb.VoxelRequest(
    player="agent0", center_x=int(p[0]), center_y=int(p[1]), center_z=int(p[2]),
    half_extent=5), timeout=5)
pal = list(r.palette)
n = r.size

def at(x, y, z):
    idx = ((x - r.origin_x) * n + (z - r.origin_z)) * n + (y - r.origin_y)
    return pal[r.data[idx]].split("[")[0] if 0 <= idx < len(r.data) else "OUT"

px, py, pz = int(p[0]), int(p[1]), int(p[2])
for dy in range(-1, 6):
    print(f"  y={py+dy}:",
          " ".join(at(px + dx, py + dy, pz).split(":")[1][:8] for dx in (-1, 0, 1)))
