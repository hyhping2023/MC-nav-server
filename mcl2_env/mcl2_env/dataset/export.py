"""从 canonical 布局导出到通用格式（DESIGN.md §7.5 / docs/m2_protocol.md §3）。

canonical episode → 三种目标：
  - WebDataset：`{__key__}.jpg + {__key__}.json` 的 tar 分片，流式加载
  - HuggingFace Dataset：image(Image())/instruction(str)/state(dict)/action(dict)，
    存为 parquet（save_to_disk），可 load_from_disk / push_to_hub
  - RLDS/TFRecords：对齐 Open X-Embodiment 字段名
    （observation/image、observation/state、action、instruction）

字段映射（m2_protocol §3 表）：
  states[i].image        → observation.pov (MineStudio) / observation/image (OXE)
  player.pos/look/hp/inv → observation.state（RLDS 展平为 float 向量）
  instructions[0].text   → instruction
  actions[i].primitive   → action.camera / action.buttons（离散按钮 + 相机增量）

缺包行为：webdataset 缺失时 raise RuntimeError（明确报错）；datasets /
tensorflow 缺失时打印明确提示并返回 out_dir（canonical 数据本身始终完整）。
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
from typing import Any, Iterator

from PIL import Image


@dataclasses.dataclass
class ExportConfig:
    source_root: str          # run_dir
    out_dir: str
    target: str = "webdataset"   # "webdataset" | "huggingface" | "rlds"
    shard_size: int = 1000    # webdataset 每 tar 的分片样本数
    img_key: str = "observation.image"   # 目标格式的字段路径（保留兼容）
    only: tuple[str, ...] = ()  # 仅导出这些 episode_id（空 = 全部，需已对齐）


# ---------------------------------------------------------------- canonical 读取

def iter_episodes(source_root: str, only: tuple[str, ...] = ()) -> Iterator[dict[str, Any]]:
    """读取 canonical 布局，yield 每个 episode 的帧级记录。

    only 非空时仅遍历指定 episode_id（例如刚采集、已通过对齐断言的一批）。
    """
    ep_root = os.path.join(source_root, "episodes")
    for name in sorted(os.listdir(ep_root)):
        if only and name not in only:
            continue
        ep_dir = os.path.join(ep_root, name)
        if not os.path.isdir(ep_dir):
            continue
        meta_path = os.path.join(ep_dir, "meta.json")
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)

        states = _read_jsonl(os.path.join(ep_dir, "states.jsonl"))
        actions = _read_jsonl(os.path.join(ep_dir, "actions.jsonl"))
        rewards = _read_jsonl(os.path.join(ep_dir, "rewards.jsonl"))
        instrs = _read_jsonl(os.path.join(ep_dir, "instructions.jsonl"))

        yield {
            "episode_id": name,
            "meta": meta,
            "states": states,
            "actions": actions,
            "rewards": rewards,
            "instructions": instrs,
            "obs_dir": os.path.join(ep_dir, "observations"),
        }


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------- 字段映射

def _instruction_text(ep: dict[str, Any]) -> str:
    """instructions[0] 的指令文本（兼容 text / instruction 两个键名）。"""
    instrs = ep.get("instructions") or []
    if not instrs:
        return ""
    first = instrs[0]
    if isinstance(first, dict):
        return first.get("text") or first.get("instruction") or ""
    return str(first)


def _summarize_state(state: dict[str, Any]) -> dict[str, Any]:
    """states 行 → 摘要：pos / look / hp / inventory（MineStudio observation 对齐）。

    inventory 收敛为 [{"item", "count"}, ...] 列表（Arrow 友好、可哈希），
    空背包为 []。
    """
    player = state.get("player") or {}
    pos = player.get("pos") or {}
    look = player.get("look") or {}
    inv = state.get("inventory") or {}
    main = inv.get("main") or []
    counts: dict[str, int] = {}
    for slot in main:
        if isinstance(slot, dict) and slot.get("item"):
            counts[slot["item"]] = counts.get(slot["item"], 0) + int(slot.get("count", 1) or 1)
    inventory = [{"item": item, "count": counts[item]} for item in sorted(counts)]
    return {
        "pos": {"x": pos.get("x"), "y": pos.get("y"), "z": pos.get("z")},
        "look": {"yaw": look.get("yaw"), "pitch": look.get("pitch")},
        "hp": player.get("hp"),
        "inventory": inventory,
    }


def _summarize_action(action: dict[str, Any]) -> dict[str, Any]:
    """actions 行 → 动作摘要：camera（相机增量）+ buttons（离散按钮）+ hotbar。

    MineStudio 字段映射：action.camera / action.buttons。按钮规范为 0/1 整数。
    """
    prim = action.get("primitive") or {}
    cam = prim.get("camera") or [0.0, 0.0]
    if isinstance(cam, (list, tuple)):
        cam = [float(cam[0]) if len(cam) > 0 else 0.0,
               float(cam[1]) if len(cam) > 1 else 0.0]
    else:
        cam = [float(cam), 0.0]
    return {
        "camera": cam,
        "buttons": {
            "forward": int(prim.get("forward", 0) or 0),
            "back": int(prim.get("back", 0) or 0),
            "left": int(prim.get("left", 0) or 0),
            "right": int(prim.get("right", 0) or 0),
            "jump": int(prim.get("jump", 0) or 0),
            "sneak": int(prim.get("sneak", 0) or 0),
            "sprint": int(prim.get("sprint", 0) or 0),
            "attack": int(prim.get("attack", 0) or 0),
            "use": int(prim.get("use", 0) or 0),
            "drop": int(prim.get("drop", 0) or 0),
        },
        "hotbar": int(prim.get("hotbar", 0) or 0),
    }


def _read_image_jpeg(state: dict[str, Any], obs_dir: str) -> bytes | None:
    """读取 states 行引用的 PNG 并转 JPEG bytes；缺图返回 None。"""
    ref = state.get("image")
    if not ref:
        return None
    path = os.path.join(obs_dir, os.path.basename(ref))
    if not os.path.exists(path):
        return None
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


# ---------------------------------------------------------------- 导出器

def export_webdataset(cfg: ExportConfig) -> str:
    """导出为 webdataset tar 分片。每样本：__key__.jpg + __key__.json。

    json 内容：instruction / state（pos/look/hp/inventory 摘要）/ action
    （primitive 字段映射）。分片大小由 cfg.shard_size 控制（默认 1000）。
    """
    try:
        import webdataset as wds
    except ImportError:
        raise RuntimeError(
            "export_webdataset: 未安装 webdataset。"
            "pip install webdataset（PEP668 拦截时用 --break-system-packages 或虚拟环境）"
        )

    os.makedirs(cfg.out_dir, exist_ok=True)
    sink: Any = None
    count = 0
    skipped = 0
    shard = 0
    try:
        for ep in iter_episodes(cfg.source_root, cfg.only):
            instr = _instruction_text(ep)
            for i, state in enumerate(ep["states"]):
                if i >= len(ep["actions"]):
                    break
                jpeg = _read_image_jpeg(state, ep["obs_dir"])
                if jpeg is None:
                    skipped += 1
                    continue
                if count % cfg.shard_size == 0:
                    if sink is not None:
                        sink.close()
                    shard += 1
                    sink = wds.TarWriter(os.path.join(cfg.out_dir, f"shard-{shard:06d}.tar"))
                key = f"{ep['episode_id']}-{i:06d}"
                sample = {
                    "__key__": key,
                    "jpg": jpeg,
                    "json": {
                        "instruction": instr,
                        "state": _summarize_state(state),
                        "action": _summarize_action(ep["actions"][i]),
                    },
                }
                sink.write(sample)
                count += 1
        if skipped:
            print(f"[export_webdataset] {skipped} 帧缺图跳过")
        print(f"[export_webdataset] 导出 {count} 帧 → {cfg.out_dir} ({shard} shard(s))")
        return cfg.out_dir
    finally:
        if sink is not None:
            sink.close()


def _hf_state(state_summary: dict[str, Any]) -> dict[str, Any]:
    """state 摘要 → HF 列式表示。

    datasets>=5 的 Sequence(dict) 是"列式 dict"语义（{item:[...], count:[...]}），
    需把 webdataset 用的行式 [{item,count},...] 转成列式，否则 encode 报错。
    """
    st = dict(state_summary)
    inv = st.get("inventory") or []
    st["inventory"] = {"item": [i["item"] for i in inv], "count": [int(i["count"]) for i in inv]}
    return st


def export_huggingface(cfg: ExportConfig) -> str:
    """导出为 HuggingFace Dataset（parquet），列：image/instruction/state/action。

    未安装 datasets 时打印明确提示并返回（canonical 数据不受影响）。
    """
    try:
        from datasets import Dataset, Features, Sequence, Value
        from datasets import Image as ImageFeature
    except ImportError as e:
        print(f"[export_huggingface] 未安装 datasets 包：{e}")
        print("[export_huggingface] pip install datasets 后重试"
              "（PEP668 拦截时用 --break-system-packages 或虚拟环境）")
        return cfg.out_dir

    rows: list[dict[str, Any]] = []
    for ep in iter_episodes(cfg.source_root, cfg.only):
        instr = _instruction_text(ep)
        for i, state in enumerate(ep["states"]):
            if i >= len(ep["actions"]):
                break
            jpeg = _read_image_jpeg(state, ep["obs_dir"])
            if jpeg is None:
                continue
            pil = Image.open(io.BytesIO(jpeg)).convert("RGB")
            rows.append({
                "image": pil,
                "instruction": instr,
                "state": _hf_state(_summarize_state(state)),
                "action": _summarize_action(ep["actions"][i]),
            })

    features = Features({
        "image": ImageFeature(),
        "instruction": Value("string"),
        "state": {
            "pos": {"x": Value("float32"), "y": Value("float32"), "z": Value("float32")},
            "look": {"yaw": Value("float32"), "pitch": Value("float32")},
            "hp": Value("int64"),
            "inventory": Sequence({"item": Value("string"), "count": Value("int64")}),
        },
        "action": {
            "camera": Sequence(Value("float32"), length=2),
            "buttons": {
                "forward": Value("int64"), "back": Value("int64"),
                "left": Value("int64"), "right": Value("int64"),
                "jump": Value("int64"), "sneak": Value("int64"),
                "sprint": Value("int64"), "attack": Value("int64"),
                "use": Value("int64"), "drop": Value("int64"),
            },
            "hotbar": Value("int64"),
        },
    })
    ds = Dataset.from_list(rows, features=features)
    # m2_protocol §3：存 parquet 到 out_dir（datasets>=5 的 save_to_disk 存 .arrow，
    # 这里显式写 parquet，可 load_dataset("parquet") / from_parquet / push_to_hub）
    os.makedirs(cfg.out_dir, exist_ok=True)
    ds.to_parquet(os.path.join(cfg.out_dir, "data.parquet"))
    print(f"[export_huggingface] 导出 {len(rows)} 行 → {cfg.out_dir}/data.parquet")
    return cfg.out_dir


def export_rlds(cfg: ExportConfig) -> str:
    """导出为 RLDS/TFRecord，对齐 Open X-Embodiment 字段名。

    features（m2_protocol §3 表）：
      observation/image    JPEG bytes
      observation/state   float 向量（pos xyz + yaw/pitch + hp）
      action              float 向量（camera 2 + buttons 10 + hotbar 1）
      instruction         UTF-8 bytes
    未安装 tensorflow 时打印明确提示并返回。
    """
    try:
        import tensorflow as tf
    except ImportError as e:
        print(f"[export_rlds] 未安装 tensorflow 包：{e}")
        print("[export_rlds] pip install tensorflow 后重试"
              "（PEP668 拦截时用 --break-system-packages 或虚拟环境）")
        return cfg.out_dir

    os.makedirs(cfg.out_dir, exist_ok=True)
    writer: Any = None
    count = 0
    shard = 0
    try:
        for ep in iter_episodes(cfg.source_root, cfg.only):
            instr = _instruction_text(ep)
            for i, state in enumerate(ep["states"]):
                if i >= len(ep["actions"]):
                    break
                jpeg = _read_image_jpeg(state, ep["obs_dir"])
                if jpeg is None:
                    continue
                if count % cfg.shard_size == 0:
                    if writer is not None:
                        writer.close()
                    shard += 1
                    writer = tf.io.TFRecordWriter(
                        os.path.join(cfg.out_dir, f"episode-{shard:06d}.tfrecord"))
                st = _summarize_state(state)
                ac = _summarize_action(ep["actions"][i])
                state_vec = [
                    float(st["pos"]["x"] or 0.0), float(st["pos"]["y"] or 0.0),
                    float(st["pos"]["z"] or 0.0),
                    float(st["look"]["yaw"] or 0.0), float(st["look"]["pitch"] or 0.0),
                    float(st["hp"] or 0),
                ]
                action_vec = (list(ac["camera"])
                              + [float(v) for v in ac["buttons"].values()]
                              + [float(ac["hotbar"])])
                feats = {
                    "observation/image": tf.train.Feature(
                        bytes_list=tf.train.BytesList(value=[jpeg])),
                    "observation/state": tf.train.Feature(
                        float_list=tf.train.FloatList(value=state_vec)),
                    "action": tf.train.Feature(
                        float_list=tf.train.FloatList(value=action_vec)),
                    "instruction": tf.train.Feature(
                        bytes_list=tf.train.BytesList(value=[instr.encode("utf-8")])),
                }
                example = tf.train.Example(features=tf.train.Features(feature=feats))
                writer.write(example.SerializeToString())
                count += 1
        print(f"[export_rlds] 导出 {count} 帧 → {cfg.out_dir} ({shard} tfrecord(s))")
        return cfg.out_dir
    finally:
        if writer is not None:
            writer.close()
