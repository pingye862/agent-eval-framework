"""胶水层（你要写的唯一代码）：把你的项目适配成 AgentAdapter。

真实项目里，这个文件通常只做三件事：
1. 拼装 context（用户身份、会话状态、notify_required 等业务前置）
2. 调你的 Agent
3. 把你项目里的 trace / 日志 / 数据库状态翻译成 AgentRun

你的 Agent 什么都不用改——适配发生在测试侧，不侵入业务代码。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent_eval_lib import AgentAdapter, AgentRun   # noqa: E402
import agent                                        # noqa: E402


class ShoppingAgentAdapter(AgentAdapter):
    """数码导购 Agent 的适配器。"""

    def run(self, query: str, context: dict) -> AgentRun:
        # 真实项目里这里可能是：requests.post(your_service, ...) 或
        # your_graph.invoke(...)，然后把响应和 trace 拼成 AgentRun
        return agent.run_agent(query, context)
