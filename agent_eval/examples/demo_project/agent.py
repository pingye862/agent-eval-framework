"""被测项目示例：一个最小的数码导购 Agent。

这就是「你的项目」的替身。真实项目里，你的 Agent 可能在 Web 服务里、
在 LangGraph 里、在别的地方——你只需要在 adapter.py 里把它包一层。

三个缺陷开关，用来演示「门禁从红变绿」：
- wrong_tool: 价格查询路由错到参数工具（工具选择错）
- skip_notify: 该发通知时没发（副作用缺失）
- hallucinate: 不知道时编造参数值

并发安全：缺陷开关与副作用日志都**优先从 context 读取**（context["bugs"] /
context["notify_log"]），模块级 BUGS / NOTIFY_LOG 只是未注入时的默认值。
跑批方（webui / CLI）按次注入，多线程并发跑批互不踩踏——不再依赖
「改全局变量再跑」这种天然有竞态的开关方式。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent_eval_lib import ToolCallRecord, AgentRun   # noqa: E402

# ---- 缺陷开关：默认全开（演示门禁拦住），修好 = 全关 ----
BUGS = {
    "wrong_tool": True,
    "skip_notify": True,
    "hallucinate": True,
}

# ---- mock 的知识库与工具 ----
PRODUCT_DB = {
    "iphone 15": {"screen": "6.1 英寸", "chip": "A16"},
    "iphone 15 pro": {"screen": "6.1 英寸", "chip": "A17 Pro"},
    "pixel 8": {"screen": "6.2 英寸", "chip": "Tensor G3"},
}
PRICE_DB = {
    "iphone 15": 5999, "iphone 15 pro": 8999, "pixel 8": 4599,
}
NOTIFY_LOG: list[dict] = []   # mock 副作用（默认落点；注入 context["notify_log"] 时用注入的）


def tool_query_product(model: str, field: str) -> str:
    info = PRODUCT_DB.get(model.lower())
    if info and field in info:
        return info[field]
    return "UNKNOWN"


def tool_query_price(model: str) -> str:
    p = PRICE_DB.get(model.lower())
    return str(p) if p else "UNKNOWN"


def tool_notify(message: str, log: list | None = None) -> str:
    (log if log is not None else NOTIFY_LOG).append({"message": message})
    return "sent"


# ---- Agent 主流程 ----

def run_agent(query: str, context: dict) -> AgentRun:
    ctx = context or {}
    bugs = ctx.get("bugs")
    if not isinstance(bugs, dict):          # 未注入：用模块默认（CLI 单进程跑）
        bugs = BUGS
    log = ctx.get("notify_log")
    if not isinstance(log, list):           # 未注入：落全局日志
        log = NOTIFY_LOG

    calls: list[ToolCallRecord] = []
    state_snapshot = lambda: {"notify_log": list(log)}

    q = query.lower()
    # 机型识别：最长匹配优先，避免 "iphone 15" 抢走 "iphone 15 pro"
    models = sorted(set(PRODUCT_DB) | set(PRICE_DB), key=len, reverse=True)
    model = next((m for m in models if m in q), None)

    # 路由：先判参数类，再判价格类（"尺寸是多少"含"多少"，不能用宽泛词）
    if "尺寸" in q or "屏幕" in q or "芯片" in q:
        if model is None:
            return AgentRun(query=query, final_output="抱歉，我没识别出机型",
                            tool_calls=calls, state=state_snapshot(), versions=VERSIONS)
        calls.append(ToolCallRecord("query_product", {"model": model, "field": "screen"}))
        output = f"{model} 屏幕尺寸 {tool_query_product(model, 'screen')}。"
        return AgentRun(query=query, final_output=output,
                        tool_calls=calls, state=state_snapshot(), versions=VERSIONS)

    if "多少钱" in q or "价格" in q or "售价" in q:
        if model is None:
            return AgentRun(query=query, final_output="抱歉，我没识别出机型",
                            tool_calls=calls, state=state_snapshot(), versions=VERSIONS)
        if bugs["wrong_tool"]:
            # 缺陷：价格问题调成了参数工具
            calls.append(ToolCallRecord("query_product",
                                        {"model": model, "field": "screen"}))
            output = f"{model} 的屏幕是 {tool_query_product(model, 'screen')}，售价请另询。"
        else:
            calls.append(ToolCallRecord("query_price", {"model": model}))
            output = f"{model} 当前售价 {tool_query_price(model)} 元。"

        # 价格场景要发通知（业务规则）。注意：必须真的调用工具，
        # 只记 calls 不调工具，副作用断言（state_after）会抓住它
        if ctx.get("notify_required") and not bugs["skip_notify"]:
            calls.append(ToolCallRecord("notify", {"message": f"price_query:{model}"}))
            tool_notify(f"price_query:{model}", log)

        return AgentRun(query=query, final_output=output,
                        tool_calls=calls, state=state_snapshot(), versions=VERSIONS)

    # 未知问题
    if bugs["hallucinate"]:
        return AgentRun(query=query,
                        final_output="这款的电池是 4252mAh，续航 22 小时。",
                        tool_calls=calls, state=state_snapshot(), versions=VERSIONS)
    return AgentRun(query=query,
                    final_output="这个问题我需要确认后回复您，已转人工跟进。",
                    tool_calls=calls, state=state_snapshot(), versions=VERSIONS)


VERSIONS = {
    "model": "mock-gpt-2026-10",
    "prompt": "p-v1.0",
    "tool_desc": "td-v1.0",
    "kb": "kb-20261031",
}
