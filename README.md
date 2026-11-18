# Agent 质量评测框架（agent-eval）

面向 AI Agent 的批量质量评测工具集：P1 断言跑批、链路第一错归因、四档门禁判定。零第三方依赖（纯 Python 标准库），适配器模式接入任意 Agent（HTTP 服务 / 进程内代码 / Node 桥接）。

## 它做什么

1. **批量执行**：评估集（JSON 用例）逐条驱动被测 Agent，每次调用采集完整轨迹（`AgentRun`：最终输出、工具调用、副作用状态、版本信息）；支持多轮对话用例（`turns`，轮次级断言）
2. **P1 断言**：必调/禁调工具（含参数存在性）、输出关键词、状态断言、异常检测，失败带中文明细
3. **P2 语义判分（LLM judge）**：用例声明 `judge_criteria`（自然语言判分标准），校准过的 judge 在 L1/L2 层级自动判语义达标；判分器故障降级「需复核」不硬判，未校准自动跳过并标注
4. **judge 金标校准**：judge 也是模型，不能裸上岗——对人工金标集先过「一致率 ≥85% 且 Spearman ≥0.8」才准参与门禁
5. **归因双口径**：链路第一错（按断言链路顺序，定位用）+ 主归因层（按 system > trajectory > component 固定优先级从全部失败断言中选，定责用；语义判分失败归 component）
6. **门禁判定**：blocker 用例一票否决（不参与百分比稀释）+ 成功率回落基线超阈值拦截
7. **pass^k 复跑与 flaky 检测**：同一用例复跑 k 次，全过才算过；部分通过自动单列为 flaky
8. **快照回放**：轨迹自动落 JSONL 快照，`--compare` 对比新旧报告给出四档判定（可合并/不合并/需排查/环境不稳作废）
9. **覆盖账本可视化**：覆盖单元按层拆解、未覆盖 6 类原因分布、高风险豁免审计红线，一键渲染成单文件 HTML 审计报告
10. **Web 控制台**：评估集增删改查（含多轮 `turns` / P2 `judge_criteria`）、自定义项目注册（HTTP/Python 适配器）、L0/L1/L2 层级跑批、语义判分统计、一键生成覆盖审计报告（零依赖，纯标准库 HTTP server）

## 目录结构

```
agent_eval/               评测框架（9 模块 + 单测 + demo 示例项目）
webui/                    评测控制台（python3 webui/server.py 启动，默认 8900 端口）
```

**模块定位（重要）**：主链路是 `harness.py`（P1 断言 + 批量执行 + 归因 + 门禁）与
`judge.py`（P2 语义判分 + 金标校准统计）。`coverage.py` / `report.py` / `replay.py`
为活代码。仓库不含未接线的「参考实现」——每个模块都在主链路上。

## 快速开始

```bash
# 1. 跑 demo 示例（内置可控缺陷的被测 Agent，无需任何外部服务）
cd agent_eval/examples/demo_project
python3 run_eval.py

# 2. 启动 Web 控制台
cd webui && python3 server.py   # http://127.0.0.1:8900
# 控制台里可选 L0/L1/L2 层级；「生成覆盖审计报告」读 webui/coverage_spec.json
# （仓库自带一份示例），产出 coverage.html
```

单测：`python3 -m pytest agent_eval/tests/ -q`（113 条，含 Web 控制台 API 测试）

## 接入自己的 Agent

只需要实现一个 `AgentAdapter`（约 20 行），把每次调用包装成 `AgentRun`：

```python
from agent_eval_lib import AgentAdapter, AgentRun, ToolCallRecord

class MyAdapter(AgentAdapter):
    def run(self, query: str, context: dict) -> AgentRun:
        output, tool_calls = my_agent_call(query)   # 你的调用逻辑
        return AgentRun(query=query, final_output=output,
                        tool_calls=[ToolCallRecord(name=t) for t in tool_calls])
```

两种典型接入方式：

- **HTTP 服务型**：Agent 是独立跑的服务 → adapter 内发 POST，把响应按点路径映射成
  `AgentRun`（final_output / tool_calls / versions）
