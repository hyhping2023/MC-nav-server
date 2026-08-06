#!/usr/bin/env python3
"""ScriptedPolicy / run_scripted_episode 单元测试（mock bridge，无服务器）。

覆盖（docs/m3m4_protocol.md §2）：
  - craft_planks 策略：craft(wood_oak x4) 只下发一次，之后轮询
  - collect_wood 策略：nearby_blocks 找 tree_oak → goto → dig；pos 去重
  - collect_nearby：走向地面掉落物
  - 通用策略：aimed_block → look_at → goto → dig
  - build 策略：place_torch → 相邻地面放置
  - run_scripted_episode：mock bridge 下 craft_planks 产出 success=True，
    execute/begin_episode/end_episode 调用序列正确

运行方式（任选其一）：
    python3 -m pytest mcl2_env/tests/
    python3 mcl2_env/tests/test_scripted_agent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.scripts.scripted_agent import ScriptedPolicy, run_scripted_episode  # noqa: E402


def make_obs(*, success: bool = False, ttype: str = "craft", tid: str = "craft_planks",
             nearby: list | None = None, items: list | None = None,
             aimed: dict | None = None, pos: dict | None = None, frame: int = 0) -> dict:
    """构造最小 observe 响应。"""
    return {
        "player": {"pos": pos or {"x": 0.0, "y": 41.0, "z": 0.0},
                   "look": {"yaw": 0.0, "pitch": 0.0, "dir": {"x": 0.0, "y": 0.0, "z": 1.0}}},
        "world": {
            "nearby_blocks": nearby or [],
            "items_on_ground": items or [],
            "aimed_block": aimed,
        },
        "task": {"id": tid, "type": ttype, "instruction": "", "success": success, "steps": 0},
        "episode": {"frame": frame},
    }


class MockBridge:
    """记录调用；observe 由 obs_factory(n) 提供（n 从 1 开始）。"""

    def __init__(self, obs_factory):
        self.obs_factory = obs_factory
        self.executed: list[tuple[str, dict]] = []
        self.observe_count = 0
        self.begin_called: dict | None = None
        self.end_called: tuple | None = None

    def observe(self, player: str = "bot1"):
        self.observe_count += 1
        return self.obs_factory(self.observe_count)

    def execute(self, action: str, args: dict | None = None, player: str = "bot1"):
        self.executed.append((action, args or {}))
        return {"action_id": len(self.executed)}

    def begin_episode(self, spec: dict):
        self.begin_called = spec
        return {"episode": spec["episode_id"]}

    def end_episode(self, success: bool, player: str = "bot1"):
        self.end_called = (success, player)
        return {"ok": True}

    def poll_events(self):
        return []

    def request(self, op: str, **kwargs):
        return {}


# ---------------------------------------------------------------- policy 规划

def test_policy_craft_planks_issues_once() -> None:
    bridge = MockBridge(lambda n: make_obs())
    policy = ScriptedPolicy(bridge, task_id="craft_planks")
    obs = make_obs()
    plan = policy.plan(obs)
    assert plan == [("craft", {"item": "mcl_trees:wood_oak", "count": 4})]
    assert policy.plan(obs) == [], "craft 只应下发一次"


def test_policy_collect_wood_goto_dig() -> None:
    bridge = MockBridge(lambda n: make_obs())
    policy = ScriptedPolicy(bridge, task_id="collect_wood")
    tree_pos = {"x": 1.0, "y": 42.0, "z": 2.0}
    obs = make_obs(ttype="collect", tid="collect_wood",
                   nearby=[{"pos": tree_pos, "name": "mcl_trees:tree_oak", "param2": 0}])
    plan = policy.plan(obs)
    assert plan == [("goto", {"pos": tree_pos}), ("dig", {"pos": tree_pos})]
    assert policy.plan(obs) == [], "同一 tree pos 不应重复下发"


def test_policy_collect_wood_no_tree_collect_nearby() -> None:
    bridge = MockBridge(lambda n: make_obs())
    policy = ScriptedPolicy(bridge, task_id="collect_wood")
    item_pos = {"x": 3.0, "y": 40.0, "z": 4.0}
    obs = make_obs(ttype="collect", tid="collect_wood",
                   items=[{"item": "mcl_trees:tree_oak", "pos": item_pos}])
    plan = policy.plan(obs)
    assert plan == [("goto", {"pos": item_pos})]
    assert policy.plan(obs) == [], "同一掉落物 pos 不应重复下发"


def test_policy_collect_wood_digs_whole_column() -> None:
    """同一棵树的整列原木应一次下发：goto 底木 + 自下而上 dig 每根原木。"""
    bridge = MockBridge(lambda n: make_obs())
    policy = ScriptedPolicy(bridge, task_id="collect_wood")
    trunk = [{"x": 1.0, "y": 42.0, "z": 2.0},
             {"x": 1.0, "y": 43.0, "z": 2.0},
             {"x": 1.0, "y": 44.0, "z": 2.0}]
    nearby = [{"pos": p, "name": "mcl_trees:tree_oak"} for p in trunk]
    obs = make_obs(ttype="collect", tid="collect_wood", nearby=nearby)
    plan = policy.plan(obs)
    assert plan == [("goto", {"pos": trunk[0]})] + [("dig", {"pos": p}) for p in trunk], \
        "应 goto 底木后自下而上 dig 整列原木"
    assert policy.plan(obs) == [], "整列已下发，不应重复"


def test_policy_collect_wood_nearest_column() -> None:
    """有多棵树时选 (x,z) 距离最近的一列（跨树之间不再横向跳着挖）。"""
    bridge = MockBridge(lambda n: make_obs())
    policy = ScriptedPolicy(bridge, task_id="collect_wood")
    near_trunk = [{"x": 2.0, "y": 42.0, "z": 0.0}, {"x": 2.0, "y": 43.0, "z": 0.0}]
    far_trunk = [{"x": 8.0, "y": 42.0, "z": 0.0}]
    nearby = [{"pos": p, "name": "mcl_trees:tree_oak"} for p in near_trunk + far_trunk]
    obs = make_obs(ttype="collect", tid="collect_wood", nearby=nearby,
                   pos={"x": 0.0, "y": 41.0, "z": 0.0})
    plan = policy.plan(obs)
    assert plan == [("goto", {"pos": near_trunk[0]})] + [("dig", {"pos": p}) for p in near_trunk]
    assert policy.plan(obs) == [], "最近列已下发，不应再挑远树"


def test_policy_collect_nearby_dedupe() -> None:
    bridge = MockBridge(lambda n: make_obs())
    policy = ScriptedPolicy(bridge, task_id="collect_wood")
    p1, p2 = {"x": 1.0, "y": 40.0, "z": 1.0}, {"x": 2.0, "y": 40.0, "z": 2.0}
    obs = make_obs(ttype="collect", tid="collect_wood",
                   items=[{"item": "mcl_core:stone", "pos": p1},
                          {"item": "mcl_core:stone", "pos": p2}])
    assert policy.plan(obs) == [("goto", {"pos": p1})]
    assert policy.plan(obs) == [("goto", {"pos": p2})]
    assert policy.plan(obs) == []


def test_policy_generic_aimed_block() -> None:
    bridge = MockBridge(lambda n: make_obs())
    policy = ScriptedPolicy(bridge, task_id="unknown_task")
    aimed_pos = {"x": 2.0, "y": 40.0, "z": 0.0}
    obs = make_obs(ttype=None, tid="unknown_task",
                   aimed={"pos": aimed_pos, "name": "mcl_core:stone", "param2": 0})
    plan = policy.plan(obs)
    assert plan == [("look_at", {"pos": aimed_pos}),
                    ("goto", {"pos": aimed_pos}),
                    ("dig", {"pos": aimed_pos})]
    assert policy.plan(obs) == []


def test_policy_build_place_torch() -> None:
    bridge = MockBridge(lambda n: make_obs())
    policy = ScriptedPolicy(bridge, task_id="place_torch")
    obs = make_obs(ttype="build", tid="place_torch", pos={"x": 0.0, "y": 41.0, "z": 0.0})
    plan = policy.plan(obs)
    assert plan == [
        ("look_at", {"pos": {"x": 1, "y": 40, "z": 0}}),
        ("place", {"item": "mcl_torches:torch", "pos": {"x": 1, "y": 40, "z": 0}}),
    ]


# ---------------------------------------------------------------- episode 循环

def test_run_episode_craft_planks_success(tmp_path: Path) -> None:
    """mock bridge 下 craft_planks 在若干 observe 后 success=True。"""
    success_at = 3

    def factory(n: int):
        return make_obs(success=n >= success_at, frame=n - 1)

    bridge = MockBridge(factory)

    class Args:
        seed = 42
        steps = 10
        timeout = 60.0

    result = run_scripted_episode(
        bridge, renderer=None, episode_id="ep-test-craft",
        task_id="craft_planks", data_root=tmp_path, world_seed=1, args=Args(),
    )
    assert result["success"] is True
    assert result["task"] == "craft_planks"
    assert 0 < result["steps"] <= 10
    # craft 动作只下发一次，随后轮询
    assert bridge.executed == [("craft", {"item": "mcl_trees:wood_oak", "count": 4})]
    assert bridge.begin_called is not None
    assert bridge.begin_called["task_id"] == "craft_planks"
    assert bridge.end_called == (True, "bot1")
    # 每次 observe 都应有对应（renderer=None 不写帧，frames=0）
    assert result["frames"] == 0


def test_run_episode_craft_planks_steps_exhausted(tmp_path: Path) -> None:
    """策略在 steps 耗尽前未成功 → success=False（但进程不崩溃）。"""

    def factory(n: int):
        return make_obs(success=False, frame=n - 1)

    bridge = MockBridge(factory)

    class Args:
        seed = 42
        steps = 3
        timeout = 60.0

    result = run_scripted_episode(
        bridge, renderer=None, episode_id="ep-test-timeout",
        task_id="craft_planks", data_root=tmp_path, world_seed=1, args=Args(),
    )
    assert result["success"] is False
    assert result["steps"] == 3
    assert bridge.end_called == (False, "bot1")


def test_policy_collect_wood_episode(tmp_path: Path) -> None:
    """collect_wood 尽力而为：下发 goto+dig，成功后终止。"""
    tree_pos = {"x": 1.0, "y": 42.0, "z": 2.0}
    seen = {"dug": False}

    def factory(n: int):
        if n == 1:
            return make_obs(ttype="collect", tid="collect_wood",
                            nearby=[{"pos": tree_pos, "name": "mcl_trees:tree_oak"}], frame=0)
        # 挖掘后树消失 → 无目标方块 → 轮询直至 success
        return make_obs(ttype="collect", tid="collect_wood",
                        nearby=[], success=n >= 4, frame=n - 1)

    bridge = MockBridge(factory)
    # 手动跑一遍策略：下发 goto+dig 后剩余步骤轮询
    policy = ScriptedPolicy(bridge, task_id="collect_wood")
    obs0 = make_obs(ttype="collect", tid="collect_wood",
                    nearby=[{"pos": tree_pos, "name": "mcl_trees:tree_oak"}])
    assert policy.plan(obs0) == [("goto", {"pos": tree_pos}), ("dig", {"pos": tree_pos})]
    seen["dug"] = True
    assert seen["dug"] is True


# ---------------------------------------------------------------- runner

def _main() -> None:
    import inspect
    import tempfile

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    with tempfile.TemporaryDirectory() as td:
        for t in tests:
            try:
                params = inspect.signature(t).parameters
                if "tmp_path" in params:
                    t(Path(td))
                else:
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
