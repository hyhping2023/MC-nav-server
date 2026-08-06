#!/usr/bin/env python3
"""export.py / 对齐工具 单元测试：伪造最小 canonical episode 目录。

覆盖（docs/m2_protocol.md §3/§1）：
  - export_webdataset：tar 分片可迭代，每样本含 .jpg 与 .json
    （json 含 instruction/state/action 字段映射）
  - export_huggingface：datasets 已装时生成 parquet（save_to_disk）；
    未装时打印明确提示并返回（不抛错）
  - export_rlds：tensorflow 已装时生成 tfrecord；未装时明确提示并返回
  - verify_alignment：states==actions==rewards==PNG 数、引用全存在、
    meta.json 含 env.engine/game/python 字段
  - resolve_frame：episode.frame 优先，states.jsonl 行数回退

运行方式（任选其一）：
    python3 -m pytest mcl2_env/tests/
    python3 mcl2_env/tests/test_export.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.dataset.export import ExportConfig, export_huggingface, export_rlds, export_webdataset
from mcl2_env.scripts._common import ENGINE, GAME, PYTHON, resolve_frame, verify_alignment

N_FRAMES = 3


def make_canonical_episode(root: str, episode_id: str = "ep-fake01",
                           n: int = N_FRAMES, *, valid: bool = True) -> Path:
    """伪造一个最小 canonical episode 目录（可导入 export 各导出器）。"""
    ep_dir = Path(root) / "episodes" / episode_id
    obs_dir = ep_dir / "observations"
    obs_dir.mkdir(parents=True)

    states, actions, rewards = [], [], []
    for i in range(n):
        if valid:
            states.append({
                "frame": i,
                "image": f"observations/{i:06d}.png",
                "player": {"pos": {"x": float(i), "y": 40.0, "z": 0.0},
                           "look": {"yaw": 0.5, "pitch": -0.2},
                           "hp": 20},
                "inventory": {"main": [{"item": "mcl_trees:tree_oak", "count": 1}]},
                "tick": 10 + i,
            })
            Image.fromarray(np.full((16, 16, 3), 40 + i * 20, dtype=np.uint8)).save(
                obs_dir / f"{i:06d}.png")
        else:
            # 缺图：states 引用不存在的 PNG
            states.append({"frame": i, "image": f"observations/{i:06d}.png",
                           "player": {"pos": {"x": 0.0, "y": 40.0, "z": 0.0},
                                      "look": {"yaw": 0.0, "pitch": 0.0}, "hp": 20},
                           "inventory": {"main": []}, "tick": 10 + i})
        actions.append({"frame": i,
                        "primitive": {"forward": 1, "camera": [0.1, -0.2], "hotbar": 2}})
        rewards.append({"frame": i, "reward": 0.0, "terminated": False, "truncated": False,
                        "info": {}, "tick": 10 + i})

    for name, rows in (("states", states), ("actions", actions), ("rewards", rewards)):
        (ep_dir / f"{name}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    (ep_dir / "instructions.jsonl").write_text(
        json.dumps({"instruction": "Collect wood from nearby trees.", "tick": 0}) + "\n", "utf-8")

    meta = {
        "schema_version": "1.0.0",
        "episode_id": episode_id,
        "env": {
            "engine": ENGINE,
            "game": GAME,
            "mod": {"name": "mcl2_agent", "version": "0.1.0"},
            "python": PYTHON,
        },
    }
    (ep_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    return ep_dir


# ---------------------------------------------------------------- webdataset

def test_export_webdataset_iterable() -> None:
    with tempfile.TemporaryDirectory() as td:
        make_canonical_episode(td)
        out = os.path.join(td, "out")
        export_webdataset(ExportConfig(source_root=td, out_dir=out, shard_size=2))

        shards = sorted(f for f in os.listdir(out) if f.endswith(".tar"))
        assert len(shards) == 2, f"2 帧分片到 2 个 tar (shard_size=2), got {shards}"

        import webdataset as wds
        files = [os.path.join(out, f) for f in shards]
        samples = list(wds.WebDataset(files, shardshuffle=False).decode())
        assert len(samples) == N_FRAMES, f"expected {N_FRAMES} samples, got {len(samples)}"
        for s in samples:
            assert "jpg" in s, f"sample missing .jpg: {s.keys()}"
            assert "json" in s, f"sample missing .json: {s.keys()}"
            js = s["json"]  # .decode() 后 .json 已解析为 dict
            assert js["instruction"] == "Collect wood from nearby trees."
            assert js["state"]["pos"]["x"] in (0.0, 1.0, 2.0)
            assert js["state"]["look"]["yaw"] == 0.5
            assert js["state"]["hp"] == 20
            assert js["state"]["inventory"][0]["item"] == "mcl_trees:tree_oak"
            assert js["action"]["camera"] == [0.1, -0.2]
            assert js["action"]["buttons"]["forward"] == 1
            assert js["action"]["hotbar"] == 2


def test_export_webdataset_multiple_episodes() -> None:
    with tempfile.TemporaryDirectory() as td:
        make_canonical_episode(td, "ep-a", n=2)
        make_canonical_episode(td, "ep-b", n=1)
        out = os.path.join(td, "out")
        export_webdataset(ExportConfig(source_root=td, out_dir=out, shard_size=1000))

        import webdataset as wds
        files = [os.path.join(out, f) for f in os.listdir(out) if f.endswith(".tar")]
        keys = sorted(s["__key__"] for s in wds.WebDataset(files, shardshuffle=False))
        assert keys == ["ep-a-000000", "ep-a-000001", "ep-b-000000"], keys


# ---------------------------------------------------------------- huggingface

def test_export_huggingface() -> None:
    with tempfile.TemporaryDirectory() as td:
        make_canonical_episode(td)
        out = os.path.join(td, "hf")
        try:
            from datasets import Dataset  # noqa: F401
        except ImportError:
            # 未装 datasets：函数应打印明确提示并返回（不抛错），目录被创建
            ret = export_huggingface(ExportConfig(source_root=td, out_dir=out))
            assert ret == out
            assert os.path.isdir(out) or not os.path.exists(out)
            return
        export_huggingface(ExportConfig(source_root=td, out_dir=out))
        parquet = os.path.join(out, "data.parquet")
        assert os.path.exists(parquet), f"缺少 parquet 文件: {os.listdir(out)}"
        ds = Dataset.from_parquet(parquet)
        assert len(ds) == N_FRAMES
        row = ds[0]
        assert row["instruction"].startswith("Collect wood")
        assert row["state"]["pos"]["x"] == 0.0
        cam = row["action"]["camera"]
        assert abs(cam[0] - 0.1) < 1e-6 and abs(cam[1] + 0.2) < 1e-6, cam


# ---------------------------------------------------------------- rlds

def test_export_rlds() -> None:
    with tempfile.TemporaryDirectory() as td:
        make_canonical_episode(td)
        out = os.path.join(td, "rlds")
        try:
            import tensorflow as tf  # noqa: F401
        except ImportError:
            ret = export_rlds(ExportConfig(source_root=td, out_dir=out))
            assert ret == out
            return
        export_rlds(ExportConfig(source_root=td, out_dir=out))
        files = [f for f in os.listdir(out) if f.endswith(".tfrecord")]
        assert len(files) >= 1, f"缺少 tfrecord: {os.listdir(out)}"
        # 解析第一个 example，验证字段名严格按 m2_protocol §3 表
        path = os.path.join(out, files[0])
        it = tf.compat.v1.io.tf_record_iterator(path)
        ex = tf.train.Example()
        ex.ParseFromString(next(it))
        feats = ex.features.feature
        assert "observation/image" in feats
        assert "observation/state" in feats
        assert "action" in feats
        assert "instruction" in feats
        assert len(feats["observation/state"].float_list.value) == 6  # xyz+yaw+pitch+hp
        assert feats["action"].float_list.value[0] == 0.1


# ---------------------------------------------------------------- 对齐断言

def test_verify_alignment_ok() -> None:
    with tempfile.TemporaryDirectory() as td:
        ep_dir = make_canonical_episode(td)
        ok, checks = verify_alignment(ep_dir)
        assert ok, f"预期对齐通过，失败项: {[(n, d) for n, o, d in checks if not o]}"
        for name, passed, detail in checks:
            assert passed, f"{name}: {detail}"


def test_verify_alignment_missing_png() -> None:
    with tempfile.TemporaryDirectory() as td:
        ep_dir = make_canonical_episode(td, valid=False)  # states 引用不存在的 PNG
        ok, checks = verify_alignment(ep_dir)
        assert not ok, "缺 PNG 时对齐断言必须 FAIL"
        names = [n for n, o, d in checks]
        assert any("引用" in n for n in names), names
        failed = {n: d for n, o, d in checks if not o}
        assert any("observations/000000.png" in str(d) for d in failed.values()), failed


def test_verify_alignment_env_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        ep_dir = make_canonical_episode(td)
        # 篡改 meta 去掉 env 字段 → 断言必须 FAIL
        meta = json.loads((ep_dir / "meta.json").read_text("utf-8"))
        meta["env"] = {}
        (ep_dir / "meta.json").write_text(json.dumps(meta), "utf-8")
        ok, checks = verify_alignment(ep_dir)
        assert not ok
        assert any("env.engine.name" in n for n, o, d in checks if not o)


# ---------------------------------------------------------------- resolve_frame

def test_resolve_frame_episode_field_first() -> None:
    with tempfile.TemporaryDirectory() as td:
        ep_dir = make_canonical_episode(td)
        obs = {"episode": {"frame": 7}, "frame": 9}
        assert resolve_frame(ep_dir, obs) == 7  # episode.frame 优先


def test_resolve_frame_states_line_count() -> None:
    with tempfile.TemporaryDirectory() as td:
        ep_dir = make_canonical_episode(td, n=3)
        assert resolve_frame(ep_dir, None) == 2  # 3 行 → frame 2


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
