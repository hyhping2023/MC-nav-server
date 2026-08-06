"""M3-C 任务生成器接口（docs/m3m4_protocol.md §3，无推理）。

- `query_tasks(bridge)`：拉取注册任务，按 difficulty 升序排序。
- `TaskGenerator.procedural(bridge, items)`：对每个物品触发 `task_generate
  {kind="procedural", item, count, difficulty}`，批量生成 craft 类任务。
- `TaskGenerator.curriculum(bridge, max_difficulty)`：触发
  `task_generate {kind="curriculum"}`，返回难度序任务列表。
- `TaskGenerator.llm_hook(bridge, prompt)`：定义 LLM 生成钩子接口——触发
  `task_generate {kind="llm", prompt}`；Lua 侧走 Mock，本机**不接真实 LLM**。
  若 op 未就绪/返回 not_implemented，回退本地 `MockLLM`（canned 任务定义）。

容错：`task_generate` op 由 lua-dev 并行交付；未就绪时抛 BridgeError
（unknown_op），本模块回退 `ping`/`tasks` 基础 op，不挂死。
"""

from __future__ import annotations

import logging
from typing import Any

from mcl2_env.bridge import BridgeError

log = logging.getLogger("mcl2_env.taskgen")

TASK_GENERATE_OP = "task_generate"


def query_tasks(bridge) -> list[dict[str, Any]]:
    """拉取注册任务并按 difficulty 升序排序（难失败则返回空表）。"""
    try:
        tasks = bridge.tasks()
    except BridgeError as e:
        log.warning("query_tasks: bridge.tasks() 失败: %s", e)
        return []
    return sorted(tasks or [], key=lambda t: (t.get("difficulty") if t.get("difficulty") is not None else 0))


def _request_task_generate(bridge, **kwargs: Any) -> dict[str, Any]:
    """调 bridge 的 task_generate op；返回响应的 result（{tasks, registered}）。"""
    return bridge.request(TASK_GENERATE_OP, **kwargs)


def _normalize_registered(reg: Any) -> list[Any]:
    """把 registered 字段归一成列表。

    Lua 侧现为 list 形态（task_generate 契约：registered=[task_id,...]，
    幂等命中/纯查询时为 []）。保留 dict/标量兜底以防旧版本或未知形态。
    """
    if reg is None:
        return []
    if isinstance(reg, list):
        return reg
    if isinstance(reg, dict):
        if reg.get("id"):
            return [reg["id"]]
        return [reg]
    return [reg]


class MockLLM:
    """本地 LLM mock：返回一条 canned 任务定义，**不调用任何真实模型**。"""

    def generate(self, prompt: str) -> list[dict[str, Any]]:
        """把 prompt 包装成一条确定性任务定义（task schema 见 DESIGN.md §6.1）。"""
        return [{
            "id": "llm_mock_collect_stone",
            "name": "(mock llm) Collect Stone",
            "instruction": f"(mock llm) {prompt}",
            "instruction_zh": f"(mock llm) {prompt}",
            "type": "collect",
            "difficulty": 1,
            "tags": ["llm_mock"],
            "success_predicate": "inventory_contains",
            "success_args": {"item": "mcl_core:stone", "count": 3},
            "timeout_ticks": 1200,
        }]


class TaskGenerator:
    """任务生成器：procedural / curriculum / llm_hook 三类生成入口。"""

    def __init__(self, bridge):
        self.bridge = bridge

    # ------------------------------------------------------------ procedural

    def procedural(
        self,
        items: list[dict[str, Any]],
        default_difficulty: int = 1,
    ) -> dict[str, Any]:
        """批量触发 procedural 生成。

        items: [{item, count?, difficulty?}, ...]（item 为 Mineclonia 物品名）。
        返回 {tasks, registered, skipped}——skipped 记录 op 未就绪时被跳过的物品。
        """
        tasks: list[dict[str, Any]] = []
        registered: list[str] = []
        skipped: list[dict[str, Any]] = []
        for spec in items:
            item = spec.get("item")
            if not item:
                continue
            try:
                res = _request_task_generate(
                    self.bridge,
                    kind="procedural",
                    item=item,
                    count=spec.get("count", 1),
                    difficulty=spec.get("difficulty", default_difficulty),
                )
            except BridgeError as e:
                log.warning("procedural(%s): task_generate 未就绪: %s", item, e)
                skipped.append({"item": item, "error": str(e)})
                continue
            tasks.extend(res.get("tasks") or [])
            registered.extend(_normalize_registered(res.get("registered")))
        return {"tasks": tasks, "registered": registered, "skipped": skipped}

    # ------------------------------------------------------------ curriculum

    def curriculum(self, max_difficulty: int | None = None) -> list[dict[str, Any]]:
        """返回按难度升序的任务列表。

        op 未就绪时回退 query_tasks（本地排序 + difficulty 过滤）。
        """
        try:
            kwargs: dict[str, Any] = {"kind": "curriculum"}
            if max_difficulty is not None:
                kwargs["max_difficulty"] = max_difficulty
            res = _request_task_generate(self.bridge, **kwargs)
            return res.get("tasks") or []
        except BridgeError as e:
            log.warning("curriculum: task_generate 未就绪，回退 query_tasks: %s", e)
            tasks = query_tasks(self.bridge)
            if max_difficulty is not None:
                tasks = [t for t in tasks if (t.get("difficulty") or 0) <= max_difficulty]
            return tasks

    # ------------------------------------------------------------ llm hook

    def llm_hook(self, prompt: str) -> dict[str, Any]:
        """LLM 生成钩子（仅接口，不接真实 LLM）。

        优先 `task_generate {kind="llm", prompt}`（Lua 侧 Mock）；op 未就绪或
        Lua 返回 not_implemented 时回退本地 MockLLM。
        """
        try:
            res = _request_task_generate(self.bridge, kind="llm", prompt=prompt)
        except BridgeError as e:
            log.warning("llm_hook: task_generate 未就绪，回退 MockLLM: %s", e)
            res = None
        tasks = (res or {}).get("tasks") or []
        if not tasks:
            # op 就绪但返回空 / not_implemented 标记 → 本地 Mock 兜底
            return {"tasks": MockLLM().generate(prompt), "registered": [], "source": "mock_llm"}
        return {"tasks": tasks, "registered": _normalize_registered((res or {}).get("registered"))}

    # ------------------------------------------------------------ demo

    @staticmethod
    def demo(bridge, max_difficulty: int | None = 3,
             items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """M3-C 验收演示：课程 / procedural（2-3 条）/ llm mock（1 条）。

        返回 {curriculum, procedural, llm}；不抛错，op 未就绪自动降级。
        """
        gen = TaskGenerator(bridge)
        if items is None:
            items = [
                {"item": "mcl_core:stick", "count": 4, "difficulty": 1},
                {"item": "mcl_tools:pick_wood", "count": 1, "difficulty": 2},
                {"item": "mcl_furnaces:furnace", "count": 1, "difficulty": 3},
            ]
        return {
            "curriculum": gen.curriculum(max_difficulty=max_difficulty),
            "procedural": gen.procedural(items),
            "llm": gen.llm_hook("Mine 3 stone blocks and return."),
        }
