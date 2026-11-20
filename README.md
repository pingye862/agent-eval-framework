# Agent 质量评测框架（agent-eval）

面向 AI Agent 的批量质量评测工具集：P1 断言跑批、链路第一错归因、四档门禁判定。零第三方依赖（纯 Python 标准库），适配器模式接入任意 Agent（HTTP 服务 / 进程内代码 / Node 桥接）。

**定位边界**：本框架是 **CI 门禁级**的离线评测库（对标 DeepEval 那一侧），
不是线上观测平台。它刻意不做无侵入探针、Span 树、线上巡检告警——
这些是 AgentLoop / ARMS 那类平台型方案的事。需要线上 Trace 时，正确姿势是
**把别家的 OTel / Trace JSON 转成 `AgentRun`**，而不是自己造探针。
`write_snapshots` 输出的 JSONL 就是两边对接的落点。

## 它做什么

1. **批量执行**：评估集（JSON 用例）逐条驱动被测 Agent，每次调用采集完整轨迹（`AgentRun`：最终输出、工具调用**及其返回值**、副作用状态、版本信息、耗时与 Token）；支持多轮对话用例（`turns`，轮次级断言）
2. **P1 断言**：必调/禁调工具（含参数存在性）、输出关键词、状态断言、异常检测，**以及证据接地断言**（工具返回值是否真的被回答采用），失败带中文明细
3. **P2 语义判分（LLM judge）**：用例声明 `judge_criteria`（自然语言判分标准），校准过的 judge 在 L1/L2 层级自动判语义达标；**裁判会同时看到 `tool_calls` / 工具返回值 / 召回片段 / 结束状态**，因此能判「结论有没有证据支持」而不只是「回答顺不顺眼」；判分器故障降级「需复核」不硬判，未校准自动跳过并标注
4. **judge 金标校准**：judge 也是模型，不能裸上岗——对人工金标集先过「一致率 ≥85% 且 Cohen's κ ≥0.8」才准参与门禁
5. **归因双口径**：链路第一错（按断言链路顺序，定位用）+ 主归因层（按 system > trajectory > component 固定优先级从全部失败断言中选，定责用；语义判分失败归 component）
6. **门禁判定**：blocker 用例一票否决（不参与百分比稀释）+ 成功率回落基线超阈值拦截
7. **pass^k 复跑与 flaky 检测**：同一用例复跑 k 次，全过才算过；部分通过自动单列为 flaky
8. **快照留档 + 报告级对比**：轨迹自动落 JSONL 快照（当前只写不回读，供外部对接 / 留档），`--compare` 基于两份报告 JSON 做逐题 diff + 四档判定（可合并/不合并/需排查/环境不稳作废）
9. **覆盖账本可视化**：覆盖单元按层拆解、未覆盖 6 类原因分布、高风险豁免审计红线，一键渲染成单文件 HTML 审计报告
10. **Web 控制台**：评估集增删改查（含多轮 `turns` / P2 `judge_criteria` / 证据接地断言 / `dataset` 标签）、自定义项目注册（HTTP/Python 适配器）、L0/L1/L2 层级跑批、语义判分统计、一键生成覆盖审计报告（零依赖，纯标准库 HTTP server）
11. **数据飞轮最小闭环**：`dataset` 分层（smoke / regression / baseline）→ 失败样本导出候选回归用例 → 人工确认后安全回灌 → 逐题 diff 版本对比（含延迟 / Token）→ 运行指纹（run_id / env / 配置签名）保证「可比的报告才放一起比」

## 目录结构

```
agent_eval/               评测框架（核心模块 + 单测 + demo 示例 + 通用案例库）
webui/                    评测控制台（python3 webui/server.py 启动，默认 8900 端口）
docs/                     小白上手指南（getting-started-zh.md，10 分钟跑通第一份报告）
```

**模块定位（重要）**：主链路是 `harness.py`（P1 断言 + 批量执行 + 归因 + 门禁）与
`judge.py`（P2 语义判分 + 金标校准统计）。`coverage.py` / `report.py` 经门面
`agent_eval_lib.py` 暴露，`replay.py` 的四档阈值被 `compare_reports` 复用。
另有库内可复用、但未接 CLI 的组件：`SnapshotStore` / `GateVerdict`
（`replay.py` / `models.py`）与 `render_run_report_html`（`report.py`）——
它们不是死代码，但也不在主跑批链路上，不要误以为「每个模块都在主链路上」。

## 快速开始

> **第一次用？先看 [小白上手指南](docs/getting-started-zh.md)**——从零到跑通
> 第一份测试报告，每步带可复制命令和输出解读。

