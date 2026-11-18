# 小白上手指南：从零跑通 AI Agent 的自动化测试

> 这份文档假设你**没写过测试**、也没读过项目 README。
> 跟着做，10 分钟内你会看到自己的第一份 Agent 测试报告。
> 每条命令都可以直接复制粘贴，每个输出都有「这一行是什么意思」的解释。

---

## 1. 这个框架是干嘛的？（30 秒版）

你做了一个 AI Agent（比如一个能查价格、发通知的客服机器人），
每次改完代码你都心里发虚：**它会不会悄悄变傻了？**

这个框架就是替你「考试」的：

| 生活里的比喻 | 框架里的说法 | 作用 |
|---|---|---|
| 一张考卷 | 评估集（evalset.json） | 一堆问题 + 标准答案要点 |
| 阅卷规则 | 断言（assertion） | 「必须调用查价工具」「回答里必须有 5999」 |
| 录取分数线 | 门禁（gate） | 不达标就**禁止合并代码** |
| 老师批改 | 跑批（run） | 让你的 Agent 真的答一遍卷子，逐题判分 |

**核心卖点：你的 Agent 一行代码都不用改**，所有接入都发生在测试侧。

---

## 2. 上手前，只需确认两件事

```bash
python3 --version    # 3.9 及以上即可，3.10+ 更佳
git --version        # 能 clone 仓库就行
```

没有 `pip install`、没有依赖地狱——整个框架**只用 Python 标准库**。
（唯一例外：把门禁挂进 CI 时需要 `pip install pytest`，第 5 节第 5 步会讲。）

---

## 3. 五分钟跑通第一个 Demo

### 3.1 拿到代码

```bash
git clone https://github.com/pingye862/agent-eval-framework.git
cd agent-eval-framework
```

### 3.2 跑一个「故意有 bug 的机器人」

仓库自带一个数码导购 Agent，内置 3 个可控缺陷（调错工具、漏发通知、编造参数）。
先进它的目录，跑一次：

```bash
cd agent_eval/examples/demo_project
python3 run_eval.py
```

你会看到类似输出（数字以实际为准）：

```
级别: L0 ｜ 用例: 8 ｜ 通过: 3 ｜ 成功率: 37.5%
✗ CS-001 [blocker] iPhone 15 现在多少钱
    [must_call] 缺少工具调用: query_price
    第一错: 1_must_call (must_call) → 主归因: system
...
门禁判定: ⛔ 拦截合并
```

**逐行解读：**

| 你看到的 | 意思 |
|---|---|
| `用例: 8 ｜ 通过: 3` | 考卷 8 道题，只答对 3 道 |
| `✗ CS-001 [blocker]` | 第 1 题挂了，且它是 blocker 级（一票否决的核心题） |
| `[must_call] 缺少工具调用: query_price` | 挂的原因：查价格却没调查价工具 |
| `第一错: 1_must_call` | 链路上**第一个**出错的位置（定位用） |
| `主归因: system` | 该怪谁：system/trajectory/component 三层里，优先归到上游 |
| `门禁判定: ⛔ 拦截合并` | 这版代码不许上线 |

### 3.3 把 bug「修好」，看门禁变绿

```bash
python3 run_eval.py --fixed
```

```
级别: L0 ｜ 用例: 8 ｜ 通过: 8 ｜ 成功率: 100.0%
门禁判定: ✅ 放行合并
```

**红 → 绿，这就是这个框架的全部工作方式。**
以后你的真实项目里：改完代码跑一次，红了就不合并，就这么简单。

### 3.4 CI 模式（返回码红 = 构建失败）

```bash
python3 run_eval.py --gate --baseline 0.95 ; echo "退出码: $?"
# 门禁不达标时退出码是 1（CI 会判红），达标是 0
```

`--baseline 0.95` 的意思：历史成功率基线是 95%，这次比基线掉超过 2 个百分点就拦截。
（防「这次 90%、上次 96%，平均看着还行」的温水煮青蛙式劣化。）

