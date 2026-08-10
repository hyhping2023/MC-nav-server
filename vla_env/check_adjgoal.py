"""检查 adjGoal=(-26,74,-145) 周围方块结构。"""
import sys
sys.path.insert(0, "scripts")
from vla_env.env import MinecraftEnv
from collect_wood_agent import name_at, blocks_3d

env = MinecraftEnv(player="agent0", task="collect_wood", ticks_per_step=2)
palette, data, origin, size = env.grpc.get_voxels(player="agent0", half_extent=16)
b3 = blocks_3d(palette, data, size)
for (x, y, z) in [(-26,74,-145), (-26,75,-145), (-26,73,-145), (-26,72,-145),
                  (-27,74,-145), (-25,74,-145), (-26,74,-144), (-26,74,-146),
                  (-24,74,-145), (-24,73,-145), (-24,72,-145), (-24,71,-145)]:
    print(f"({x},{y},{z}) = {name_at(b3, origin, (x,y,z))}")
env.close()
