"""P2 语义判分：LLM judge + 金标校准。

P1 断言（工具调用 / 关键词 / 状态）只能挡「确定性错误」，
语义层的问题——"回答得对不对、有没有答非所问、兜底有没有兜住"——
需要 LLM judge。但 judge 本身也是模型，不能裸上岗：

- 校准门槛：judge 对金标集的判断先过「一致率 ≥85% 且 Spearman ≥0.8」才准参与门禁
- 未校准 / 未配置时自动跳过并标注，绝不硬判
- 证据不足输出「需复核」（need_review），不猜

用法（0 依赖，OpenAI 兼容接口，读 EVAL_MODEL_* 环境变量）::

    from judge import SemanticJudge, load_judge_from_env

    judge = load_judge_from_env()          # 未配环境变量返回 None
    if judge:
        judge.mark_calibrated()            # 金标校准通过后
    runner = BatchRunner(adapter, judge=judge, judge_level=("L1", "L2"))
    report = runner.run_all(cases, level="L1")
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from evaluator import CalibrationReport, agreement_rate, spearman

JUDGE_SYSTEM_PROMPT = """你是 AI Agent 评测的语义判分器。根据判分标准，判断 Agent 的回答是否合格。

只输出一个 JSON 对象，不要输出其他内容：
{"verdict": "pass|fail|need_review", "evidence": ["证据1", "证据2"]}

判定规则：
- pass: 回答完全满足全部判分标准
- fail: 回答违反至少一条判分标准
- need_review: 证据不足、无法判断（宁可复核，不硬判）
- evidence 每条必须引用回答中的具体内容作为依据"""


class JudgeError(Exception):
    """LLM 调用或解析失败。"""


class OpenAICompatClient:
    """OpenAI 兼容 chat completions 客户端（纯标准库，直连不走系统代理）。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 60, temperature: float = 0.0,
                 retries: int = 2) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.retries = retries     # 一次网络抖动不该让整批语义断言全部跳过、门禁误放行

    def chat(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        # 直连 opener：评测机常见系统代理只放行白名单域，绕开防 502
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with opener.open(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as e:  # 网络 / HTTP 错误按次数重试，最终失败再降级
                last = e
        else:
            raise JudgeError(f"LLM 调用失败（重试 {self.retries} 次）: {last}") from last
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise JudgeError(f"LLM 响应格式异常: {data}") from e


def _parse_verdict(text: str) -> dict:
    """从模型输出里抠 JSON（容忍代码围栏和前后杂文）。抠不出 → need_review。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text[3:].strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {"verdict": "need_review", "evidence": [f"模型输出无法解析: {text[:120]!r}"]}
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {"verdict": "need_review", "evidence": [f"JSON 解析失败: {text[:120]!r}"]}
    verdict = str(obj.get("verdict", "")).lower()
    if verdict not in ("pass", "fail", "need_review"):
        return {"verdict": "need_review", "evidence": [f"verdict 非法: {verdict!r}"]}
    evidence = obj.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = [str(evidence)]
    return {"verdict": verdict, "evidence": [str(e) for e in evidence]}


@dataclass
class SemanticJudge:
    """语义判分器。未校准（calibrated=False）时 harness 会自动跳过它。"""

    client: object                      # 实现 chat(system, user) -> str 的客户端
    name: str = "llm-judge"
    calibrated: bool = False
    calibration: Optional[CalibrationReport] = None
    last_errors: list[str] = field(default_factory=list)   # 调试留档：最近几次调用失败原因
    cache_judgments: bool = True   # 相同 (query, criteria, output) 只判一次，pass^k 复跑不重复烧 token
    _cache: dict = field(default_factory=dict, repr=False)

    def judge(self, query: str, criteria: list[str], output: str) -> dict:
        """判一条输出。返回 {"verdict": pass/fail/need_review, "evidence": [...]}。

        任何调用失败都降级为 need_review —— 判分器故障不能污染跑批结果。
        """
        key = (query, tuple(criteria), output)
        if self.cache_judgments and key in self._cache:
            return self._cache[key]
        user = (f"【判分标准】\n" + "\n".join(f"- {c}" for c in criteria)
                + f"\n\n【用户问题】\n{query}"
                + f"\n\n【Agent 回答】\n{output or '（空输出）'}")
        try:
            raw = self.client.chat(JUDGE_SYSTEM_PROMPT, user)
            result = _parse_verdict(raw)
        except JudgeError as e:
            self.last_errors.append(str(e))
            self.last_errors = self.last_errors[-10:]
            result = {"verdict": "need_review", "evidence": [f"judge 调用降级: {e}"]}
        if self.cache_judgments and result["verdict"] != "need_review":
            # 不缓存 need_review：网络恢复 / 证据补齐后允许重新判
            self._cache[key] = result
        return result

    def mark_calibrated(self, report: Optional[CalibrationReport] = None) -> None:
        """金标校准通过后调用。没有校准报告只给 False 的红线在 calibrate_judge 里。"""
        self.calibrated = True
        self.calibration = report


def calibrate_judge(machine: list[str], human: list[str],
                    min_samples: int = 200) -> CalibrationReport:
    """judge 上岗校准。machine/human 为 pass/fail 标签序列（人工金标 vs judge 判定）。

    门槛与维度精评一致：一致率 ≥85% 且 Spearman ≥0.8。
    need_review 样本按「不一致」计——judge 说不准本身就是不达标。
    """
    if len(machine) != len(human) or not machine:
        raise ValueError("金标集样本数为空或长度不一致")
    if len(machine) < min_samples:
        raise ValueError(
            f"金标集不足 {min_samples} 条（当前 {len(machine)}），不能给 judge 上岗"
            "——小样本校准出来的 judge 是过拟合的")
    m01 = [1.0 if v == "pass" else 0.0 for v in machine]
    h01 = [1.0 if v == "pass" else 0.0 for v in human]
    ag = agreement_rate(m01, h01)
    sp = spearman(m01, h01)
    ok_ag, ok_sp = ag >= 0.85, sp >= 0.80
    return CalibrationReport(
        dim="semantic_judge",
        n=len(machine),
        agreement=round(ag, 4),
        spearman=round(sp, 4),
        pass_agreement=ok_ag,
        pass_spearman=ok_sp,
        qualified=ok_ag and ok_sp,
    )


def load_judge_from_env() -> Optional[SemanticJudge]:
    """从环境变量构建 judge。三件套缺一即返回 None（不配 key 也能跑 P1）。::

        EVAL_MODEL_URL=https://api.deepseek.com/v1/chat/completions
        EVAL_MODEL_NAME=deepseek-chat
        EVAL_MODEL_KEY=<key>
    """
    url, name, key = (os.environ.get("EVAL_MODEL_URL"),
                      os.environ.get("EVAL_MODEL_NAME"),
                      os.environ.get("EVAL_MODEL_KEY"))
    if not (url and name and key):
        return None
    return SemanticJudge(client=OpenAICompatClient(url, key, name))


__all__ = [
    "JudgeError", "OpenAICompatClient", "SemanticJudge",
    "calibrate_judge", "load_judge_from_env",
    "JUDGE_SYSTEM_PROMPT", "Optional",
]
