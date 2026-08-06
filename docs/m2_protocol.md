# M2 协议规范：数据管道对齐 + 导出

> M2 目标（DESIGN.md §11）：
> 1. **帧/状态一一对齐**：canonical episode 中每行 states 引用的 PNG 真实存在且为同一时刻
> 2. **meta.json 完整**：补 engine/game/python 版本字段
> 3. **导出工具实现**：WebDataset / HuggingFace / RLDS / MineStudio 字段映射
> 4. **小数据集采集 + 导出验证**：多 episode（含成功任务），导出后验证可加载
>
> 前置：M1 已完成——真实玩家驱动 + 客户端抓帧 + random_agent PASS。
> 问题背景：M1-E 中 Lua 侧全局 step 按 5Hz 采样出 302 行 states，Python 每 step 写 60 帧，
> states 引用的 PNG 大量缺失（帧/状态数量不一致）。

## 1. 帧/状态对齐协议（请求式采样）

**核心决策：states 采样改为"请求式"**——由 Python 的 `observe` 触发，不再由全局 step 独立定时采样。
这样一行 states 对应 Python 的一次观察与一帧图像，数量完全一致。

### Lua 侧（record.lua）

- `record.sample(sess, tick)` 不再在全局 step 中调用。
- 改为：`observe` bridge handler 中，若 `sess.episode` 激活，调用 `record.sample(sess, tick)`。
  - 每调用一次写一行 states/actions/rewards，`rec.frame += 1`。
  - `record.sample` 内部去掉 5Hz 间隔判断（请求式已天然限速）。
- states 行新增字段：
  - `server_tick`（= tick）
  - `wall_us`（= `minetest.get_us_time()`，微秒，用于与帧时间戳核对）
- `image` 字段保持 `observations/%06d.png`，frame 与行号一致。
- begin_episode 时写 meta.json + instructions.jsonl（已有）；end_episode 写 summary（已有）。

### Python 侧（random_agent / 采集脚本）

- 每 step 后 `observe`（已如此）→ Lua 写一行 states。
- 从 renderer 取帧 → 用 observe 响应 `episode.frame` 得到 frame 号
  （优先；读不到再回退 `states.jsonl` 行数）→ `EpisodeWriter.write_frame(img, frame)` 写入同名 PNG。
- Lua 在 `record.sample` 后 flush states/actions/rewards，Python 读行数/PNG 不受缓冲影响。
- 循环结束 `end_episode` 后，断言：states.jsonl 行数 == observations/*.png 数，
  且每行 `image` 引用的文件存在。

### 验收断言（M2）

```
states.jsonl 行数 N == actions.jsonl 行数 N == rewards.jsonl 行数 N == PNG 数 N
∀ row in states: os.path.exists(ep_dir/row.image)
meta.json 含 env.engine.name/version、env.game.name/version、env.python.package/version
```

## 2. meta.json env 字段

`begin_episode` 请求携带（Python 侧补齐）：
```json
"engine": {"name": "luanti", "version": "5.17.0-dev", "fork": "mcl2-agent-fork"},
"game": {"name": "mineclonia", "version": "0.123.0"},
"python": {"package": "mcl2_env", "version": "0.1.0"}
```
Lua `record.write_meta` 已写 env 字段（读 `rec.info.engine/game/python`）——只需 Python 传入。

## 3. 导出工具（mcl2_env/dataset/export.py）

canonical episode → 各目标格式。字段映射对齐 MineStudio / Open X-Embodiment：

| 概念 | canonical（states/actions） | MineStudio 字段 | Open X-Embodiment |
|---|---|---|---|
| 第一人称图 | `states[i].image` | `observation.pov` | `observation/image` |
| 位置 | `player.pos` | `observation.location_stats.pos` | `observation/state`(部分) |
| 朝向 | `player.look` | — | — |
| 背包/生命 | `player.hp/inventory` | — | `observation/state` |
| 指令 | `instructions[0].text` | `action.instruction` | `instruction` |
| 原始动作 | `actions[i].primitive` | `action.camera/buttons` | `action`(离散) |
| 语义动作 | `actions[i].semantic` | — | — |

三个导出器（`ExportConfig` 见 export.py）：
1. **WebDataset**：`{__key__}.jpg + {__key__}.json`（json 含 instruction/state/action）。
   - 实现 `export_webdataset`：遍历 episodes，每帧一个样本，shard 分片（默认 1000）。
   - 验证：`wds.WebDataset(path)` 可迭代，样本含 `.jpg` 与 `.json`。
2. **HuggingFace**：`export_huggingface` → `datasets.Dataset`（image: Image(), instruction: str,
   state: dict, action: dict），保存为 parquet，可 `load_from_disk` / `push_to_hub`。
3. **RLDS/TFRecord + MineStudio**：`export_rlds` 把字段按上表映射为 tf.train.Example
   （observation/action/instruction）。不装 TF 时打印明确提示。

## 4. 小数据集采集

`scripts/collect_dataset.py`（新建）：
- 参数 --repo/--world/--episodes/--tasks/--out。
- 循环采集 N 个 episode：起 server+client → begin_episode(task) → 随机原始动作
  （与 random_agent 相同逻辑）→ end_episode → 下一个。
- 任务建议含 `craft_planks`（成功可达）与 `collect_wood`（随机探索）。
- 采集完后调 `export_webdataset`，并把结果目录报告给 team-lead。

## 5. 运行方式

```bash
# 单 episode 验证（沿用 random_agent）
python mcl2_env/mcl2_env/scripts/random_agent.py --repo <repo> --world m0world \
    --renderer engine_fork --spawn-client --steps 60 --task collect_wood

# 数据集采集
python mcl2_env/mcl2_env/scripts/collect_dataset.py --repo <repo> --world m0world \
    --episodes 4 --tasks craft_planks,collect_wood --out <repo>/datasets/m2_run
```
