"""随机 agent：验证本地 Env 全链路（bridge -> Lua 环境）。"""

from __future__ import annotations

import time

from mcl2_env.env import GymnasiumEnv
from mcl2_env.renderer.voxel import VoxelRenderer


def main() -> None:
    env = GymnasiumEnv(
        player="bot1",
        bridge_host="127.0.0.1",
        bridge_port=25585,
        renderer=None,  # 骨架阶段无渲染器；有 fork 后换成 EngineForkRenderer()
        action_mode="primitive",
        run_id="demo",
    )
    env.reset(options={"task": "collect_wood"})

    for i in range(600):
        action = env.action_space.sample()
        obs, reward, terminated, truncated, info = env.step(action)
        if i % 100 == 0:
            print(f"[step {i}] terminated={terminated} truncated={truncated}")
        if terminated or truncated:
            print("episode finished")
            env.reset(options={"task": "craft_planks"})
        time.sleep(0.05)

    env.close()


if __name__ == "__main__":
    main()
