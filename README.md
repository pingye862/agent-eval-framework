# Agent 质量评测框架（agent-eval）

面向 AI Agent 的批量质量评测工具集：P1 断言跑批、链路第一错归因、四档门禁判定。零第三方依赖（纯 Python 标准库），适配器模式接入任意 Agent（HTTP 服务 / 进程内代码 / Node 桥接）。

## 它做什么

1. **批量执行**：评估集（JSON 用例）逐条驱动被测 Agent，每次调用采集完整轨迹（`AgentRun`：最终输出、工具调用、副作用状态、版本信息）
2. **P1 断言**：必调/禁调工具（含参数存在性）、输出关键词、状态断言、异常检测，失败带中文明细
3. **第一错归因**：失败用例自动定位链路第一错，归因到 system / trajectory / component 层
4. **门禁判定**：blocker 用例一票否决（不参与百分比稀释）+ 成功率回落基线超阈值拦截
5. **Web 控制台**：评估集增删改查、自定义项目注册、跑批与报告展示（零依赖，纯标准库 HTTP server）

## 目录结构

```
agent_eval/               评测框架（9 模块 + 单测 + demo 示例项目）
webui/                    评测控制台（python3 webui/server.py 启动，默认 8900 端口）
```

## 快速开始

```bash
# 1. 跑 demo 示例（内置可控缺陷的被测 Agent，无需任何外部服务）
cd agent_eval/examples/demo_project
python3 run_eval.py

# 2. 启动 Web 控制台
cd webui && python3 server.py   # http://127.0.0.1:8900
```

单测：`python3 -m pytest agent_eval/tests/ -q`

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

## 运行环境已知坑（WorkBuddy 沙箱内跑服务）

- 启动被测服务前 `unset PYTHONPATH`（WorkBuddy 注入的 sitecustomize 会劫持 `mkdir`，导致 `mkdir(exist_ok=True)` 在目录已存在时仍抛异常）
- 本机 curl 加 `--noproxy '*'`；urllib 用 `ProxyHandler({})` 直连 opener
- 启动脚本自愈写法：启动前先 `unset PYTHONPATH PYTHONSTARTUP`

## 环境变量

被测 Agent 需要模型 API key 时，统一走环境变量：

```
FDE_MODEL_URL=https://api.deepseek.com/v1/chat/completions
FDE_MODEL_NAME=deepseek-chat
FDE_MODEL_KEY=<你的 key>
```

**key 只放环境变量，不进代码、不进仓库**（`.gitignore` 已排除 `model_env.sh`）。