```bash
# 1. 跑 demo 示例（内置可控缺陷的被测 Agent，无需任何外部服务）
cd agent_eval/examples/demo_project
python3 run_eval.py

# 2. 启动 Web 控制台
cd webui && python3 server.py   # http://127.0.0.1:8900
# 控制台里可选 L0/L1/L2 层级；「生成覆盖审计报告」读 webui/coverage_spec.json
# （仓库自带一份示例），产出 coverage.html

# 3. 跑通用案例库（与业务无关的 10 个能力维度，零依赖参考实现，应全绿）
cd agent_eval/examples/universal_cases && python3 run_eval.py -v
```

单测：`python3 -m pytest agent_eval/tests/ -q`（230 条，含 Web 控制台 API、数据飞轮闭环与门禁可靠性回归；数字以 `--collect-only` 为准）

## 通用案例库：一套用例跑任何系统

`agent_eval/examples/universal_cases/` 是**与业务无关**的评测资产，覆盖任何 Agent 都该
具备的 10 个能力维度：存活性与指令遵循、工具选择、结果采用、危险操作禁令、写副作用、
证据接地、多轮记忆、轮次级断言、错误韧性、语义质量。自带零依赖参考实现，clone 即跑：

```bash
cd agent_eval/examples/universal_cases
python3 run_eval.py -v              # 11 条全量，逐条看结果（实测 11/11，100%，门禁放行）
python3 run_eval.py --dataset smoke # 3 条冒烟
python3 run_eval.py --demo-undetermined   # 看 undetermined 如何拦住「没测过的绿灯」
python3 -m pytest test_universal_cases.py -q   # 案例库自检 7 项
```

断言天生和业务绑定，所以「通用」靠三个约定做出来，而非魔法：

| 约定 | 内容 | 换来什么 |
|---|---|---|
| **标准动作** | `lookup` 读 / `compute` 算 / `record` 写 / `destroy` 删 / `deliver` 外发 | 只问「该不该做这类事」，任何业务工具都能归到这 5 类 |
| **回显令牌** | 通用 query 要求原样回显 `ECHO-42` | 「回答必须含 X」与业务解耦；过不了就是指令遵循有缺口 |
| **状态路径** | `state["audit_log"]` 记副作用、`state["memory"]` 存会话记忆 | 写操作与多轮记忆可观测，不绑定具体存储结构 |

接自己的系统有两条路，选一条：**改 adapter**（把真实工具名归一成标准动作，改一处）或
**改配置**（`render.py` + `profile.json` 把案例里的标准名/夹具值换成你的真实值）。
细节与「别当银弹」的边界说明见
[universal_cases/README.md](agent_eval/examples/universal_cases/README.md)。

## 数据飞轮最小闭环：Bad Case → 回归资产

一条线上 Bad Case 不该是「修完就忘」。框架提供离线可跑的最小闭环
（不需要探针、不需要平台）：

```bash
# 1) 只跑回归集（评估集里用 dataset 标签分层）
python3 run_eval.py --dataset regression --env staging \
    --report-json report.json --export-failures failures.json

# 2) 打开 failures.json，人工确认 suggested_case 里的判分标准草稿
#    （实际输出只放在 actual 里当证据，绝不会变成新用例的标准答案）

# 3) 回灌：默认另存 <name>.promoted.json，不覆盖原评估集；id 冲突自动避让
python3 run_eval.py --promote-failures failures.json

# 4) 修复后重跑，与上次报告逐题对比（回答变了什么、断言变了什么、
#    延迟/Token 涨了多少），并给出四档回放判定
python3 run_eval.py --fixed --dataset regression --env staging --compare report.json
```

**三层护栏**：
- **运行指纹**：每次跑批生成 `run_id` + `env` + `dataset` + `config_sig`（版本四要素哈希）。`compare_reports` 发现环境不一致时**直接判作废**——生产与预发的报告混在一起比是最常见的假结论来源
- **逐题 diff**：不只看成功率，还列同一用例的 `final_output` / 断言状态 / 工具调用 / 耗时 / Token 变化。用例增删、定义指纹改变或仅一边有指纹时，不给出「提升可合并」判定；建议用新版重新生成基线。
- **回灌安全**：另存不覆盖、整集 `load_evalset` 校验、临时文件写盘、重复 id 自动加后缀

**渐进接入红线**：证据接地断言（`tool_result_used` / `output_grounded_in`）在
adapter 没上报工具返回值时一律 `skipped`（审计可见，不参与判定），
绝不把「不可观测」误判成「Agent 做错了」。老 adapter 不会因为升级框架就全红。

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

