"""OpenBidKit 适配器：spawn Node runner，调用仓库真实代码（bidAnalysisTask.cjs）。

query 即招标文件文本；aiService 由 runner 注入（OpenAI 兼容接口，默认 DeepSeek）。
环境变量：FDE_MODEL_KEY（必填）/ FDE_MODEL_URL / FDE_MODEL_NAME（可选）。
"""
import json
import os
import subprocess
from pathlib import Path

from agent_eval_lib import AgentAdapter, AgentRun, ToolCallRecord

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "node_runner.cjs"
REPO_CLIENT = HERE.parent / "repo" / "client"

DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"


def _resolve_base(url: str) -> str:
    """兼容完整 endpoint 或 base_url 两种写法。"""
    url = (url or DEFAULT_ENDPOINT).strip()
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url.rstrip("/")


class OpenBidKitAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.key = os.environ.get("FDE_MODEL_KEY", "")
        self.base = _resolve_base(os.environ.get("FDE_MODEL_URL", ""))
        self.model = os.environ.get("FDE_MODEL_NAME", "deepseek-chat")

    def health(self) -> bool:
        if not self.key:
            return False
        if not RUNNER.exists() or not (REPO_CLIENT / "electron" / "services" / "bidAnalysisTask.cjs").exists():
            return False
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=10)
            return True
        except Exception:  # noqa: BLE001
            return False

    def run(self, query: str, context: dict) -> AgentRun:
        payload = {
            "file_content": query,
            "section_hint": context.get("section_hint", ""),
            "api": {"base_url": self.base, "key": self.key, "model": self.model, "timeout": 180000},
        }
        try:
            proc = subprocess.run(
                ["node", str(RUNNER)],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True, text=True, timeout=240, cwd=str(HERE),
            )
            out = (proc.stdout or "").strip()
            if not out:
                return AgentRun(query=query, final_output="",
                                error=f"node 无输出: {(proc.stderr or '')[:300]}")
            data = json.loads(out)
        except Exception as e:  # noqa: BLE001
            return AgentRun(query=query, final_output="", error=repr(e))

        if not data.get("ok"):
            return AgentRun(query=query, final_output="", versions={"model": data.get("model", "")},
                            tool_calls=[ToolCallRecord("ai.chat", {"count": data.get("chat_calls", 0)})],
                            error=data.get("error") or "unknown")
        return AgentRun(
            query=query,
            final_output=data.get("content", ""),
            tool_calls=[ToolCallRecord("ai.chat", {"count": data.get("chat_calls", 0)})],
            versions={"model": data.get("model", ""), "runner": "node_runner.cjs"},
        )
