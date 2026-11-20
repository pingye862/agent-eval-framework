# agent_eval

把一套可落地的 Agent 质量评测方法论变成可执行的测试代码。

## 目录结构

```
agent_eval/
├── models.py            数据模型：覆盖单元 / 快照 / 归因层 / 门禁判定
├── coverage.py          覆盖账本 + 6 类未覆盖原因 + 覆盖率审计
├── attribution.py       归因引擎：三层只认一个主归因 + 链路第一错
├── judge.py             P2 语义判分（带执行证据）+ 金标校准（一致率 / Cohen's κ）
├── harness.py           执行器：P1 断言（含证据接地）+ 跑批 + 归因 + 门禁
│                        + 运行指纹 / 失败样本回流 / 逐题报告对比
├── evalset.py           评估集加载（dataset 分层 + 新断言字段校验）
├── replay.py            快照管理 + 单变量回放（3 种情形 + 门禁）
├── report.py            覆盖账本可视化（HTML 审计报告）
├── agent_eval_lib.py    门面，外部项目只 import 它
├── tests/
│   ├── test_attribution.py / test_coverage.py / test_harness.py
│   ├── test_judge.py / test_evalset.py / test_report.py
│   ├── test_replay.py / test_webui.py
│   └── test_flywheel.py   数据飞轮增量能力（轨迹证据/回流/分层/性能/接地）
├── examples/
│   └── demo_project/    虚构的数码导购 Agent（带 4 个可控缺陷，含 Case B 演示）
└── README.md
```

## 运行

```bash
cd agent_eval
python -m pytest tests/ -v          # 框架自身单元测试（与 webui 测试合计 158 条）

# 完整接入示例（推荐先跑这个）
cd examples/demo_project
python3 run_eval.py                 # 带缺陷 → 33.3%，门禁红，exit 1
python3 run_eval.py --fixed         # 修复缺陷 → 100%，门禁绿，exit 0
python3 run_eval.py --dataset smoke # 只跑冒烟集（3 条）
GATE_FIXED=1 python3 -m pytest test_gate.py -v   # pytest 门禁（CI 接入形态）
```

## 接入你自己的项目：四步

你的 Agent **一行代码都不用改**，所有接入都发生在测试侧：

**第 1 步：写适配器**（唯一要写的胶水，参考 `examples/demo_project/adapter.py`）

```python
from agent_eval_lib import AgentAdapter, AgentRun

class MyAgentAdapter(AgentAdapter):
    def run(self, query: str, context: dict) -> AgentRun:
        resp = my_project.invoke(query, **context)   # 调你的 Agent
        return AgentRun(
            query=query,
            final_output=resp["answer"],
            tool_calls=[ToolCallRecord(name=t["name"], args=t["args"],
                                      result=t.get("result"))
                        for t in resp["trace"]["tool_calls"]],
            state=my_project.read_mock_state(),       # 副作用后的系统状态
            versions=resp["versions"],                # 模型/prompt/工具描述/KB 四要素
            latency_ms=resp.get("latency_ms"),        # 可选：性能对比用
            token_usage=resp.get("token_usage", {}),  # 可选：成本对比用
        )
```

`ToolCallRecord.result` 是工具真实返回值。带上它，证据接地断言与
judge 的证据核验才有数据；不带也能跑，相关能力自动标 skipped。

**第 2 步：准备评估集**（JSON，参考 `evalset.json`）

关键字段：`severity`（blocker 一票否决）、`dataset`（smoke / regression / baseline 分层）、
`must_call` / `must_not_call`（轨迹断言）、`response_contains`（结果断言）、
`state_after`（副作用断言）、`context_overrides`（用例级前置状态）、
`judge_criteria`（P2 语义判分标准）、`tool_result_used` / `output_grounded_in`
（证据接地断言，见根目录 README）。

**第 3 步：命令行跑批**

```bash
python3 run_eval.py --gate --baseline 0.95   # 门禁模式：比基线掉 2pp 或 blocker 挂 → exit 1
```

**第 4 步：接进 CI**（在仓库根目录）

```yaml
# GitHub Actions 示例
- run: pip install pytest
- run: pytest eval/test_gate.py --junitxml=gate.xml   # 红 = 不许合并
```

## 模块对照

| 代码 | 作用 |
|---|---|
| `agent_eval_lib.py` | 门面，外部项目只 import 它 |
| `harness.py` | 执行器：AgentAdapter 协议 + P1 断言（含证据接地）+ P2 语义判分接入 + 跑批 + 归因 + 门禁 + 运行指纹 / 失败回流 / 逐题对比 |
| `judge.py` | P2 语义判分：LLM judge（带执行证据）+ 金标校准（一致率 ≥85% 且 Cohen's κ ≥0.8 才上岗） |
| `report.py` | 覆盖账本可视化：单文件 HTML 审计报告（按层拆解 / 6 类原因 / 高风险红线） |
| `evalset.py` | 评估集加载 + dataset 分层过滤 + 半成品字段直接报错 |
| `models.py` / `attribution.py` / `replay.py` / `coverage.py` | 方法论内核（见下表） |

## 核心模块说明

| 代码 | 设计要点 |
|---|---|
| `coverage.py` | 覆盖单元 / 覆盖账本与 6 类未覆盖原因（只标注、不删除） |
| `attribution.py` | 三层单归因（系统/轨迹/组件只认一层）/ 链路第一错 |
| `replay.py` | 快照 / 回放三种情形（换提示词、换模型、换知识库）/ 回放门禁四档 |

## Web 控制台

```bash
python3 webui/server.py        # → http://127.0.0.1:8900（纯标准库，无新增依赖）
```

开箱即用：demo 示例（缺陷/修复红→绿切换）。其他被测对象通过「➕ 自定义项目」接入
（HTTP 服务或 Python 适配器，注册后出现在评估集与被测对象下拉里）。

### 手动接入新项目（不改代码）

页面「➕ 自定义项目」表单，或直接编辑 `webui/projects.json`（热加载，保存即生效）。两种类型：

- **http**：任意 REST 形态的 Agent 服务。填接口 URL、请求体模板（`{query}`/`{session}` 占位）、
  响应字段点路径映射（回答、工具轨迹、版本四要素）。添加时自动校验评估集可加载、接口可达。
- **python**：指向一个 `.py` 文件里的 `AgentAdapter` 子类（进程内/特殊协议场景）。

注册表落在 `webui/projects.json`，可增删、持久化、跨会话保留。
