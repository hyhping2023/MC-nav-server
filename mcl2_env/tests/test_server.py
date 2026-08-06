#!/usr/bin/env python3
"""VLAServer 单元测试：MockAdapter 下 /session /reset /step /observe /tasks
/visualize /record /generate_task 全链路冒烟（直接调 handler，不依赖 fastapi）。

运行：
    python3 -m pytest mcl2_env/tests/test_server.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

# ---- 包导入引导（同 test_file_bridge.py）----
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.server import BridgeEnv, VLAServer  # noqa: E402
from mcl2_env.adapters import MockAdapter, get_adapter  # noqa: E402


# ---------------------------------------------------------------- fakes

class FakeRenderer:
    """返回固定非纯色帧（16x16），让 /visualize 出真 PNG。"""

    def __init__(self) -> None:
        img = np.zeros((16, 16, 3), dtype=np.uint8)
        img[:, :, 0] = np.arange(16)[:, None]  # 非纯色，避免 PIL 单色压缩
        img[:, :, 1] = np.arange(16)[None, :]
        self.frame = SimpleNamespace(image=img)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_frame(self) -> SimpleNamespace:
        return self.frame


class FakeBridge:
    """模拟 Lua 侧：record 请求式采样 + 任务注册表。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.success = False

    def begin_episode(self, spec: dict) -> dict:
        self.calls.append(("begin", spec))
        return {"episode": spec.get("episode_id")}

    def end_episode(self, success: bool, player: str) -> dict:
        self.calls.append(("end", success, player))
        return {"ok": True}

    def observe(self, player: str) -> dict:
        return {
            "player": {"pos": {"x": 1.0, "y": 2.0, "z": 3.0}, "look": {"yaw": 0.0, "pitch": 0.0}},
            "task": {"id": "craft_planks", "instruction": "craft 4 planks",
                     "success": self.success, "steps": 0},
            "world": {"timeofday": 0.5, "biome": "plains", "nearby_blocks": []},
            "inventory": {"main": [{"item": "mcl_trees:tree_oak", "count": 3}]},
            "stats": {"xp": 0, "level": 0},
            "episode": {"frame": 0},
        }

    def execute(self, action: str, args: dict, player: str) -> dict:
        self.calls.append(("execute", action, args))
        return {"action_id": f"aid-{len(self.calls)}"}

    def step(self, primitive: dict, player: str) -> dict:
        self.calls.append(("step", primitive))
        return {"ok": True}

    def tasks(self) -> list[dict]:
        return [{"id": "craft_planks", "instruction": "craft 4 planks", "difficulty": 1}]

    def request(self, op: str, **kwargs: object) -> dict:
        self.calls.append(("request", op, kwargs))
        return {"tasks": self.tasks(), "registered": {"kind": kwargs.get("kind")}}

    def close(self) -> None:
        pass


def make_server(obs_mode: str = "list") -> tuple[VLAServer, str, FakeBridge]:
    bridge = FakeBridge()

    def env_factory() -> BridgeEnv:
        return BridgeEnv(player="bot1", bridge=bridge, renderer=FakeRenderer(), img_size=(16, 16))

    srv = VLAServer(env_factory=env_factory, adapter_factory=lambda: get_adapter("mock"), obs_mode=obs_mode)
    sid = srv.new_session()
    return srv, sid, bridge


def _assert_json_safe(obj: object) -> None:
    json.dumps(obj)  # 结果必须可 JSON 序列化（无 numpy 类型）


# ---------------------------------------------------------------- tests

def test_new_session_holds_env_and_mock_adapter() -> None:
    srv, sid, _ = make_server()
    assert sid in srv.sessions
    assert isinstance(srv._adapter(sid), MockAdapter)
    srv.close_session(sid)
    assert sid not in srv.sessions


def test_reset_returns_obs_and_info() -> None:
    srv, sid, bridge = make_server()
    out = srv.handle_reset(sid, task="craft_planks", seed=42)
    assert out["obs"]["task"]["id"] == "craft_planks"
    assert out["info"]["episode_id"].startswith("ep-")
    assert bridge.calls[0][0] == "begin"
    assert bridge.calls[0][1]["task_id"] == "craft_planks"
    assert bridge.calls[0][1]["task_seed"] == 42
    _assert_json_safe(out)


def test_step_primitive() -> None:
    srv, sid, bridge = make_server()
    srv.handle_reset(sid, task="craft_planks")
    out = srv.handle_step(sid, {"forward": 1, "jump": 0, "camera": [0.1, -0.2]})
    assert out["reward"] == 0.0
    assert out["terminated"] is False
    assert out["truncated"] is False
    assert bridge.calls[-1][0] == "step"
    assert bridge.calls[-1][1]["forward"] is True
    _assert_json_safe(out)


