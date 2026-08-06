#!/usr/bin/env python3
"""FileBridgeClient 单元测试：用临时目录模拟 Lua 侧文件行为（无服务器）。

运行方式（任选其一）：
    python3 -m pytest mcl2_env/tests/
    python3 mcl2_env/tests/test_file_bridge.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---- 包导入引导：不依赖 pip install -e ----
# bridge.py 只用标准库；若 mcl2_env 包因缺依赖（pydantic/gymnasium）无法导入，
# 直接按文件路径加载 bridge.py，避免触发包 __init__ 的完整依赖链。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))


def _load_bridge():
    try:
        from mcl2_env.bridge import BridgeError, FileBridgeClient
        return BridgeError, FileBridgeClient
    except ImportError:
        import importlib.util

        bridge_path = _PROJECT_ROOT / "mcl2_env" / "bridge.py"
        spec = importlib.util.spec_from_file_location("_mcl2_env_bridge", bridge_path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod.BridgeError, mod.FileBridgeClient


BridgeError, FileBridgeClient = _load_bridge()


def make_world(tmp: Path):
    """建一个模拟世界目录 + 连接它的 FileBridgeClient。"""
    world = tmp / "world"
    ipc = world / "mcl2_agent" / "ipc"
    for d in ("requests", "responses", "events"):
        (ipc / d).mkdir(parents=True)
    bridge = FileBridgeClient(world, timeout=5.0, poll_interval=0.02)
    return world, bridge


def lua_respond(world: Path, req_id: int, *, ok: bool = True, result=None, delay: float = 0.0) -> threading.Thread:
    """后台线程模拟 Lua 侧：等请求文件 → 校验 → 原子写响应文件。

    返回线程；客户端 request() 返回时线程通常已跑完。
    """
    ipc = world / "mcl2_agent" / "ipc"
    req_dir, resp_dir = ipc / "requests", ipc / "responses"

    def run() -> None:
        deadline = time.monotonic() + 5
        req_path: Path | None = None
        while time.monotonic() < deadline:
            matches = sorted(req_dir.glob("req_*.json"))
            if matches:
                req_path = matches[-1]
                break
            time.sleep(0.01)
        assert req_path is not None, "no request file appeared"
        # 请求文件名必须与协议一致：req_<seq>.json（seq 与 req_id 同步递增）
        assert req_path.name == f"req_{req_id}.json", f"unexpected request file: {req_path.name}"
        req = json.loads(req_path.read_text("utf-8"))
        assert req.get("req_id") == req_id, f"req_id mismatch: {req}"
        req_path.unlink()  # Lua 处理后删除
        if delay:
            time.sleep(delay)
        payload = {"req_id": req_id, "ok": ok, "result": result or {}}
        tmp = resp_dir / f"resp_{req_id}.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        os.replace(tmp, resp_dir / f"resp_{req_id}.json")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- tests

def test_wait_ready() -> None:
    with tempfile.TemporaryDirectory() as td:
        world, bridge = make_world(Path(td))
        ipc = world / "mcl2_agent" / "ipc"

        # 握手 ping（req_id=1）由 mock Lua 侧响应
        lua_respond(world, 1, result={"pong": True, "version": "test-0.1", "tick": 1})

        def write_ready() -> None:
            time.sleep(0.05)
            tmp = ipc / "ready.json.tmp"
            tmp.write_text(json.dumps({"ready": True, "version": "test-0.1"}, ensure_ascii=False), "utf-8")
            os.replace(tmp, ipc / "ready.json")

        threading.Thread(target=write_ready, daemon=True).start()
        data = bridge.wait_ready(timeout=2.0)
        assert data["ready"] is True
        assert data["version"] == "test-0.1"


def test_wait_ready_timeout() -> None:
    with tempfile.TemporaryDirectory() as td:
        _, bridge = make_world(Path(td))
        try:
            bridge.wait_ready(timeout=0.2)
            raise AssertionError("expected BridgeError on timeout")
        except BridgeError:
            pass


def test_request_ok() -> None:
    with tempfile.TemporaryDirectory() as td:
        world, bridge = make_world(Path(td))
        lua_respond(world, 1, result={"pong": True, "version": "v", "tick": 7})
        res = bridge.request("ping")
        assert res == {"pong": True, "version": "v", "tick": 7}
        ipc = world / "mcl2_agent" / "ipc"
        assert list((ipc / "requests").glob("req_*.json")) == [], "request file should be deleted by Lua"
        assert list((ipc / "responses").glob("resp_*.json")) == [], "response file should be deleted by client"


def test_request_kwargs() -> None:
    with tempfile.TemporaryDirectory() as td:
        world, bridge = make_world(Path(td))
        lua_respond(world, 1, result={"episode": "ep-000001"})
        res = bridge.request("begin_episode", player="bot1", task_id="craft_planks", run_id="m0_run",
                             episode_id="ep-000001", world_seed=123, task_seed=42, reset_seed=43)
        assert res == {"episode": "ep-000001"}


def test_request_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        world, bridge = make_world(Path(td))
        lua_respond(world, 1, ok=False, result={"error": "unknown_task:craft_xyz"})
        try:
            bridge.request("begin_episode")
            raise AssertionError("expected BridgeError for ok=false")
        except BridgeError as e:
            assert "unknown_task:craft_xyz" in str(e), str(e)


def test_request_timeout() -> None:
    with tempfile.TemporaryDirectory() as td:
        _, bridge = make_world(Path(td))
        try:
            bridge.request("ping", timeout=0.2)
            raise AssertionError("expected BridgeError on response timeout")
        except BridgeError as e:
            assert "timeout" in str(e).lower()


def test_req_id_increment() -> None:
    with tempfile.TemporaryDirectory() as td:
        world, bridge = make_world(Path(td))
        lua_respond(world, 1, result={"r": 1})
        assert bridge.request("ping") == {"r": 1}
        lua_respond(world, 2, result={"r": 2})
        assert bridge.request("ping") == {"r": 2}


def test_poll_events() -> None:
    with tempfile.TemporaryDirectory() as td:
        world, bridge = make_world(Path(td))
        ev_dir = world / "mcl2_agent" / "ipc" / "events"
        for i in (2, 1):  # 乱序写入，验证按 seq 排序返回
            ev_dir.joinpath(f"ev_{i}.json").write_text(
                json.dumps({"event": f"e{i}", "data": {"i": i}}, ensure_ascii=False), "utf-8")
        evs = bridge.poll_events()
        assert [e["event"] for e in evs] == ["e1", "e2"], evs
        assert list(ev_dir.glob("ev_*.json")) == [], "event files should be deleted after read"
        assert bridge.poll_events() == [], "second poll should be empty"


# ---------------------------------------------------------------- runner

def _main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    print(f"{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    _main()
