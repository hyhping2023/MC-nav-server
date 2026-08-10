"""检查 (-22,74,-145) 与 (-24,74,-145) 树顶原木周围结构。"""
import sys
sys.path.insert(0, "scripts")
from vla_env.env import MinecraftEnv
from collect_wood_agent import name_at, blocks_3d

env = MinecraftEnv(player="agent0", task="collect_wood", ticks_per_step=2)
palette, data, origin, size = env.grpc.get_voxels(player="agent0", half_extent=16)
b3 = blocks_3d(palette, data, size)
for y in range(70, 78):
    for (x, z) in [(-22, -145), (-24, -145), (-26, -145), (-27, -145)]:
        n = name_at(b3, origin, (x, y, z))
        if n != "minecraft:air":
            print(f"({x},{y},{z}) = {n}")
env.close()
