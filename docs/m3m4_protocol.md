# M3-M4 协议规范：任务套件 + 语义动作落盘 + VLA 框架（无推理）

> 范围：M3（任务与课程 + 数据质量）+ M4（VLA 框架）。
> **硬约束：任何涉及模型推理的部分一律不执行**——不加载/不运行 OpenVLA/Pi0/GROOT/STEVE-1/LLM。
> 只搭框架、接口、适配器与本地 mock 冒烟。真实模型推理由用户在其服务器上完成（本机无 GPU/无推理环境）。

## 0. 前置状态

M0-M2 已交付并端到端验证：
- 文件 IPC（`<world>/mcl2_agent/ipc/`），请求式采样（一次 observe 一行 states，帧号取 `obs["episode"]["frame"]`）
- 引擎 fork：`core.set_player_control`（客户端权威移动注入）+ `/tmp/mcl2_frames` 抓帧
- 数据质量修复：地面感知出生点 + 自动重生 + timeofday 钉住 + 死状态守卫
- 导出：WebDataset/HF/RLDS；已验证数据集 `datasets/m2_fix2_verify/`

## 1. M3-A：语义动作落盘修复（Lua 侧）

**问题**：M0 遗留——craft 等瞬时语义动作完成太快，record.sample 的 `current_action_row` 只取 `sess.current_action`，动作开始/结束时恰好错过采样 → `actions.jsonl` 的 semantic 字段缺失。

**方案**：动作生命周期事件 + 队列快照。
- `api/action.lua`：动作状态机新增**持久队列快照** `sess.action_log = {}`——每次语义动作 `queued`/`running`/`success`/`timeout`/`error` 状态迁移时，`action_log[action_id] = {id, args, action_id, status, t0, tick, frame}`（frame 由当时 `rec.frame` 给出，无 episode 时为 nil）。
- `api/record.lua` 的 `current_action_row`：semantic 字段取**该帧时刻正在 running 的动作**（从 action_log 推断：`status=="running"` 且该帧落在其 t0..t_end 区间的动作），找不到时回退为最近的 success 动作（带 `status="completed_recently"` 标记）。保证**每行 actions.jsonl 都有 semantic 字段**（无动作时为 `{"id": null, "status": "idle"}`）。
- stub 测试：断言 5 次 observe 后 actions.jsonl 每行都有 semantic 键；跑一次 craft 断言 semantic.id=="craft" 且 status 含 "success"。

## 2. M3-B：脚本化成功 agent（Python，无推理）

目标：产出**确定性成功轨迹**（模仿学习正样本），不依赖随机探索。

- 新 `mcl2_env/mcl2_env/scripts/scripted_agent.py`：
  - `ScriptedPolicy`：按任务类型生成确定性动作序列。
    - `collect_wood`：扫描 `obs.world.entities/nearby_blocks` 找 `mcl_trees:tree_oak` → `execute("goto",{pos})` → `execute("dig",{pos})` → 轮询 observe 直到 `task.success` 或超时。
    - `craft_planks`：确保背包有 wood（reset 已给 3）→ `execute("craft",{item="mcl_trees:wood_oak",count=4})` → 轮询 success。
    - 通用策略：`look_at` → `goto` → `dig/place/craft` → `collect_nearby`。
  - 循环与对齐断言复用 `_common.py`（begin_episode/observe 取帧/对齐校验/导出）。
  - **验收**：`craft_planks` 跑出 `success=True` 的 episode（steps<60），`collect_wood` 尽力而为（不强制成功）。
- 复用 `record` 请求式采样，成功 episode 的 states/actions 是正样本。

## 3. M3-C：任务生成器接口（课程 + LLM 钩子，LLM 不调用）

- Lua `api/task.lua` 已具备：`task.register`、`generate_craft_tasks`、`curriculum(max_difficulty)`、`generate_llm(prompt)`（占位返回 not_implemented）。
- Python 侧新增 `mcl2_env/mcl2_env/taskgen.py`：
  - `query_tasks(bridge)`：`bridge.tasks()` 拉取注册任务，按 difficulty 排序。
  - `TaskGenerator`：`procedural(registry)` 从 Mineclonia 物品注册表参数化任务（复用 Lua 的 `generate_craft_tasks`，经新 bridge op `task_generate` 触发）；`curriculum()` 返回难度序；`llm_hook(prompt)` 定义接口 + `MockLLM`（返回一条 canned 任务定义），**不接真实 LLM**。
  - bridge.lua 新增 op：`task_generate {kind="procedural", item=...}` / `{kind="curriculum"}` / `{kind="llm", prompt=...}`（llm 走 Mock 或返回 not_implemented 标记）。
