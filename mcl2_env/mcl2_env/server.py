"""VLA Server：把环境能力暴露为 REST + WebSocket，供任意模型接入（M4，不推理）。

端点（DESIGN.md §8.2 / m3m4_protocol.md §4.1）：
    POST /session        创建会话（env 实例 + ModelAdapter）
    POST /reset  {task,seed}   重置 + 返回 obs
    POST /step   {action}      执行动作（原始或语义）→ {obs,reward,terminated,truncated,info}
    POST /execute {action,args} 非阻塞语义动作
    GET  /observe              当前观测
    GET  /tasks                任务列表
    POST /generate_task        任务生成（M3-C 的 task_generate op）
    POST /record/start|stop    数据落盘控制（会话级标志）
    POST /visualize            当前帧 PNG（base64）
    WS   /ws                   推送观测/事件

本模块**不依赖 gymnasium / pydantic**：环境后端用 bridge 直驱（BridgeEnv），
FastAPI 适配层用 Request 读取 JSON body。模型推理一律不在此处执行——
Server 只持有 ModelAdapter 并暴露 `encode_obs` / `decode_action` 的转发方法，
编排在 examples / 用户侧。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import uuid
from typing import Any, Callable, Optional

import numpy as np

from .adapters import ModelAdapter, get_adapter

log = logging.getLogger("mcl2_env.server")

# primitive 动作字段白名单（对齐 schemas.ActionPrimitive / MineStudio action.buttons）
_PRIMITIVE_KEYS = frozenset({
    "forward", "back", "left", "right", "jump",
    "sneak", "sprint", "attack", "use", "drop",
})


# ================================================================ 序列化

def _jsonify(v: Any, mode: str) -> Any:
    """递归把观测转成 JSON 可序列化结构。mode: "list" | "base64"。"""
    if isinstance(v, np.ndarray):
        if mode == "base64":
            return _img_to_b64(v)
        return v.tolist()
    if isinstance(v, (np.bool_, np.integer, np.floating)):
        return v.item()
    if isinstance(v, dict):
        return {k: _jsonify(x, mode) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonify(x, mode) for x in v]
    # 容错：pydantic BaseModel（GymnasiumEnv 的 Observation，可选依赖）
    dump = getattr(v, "model_dump", None)
    if callable(dump):
        return _jsonify(dump(), mode)
    return v


def _img_to_b64(img: np.ndarray) -> dict[str, Any]:
    """ndarray -> PNG base64 dict（含 shape，供客户端还原）。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return {
        "encoding": "base64",
        "mime": "image/png",
        "shape": list(img.shape),
        "data": base64.b64encode(buf.getvalue()).decode(),
    }


def _primitive(action: dict[str, Any]) -> dict[str, Any]:
    """过滤成 bridge.step 接受的 primitive 字段（bool 化）。"""
    d = {k: bool(v) for k, v in action.items() if k in _PRIMITIVE_KEYS}
    if "hotbar" in action:
        d["hotbar"] = int(action["hotbar"])
    if "camera" in action:
        d["camera"] = [float(x) for x in action["camera"]]
    return d


def _parse_action(action: Any) -> tuple[str, dict[str, Any]]:
    """把动作归一化为 (kind, 动作内容)。支持三种写法：

    - semantic: {"id": "goto", "args": {...}} / {"type": "semantic", "id": ..., ...}
    - primitive: {"forward": 1, ...} / {"type": "primitive", ...} / {"primitive": {...}}
    """
    if not isinstance(action, dict):
        raise ValueError(f"action must be a dict, got {type(action).__name__}")

    if isinstance(action.get("semantic"), dict):  # {"semantic": {...}}
        s = action["semantic"]
        return "semantic", {"id": s["id"], "args": s.get("args", {})}
    if action.get("type") == "semantic":
        return "semantic", {"id": action["id"], "args": action.get("args", {})}
    if isinstance(action.get("id"), str):  # {"id": "goto", "args": {...}}
        return "semantic", {"id": action["id"], "args": action.get("args", {})}

    if isinstance(action.get("primitive"), dict):  # {"primitive": {...}}
        action = action["primitive"]
    return "primitive", _primitive(action)


# ================================================================ 环境后端