---

## 4. 不想敲命令？用 Web 界面

```bash
cd webui
python3 server.py
# 浏览器打开 http://127.0.0.1:8900
```

界面上能做的事：

- **跑批**：选评估集 → 勾不勾「修复缺陷后跑」→ 点跑，报告直接展示
- **看逐条明细**：哪条用例挂了、挂在哪个断言、Agent 实际回答了什么
- **增删改用例**：表单编辑，保存直接写回 JSON（多轮对话、语义判分标准都能配）
- **加你自己的项目**：点「➕ 自定义项目」，填你的 Agent 接口地址就能测
- **生成覆盖审计报告**：点一下产出 `coverage.html`，看哪些业务场景还没被测到

> 提示：`L0 / L1 / L2` 是评测层级。**L0 = 只跑确定性断言**（关键词、工具调用），
> L1/L2 会额外启用「LLM 语义判分」（第 7 节）。新手先用 L0 完全够。

---

## 5. 接你自己的 Agent（手把手，约 20 行代码）

这是本文档最重要的一节。整体结构长这样：

```
你的 Agent（不用改） ──> adapter.py（你写，~20 行） ──> 框架跑批/门禁
```

### 第 1 步：建目录，抄文件

```bash
cd agent-eval-framework
mkdir my_project
cp agent_eval/examples/demo_project/run_eval.py my_project/
```

抄来的 `run_eval.py` 有两处写死了 demo 的信息，**必须改**（就这两行）：

```python
# ① 框架路径：demo 在 agent_eval/ 下两层，你的项目在仓库根目录，只隔一层
- sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
+ sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent_eval"))

# ② 类名：换成你自己的 adapter 类（下一节定义）
- adapter_cls = __import__("adapter").ShoppingAgentAdapter
+ adapter_cls = __import__("adapter").MyAgentAdapter
```

### 第 2 步：写 adapter.py（唯一要动脑的文件）

新建 `my_project/adapter.py`，把「你的 Agent 怎么调用、返回什么」翻译给框架。
两种常见形态：

**形态 A：你的 Agent 是个 HTTP 服务**

```python
import json, sys, os, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent_eval"))
from agent_eval_lib import AgentAdapter, AgentRun, ToolCallRecord

class MyAgentAdapter(AgentAdapter):
    def run(self, query: str, context: dict) -> AgentRun:
        body = json.dumps({"query": query, "session": context.get("session_id", "")}).encode()
        req = urllib.request.Request(
            "http://localhost:8000/agent", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return AgentRun(
            query=query,
            final_output=data["answer"],                    # 最终回答
            tool_calls=[ToolCallRecord(name=t["name"], args=t.get("args", {}))
                        for t in data.get("trace", {}).get("tool_calls", [])],
            state=data.get("state", {}),                    # 副作用后的状态（如有）
            versions=data.get("versions", {}),              # 模型/prompt 版本（如有）
        )

    def health(self) -> bool:
        return True     # Web 控制台探活用，有它就不报「服务不在线」
```

**形态 B：你的 Agent 是 Python 代码（进程内直接 import）**

下面这份**可以整文件照抄**（把 `my_bot.invoke` 换成你的入口即可）：

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent_eval"))
from agent_eval_lib import AgentAdapter, AgentRun, ToolCallRecord
from my_bot import invoke          # ← 你的 Agent 入口

class MyAgentAdapter(AgentAdapter):
    def run(self, query: str, context: dict) -> AgentRun:
        result = invoke(query, context)
        return AgentRun(
            query=query,
            final_output=result["answer"],
            tool_calls=[ToolCallRecord(t["name"], t.get("args", {}))
                        for t in result["tools"]],
        )

    def health(self) -> bool:
        return True
