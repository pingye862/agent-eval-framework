# 通用案例库（Universal Case Library）

一套**与具体业务无关**的 Agent 评测用例，覆盖「任何 Agent 都该具备」的 10 个能力维度。
配套一个零依赖的参考实现（`reference_agent.py`），clone 下来即可跑出全绿，用来确认
框架装对了、案例库没漂移；接自己的系统时，只需一层薄适配（改一个类或一份配置）。

```
python3 run_eval.py                 # 11 条全量
python3 run_eval.py -v              # 逐条看结果
python3 run_eval.py --dataset smoke # 3 条冒烟
python3 -m pytest test_universal_cases.py -q   # 自检 7 项
```

> 当前实测：**11/11 通过，100.0%，门禁放行**（L0，零 API Key、零网络、零随机）。

---

## 为什么这套案例能「通用」

评测断言天生和业务绑定：`must_call` 要具体工具名、`response_contains` 要具体关键词。
所以**不存在**「一份 evalset.json 原样丢给任何系统都出有意义结论」这种事。这套库用三个
约定把通用性做出来，代价是被测系统要遵守它们：

| 约定 | 内容 | 换来什么 |
|---|---|---|
| **标准动作** | `lookup` 读 / `compute` 算 / `record` 写 / `destroy` 删 / `deliver` 外发 | 断言只问「该不该做这类事」，任何业务工具都能归到这 5 类 |
| **回显令牌** | 通用 query 要求原样回显 `ECHO-42` | 「回答必须含 X」与业务无关；过不了说明指令遵循有缺口 |
| **状态路径** | `state["audit_log"]` 记副作用、`state["memory"]` 存会话记忆 | 写操作与多轮记忆可被观测，不依赖具体存储结构 |

`reference_agent.py` 就是这三个约定的示范实现，也是你接自己系统时照抄的模板。

---

## 目录

| 文件 | 作用 |
|---|---|
| `evalset.json` | 11 条通用用例，按 `dataset` 分 smoke(3) / regression(7) / baseline(1) |
| `reference_agent.py` | 零依赖参考 Agent + Adapter，实现上述三个约定（**换系统就改这里**） |
| `run_eval.py` | 一键跑批 CLI（支持 `--dataset` / `--level` / `--report-json` / `--compare` / `--export-failures`） |
| `render.py` + `profile.example.json` | 可选：不想改 adapter 时，把标准名/期望值换成你的真实值，生成 `evalset.mine.json` |
| `evalset.undetermined-demo.json` | 反面教材：只有语义标准、没有 P1 断言 → 演示 undetermined 拦截 |
| `test_universal_cases.py` | 自检：schema 合法、参考实现全绿、undetermined 能拦门禁、render 可用 |

---

## 11 条用例覆盖什么

| ID | 维度 | 测什么 | 判定依据 | 分层 |
|---|---|---|---|---|
| UC-001 | `liveness` | 能听懂指令、给非空回答 | `response_contains` | smoke |
| UC-002 | `instruction_following` | 一句话两个约束，是否漏掉后半句 | `response_contains` ×2 | smoke |
| UC-003 | `tool_selection` | 读操作走对工具，且单次调用带齐参数 | `must_call` + `required_args` + `must_not_call` | smoke |
| UC-004 | `result_usage` | 工具调用成功 ≠ 任务完成，结果要进回答 | `must_call` + `tool_result_used` + `response_contains` | regression |
| UC-005 | `forbidden_ops` | 明确只读时，删除/外发一次都不许发生 | `must_not_call` ×2 | regression |
| UC-006 | `side_effect` | 写操作要真的落到状态里，不只回一句「已完成」 | `must_call` + `state_after` | regression |
| UC-007 | `grounding` | 结论必须落在执行证据上，不能凭印象编 | `must_call` + `output_grounded_in` | regression |
| UC-008 | `multi_turn_memory` | 第 2 轮追前一轮的信息，会话上下文是否真的保留 | `turns[].response_contains` | regression |
| UC-009 | `turn_scoped_assertions` | 第 1 轮只读、第 2 轮才算，断言按轮生效 | `turns[].must_call` / `must_not_call` | regression |
| UC-010 | `error_resilience` | 畸形输入不崩溃，有可理解的兜底回执 | `no_error` + `response_contains` | regression |
| UC-011 | `semantic_quality` | 语义标准（P2）必须配一条 P1 断言 | `response_contains` + `judge_criteria` | baseline |

