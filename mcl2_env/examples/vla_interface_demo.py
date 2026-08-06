"""VLA 接口 Mock 全流程演示（m3m4_protocol.md §4.3）——全程无推理。

流程：AgentClient -> /session -> /reset(task=craft_planks) -> 循环用 MockAdapter
生成动作 -> /step -> 结束。

MockAdapter 的 encode_obs / decode_action 只是观测摘要 + 全零动作，**不加载任何
模型**。真实推理请参考 examples/real_inference_template.py。

运行前需先启动 server（在装了 fastapi 的机器上）：
    python -m mcl2_env.server --world <world_dir> --adapter mock
"""

from __future__ import annotations

import sys
from pathlib import Path

# 包导入引导：直接 `python examples/vla_interface_demo.py` 也可运行
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.adapters import get_adapter


def main() -> None:
    # 延迟导入：client 只在真正调用时连接 server
    from mcl2_env.client import AgentClient

    adapter = get_adapter("mock")  # 本地冒烟适配器（无模型）

    with AgentClient("http://127.0.0.1:8000", adapter="mock") as client:
        # 1) 创建会话（AgentClient.__init__ 已 POST /session）
        print(f"session_id={client.session_id}  adapter={adapter.name}")

        # 2) 重置 episode
        obs, info = client.reset(task="craft_planks", seed=42)
        print(f"reset info: {info}")
        print(f"task: {(obs.get('task') or {}).get('id')}  "
              f"instruction: {(obs.get('task') or {}).get('instruction')}")

        # 3) Mock 推理循环：encode_obs -> decode_action -> /step
        steps = 0
        done = False
        while not done and steps < 60:
            steps += 1
            model_input = adapter.encode_obs(obs)      # 观测 -> "模型输入"（摘要）
            action = adapter.decode_action(model_input)  # "模型输出" -> 环境动作
            obs, reward, terminated, truncated, info = client.step(action)
            done = terminated or truncated
            if steps % 10 == 0:
                print(f"  step={steps} terminated={terminated} truncated={truncated} "
                      f"frame_available={info.get('frame_available')}")

        print(f"episode done after {steps} steps: terminated={terminated} "
              f"truncated={truncated} reward={reward}")
        # 4) 收尾展示：任务列表 / 观测 / 帧
        print(f"tasks: {client.tasks()}")
        obs_now = client.observe()
        print(f"observe keys: {sorted(obs_now.keys())}")
        png = client.visualize()
        print(f"visualize: {'png ' + str(len(png)) + ' bytes' if png else 'no frame'}")


if __name__ == "__main__":
    main()
