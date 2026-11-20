"""通用案例库的参考实现（Reference Agent）。

它存在的意义：

1. **让案例库立刻可跑**。纯标准库、零网络、零 API Key、零随机数，同样的输入
   永远得到同样的输出。任何装了 Python 3.8+ 的机器，clone 下来就能跑出全绿，
   用来确认「框架装对了、案例库没有 drift」。
2. **把「通用」落成两个可实现的协议**。案例库里的断言只引用标准动作名与固定
   令牌，这个文件就是这两个协议的示范实现。你接自己的系统时，照着它把工具名
   映射过去即可。

协议一：标准动作（STANDARD_TOOLS）
    lookup   读 / 检索 / 查询（无副作用）
    compute  计算 / 转换 / 取数加工
    record   写 / 落库 / 追加审计
    destroy  删除 / 撤销 / 不可逆操作
    deliver  外发 / 通知 / 推送

    任何业务工具都能归到这 5 类里。案例库的 must_call / must_not_call 只写
    标准名，验证的是「该不该做这类事」，而不是「必须调用某个具体函数」。

协议二：回显令牌（ECHO_TOKEN）
    通用 query 要求 Agent 原样回显一个固定字符串，response_contains 就断言这个
    令牌。这样「回答里必须出现 X」这条断言与业务无关，任何能遵循指令的 Agent
    都能过；过不了，说明指令遵循能力有缺口——这本身就是结论。

状态协议：state["audit_log"] 记录副作用，state["memory"] 保存会话记忆。
"""
from __future__ import annotations

import os
import sys

# 允许两种运行方式：在框架仓库内直接跑；整目录拷到别处后用 AGENT_EVAL_SRC 指路
_SRC = os.environ.get("AGENT_EVAL_SRC") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agent_eval_lib import AgentAdapter, AgentRun, ToolCallRecord  # noqa: E402

# ---- 协议常量：案例库与本文件必须保持一致 ----
STANDARD_TOOLS = ("lookup", "compute", "record", "destroy", "deliver")
ECHO_TOKEN = "ECHO-42"
LOOKUP_HIT = "ALPHA-7"          # lookup 的固定返回值（证据接地用）
COMPUTE_VALUE = 84              # compute 的固定返回值
MEMORY_CODE = "OMEGA-9"         # 多轮记忆的固定代号

# 固定版本与性能数字：让指纹和性能对比可复现，不引入时间/随机噪声
VERSIONS = {"model": "reference-1.0", "prompt": "p-1.0",
            "tool_desc": "td-1.0", "kb": "kb-1.0"}
METRICS = {"latency_ms": 12.0,
           "token_usage": {"prompt": 48, "completion": 12, "total": 60}}


def describe_protocol() -> dict:
    """给 README / 自检脚本用的协议摘要。"""
    return {
        "standard_tools": list(STANDARD_TOOLS),
        "echo_token": ECHO_TOKEN,
        "state_paths": ["audit_log", "memory"],
        "fixtures": {"lookup_hit": LOOKUP_HIT,
                     "compute_value": COMPUTE_VALUE,
                     "memory_code": MEMORY_CODE},
    }


class ReferenceAgent:
    """确定性的最小 Agent：按 query 里的关键词走固定分支。

    会话状态放在实例内、以 session_id 归并——框架不替被测系统存 history，
    只注入 session_id / turn_index / is_last_turn。这一层「自己维护上下文」
    正是真实接入时的样子。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}

    def _session(self, context: dict) -> dict:
        sid = str((context or {}).get("session_id") or "default")
        return self._sessions.setdefault(sid, {"memory": {}, "audit_log": []})

    @staticmethod
    def _done(query: str, output: str, calls: list[ToolCallRecord],
              sess: dict) -> AgentRun:
        return AgentRun(
            query=query,
            final_output=output,
            tool_calls=calls,
            state={"memory": dict(sess["memory"]),
                   "audit_log": list(sess["audit_log"])},
            versions=dict(VERSIONS),
            **METRICS,
        )

    def run(self, query: str, context: dict) -> AgentRun:
        sess = self._session(context)
        q = query or ""
        calls: list[ToolCallRecord] = []

        # 分支顺序有讲究：先处理「写记忆」，再处理「读记忆」，避免第 1 轮
        # 的「记住代号」被后面的通用规则抢走。
        if "记住代号" in q:
            sess["memory"]["code"] = MEMORY_CODE
            return self._done(query, f"已记住 {MEMORY_CODE}", calls, sess)

        if "代号" in q and sess["memory"].get("code"):
            return self._done(query, f"代号是 {sess['memory']['code']}", calls, sess)

        # 回显协议：命中固定令牌即原样回显；附加要求靠 query 里的关键词触发
        if ECHO_TOKEN in q:
            tail = " END" if "END" in q else ""
            note = "说明：已按指令原样回显令牌。" if "说明" in q else ""
            return self._done(query, f"已回显 {ECHO_TOKEN}{tail}。{note}", calls, sess)

        # 计算类：调用 compute，并把返回值汇总进回答（反「调了不用」）
        if "算" in q or "compute" in q.lower():
            calls.append(ToolCallRecord(
                "compute", {"expression": "12*7"}, result={"value": COMPUTE_VALUE}))
            return self._done(query, f"计算结果是 {COMPUTE_VALUE}", calls, sess)

        # 写操作类：落审计日志（副作用可被 state_after 观测到）
        if "记录" in q or "写入" in q:
            entry = "巡检通过"
            calls.append(ToolCallRecord("record", {"entry": entry}, result="ok"))
            sess["audit_log"].append({"entry": entry})
            return self._done(query, f"已记录：{entry}", calls, sess)

        # 只读查询类
        if "查" in q or "检索" in q or "lookup" in q.lower():
            calls.append(ToolCallRecord(
                "lookup", {"query": q},
                result={"hit": LOOKUP_HIT, "score": 0.91}))
            return self._done(query, f"查到条目 {LOOKUP_HIT}", calls, sess)

        # 兜底：不崩、给出明确回执（错误韧性用例靠这条）
        return self._done(query, "已收到，无法识别意图", calls, sess)


class ReferenceAdapter(AgentAdapter):
    """把 ReferenceAgent 包成框架认识的 adapter。

    换你自己的系统时，只需要改这一个类：在 run() 里调用你的 Agent，
    再把工具调用名归一成 STANDARD_TOOLS，把返回值填进 ToolCallRecord.result。
    """

    def __init__(self) -> None:
        self.agent = ReferenceAgent()

    def run(self, query: str, context: dict) -> AgentRun:
        return self.agent.run(query, context)
