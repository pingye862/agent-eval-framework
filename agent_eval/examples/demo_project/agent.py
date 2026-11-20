"""被测项目示例：一个最小的数码导购 Agent。

这就是「你的项目」的替身。真实项目里，你的 Agent 可能在 Web 服务里、
在 LangGraph 里、在别的地方——你只需要在 adapter.py 里把它包一层。

四个缺陷开关，用来演示「门禁从红变绿」：
- wrong_tool:    价格查询路由错到参数工具（工具选择错）
- skip_notify:   该发通知时没发（副作用缺失）
- hallucinate:   不知道时编造参数值
- ignore_result: 工具成功返回、主流程却没采用结果（文章 Case B：
                 6 次查询都成功，回答却说「没有数据」）

并发安全：缺陷开关与副作用日志都**优先从 context 读取**（context["bugs"] /
context["notify_log"]），模块级 BUGS / NOTIFY_LOG 只是未注入时的默认值。
跑批方（webui / CLI）按次注入，多线程并发跑批互不踩踏——不再依赖
「改全局变量再跑」这种天然有竞态的开关方式。

轨迹证据：每个 ToolCallRecord 都带 result（工具真实返回值），AgentRun 带
latency_ms / token_usage。这两项是证据接地断言、judge 证据、性能对比的数据源。
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
    "ignore_result": True,
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

VERSIONS = {
    "model": "mock-gpt-2026-10",
    "prompt": "p-v1.0",
    "tool_desc": "td-v1.0",
    "kb": "kb-20261031",
}


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


def _metrics(ctx: dict) -> dict:
    """延迟 / Token 从 context 注入，方便实验对比演示「改好了但更慢更贵」。"""
    return {
        "latency_ms": ctx.get("latency_ms", 120.0),
        "token_usage": ctx.get("token_usage",
                               {"prompt": 320, "completion": 60, "total": 380}),
    }


# ---- Agent 主流程 ----

def run_agent(query: str, context: dict) -> AgentRun:
    ctx = context or {}
    # 局部覆盖：单条用例可用 context_overrides.bugs 只改某一个开关
    bugs = {**BUGS, **(ctx.get("bugs") or {})}
    log = ctx.get("notify_log")
    if not isinstance(log, list):           # 未注入：落全局日志（CLI 单进程）
        log = NOTIFY_LOG

    calls: list[ToolCallRecord] = []
    state_snapshot = lambda: {"notify_log": list(log)}
    metrics = _metrics(ctx)

    def done(output: str) -> AgentRun:
        return AgentRun(query=query, final_output=output, tool_calls=calls,
                        state=state_snapshot(), versions=VERSIONS, **metrics)

    q = query.lower()
    # 机型识别：最长匹配优先，避免 "iphone 15" 抢走 "iphone 15 pro"
    models = sorted(set(PRODUCT_DB) | set(PRICE_DB), key=len, reverse=True)
    model = next((m for m in models if m in q), None)

    # Case B 分支：工具真的调了、真的返回了，主流程却没把结果汇总进回答。
    # must_call / must_not_call / response_contains 在这里全都会通过，
    # 只有 tool_result_used（证据接地）能抓到「结果已返回但没被使用」。
    if "汇总" in query or "结果回传" in query:
        if model is None:
            return done("抱歉，我没识别出机型")
        price = tool_query_price(model)
        calls.append(ToolCallRecord("query_price", {"model": model},
                                    result={"price": price, "currency": "CNY"}))
        if bugs["ignore_result"]:
            return done("已为您完成价格查询，具体结果请稍后查看。")
        return done(f"{model} 当前售价 {price} 元。")

    # 路由：先判参数类，再判价格类（"尺寸是多少"含"多少"，不能用宽泛词）
    if "尺寸" in q or "屏幕" in q or "芯片" in q:
        if model is None:
            return done("抱歉，我没识别出机型")
        value = tool_query_product(model, "screen")
        calls.append(ToolCallRecord("query_product",
                                    {"model": model, "field": "screen"},
                                    result={"screen": value}))
        return done(f"{model} 屏幕尺寸 {value}。")

    if "多少钱" in q or "价格" in q or "售价" in q:
        if model is None:
            return done("抱歉，我没识别出机型")
        if bugs["wrong_tool"]:
            # 缺陷：价格问题调成了参数工具
            value = tool_query_product(model, "screen")
            calls.append(ToolCallRecord("query_product",
                                        {"model": model, "field": "screen"},
                                        result={"screen": value}))
            output = f"{model} 的屏幕是 {value}，售价请另询。"
        else:
            price = tool_query_price(model)
            calls.append(ToolCallRecord("query_price", {"model": model},
                                        result={"price": price, "currency": "CNY"}))
            output = f"{model} 当前售价 {price} 元。"

        # 价格场景要发通知（业务规则）。注意：必须真的调用工具，
        # 只记 calls 不调工具，副作用断言（state_after）会抓住它
        if ctx.get("notify_required") and not bugs["skip_notify"]:
            calls.append(ToolCallRecord("notify",
                                        {"message": f"price_query:{model}"},
                                        result="sent"))
            tool_notify(f"price_query:{model}", log)
        return done(output)

    # 未知问题
    if bugs["hallucinate"]:
        return done("这款的电池是 4252mAh，续航 22 小时。")
    return done("这个问题我需要确认后回复您，已转人工跟进。")