- 验收：Python 能列出课程任务、触发 procedural 生成一批 craft 任务、MockLLM 生成一条任务并注册。

## 4. M4：VLA 框架（不推理）

### 4.1 VLA Server（mcl2_env/server.py 完善）

REST + WS 端点（DESIGN.md §8.2），会话管理：
```
POST /session                创建会话（env 实例 + model adapter）
POST /reset   {task, seed}   重置 + 返回 obs
POST /step    {action}       执行动作（原始或语义）→ {obs,reward,terminated,truncated,info}
POST /execute {action, args}  非阻塞语义动作
GET  /observe                 当前观测
GET  /tasks                  任务列表
POST /generate_task           任务生成（见 §3）
POST /record/start|stop       数据落盘控制
POST /visualize               当前帧 PNG（base64）
```
- Server 持有 `ModelAdapter`（注入），**只调用 `adapter.encode_obs` 与 `adapter.decode_action`**——但 M4 阶段 Server 不主动调模型；推理编排在 `examples`/用户侧。
- 本机 smoke：server 启动、`/session`、`/reset`、`/step`（MockAdapter）全链路无推理可跑。

### 4.2 ModelAdapter 抽象（mcl2_env/adapters/）

```
mcl2_env/adapters/base.py     ModelAdapter ABC
mcl2_env/adapters/mock.py     MockAdapter（测试/本地冒烟，无模型）
mcl2_env/adapters/openvla.py  OpenVLA 适配器（encode_obs/decode_action 骨架 + TODO 注释）
mcl2_env/adapters/pi0.py      Pi0 适配器（骨架）
mcl2_env/adapters/groot.py    GROOT 适配器（骨架，字段对齐 MineStudio observation.pov/action.buttons）
mcl2_env/adapters/steve1.py   STEVE-1 适配器（骨架，vpt_token 动作）
mcl2_env/adapters/registry.py 名称→适配器工厂
```

ModelAdapter 接口（统一契约，供用户在服务器上实现真实推理）：
```python
class ModelAdapter(ABC):
    name: str
    def encode_obs(self, obs: dict) -> Any: ...      # obs -> 模型输入
    def decode_action(self, model_out: Any) -> dict:  # 模型输出 -> 环境动作
    def is_available(self) -> bool: return False      # 未接入真实模型恒 False
```
- MockAdapter：`encode_obs` 返回 obs 摘要，`decode_action` 返回一个原始动作（forward=0），`is_available()` False。
- 真实适配器（openvla/pi0/groot/steve1）：**只写输入/输出字段映射与 TODO，不 import 模型库，不加载权重**。import 真实库的代码用 `try/except ImportError` 包裹并标注"在用户服务器启用"。

### 4.3 SDK + 演示（mcl2_env/client.py 完善 + examples/）

- `AgentClient` 补：`execute`/`observe`/`visualize`/`generate_task`/`record_start|stop`（已有基础）。
- `examples/vla_interface_demo.py` 改为 Mock 流程：AgentClient → `/session` → `/reset(task=craft_planks)` → 循环 `MockAdapter` 输出动作 `/step` → 结束。**全程无推理**。
- 新增 `examples/real_inference_template.py`：给用户服务器用的模板——加载真实模型的占位函数 `run_inference(adapter, obs)`，标注 TODO，不执行。

## 5. 验收

1. **M3-A**：`actions.jsonl` 每行都有 semantic 字段（stub 断言 + 真机采集抽查）。
2. **M3-B**：`scripted_agent.py --task craft_planks` 产出 `success=True` episode（对齐断言过）。
3. **M3-C**：Python 可列出课程、触发 procedural 生成、MockLLM 注册任务（`task_generate` op 工作）。
4. **M4**：server 全端点 smoke 通过（MockAdapter）；`real_inference_template.py` 存在且不执行；仓库中**无任何真实模型加载/推理代码路径被执行**。
5. 回归：Lua stub 全绿 + pytest 全绿。

## 6. 环境注意

- 本机：py3.14、无 torch/transformers/TF——正好强制"不推理"。所有模型相关 import 必须延迟/可选。
- 采集验证用真实服务器+客户端（engine_fork 渲染器），与 M2 相同流程。