"""真实 VLA 推理模板（给用户 GPU 服务器用）——本文件**不执行**任何推理。

复制到你的 GPU 服务器，替换 `run_inference` 里的 TODO 为真实模型调用，
然后配合 mcl2_env.server 或 AgentClient 跑通闭环。

流程（与 vla_interface_demo.py 相同，只换 adapter）：
    /session -> /reset(task=...) -> run_inference(adapter, obs) -> /step -> done

本模板只定义接口占位函数，import 后不执行任何东西。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 包导入引导：直接 `python examples/real_inference_template.py` 也可运行
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.adapters import get_adapter


def run_inference(adapter: Any, obs: dict[str, Any]) -> dict[str, Any]:
    """占位：把 obs 喂给真实模型，返回环境动作。

    TODO(用户服务器)：
        1. model_input = adapter.encode_obs(obs)      # 观测 -> 模型输入
        2. model_out   = YOUR_MODEL.forward(model_input)  # 在此加载真实模型并推理
        3. action      = adapter.decode_action(model_out) # 模型输出 -> 环境动作
        4. return action
    """
    # 兜底（无模型时）：直接用 MockAdapter 生成动作，保证链路可跑
    model_input = adapter.encode_obs(obs)
    return adapter.decode_action(model_input)


def main() -> None:
    """真实推理闭环骨架（默认不执行——请先替换 run_inference 并设 INFERENCE=1）。"""
    import os

    if os.environ.get("MCL2_REAL_INFERENCE") != "1":
        print("SKIP: real inference disabled. Set MCL2_REAL_INFERENCE=1 on your GPU server.")
        return

    # 换成你接入的模型：mock/openvla/pi0/groot/steve1（真实适配器 is_available() 为 True）
    adapter = get_adapter("mock")
    assert adapter.is_available(), "adapter has no loaded model weights"

    from mcl2_env.client import AgentClient

    with AgentClient("http://127.0.0.1:8000", adapter="mock") as client:
        obs, info = client.reset(task="craft_planks", seed=42)
        done = False
        while not done:
            action = run_inference(adapter, obs)
            obs, reward, terminated, truncated, info = client.step(action)
            done = terminated or truncated
        print(f"episode done: terminated={terminated} truncated={truncated}")


if __name__ == "__main__":
    main()