```

### 第 3 步：写考卷 evalset.json

新建 `my_project/evalset.json`，最小可用版只要 3 个必填字段：

```json
[
  {
    "id": "T-001",
    "query": "帮我查下北京明天的天气",
    "severity": "blocker",
    "must_call": [{"tool": "get_weather"}],
    "response_contains": ["晴"]
  }
]
```

**用例字段字典**（按需加，全部可选除前三项）：

| 字段 | 类型 | 作用 | 例子 |
|---|---|---|---|
| `id` | 字符串，必填 | 用例编号，报告里靠它定位 | `"T-001"` |
| `query` | 字符串，必填 | 用户的原话 | `"查下明天天气"` |
| `severity` | 三选一，必填 | 严重级别：`blocker` 挂一条就拦截；`major` 计入成功率；`minor` 提示 | `"blocker"` |
| `must_call` | 数组 | 必须调用的工具（可带参数存在性检查） | `[{"tool": "get_weather", "required_args": ["city"]}]` |
| `must_not_call` | 数组 | 禁止调用的工具 | `[{"tool": "delete_record"}]` |
| `response_contains` | 数组 | 回答里必须出现的关键词 | `["晴", "25 度"]` |
| `state_after` | 数组 | 执行完后的状态检查（副作用） | `[{"path": "notify_log", "min_length": 1}]` |
| `context_overrides` | 对象 | 这条用例的前置条件（如 VIP 身份） | `{"notify_required": true}` |
| `persona` | 字符串 | 用户画像备注，给人看的 | `"VIP 用户"` |
| `judge_criteria` | 数组 | 语义判分标准（自然语言），见第 7 节 | `["不得编造数据"]` |
| `turns` | 数组 | 多轮对话的后续轮次，见第 7 节 | `[{"query": "那后天呢"}]` |

> **出题心法**：`blocker` 只留给「错了就是事故」的场景（转账金额错、删库），
> 别全标 blocker——满卷都是重点等于没有重点。参考 demo 的 8 条：3 blocker / 4 major / 1 minor。

### 第 4 步：跑起来

```bash
cd my_project
python3 run_eval.py --evalset evalset.json --gate
```

> 本节流程已实测可跑通：新建目录 → 抄 run_eval.py 改那两行 → 写 adapter 和
> evalset.json → 跑批出报告，全程不需要动框架一行代码。

### 第 5 步（可选）：挂进 GitHub Actions

仓库根目录 `.github/workflows/gate.yml`：

```yaml
on: [pull_request]
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.11"}
      - run: pip install pytest
      - run: |
          cd my_project
          pytest -q gate_test.py    # 或直接 python3 run_eval.py --gate
```

从此每次提 PR，机器人自动替你考一遍，红一条都不许合并。

---

## 6. 怎么知道该测什么？（出题四象限）

对着你的 Agent 业务，按这张表填空，每格至少 1 条用例：

| | 用户问得正常 | 用户问得刁钻 |
|---|---|---|
| **Agent 会** | 主流程：查价、查询、下单 | 边界：没听清机型、两个需求混一句 |
| **Agent 不会** | 兜底：转人工而不是瞎编 | 红线：没权限的数据绝不能吐 |

demo 的 CS-006 就是「不会 + 刁钻」格：问电池容量（知识库里没有），
断言回答必须包含「人工」——**防幻觉是 Agent 测试的重灾区，优先覆盖**。

---

## 7. 进阶功能（用到再看）

### 7.1 多轮对话测试

模拟「用户追问」的场景：

```json
{
  "id": "T-100",
  "query": "查下 iPhone 15 的价格",
  "severity": "major",
  "turns": [
    {"query": "那 Pro 版呢？", "response_contains": ["8999"]}
  ]
}
```

第 2 轮的断言只看第 2 轮的回答——考验 Agent 记不记得上下文。

### 7.2 LLM 语义判分（P2）

关键词断言挡不住「答非所问但碰巧含关键词」。给用例写 `judge_criteria`，
让一个 LLM 按你的标准判分：

```json
"judge_criteria": ["回答必须说明无法确认机型，并引导补充信息，不许编造"]
```

启用方式：

```bash
export EVAL_MODEL_URL=https://api.deepseek.com/v1/chat/completions
export EVAL_MODEL_NAME=deepseek-chat
export EVAL_MODEL_KEY=你的key
python3 run_eval.py --level L1
```

**两条红线**（框架强制）：

1. **judge 必须先过金标校准才上岗**：拿 200+ 条人工标好 pass/fail 的样本跑
   `calibrate_judge()`，一致率 ≥85% 且 Cohen's κ ≥0.8 才放行。
   （κ 修正「碰巧一致」：全判 pass 的懒 judge 一致率虚高，κ≈0 会被拦下。）
2. **judge 拿不准就输出「需复核」，不硬判**——判分器自己不能成为事故源。

本地想先看效果可以加 `--force-uncalibrated`（跳过校准，**仅限本地演示**）。

### 7.3 复跑防「抽风」（flaky 检测）

在你的 `run_eval.py` 里把跑批那行改成复跑 3 次：

```python
report = BatchRunner(adapter_cls(), judge=judge).run_all(
    cases, level=args.level, context=context, repeat=3)
