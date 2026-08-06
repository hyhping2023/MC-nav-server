#!/usr/bin/env python3
"""ModelAdapter 单元测试：MockAdapter + registry + 骨架适配器（均不触发模型 import）。

运行：
    python3 -m pytest mcl2_env/tests/test_adapters.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---- 包导入引导（同 test_file_bridge.py，不依赖 pip install -e）----
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.adapters import MockAdapter, ModelAdapter, get_adapter  # noqa: E402
from mcl2_env.adapters.registry import get_adapter as get_adapter_factory  # noqa: E402


OBS = {
    "task": {"id": "craft_planks", "instruction": "craft 4 planks", "success": False, "steps": 3},
    "player": {"pos": {"x": 1, "y": 2, "z": 3}, "look": {"yaw": 0.5, "pitch": 0.1}},
    "image": [[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]]],
}


# ---------------------------------------------------------------- MockAdapter

def test_mock_is_model_adapter() -> None:
    assert isinstance(MockAdapter(), ModelAdapter)
    assert MockAdapter().name == "mock"


def test_mock_encode_obs_summary() -> None:
    s = MockAdapter().encode_obs(OBS)
    assert s["task_id"] == "craft_planks"
    assert s["instruction"] == "craft 4 planks"
    assert s["player_pos"] == [1, 2, 3]
    assert s["image_shape"] == [2, 2, 3]
    assert s["has_image"] is True


def test_mock_encode_obs_handles_base64_image() -> None:
    obs = dict(OBS, image={"encoding": "base64", "mime": "image/png", "shape": [2, 2, 3], "data": "abc"})
    s = MockAdapter().encode_obs(obs)
    assert s["image_shape"] == [2, 2, 3]
    assert s["has_image"] is True


def test_mock_encode_obs_no_image() -> None:
    obs = dict(OBS, image=None)
    s = MockAdapter().encode_obs(obs)
    assert s["has_image"] is False
    assert s["image_shape"] is None


def test_mock_decode_action_zero_action() -> None:
    a = MockAdapter().decode_action(None)
    for k in ("forward", "back", "left", "right", "jump", "sneak", "sprint", "attack", "use", "drop"):
        assert a[k] == 0
    assert a["hotbar"] == 0
    assert a["camera"] == [0.0, 0.0]


def test_mock_not_available() -> None:
    assert MockAdapter().is_available() is False


# ---------------------------------------------------------------- registry

def test_registry_mock() -> None:
    assert isinstance(get_adapter("mock"), MockAdapter)


def test_registry_all_known_names() -> None:
    for name in ("mock", "openvla", "pi0", "groot", "steve1"):
        a = get_adapter_factory(name)
        assert isinstance(a, ModelAdapter)
        assert a.name == name


def test_registry_case_insensitive() -> None:
    assert isinstance(get_adapter("MOCK"), MockAdapter)


def test_registry_unknown_name() -> None:
    try:
        get_adapter("no_such_model")
        raise AssertionError("expected ValueError for unknown adapter")
    except ValueError:
        pass


# ---------------------------------------------------------------- skeleton adapters

def test_skeleton_adapters_import_without_model_libs() -> None:
    """骨架适配器可导入、可调用，且不抛 ImportError（不加载模型库）。"""
    model_outs = {
        "openvla": [1, 12, 14],  # 离散动作 token id 序列
        "pi0": [1, 0, 0],
        "groot": {"buttons": {"forward": True}, "camera": [0.1, -0.2], "hotbar": 1},
        "steve1": {"buttons": {"jump": True}, "camera": [1.0, 2.0]},
    }
    for name in ("openvla", "pi0", "groot", "steve1"):
        a = get_adapter(name)
        assert a.is_available() is False  # 未接入真实模型恒 False
        assert a.encode_obs(OBS) is not None  # 只做字段映射，无推理
        act = a.decode_action(model_outs[name])
        assert isinstance(act, dict)
        assert "camera" in act


def test_openvla_decode_token_sequence() -> None:
    a = get_adapter("openvla")
    act = a.decode_action([1, 12, 14])  # forward + camera up + hotbar 1
    assert act["forward"] == 1
    assert act["hotbar"] == 1
    assert act["camera"] != [0.0, 0.0]


def test_groot_decode_minestudio_action() -> None:
    a = get_adapter("groot")
    out = {"buttons": {"forward": True, "jump": True}, "camera": [0.1, -0.2], "hotbar": 2}
    act = a.decode_action(out)
    assert act["forward"] == 1
    assert act["jump"] == 1
    assert act["camera"] == [0.1, -0.2]
    assert act["hotbar"] == 2
