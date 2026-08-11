"""可复用的单 episode 人类式录制流程。

`scripts/demo_human.py` 的单次 CLI 与长期运行的 `record_worker.py` 共用这里的实现：
连接（SeedReplayApi / WS / gRPC）归调用方长期持有；每个 episode 只创建独立的
HumanRecorder 和输出目录。因此 worker 能连续 reset/录制，而无需重复启动 Fabric 客户端。
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from ..action_space import BUTTONS
from ..orchestrator import KitAgent
from ..tasks import SURVIVAL_KIT, get_profile
from .human_recorder import HumanRecorder


@dataclass(frozen=True)
class EpisodeConfig:
    """一个可串行或并发分配的录制 job。"""

    outdir: str
    task: str
    seed: int
    surface: Optional[str] = None
    max_steps: int = 600
    ticks: int = 2
    half_extent: int = 16
    capture: str = "native"
    hud: bool = True
    humanize: bool = True
    provision: bool = True
    spawn: Optional[Sequence[float]] = None
    tail_seconds: float = 0.0
    # True 时 `api.reset()` 会执行 SelectSurfaceWorld；持久 worker 已在启动时绑定
    # 专属地图，应设 False，防止每个 episode 重复 teleport/选择 world。
    select_surface: bool = True
    # 仅作元数据标记；由 worker 传入，方便数据集审计。
    worker_id: Optional[str] = None
    map_seed: Optional[int] = None

    @classmethod
    def from_mapping(cls, job: Dict[str, Any], *, outdir: str,
                     default_surface: Optional[str] = None,
                     default_max_steps: int = 600,
                     default_ticks: int = 2,
                     default_half_extent: int = 16,
                     default_capture: str = "native",
                     default_hud: bool = True,
                     default_humanize: bool = True,
                     default_provision: bool = True,
                     select_surface: bool = True,
                     worker_id: Optional[str] = None,
                     map_seed: Optional[int] = None) -> "EpisodeConfig":
        """把 coordinator 的 JSON job 合并为完整配置。"""
        return cls(
            outdir=outdir,
            task=str(job["task"]),
            seed=int(job["seed"]),
            surface=(str(job["surface"]) if job.get("surface") is not None
                     else default_surface),
            max_steps=int(job.get("max_steps", default_max_steps)),
            ticks=int(job.get("ticks", default_ticks)),
            half_extent=int(job.get("half_extent", default_half_extent)),
            capture=str(job.get("capture", default_capture)),
            hud=bool(job.get("hud", default_hud)),
            humanize=bool(job.get("humanize", default_humanize)),
            provision=bool(job.get("provision", default_provision)),
            spawn=job.get("spawn"),
            tail_seconds=float(job.get("tail_seconds", 0.0)),
            select_surface=bool(job.get("select_surface", select_surface)),
            worker_id=worker_id if worker_id is not None else job.get("worker_id"),
            map_seed=map_seed if map_seed is not None else job.get("map_seed"),
        )


def parse_spawn(value: Optional[str | Sequence[float]]) -> Optional[list[float]]:
    """CLI 字符串或 JSON list 统一转成 reset 所需 float list。"""
    if value is None:
        return None
    if isinstance(value, str):
        return [float(v) for v in value.split(",")]
    return [float(v) for v in value]


def ensure_pigs(api: Any, rng: random.Random, half_extent: int, count: int = 2) -> int:
    """在受控单材质平地上确定性补给猪（kill 任务）。"""
    st = api.grpc.get_state(player=api.player)
    px, py, pz = (float(v) for v in st["player"]["pos"])
    offsets = [(9, 0), (0, -9), (-9, 0), (0, 9), (-8, 8), (8, -8), (7, 7), (-7, -7)]
    rng.shuffle(offsets)
    spawned = 0
    for dx, dz in offsets:
        if spawned >= count:
            break
        api.grpc.spawn_entity(
            player=api.player,
            entity_type="minecraft:pig",
            pos=(int(px) + dx + 0.5, int(py), int(pz) + dz + 0.5),
            count=1,
        )
        spawned += 1
    return spawned


def _configure_capture(api: Any, capture: str, hud: bool) -> None:
    if capture.lower() == "native":
        api.ws.send({"cmd": "set_capture", "width": 0, "height": 0})
    else:
        width, height = (int(x) for x in capture.lower().split("x"))
        api.ws.send({"cmd": "set_capture", "width": width, "height": height})
    # FBO 重建发生在渲染线程；等待后排空旧尺寸积压帧。
    time.sleep(0.5)
    api.ws.send({"cmd": "set_capture_ui", "hud": bool(hud)})
    time.sleep(0.3)
    for _ in range(120):
        frame = api.ws.recv_frame(timeout=0.2)
        if frame is None:
            break
        if capture.lower() == "native" and frame.rgb.shape[1] != 224:
            break


def record_human_episode(
    api: Any,
    config: EpisodeConfig,
    *,
    compose_mp4: Optional[Callable[[str, str], bool]] = None,
    retry_resets: int = 30,
    log: Callable[[str], None] = lambda line: print(line, flush=True),
) -> Dict[str, Any]:
    """录制一个 episode，返回可 JSON 序列化的结果。

    不关闭 api：调用方可以在循环中重用同一个 Fabric client、WS 和 gRPC channel。
    出错时仍会尽量关闭 recorder/key-log，避免污染后续 job。
    """
    profile = get_profile(config.task)
    task_id = profile.task_id
    outdir = Path(config.outdir)
    outdir.parent.mkdir(parents=True, exist_ok=True)
    if outdir.exists():
        if any(outdir.iterdir()):
            raise FileExistsError(f"episode outdir already exists and is not empty: {outdir}")
    else:
        outdir.mkdir(parents=True)
    mp4_path = f"{outdir}.mp4"
    spawn = parse_spawn(config.spawn)

    frame = None
    obs = None
    for attempt in range(retry_resets):
        try:
            frame, obs = api.reset(
                seed=config.seed,
                task=task_id,
                surface=config.surface,
                items=SURVIVAL_KIT,
                spawn=spawn,
                humanize=config.humanize,
                select_surface=config.select_surface,
            )
            break
        except Exception as exc:  # noqa: BLE001 -- worker needs retry/recovery boundary
            log(f"[reset] attempt {attempt + 1}/{retry_resets} failed: "
                f"{type(exc).__name__}: {exc}")
            time.sleep(2)
    if obs is None:
        raise RuntimeError("env.reset failed after retries")

    log(f"[reset] task={config.task} seed={config.seed} "
        f"pos={obs['player']['pos']} checksum={api.last_checksum}")
    rng = random.Random(config.seed)
    if profile.kind == "kill" and config.provision:
        supplied = ensure_pigs(api, rng, config.half_extent, profile.count)
        if supplied == 0:
            raise RuntimeError("kill task provisioning failed: no pigs spawned")

    _configure_capture(api, config.capture, config.hud)
    ping = api.grpc.ping()
    try:
        state_at_start = api.grpc.get_state(player=api.player)
        episode_world_name = state_at_start["player"]["dimension"]
    except Exception:  # noqa: BLE001 -- Ping only knows server primary world
        episode_world_name = ping["world_name"]
    meta = {
        "format": "m11_human_demo_v3",
        "task": config.task,
        "task_id": task_id,
        "seed": config.seed,
        "surface": config.surface,
        "map_seed": config.map_seed,
        "worker_id": config.worker_id,
        "kit": SURVIVAL_KIT,
        "player": api.player,
        "spawn": spawn,
        "humanize": config.humanize,
        "controlled_plains": True,
        "ticks_per_step": config.ticks,
        "capture": config.capture,
        "hud": config.hud,
        "world_name": episode_world_name,
        "mc_version": ping["version"],
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    recorder = HumanRecorder(outdir, meta)
    recorder.start(api.ws)
    key_log_enabled = False
    summary: Dict[str, Any] = {}
    try:
        api.ws.send_set_key_log(True)
        key_log_enabled = True

        def step_fn(action: Dict[str, Any], ticks: int) -> Dict[str, Any]:
            api.ws.send_action(action)
            step = api.grpc.get_step_result(player=api.player, await_ticks=ticks)
            try:
                state = api.grpc.get_state(player=api.player)
                recorder.add_step(state, step["server_tick"], step["progress"])
            except Exception as exc:  # noqa: BLE001 -- state file is non-critical
                log(f"[state] skipped: {type(exc).__name__}: {exc}")
            return {
                "progress": float(step["progress"]),
                "terminated": bool(step["terminated"]),
                "truncated": bool(step["truncated"]),
                "reward": float(step["reward"]),
            }

        agent = KitAgent(
            api,
            profile,
            rng=random.Random(config.seed),
            half_extent=config.half_extent,
            recorder=recorder,
            protected_ground_y=63,
            on_no_target=(
                (lambda: ensure_pigs(api, rng, config.half_extent, 1) > 0)
                if profile.kind == "kill" and config.provision else None
            ),
        )
        ok, steps, progress = agent.run(step_fn, max_steps=config.max_steps)

        if config.tail_seconds > 0:
            tail_ticks = int(config.tail_seconds * 20)
            yaw_delta = 360.0 / max(1, tail_ticks)
            for _ in range(tail_ticks):
                idle = {name: False for name in BUTTONS}
                idle["hotbar"] = -1
                idle["camera"] = [0.0, yaw_delta]
                api.ws.send_action(idle)
                step = api.grpc.get_step_result(player=api.player, await_ticks=1)
                try:
                    state = api.grpc.get_state(player=api.player)
                    recorder.add_step(state, step["server_tick"], float(step["progress"]))
                except Exception:  # noqa: BLE001
                    pass

        if key_log_enabled:
            api.ws.send_set_key_log(False)
            key_log_enabled = False
        time.sleep(0.5)
        summary = recorder.finalize({
            "success": bool(ok),
            "steps": int(steps),
            "progress": float(progress),
            "seed": config.seed,
        })
        align_ok = bool(summary["align_ok"]) and summary["frames"] > 0
        mp4_ok = True
        if compose_mp4 is not None:
            mp4_ok = bool(compose_mp4(str(outdir / "frames"), mp4_path))
        return {
            "ok": bool(ok) and align_ok and mp4_ok,
            "success": bool(ok),
            "align_ok": align_ok,
            "mp4_ok": mp4_ok,
            "steps": int(steps),
            "progress": float(progress),
            "outdir": str(outdir),
            "mp4_path": mp4_path,
            "summary": summary,
            "config": asdict(config),
        }
    except Exception:
        if key_log_enabled:
            try:
                api.ws.send_set_key_log(False)
            except Exception:  # noqa: BLE001
                pass
        if not summary:
            try:
                recorder.finalize({"success": False, "error": "episode_exception"})
            except Exception:  # noqa: BLE001
                pass
        raise