class BridgeEnv:
    """bridge 直驱环境后端：不依赖 gymnasium/pydantic（本机未装）。

    接口对齐 GymnasiumEnv 的关键方法（reset/step/render/close），供 VLAServer 使用。
    obs 为 bridge.observe() 返回的原始 dict；image 由 renderer 注入（无则 None）。
    """

    def __init__(
        self,
        player: str = "bot1",
        bridge: Any = None,
        renderer: Any = None,
        img_size: tuple[int, int] = (224, 224),
        run_id: str = "server",
    ):
        self.player = player
        self.bridge = bridge
        self.renderer = renderer
        self.img_size = img_size
        self.run_id = run_id
        self.episode_count = 0
        self.recording = True
        self._last_obs: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------ lifecycle

    def reset(self, *, seed: Optional[int] = None, options: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], dict[str, Any]]:
        opts = options or {}
        seed = opts.get("seed", seed)  # server 经 options 传入 seed
        task = opts.get("task")
        self.episode_count += 1
        spec = {
            "player": self.player,
            "task_id": task,
            "run_id": opts.get("run_id", self.run_id),
            "episode_id": opts.get("episode_id", f"ep-{self.episode_count:06d}"),
            "task_seed": opts.get("task_seed", seed or 0),
            "reset_seed": opts.get("reset_seed", (seed or 0) + 1),
        }
        if opts.get("world_seed") is not None:
            spec["world_seed"] = opts["world_seed"]
        self.bridge.begin_episode(spec)
        obs = self._observe()
        info = {"episode_id": spec["episode_id"], "frame_available": obs.get("image") is not None}
        return obs, info

    def step(self, action: Any) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        kind, a = _parse_action(action)
        if kind == "semantic":
            self.bridge.execute(a["id"], a.get("args", {}), player=self.player)
        else:
            self.bridge.step(a, player=self.player)

        obs = self._observe()
        task = obs.get("task") or {}
        terminated = bool(task.get("success"))
        truncated = False
        reward = 0.0
        if terminated or truncated:
            self.bridge.end_episode(success=terminated, player=self.player)
            if self.renderer is not None:
                self.renderer.stop()
        info = {"frame_available": obs.get("image") is not None}
        return obs, reward, terminated, truncated, info

    def _observe(self) -> dict[str, Any]:
        raw = self.bridge.observe(player=self.player)
        raw = dict(raw)  # 浅拷贝，避免污染 bridge 缓存
        if self.renderer is not None:
            if hasattr(self.renderer, "set_camera"):
                pl = raw.get("player") or {}
                wd = raw.get("world") or {}
                self.renderer.set_camera(pl.get("pos"), pl.get("look"), wd.get("voxels"))
            frame = self.renderer.get_frame()
            raw["image"] = np.asarray(frame.image, dtype=np.uint8) if frame is not None else None
        else:
            raw["image"] = None
        self._last_obs = raw
        return raw

    def render(self) -> Optional[np.ndarray]:
        if self._last_obs is not None and self._last_obs.get("image") is not None:
            return self._last_obs["image"]
        return None

    def close(self) -> None:
        if self.renderer is not None:
            self.renderer.stop()
        self.bridge.close()


# ================================================================ VLAServer