```

同一条用例跑 3 次，全过才算过；时好时坏的会单独标记为 flaky
（LLM 应用的经典病，宁可错杀不可放过）。

### 7.4 覆盖审计报告

```bash
python3 run_eval.py --coverage     # 产出 coverage.html，浏览器打开
```

按业务线/层级展示：哪些场景有用例盖住、哪些没有、没盖的原因（待补数据/
范围排除/风险豁免…6 类）、高风险裸奔场景标红线——**给老板看的一页纸**。

---

## 8. 常见报错急救表

| 报错/现象 | 原因 | 解法 |
|---|---|---|
| `ModuleNotFoundError: agent_eval_lib` | 不在项目目录里跑 | `cd` 进你的项目目录再跑；项目需与 `agent_eval/` 保持 demo 的相对位置 |
| `连接被拒绝 / 超时` | 你的 Agent 服务没起 | 先 `curl` 你的接口确认活着，再跑评测 |
| `P2 语义判分跳过` 提示 | 没配 `EVAL_MODEL_*` 或 judge 未校准 | L0 不需要管它；要用 P2 就配环境变量并跑校准 |
| `转人工 / need_review` | judge 拿不准 | 正常设计：人工看一眼这批样本，别硬判 |
| 端口 8900 被占用 | webui 已在跑 | `pkill -f webui/server.py` 后重启，或改 `server.py` 末尾端口 |
| 成功率 100% 但线上还是翻车 | 考卷太水 | 回第 6 节：刁钻格和红线格补题；给关键题加 `judge_criteria` |
| `severity 非法` | 拼错 | 只能是 `blocker` / `major` / `minor` 三个词 |

---

## 9. 命令速查

```bash
# demo
cd agent_eval/examples/demo_project
python3 run_eval.py                    # 带缺陷跑（红）
python3 run_eval.py --fixed            # 修好后跑（绿）
python3 run_eval.py --gate --baseline 0.95   # CI 模式，不达标 exit 1
GATE_FIXED=1 python3 -m pytest test_gate.py -v   # pytest 形态的同一门禁

# web 控制台
cd webui && python3 server.py          # http://127.0.0.1:8900

# 框架自身单测（118 条）
python3 -m pytest agent_eval/tests/ -q

# 你的项目
cd my_project
python3 run_eval.py --evalset evalset.json --gate
python3 run_eval.py --level L1         # 加语义判分（需 EVAL_MODEL_*）
python3 run_eval.py --coverage         # 加覆盖审计报告
```

---

## 卡住了？

- 看主 [README](../README.md)（完整能力清单）
- 看 [agent_eval/README.md](../agent_eval/README.md)（模块设计与接入协议）
- 直接读 demo：`agent_eval/examples/demo_project/` 里的 4 个文件就是完整答案

祝你的 Agent 永远绿。🌿
