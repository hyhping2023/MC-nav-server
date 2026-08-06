#!/usr/bin/env python3
"""taskgen.py 单元测试（mock bridge，无服务器）。

覆盖（docs/m3m4_protocol.md §3）：
  - query_tasks：按 difficulty 升序排序
  - TaskGenerator.procedural：逐物品发 task_generate {kind=procedural, item,
    count, difficulty}；op 未就绪时跳过不崩
  - TaskGenerator.curriculum：发 {kind=curriculum, max_difficulty}；未就绪时
    回退 query_tasks + 本地过滤
  - TaskGenerator.llm_hook：发 {kind=llm, prompt}；未就绪/空结果回退 MockLLM
  - MockLLM：返回一条 canned 任务定义，无任何模型调用

运行方式（任选其一）：
    python3 -m pytest mcl2_env/tests/
    python3 mcl2_env/tests/test_taskgen.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.bridge import BridgeError  # noqa: E402
from mcl2_env.taskgen import MockLLM, TaskGenerator, query_tasks  # noqa: E402

class FakeBridge:
    """记录 request 调用；可配置 task_generate 失败（模拟 op 未就绪）。

    taskgen_results 为队列：每次 task_generate 调用弹出并返回一个结果
    （对应真实 Lua 侧每次生成返回该次任务的响应）。
    """

    def __init__(self, fail_task_generate: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail_task_generate = fail_task_generate
        self.taskgen_result: dict = {}
        self.taskgen_results: list[dict] = []
        self.tasks_result: list[dict] = [
            {"id": "collect_wood", "instruction": "Collect wood", "type": "collect", "difficulty": 0},
            {"id": "craft_planks", "instruction": "Craft planks", "type": "craft", "difficulty": 1},
            {"id": "place_torch", "instruction": "Place torch", "type": "build", "difficulty": 1},
            {"id": "smelt_iron", "instruction": "Smelt iron", "type": "craft", "difficulty": 3},
        ]

    def request(self, op: str, **kwargs):
        self.calls.append((op, kwargs))
        if op == "task_generate":
            if self.fail_task_generate:
                raise BridgeError("task_generate: unknown_op:task_generate")
            if self.taskgen_results:
                return self.taskgen_results.pop(0)
            return self.taskgen_result
        return self.taskgen_result

    def tasks(self):
        self.calls.append(("tasks", {}))
        return list(self.tasks_result)

    def ping(self):
        self.calls.append(("ping", {}))
        return {"pong": True}


# ---------------------------------------------------------------- query_tasks

def test_query_tasks_sorted_by_difficulty() -> None:
    bridge = FakeBridge()
    tasks = query_tasks(bridge)
    assert [t["id"] for t in tasks] == ["collect_wood", "craft_planks", "place_torch", "smelt_iron"]
    assert bridge.calls[-1] == ("tasks", {})
    assert all(tasks[i]["difficulty"] <= tasks[i + 1]["difficulty"] for i in range(len(tasks) - 1))


def test_query_tasks_tolerates_failure() -> None:
    class BrokenBridge(FakeBridge):
        def tasks(self):
            raise BridgeError("tasks: timeout")

    assert query_tasks(BrokenBridge()) == []


# ---------------------------------------------------------------- procedural

def test_procedural_passes_params_and_returns() -> None:
    bridge = FakeBridge()
    bridge.taskgen_results = [
        {"tasks": [{"id": "craft_mcl_core_stick", "instruction": "Craft 4", "difficulty": 1}],
         "registered": ["craft_mcl_core_stick"]},
        {"tasks": [{"id": "craft_mcl_core_cobble", "instruction": "Craft 2", "difficulty": 2}],
         "registered": ["craft_mcl_core_cobble"]},
    ]
    gen = TaskGenerator(bridge)
    res = gen.procedural([
        {"item": "mcl_core:stick", "count": 4, "difficulty": 1},
        {"item": "mcl_core:cobble", "count": 2, "difficulty": 2},
    ])
    gen_calls = [c for c in bridge.calls if c[0] == "task_generate"]
    assert len(gen_calls) == 2
    assert gen_calls[0][1] == {"kind": "procedural", "item": "mcl_core:stick",
                               "count": 4, "difficulty": 1}
    assert gen_calls[1][1] == {"kind": "procedural", "item": "mcl_core:cobble",
                               "count": 2, "difficulty": 2}
    assert [t["id"] for t in res["tasks"]] == ["craft_mcl_core_stick", "craft_mcl_core_cobble"]
    assert res["registered"] == ["craft_mcl_core_stick", "craft_mcl_core_cobble"]
    assert res["skipped"] == []


def test_procedural_default_difficulty() -> None:
    bridge = FakeBridge()
    gen = TaskGenerator(bridge)
    gen.procedural([{"item": "mcl_core:stone"}])
    kwargs = bridge.calls[-1][1]
    assert kwargs["kind"] == "procedural"
    assert kwargs["count"] == 1
    assert kwargs["difficulty"] == 1


def test_procedural_fallback_skips_when_op_missing() -> None:
    bridge = FakeBridge(fail_task_generate=True)
    gen = TaskGenerator(bridge)
    res = gen.procedural([{"item": "mcl_core:stick", "count": 4, "difficulty": 1}])
    assert res["tasks"] == []
    assert res["registered"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["item"] == "mcl_core:stick"
    assert "unknown_op" in res["skipped"][0]["error"]


def test_procedural_registered_dict_normalized() -> None:
    """lua-dev 实现返回 registered={kind, id}（dict），应归一成 id 列表。"""
    bridge = FakeBridge()
    bridge.taskgen_result = {"tasks": [{"id": "craft_mcl_core_stick", "difficulty": 1}],
                             "registered": {"kind": "procedural", "id": "craft_mcl_core_stick"}}
    gen = TaskGenerator(bridge)
    res = gen.procedural([{"item": "mcl_core:stick", "count": 4, "difficulty": 1}])
    assert res["registered"] == ["craft_mcl_core_stick"]


# ---------------------------------------------------------------- curriculum

def test_curriculum_passes_max_difficulty() -> None:
    bridge = FakeBridge()
    bridge.taskgen_result = {"tasks": [{"id": "craft_planks", "difficulty": 1}]}
    gen = TaskGenerator(bridge)
    tasks = gen.curriculum(max_difficulty=2)
    assert bridge.calls[-1] == ("task_generate", {"kind": "curriculum", "max_difficulty": 2})
    assert tasks == bridge.taskgen_result["tasks"]


def test_curriculum_without_max_difficulty() -> None:
    bridge = FakeBridge()
    gen = TaskGenerator(bridge)
    gen.curriculum()
    assert bridge.calls[-1] == ("task_generate", {"kind": "curriculum"})


def test_curriculum_fallback_query_tasks() -> None:
    bridge = FakeBridge(fail_task_generate=True)
    gen = TaskGenerator(bridge)
    tasks = gen.curriculum(max_difficulty=1)
    assert [t["id"] for t in tasks] == ["collect_wood", "craft_planks", "place_torch"]
    # 回退路径里也调用了 tasks() 拉注册表
    assert any(op == "tasks" for op, _ in bridge.calls)


# ---------------------------------------------------------------- llm hook

def test_llm_hook_uses_op() -> None:
    bridge = FakeBridge()
    bridge.taskgen_result = {
        "tasks": [{"id": "llm_task_1", "instruction": "mock llm task", "difficulty": 1}],
        "registered": ["llm_task_1"],
    }
    gen = TaskGenerator(bridge)
    res = gen.llm_hook("Mine 3 stone.")
    assert bridge.calls[-1] == ("task_generate", {"kind": "llm", "prompt": "Mine 3 stone."})
    assert res["tasks"] == bridge.taskgen_result["tasks"]
    assert res["registered"] == ["llm_task_1"]
    assert "source" not in res


def test_llm_hook_fallback_mock_when_op_missing() -> None:
    bridge = FakeBridge(fail_task_generate=True)
    gen = TaskGenerator(bridge)
    res = gen.llm_hook("Mine 3 stone blocks.")
    assert res["source"] == "mock_llm"
    assert res["registered"] == []
    assert len(res["tasks"]) == 1
    assert res["tasks"][0]["id"] == "llm_mock_collect_stone"
    assert "Mine 3 stone blocks." in res["tasks"][0]["instruction"]


def test_llm_hook_fallback_mock_when_empty() -> None:
    """op 就绪但返回空 tasks（如 not_implemented 标记）→ 也回退 MockLLM。"""
    bridge = FakeBridge()
    bridge.taskgen_result = {"tasks": [], "registered": [], "not_implemented": True}
    gen = TaskGenerator(bridge)
    res = gen.llm_hook("collect something")
    assert res["source"] == "mock_llm"
    assert res["tasks"][0]["id"] == "llm_mock_collect_stone"


# ---------------------------------------------------------------- MockLLM

def test_mock_llm_canned_definition() -> None:
    tasks = MockLLM().generate("test prompt")
    assert len(tasks) == 1
    t = tasks[0]
    assert t["id"] == "llm_mock_collect_stone"
    assert t["type"] == "collect"
    assert t["success_args"] == {"item": "mcl_core:stone", "count": 3}
    assert isinstance(t["tags"], list)  # JSON 可序列化


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