class VLAServer:
    """环境网关：维护一组会话（env + adapter），暴露 REST/WS 端点逻辑。"""

    def __init__(
        self,
        env_factory: Callable[[], Any],
        adapter_factory: Optional[Callable[[], ModelAdapter]] = None,
        obs_mode: str = "list",
    ):
        self.env_factory = env_factory
        self.adapter_factory = adapter_factory or (lambda: get_adapter("mock"))
        self.obs_mode = obs_mode  # "list" | "base64"（观测 image 序列化方式）
        self.sessions: dict[str, dict[str, Any]] = {}
        self.tasks: list[dict[str, Any]] = []

    # ------------------------------------------------------------ session

    def new_session(self, adapter: Optional[Any] = None) -> str:
        """创建会话。adapter 可为 ModelAdapter 实例、注册名（str）或 None（用默认）。"""
        if isinstance(adapter, str):
            adapter = get_adapter(adapter)
        if adapter is None:
            adapter = self.adapter_factory()
        sid = uuid.uuid4().hex[:12]
        self.sessions[sid] = {
            "env": self.env_factory(),
            "adapter": adapter,
            "task": None,
            "current_action": None,
            "recording": True,
        }
        return sid

    def close_session(self, sid: str) -> None:
        sess = self.sessions.pop(sid, None)
        if sess is not None:
            sess["env"].close()

    def _env(self, sid: str) -> Any:
        return self.sessions[sid]["env"]

    def _adapter(self, sid: str) -> ModelAdapter:
        return self.sessions[sid]["adapter"]

    # ------------------------------------------------------------ handlers

    def handle_reset(self, sid: str, task: Optional[str] = None, seed: Optional[int] = None, **opts: Any) -> dict[str, Any]:
        env = self._env(sid)
        obs, info = env.reset(options={"task": task, "seed": seed, **opts})
        self.sessions[sid]["task"] = task
        return {"obs": self._obs_to_json(obs), "info": info}

    def handle_step(self, sid: str, action: dict[str, Any]) -> dict[str, Any]:
        env = self._env(sid)
        obs, reward, terminated, truncated, info = env.step(action)
        return {
            "obs": self._obs_to_json(obs),
            "reward": reward,
            "terminated": terminated,
            "truncated": truncated,
            "info": info,
        }

    def handle_execute(self, sid: str, action: str, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        env = self._env(sid)
        return env.bridge.execute(action, args or {}, player=env.player)  # {"action_id": ...}

    def handle_observe(self, sid: str) -> dict[str, Any]:
        env = self._env(sid)
        return self._obs_to_json(env._observe())

    def handle_tasks(self) -> list[dict[str, Any]]:
        """任务列表：经 taskgen.query_tasks（难度排序），失败回退缓存。"""
        for sess in self.sessions.values():
            bridge = getattr(sess["env"], "bridge", None)
            if bridge is None or not hasattr(bridge, "tasks"):
                continue
            try:
                try:
                    from mcl2_env.taskgen import query_tasks

                    self.tasks = list(query_tasks(bridge))
                except ImportError:
                    self.tasks = sorted(
                        (bridge.tasks() or []),
                        key=lambda t: (t.get("difficulty") if t.get("difficulty") is not None else 0),
                    )
                break
            except Exception as e:  # noqa: BLE001
                log.warning("handle_tasks: %s", e)
        return self.tasks

    def handle_generate_task(self, sid: str, kind: str = "procedural", **kwargs: Any) -> dict[str, Any]:
        """任务生成（m3m4_protocol.md §3）：经 mcl2_env.taskgen 触发 bridge op。

        kind="procedural"：items=[{item,count,difficulty},...] 或单条 item/count/difficulty
        kind="curriculum"：max_difficulty
        kind="llm"：prompt（Lua 侧 Mock，不接真实 LLM）
        taskgen 未就绪时直接调 bridge op task_generate。
        """
        env = self._env(sid)
        bridge = env.bridge
        if bridge is None or not hasattr(bridge, "request"):
            return {"status": "not_implemented", "reason": "env has no bridge.request"}
        try:
            from mcl2_env.taskgen import TaskGenerator

            gen = TaskGenerator(bridge)
            if kind == "procedural":
                items = kwargs.get("items")
                if items is None and "item" in kwargs:
                    items = [kwargs]  # 单条 {"item", "count", "difficulty"}
                if not items:
                    return {"status": "error", "error": "procedural requires items list or item"}
                res = gen.procedural(items)
            elif kind == "curriculum":
                res = {"tasks": gen.curriculum(max_difficulty=kwargs.get("max_difficulty"))}
            elif kind == "llm":
                res = gen.llm_hook(kwargs.get("prompt", ""))
            else:
                return {"status": "error", "error": f"unknown kind: {kind!r}"}
        except ImportError:
            try:
                res = bridge.request("task_generate", kind=kind, **kwargs)
            except Exception as e:  # noqa: BLE001
                return {"status": "error", "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": str(e)}
        if isinstance(res, dict) and res.get("tasks"):
            self.tasks = list(res["tasks"])
        return {"status": "ok", "result": res}

    def handle_record(self, sid: str, action: str) -> dict[str, Any]:
        """数据落盘控制（会话级标志）。start/stop。"""
        if action not in ("start", "stop"):
            raise ValueError(f"record action must be 'start' or 'stop', got {action!r}")
        self.sessions[sid]["recording"] = action == "start"
        return {"recording": self.sessions[sid]["recording"]}

    def handle_visualize(self, sid: str) -> dict[str, Any]:
        """当前帧 PNG（base64）。无帧时 {"png": None}。"""
        env = self._env(sid)
        img = env.render()
        if img is None:
            return {"png": None}
        return {"png": _img_to_b64(img)["data"]}

    # ------------------------------------------------------------ adapter 契约

    def encode_obs(self, sid: str, obs: Optional[dict[str, Any]] = None) -> Any:
        """Session 持有的 adapter 把 obs 编码成模型输入（不推理）。"""
        if obs is None:
            env = self._env(sid)
            obs = self._obs_to_json(env._observe())
        return self._adapter(sid).encode_obs(obs)

    def decode_action(self, sid: str, model_out: Any) -> dict[str, Any]:
        """Session 持有的 adapter 把模型输出解码成环境动作（不推理）。"""
        return self._adapter(sid).decode_action(model_out)

    # ------------------------------------------------------------ serialization

    def _obs_to_json(self, obs: dict[str, Any]) -> dict[str, Any]:
        return _jsonify(obs, self.obs_mode)


# ================================================================ FastAPI 适配

async def _json_body(request: Any) -> dict[str, Any]:
    """读 JSON body，空/非法返回 {}（不依赖 pydantic）。"""
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def build_fastapi_app(server: VLAServer):
    """把 VLAServer 包成 FastAPI app（fastapi 延迟导入，本机未装时可跳过）。"""
    from fastapi import FastAPI, HTTPException, Request, WebSocket
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="MCL2-Env VLA Server")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def _require_sid(request: Request) -> str:
        sid = request.headers.get("X-Session-Id")
        if not sid or sid not in server.sessions:
            raise HTTPException(400, "missing or invalid X-Session-Id header")
        return sid

    @app.post("/session")
    async def create_session(request: Request):
        body = await _json_body(request)
        sid = server.new_session(adapter=body.get("adapter"))
        return {"session_id": sid}

    @app.post("/reset")
    async def reset(request: Request):
        sid = _require_sid(request)
        body = await _json_body(request)
        try:
            return server.handle_reset(sid, task=body.get("task"), seed=body.get("seed"),
                                       **{k: v for k, v in body.items() if k not in ("task", "seed")})
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e))

    @app.post("/step")
    async def step(request: Request):
        sid = _require_sid(request)
        body = await _json_body(request)
        try:
            return server.handle_step(sid, body.get("action"))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e))

    @app.post("/execute")
    async def execute(request: Request):
        sid = _require_sid(request)
        body = await _json_body(request)
        try:
            return server.handle_execute(sid, body.get("action"), body.get("args"))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e))

    @app.get("/observe")
    async def observe(request: Request):
        sid = _require_sid(request)
        try:
            return server.handle_observe(sid)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e))

    @app.get("/tasks")
    async def tasks():
        return {"tasks": server.handle_tasks()}

    @app.post("/generate_task")
    async def generate_task(request: Request):
        sid = _require_sid(request)
        body = await _json_body(request)
        kind = body.pop("kind", "procedural")
        return server.handle_generate_task(sid, kind=kind, **body)

    @app.post("/record/start")
    async def record_start(request: Request):
        return server.handle_record(_require_sid(request), "start")

    @app.post("/record/stop")
    async def record_stop(request: Request):
        return server.handle_record(_require_sid(request), "stop")

    @app.post("/visualize")
    async def visualize(request: Request):
        return server.handle_visualize(_require_sid(request))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        """WS 会话：客户端发 {"op": "observe"|"step"|"reset", ...}，服务端回结果。"""
        await websocket.accept()
        sid = websocket.headers.get("X-Session-Id") or websocket.query_params.get("session_id")
        if not sid or sid not in server.sessions:
            await websocket.send_json({"error": "invalid session"})
            await websocket.close()
            return
        try:
            while True:
                msg = await websocket.receive_json()
                op = msg.get("op")
                if op == "observe":
                    await websocket.send_json({"obs": server.handle_observe(sid)})
                elif op == "step":
                    await websocket.send_json(server.handle_step(sid, msg.get("action")))
                elif op == "reset":
                    await websocket.send_json(server.handle_reset(sid, task=msg.get("task"), seed=msg.get("seed")))
                elif op == "visualize":
                    await websocket.send_json(server.handle_visualize(sid))
                else:
                    await websocket.send_json({"error": f"unknown op: {op}"})
        except Exception:  # noqa: BLE001 客户端断开
            return

    return app