**每条都至少有一条 P1 有效断言**——这是刻意的：只有 `judge_criteria` 的用例在 judge 未
上岗时会判 `undetermined`（见下节），不能算通过。

---

## 接自己的系统：两条路，选一条

### 路 A（推荐）：改 adapter，把工具名归一成标准动作

```python
# 复制 reference_agent.py 的 ReferenceAdapter，改 run()
class MyAdapter(AgentAdapter):
    def run(self, query, context):
        out = my_agent.invoke(query, session=context.get("session_id"))
        calls = [ToolCallRecord(_norm(t.name), t.args, result=t.result)
                 for t in out.trace]          # 真实工具名 → lookup/compute/record/...
        return AgentRun(query=query, final_output=out.text,
                        tool_calls=calls, state=out.state, versions=my_versions())
```

`_norm()` 就是一张 5 行的映射表：`search_kb→lookup`、`run_calc→compute`、
`append_audit→record`、`delete_api→destroy`、`send_notice→deliver`。

### 路 B：不改代码，用 render 改案例

```bash
cp profile.example.json profile.mine.json   # 填真实工具名/期望值
python3 render.py --profile profile.mine.json --out evalset.mine.json
python3 run_eval.py --evalset evalset.mine.json
```

`render.py` 只改四类位置：`tool`、`state_after[].path`、`required_args`、字符串里的
夹具值（`ECHO-42` / `ALPHA-7` / `OMEGA-9` / `84`），改完用框架自己的 `load_evalset()`
校验，字段类型错一处就报错。

> 两条路**不要同时用**，会双重改名。

---

## undetermined：这套库最容易踩的坑

```bash
python3 run_eval.py --demo-undetermined
```

```
级别: L0 ｜ 用例: 1 ｜ 通过: 0 ｜ 成功率: 0.0%（有效判定 0 条）
⚠ 未定 1 条（未有效测试，不参与通过率）: ['UC-UND-001']
门禁判定: ⛔ 拦截合并
  - 全部用例未有效测试（undetermined），拒绝放行
```

那条用例只写了 `judge_criteria`、没写任何 P1 断言。L0 下 judge 不上岗 → 语义断言被跳过
→ 这条用例其实**没被测过**。框架不会把它算进成功率分母，blocker 未测则直接拦门禁。

**修法**：给它补一条 P1 断言。UC-011 就是正确示范——同样的语义标准，外加
`response_contains: ["ECHO-42"]`，于是 L0 也能判、L1 再叠加语义。

---

## 边界与取舍（别当成银弹）

- **它测的是「通用能力」，不是你的业务正确性**。业务回归集应该在这套之上叠加，而不是替代。
- **通用性靠约定，不靠魔法**。被测系统必须能遵循回显指令、愿意上报工具调用与返回值；
  不能（比如纯检索式 chatbot），挂哪条就是哪项能力不达标的结论，不是框架的错。
- **L0 全部是字面断言**，可能被「恰好包含关键词」蒙过。要判语义质量得走 `--level L1`，
  且必须先用金标集校准 judge（一致率 ≥85% 且 Cohen's κ ≥0.8）。
- **夹具值是固定的**（`ALPHA-7` / `84` / `OMEGA-9`），换系统时要按路 A 或路 B 对齐，
  不会自动适配。
- 与 `examples/demo_project/` 的分工：demo 演示「门禁从红变绿 + 归因」，本库演示
  「换任何系统都能复用同一套能力契约」。

---

## 拷到框架外面跑

```bash
cp -R agent_eval/examples/universal_cases ~/my-eval-cases
AGENT_EVAL_SRC=/path/to/agent-eval/agent_eval python3 ~/my-eval-cases/run_eval.py
```

`AGENT_EVAL_SRC` 指向框架的 `agent_eval/` 目录即可，不需要把案例库塞进框架仓库。
