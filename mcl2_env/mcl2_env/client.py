"""AgentClient：给 VLA 模型的 SDK（DESIGN.md §8.3 / m3m4_protocol.md §4.3）。

任何模型（OpenVLA / Pi0 / GROOT / ROCKET / STEVE-1 / LLM planner）通过它
与 MCL2 环境交互：session -> reset -> step/execute -> observe/visualize -> done。

会话模型：`__init__` 自动 POST /session 拿 session_id，之后所有请求带
`X-Session-Id` 头。adapter 名在创建会话时声明（mock / openvla / pi0 / groot / steve1）。
"""

from __future__ import annotations

import base64
from typing import Any, Optional

import httpx


class AgentClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        timeout: float = 60.0,
        adapter: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.session_id: Optional[str] = None
        self.adapter = adapter
        self._headers: dict[str, str] = {}
        self._http = httpx.Client(timeout=timeout)
        self._ensure_session()

    def _ensure_session(self) -> None:
        payload = {"adapter": self.adapter} if self.adapter else {}
        r = self._http.post(f"{self.base_url}/session", json=payload)
        r.raise_for_status()
        self.session_id = r.json()["session_id"]
        self._headers = {"X-Session-Id": self.session_id}

    def reset(self, task: Optional[str] = None, seed: Optional[int] = None) -> tuple[dict[str, Any], dict[str, Any]]:
        r = self._http.post(f"{self.base_url}/reset", json={"task": task, "seed": seed}, headers=self._headers)
        r.raise_for_status()
        d = r.json()
        return d["obs"], d.get("info", {})

    def step(self, action: dict[str, Any]) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        """执行一个动作（primitive 或 semantic），推进一帧。"""
        r = self._http.post(f"{self.base_url}/step", json={"action": action}, headers=self._headers)
        r.raise_for_status()
        d = r.json()
        return d["obs"], d["reward"], d["terminated"], d["truncated"], d.get("info", {})

    def execute(self, action: str, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """非阻塞执行语义动作，返回 action_id。"""
        r = self._http.post(
            f"{self.base_url}/execute", json={"action": action, "args": args or {}}, headers=self._headers
        )
        r.raise_for_status()
        return r.json()

    def observe(self) -> dict[str, Any]:
        r = self._http.get(f"{self.base_url}/observe", headers=self._headers)
        r.raise_for_status()
        return r.json()

    def tasks(self) -> list[dict[str, Any]]:
        r = self._http.get(f"{self.base_url}/tasks")
        r.raise_for_status()
        return r.json().get("tasks", [])

    def generate_task(self, kind: str = "procedural", **kwargs: Any) -> dict[str, Any]:
        """触发任务生成（procedural / curriculum / llm，见 m3m4_protocol.md §3）。"""
        r = self._http.post(f"{self.base_url}/generate_task", json={"kind": kind, **kwargs}, headers=self._headers)
        r.raise_for_status()
        return r.json()

    def record_start(self) -> dict[str, Any]:
        r = self._http.post(f"{self.base_url}/record/start", headers=self._headers)
        r.raise_for_status()
        return r.json()

    def record_stop(self) -> dict[str, Any]:
        r = self._http.post(f"{self.base_url}/record/stop", headers=self._headers)
        r.raise_for_status()
        return r.json()

    def visualize(self, save_to: Optional[str] = None) -> Optional[bytes]:
        """取当前帧 PNG（人看/调试）。"""
        r = self._http.post(f"{self.base_url}/visualize", headers=self._headers)
        r.raise_for_status()
        png = r.json().get("png")
        if png is None:
            return None
        data = base64.b64decode(png)
        if save_to:
            with open(save_to, "wb") as f:
                f.write(data)
        return data

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "AgentClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
