"""FDE W03 工程 → agent_eval 框架的适配器。

被测对象：fde-agent-w03 的标准问数内核（聊天入口 POST /api/run，默认 127.0.0.1:8765）。
接入方式：HTTP 调用，不侵入工程代码。工程侧只要求先启动服务：
    .venv/bin/python tools/product.py start --home .runtime/<project>

响应映射（依据 platform/fde_product/agent.py 第 212 行的 safe 字典）：
    response 结构（真实）：
        {
          status: completed | clarification | refused | incomplete | awaiting_approval,
          result: {
            status, summary,
            metric_query: {status, intent, rows, columns, units, answer, ...}   ← 引擎原始结果
          },
          observations: [{name, result, call_id}],
          model_name / prompt_sha256 / product_version / runtime_revision
        }
    注意：**指标数据在 result.metric_query 下**，不在 result 下。result.status 是 Agent 层
    状态（completed/clarification/refused/incomplete），metric_query.status 才是引擎层状态
    （ok/clarify/unsupported/error）。

    final_output ← result.metric_query.answer → result.summary 兜底
    tool_calls   ← observations[].name（query_metrics / tag_* 等）
    state        ← 整个响应包进 {"response": ...}，断言用 response.result.metric_query.* 点路径
    versions     ← model_name / prompt_sha256 / product_version / runtime_revision
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# 引入 agent_eval 框架（按实际目录调整）
AGENT_EVAL_DIR = Path(__file__).resolve().parent.parent / "agent_eval"
sys.path.insert(0, str(AGENT_EVAL_DIR))

from harness import AgentAdapter, AgentRun, ToolCallRecord  # noqa: E402


class W03AgentAdapter(AgentAdapter):
    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout: int = 620):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout  # 工程侧单任务上限 600 秒，适配器略放宽
        self._seq = 0

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # 工程强制要求：缺这个头直接 403 ORIGIN_REJECTED
                "X-FDE-Client": "local-ui",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def health(self) -> bool:
        """服务是否在线（GET / 返回页面即认为在线）。"""
        try:
            with urllib.request.urlopen(self.base_url + "/", timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def run(self, query: str, context: dict) -> AgentRun:
        # 每条用例独立会话，避免上一条的历史影响本条判定
        self._seq += 1
        session = f"eval-{int(time.time())}-{self._seq}"
        try:
            resp = self._post("/api/run", {"text": query, "session_id": session})
        except Exception as e:
            return AgentRun(query=query, final_output="", error=f"HTTP 调用失败: {e}")

        result = resp.get("result") or {}
        metric = result.get("metric_query") or {}      # 引擎原始结果（rows/units/status）
        answer = metric.get("answer") or result.get("summary") or f"[{resp.get('status')}]"

        # observations: [{name, result, call_id}] —— 工具调用轨迹
        tool_calls = [
            ToolCallRecord(name=o.get("name", "unknown"), args={k: v for k, v in (o.get("args") or {}).items()})
            for o in (resp.get("observations") or [])
        ]

        return AgentRun(
            query=query,
            final_output=str(answer),
            tool_calls=tool_calls,
            # 全量响应入 state；同时把 metric_query 提到顶层，断言可写 metric.rows 这种短路径
            state={"response": resp, "metric": metric, "agent_status": resp.get("status")},
            versions={
                "model": resp.get("model_name", ""),
                "prompt": resp.get("prompt_sha256", "")[:16],
                "product": resp.get("product_version", ""),
                "runtime": resp.get("runtime_revision", ""),
            },
        )