def test_step_semantic() -> None:
    srv, sid, bridge = make_server()
    srv.handle_reset(sid, task="craft_planks")
    out = srv.handle_step(sid, {"id": "goto", "args": {"pos": {"x": 1, "y": 2, "z": 3}}})
    assert out["terminated"] is False
    assert bridge.calls[-1][0] == "execute"
    assert bridge.calls[-1][1] == "goto"
    _assert_json_safe(out)


def test_step_task_success_ends_episode() -> None:
    srv, sid, bridge = make_server()
    srv.handle_reset(sid, task="craft_planks")
    bridge.success = True
    out = srv.handle_step(sid, {"forward": 0})
    assert out["terminated"] is True
    assert bridge.calls[-1][0] == "end"
    assert bridge.calls[-1][1] is True  # success=True


def test_observe() -> None:
    srv, sid, _ = make_server()
    srv.handle_reset(sid, task="craft_planks")
    obs = srv.handle_observe(sid)
    assert "image" in obs
    assert obs["image"] is not None  # renderer 注入
    assert obs["player"]["pos"]["x"] == 1.0
    _assert_json_safe(obs)


def test_visualize_returns_png_base64() -> None:
    srv, sid, _ = make_server()
    srv.handle_reset(sid, task="craft_planks")
    png_b64 = srv.handle_visualize(sid)["png"]
    assert png_b64 is not None
    png = base64.b64decode(png_b64)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "should be a valid PNG"


def test_visualize_no_frame() -> None:
    srv, sid, _ = make_server()
    # 未 reset：BridgeEnv 无 _last_obs
    assert srv.handle_visualize(sid)["png"] is None


def test_tasks_from_bridge() -> None:
    srv, sid, _ = make_server()
    tasks = srv.handle_tasks()
    assert tasks[0]["id"] == "craft_planks"


def test_generate_task_procedural() -> None:
    srv, sid, bridge = make_server()
    out = srv.handle_generate_task(sid, kind="procedural", item="mcl_trees:wood_oak")
    assert out["status"] == "ok"
    assert out["result"]["tasks"], "procedural 应生成任务"
    assert bridge.calls[-1][1] == "task_generate"
    assert bridge.calls[-1][2]["kind"] == "procedural"


def test_generate_task_curriculum() -> None:
    srv, sid, bridge = make_server()
    out = srv.handle_generate_task(sid, kind="curriculum", max_difficulty=3)
    assert out["status"] == "ok"
    assert out["result"]["tasks"]
    assert bridge.calls[-1][2]["kind"] == "curriculum"


def test_generate_task_llm() -> None:
    srv, sid, bridge = make_server()
    out = srv.handle_generate_task(sid, kind="llm", prompt="Mine 3 stone blocks")
    assert out["status"] == "ok"
    assert bridge.calls[-1][2]["kind"] == "llm"


def test_generate_task_procedural_requires_items() -> None:
    srv, sid, _ = make_server()
    out = srv.handle_generate_task(sid, kind="procedural")
    assert out["status"] == "error"


def test_record_start_stop() -> None:
    srv, sid, _ = make_server()
    assert srv.handle_record(sid, "stop") == {"recording": False}
    assert srv.handle_record(sid, "start") == {"recording": True}


def test_obs_mode_base64() -> None:
    srv, sid, _ = make_server(obs_mode="base64")
    srv.handle_reset(sid, task="craft_planks")
    obs = srv.handle_observe(sid)
    img = obs["image"]
    assert isinstance(img, dict)
    assert img["encoding"] == "base64"
    assert img["shape"] == [16, 16, 3]
    _assert_json_safe(obs)


def test_adapter_forwarding() -> None:
    srv, sid, _ = make_server()
    srv.handle_reset(sid, task="craft_planks")
    enc = srv.encode_obs(sid)  # 通过 server 调 adapter.encode_obs
    assert enc["task_id"] == "craft_planks"
    act = srv.decode_action(sid, None)  # 通过 server 调 adapter.decode_action
    assert act["forward"] == 0


def test_execute() -> None:
    srv, sid, bridge = make_server()
    out = srv.handle_execute(sid, "goto", {"pos": {"x": 1, "y": 2, "z": 3}})
    assert out["action_id"].startswith("aid-")
    assert bridge.calls[-1][1] == "goto"


# ---------------------------------------------------------------- FastAPI 层（可选）

def test_fastapi_app_wiring() -> None:
    """fastapi 已装时才跑：build_fastapi_app 注册全部端点（含 /ws）。"""
    pytest.importorskip("fastapi")
    from mcl2_env.server import build_fastapi_app

    srv, sid, _ = make_server()
    app = build_fastapi_app(srv)
    paths = {getattr(r, "path", "") for r in app.routes if getattr(r, "path", "")}
    expected = {"/session", "/reset", "/step", "/execute", "/observe", "/tasks",
                "/generate_task", "/record/start", "/record/stop", "/visualize", "/ws"}
    assert expected <= paths, f"missing endpoints: {expected - paths}"