# ================================================================ CLI

def _build_server(bridge_factory: Callable[[], Any], adapter_name: str = "mock", obs_mode: str = "list") -> VLAServer:
    def env_factory() -> BridgeEnv:
        return BridgeEnv(player="bot1", bridge=bridge_factory())

    return VLAServer(
        env_factory=env_factory,
        adapter_factory=lambda: get_adapter(adapter_name),
        obs_mode=obs_mode,
    )


def main() -> None:
    """python -m mcl2_env.server [--world <dir>] [--bridge host:port] [--adapter mock] [--port 8000]"""
    import uvicorn

    logging.basicConfig(level=logging.INFO)

    p = argparse.ArgumentParser(description="MCL2-Env VLA Server (no inference)")
    p.add_argument("--world", default=None, help="world dir for FileBridgeClient (文件 IPC)")
    p.add_argument("--bridge", default=None, help="TCP bridge host:port (default engine fork)")
    p.add_argument("--adapter", default="mock", help="adapter name: mock/openvla/pi0/groot/steve1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--obs-mode", default="list", choices=["list", "base64"])
    args = p.parse_args()

    if args.world:
        from .bridge import FileBridgeClient

        def bridge_factory() -> FileBridgeClient:
            return FileBridgeClient(args.world)
    else:
        host, _, port = (args.bridge or "127.0.0.1:25585").partition(":")
        from .bridge import BridgeClient

        def bridge_factory() -> BridgeClient:
            return BridgeClient(host, int(port))

    server = _build_server(bridge_factory, args.adapter, args.obs_mode)
    app = build_fastapi_app(server)
    log.info("VLA server (adapter=%s, obs_mode=%s) on :%d", args.adapter, args.obs_mode, args.port)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