- **进程内代码**（含 Node/Electron 项目）：adapter 直接调用被测代码，或 spawn 一个
  Node runner `require` 仓库真实模块并注入 LLM 服务（依赖注入式入口最容易测）

评估集格式见 `agent_eval/README.md`（blocker / major / minor 分层出题法，金标集人工确认）。

## P2 语义判分：让 judge 先校准再上岗

P1 断言只能挡「确定性错误」。语义层的问题——回答对不对、有没有答非所问、
兜底有没有兜住——交给 LLM judge，但 judge 本身也是模型，必须先校准：

```python
from agent_eval_lib import (BatchRunner, load_evalset,
                            SemanticJudge, OpenAICompatClient, calibrate_judge)

judge = SemanticJudge(client=OpenAICompatClient(
    "https://api.deepseek.com/v1/chat/completions", key, "deepseek-chat"))

# 1) 金标校准：200+ 条人工标注，一致率 ≥85% 且 Spearman ≥0.8 才放行
report = calibrate_judge(machine_labels, human_labels)   # pass/fail 标签序列
if report.qualified:
    judge.mark_calibrated(report)

# 2) 接入跑批：L1/L2 层级自动判语义；未校准自动跳过并标注，绝不硬判
runner = BatchRunner(adapter, judge=judge, judge_level=("L1", "L2"))
eval_report = runner.run_all(cases, level="L1")
print(eval_report.semantic_stats())     # 实判 / 失败 / 转人工 / 跳过
```

评估集里声明判分标准（自然语言，一条或多条）：

```json
{
  "id": "CS-006",
  "query": "这款手机的电池容量多大",
  "severity": "blocker",
  "route": "unanswerable",
  "judge_criteria": [
    "回答必须明确表示无法回答（缺少机型信息），而不是瞎猜一个型号",
    "回答应引导用户补充机型，或提供转人工的路径"
  ]
}
```

判定规则：`pass` 计过；`fail` 语义断言失败（归因 component 层，blocker 照样一票否决）；
`need_review` / 调用失败 → 自动转人工队列，不硬判污染结果。

## 覆盖账本可视化

覆盖率不是报个百分数就完事。覆盖账本回答三个审计问题：按层拆（system /
trajectory / component）哪层欠账、未覆盖的 6 类原因分布（「风险豁免」有没有被
滥用）、高风险且未覆盖的单元是哪几个（审计红线，一个都不能有）。

```bash
# demo 示例：跑批 + 生成 coverage.html（双击即看，单文件无外部依赖）
cd agent_eval/examples/demo_project
python3 run_eval.py --fixed --coverage

# 想本地看 LLM 语义判分的效果（跳过金标校准，仅限演示）
python3 run_eval.py --fixed --level L1 --force-uncalibrated

# 自己的账本：写一个覆盖规格 JSON，一行命令出审计报告
python3 agent_eval/report.py --spec coverage_spec.json --out coverage.html --text
```

> **校准红线**：`--force-uncalibrated` 只为本地演示放行未校准 judge。生产/CI 必须
> 先跑 `calibrate_judge`（≥200 条人工金标，一致率 ≥85% 且 Spearman ≥0.8），
> 否则语义断言一律跳过，绝不裸判。Web 控制台同样要求显式传 `force_uncalibrated`。

规格格式（未覆盖单元必须标 6 类原因之一，否则审计不过）：

```json
[
  {"unit_id": "U001", "description": "价格查询", "business_line": "售前",
   "layer": "trajectory", "covered_by": ["CS-001"], "risk_level": "high"},
  {"unit_id": "U005", "description": "多轮上下文", "business_line": "售前",
   "layer": "component", "covered_by": [], "not_covered_reason": "待澄清"}
]
```

## 环境变量

被测 Agent 需要模型 API key 时，统一走环境变量：

```
EVAL_MODEL_URL=https://api.deepseek.com/v1/chat/completions
EVAL_MODEL_NAME=deepseek-chat
EVAL_MODEL_KEY=<你的 key>
```

**key 只放环境变量，不进代码、不进仓库**（`.gitignore` 已排除 `model_env.sh`）。