想让判分更严，把工具返回值也带上（`ToolCallRecord(name, args, result=...)`），
再补 `latency_ms` / `token_usage`。这样证据接地断言、judge 证据核验、
版本性能对比才有数据可用；不带也能跑，相关能力自动跳过。

`ToolCallRecord` 三个可选字段：

| 字段 | 用途 |
|---|---|
| `result` | 工具真实返回值。证据接地断言与 judge 的证据来源；不填则这两类断言标 skipped |
| `ok` | 该次调用是否成功（默认 True） |
| `latency_ms` / `token_usage`（在 `AgentRun` 上） | 端到端耗时与 Token 用量，供 `--compare` 出性能 delta |

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

# 1) 金标校准：200+ 条人工标注，一致率 ≥85% 且 Cohen's κ ≥0.8 才放行
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

**judge 拿到什么**：`【判分标准】+【用户问题】+【Agent 回答】+【执行证据】`。
执行证据 = 全程工具调用（含返回值）/ 召回片段 / 结束状态，由
`AgentRun.trace_context()` 自动打包。system prompt 里明确写了
「工具调用成功不代表任务完成」「回答中的数字、型号、结论若在证据里找不到依据，判 fail」——
这是为了抓住「工具都成功了、回答却说没数据」这类只在过程里才暴露的问题。
判定缓存的 key 也纳入了轨迹，同一句回答背后证据不同会重新判，不会串判定。

## 证据接地断言：工具调用成功 ≠ 任务完成

`must_call` 只能证明「调过工具」，证明不了「结果被用了」。两类断言补这个洞：

```json
{
  "id": "CS-009",
  "query": "帮我把 Pixel 8 的价格查询结果汇总一下",
  "severity": "blocker",
  "dataset": "regression",
  "must_call": [{"tool": "query_price"}],
  "tool_result_used": [{"tool": "query_price", "min_values": 1}],
  "output_grounded_in": {"min_values": 2,
                         "sources": ["tool_results", "retrieved"]},
  "judge_criteria": ["回答必须给出工具返回的具体价格数字，不能只说「已完成查询」"]
}
```

| 断言 | 判什么 | 典型场景 |
|---|---|---|
| `tool_result_used` | 指定工具的返回值里至少 N 个值出现在回答中 | 子任务结果没回传、文档没重新读取就凭旧上下文作答 |
| `output_grounded_in` | 回答里至少命中 N 个来自执行证据的值 | 整段结论缺乏证据支撑 |

值匹配会做归一化（`5,999` ↔ `5999`、全角 ↔ 半角、大小写、空白）。
可用 `values` 显式指定期望值，不填则从工具返回值/召回片段自动抽取；
显式 `values` 会按归一化结果去重，重复值不能凑满 `min_values`。
**adapter 没上报 `ToolCallRecord.result` 时标 `skipped`**，不硬判。

`dataset` 标签（默认 `regression`）用于分层跑批：`smoke` 冒烟集、
`regression` 回归集、`baseline` 基准集，也允许自定义值。

### 门禁可靠性：跳过 ≠ 通过

用例有三态：`passed` / `failed` / `undetermined`。当一条用例的所有断言都被
跳过（judge 未配置或未校准、返回 `need_review`、adapter 未上报工具返回值），
它**不计入通过率的分子与分母**，报告单列 `undetermined_total` /
`determined_total` / `undetermined_cases`。其中 **blocker 未有效测试时门禁
直接拒绝放行**——绿灯里不允许混着没真正测过的用例。其余未定用例只标注、
不阻断。空报告（0 条用例）与全部未定的报告同样拒绝放行。

> **升级提示**：从旧版本升级后，若评估集中存在「只写 `judge_criteria`、
> 没有任何 P1 断言」的 blocker 用例，在 judge 未上岗时会由「放行」变为
> 「未定 → 拦截」。这是预期的语义修正，请为这类用例补至少一条 P1 断言。

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
> 先跑 `calibrate_judge`（≥200 条人工金标，一致率 ≥85% 且 Cohen's κ ≥0.8），
> 否则语义断言一律跳过，绝不裸判。`/api/run` 请求体支持 `force_uncalibrated`
> （仅 API 调用可用，Web 控制台前端不暴露该控件，避免误点）。

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
EVAL_ENV=staging            # 可选：运行指纹里的环境标识；不同环境报告不可比
```

**key 只放环境变量，不进代码、不进仓库**（`.gitignore` 已排除 `model_env.sh`）。
