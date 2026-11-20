"""P2 语义判分：LLM judge + 金标校准。

P1 断言（工具调用 / 关键词 / 状态）只能挡「确定性错误」，
语义层的问题——"回答得对不对、有没有答非所问、兜底有没有兜住"——
需要 LLM judge。但 judge 本身也是模型，不能裸上岗：

- 校准门槛：judge 对金标集的判断先过「一致率 ≥85% 且 Cohen's κ ≥0.8」才准参与门禁
  （κ 对随机一致性做了修正——90% 全 pass 的金标集里两边全 pass 的一致率是碰巧，κ≈0）
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
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

JUDGE_SYSTEM_PROMPT = """你是 AI Agent 评测的语义判分器。根据判分标准，判断 Agent 的回答是否合格。

只输出一个 JSON 对象，不要输出其他内容：
{"verdict": "pass|fail|need_review", "evidence": ["证据1", "证据2"]}

判定规则：
- pass: 回答完全满足全部判分标准
- fail: 回答违反至少一条判分标准
- need_review: 证据不足、无法判断（宁可复核，不硬判）
- evidence 每条必须引用回答中的具体内容作为依据

多轮对话使用规则（若提供【多轮对话】段）：
- 【末轮用户问题】和【末轮 Agent 回答】是同一对话的末轮，不是额外一轮
- 【多轮对话】按发生顺序列出各轮问答；按判分标准核对末轮回答及跨轮表现，不要把早先的回答当作末轮答案

执行证据使用规则（若提供【执行证据】段）：
- 工具调用成功、接口状态正常，都不代表任务完成；须核对回答中的事实性结论
  是否与工具返回值 / 召回片段一致，以及关键结论是否完整交付
- 回答中的数字、型号、状态、结论若在证据里找不到依据，或与证据冲突，判 fail
- 证据显示工具已返回结果、但回答声称「没有数据 / 查不到」，判 fail
- 证据本身缺失或读不出结论时判 need_review，不要凭空猜测
- 判分标准始终是唯一依据；执行证据只用于核验证据支持度，不额外增加标准"""


# ---------- 校准统计（金标集驱动） ----------

def agreement_rate(machine: list[float], human: list[float]) -> float:
    """一致率 = 完全一致样本数 / 总数（粗筛口径）。"""
    if len(machine) != len(human) or not machine:
        raise ValueError("样本数不一致或为空")
    hits = sum(1 for m, h in zip(machine, human) if m == h)
    return hits / len(machine)


def cohen_kappa(machine: list[float], human: list[float]) -> float:
    """Cohen's κ（二值标签）。一致率按「随机一致性」修正：κ = (po - pe) / (1 - pe)。

    为什么不用 Spearman：判分是 pass/fail 二值，机器全判 pass 时秩全部并列，
    Spearman 会算出 1.0 的假象——「一致率 0.9 但 κ≈0」的 judge 照样被放行。
    κ 的解读（Landis & Koch）：≥0.8 几乎完美，0.6~0.8 可观，≤0 纯碰巧。
    """
    if len(machine) != len(human) or not machine:
        raise ValueError("样本数不一致或为空")
    n = len(machine)
    po = agreement_rate(machine, human)
    p_m = sum(machine) / n                # 机器判 pass 的边际率
    p_h = sum(human) / n                  # 人工判 pass 的边际率
    pe = p_m * p_h + (1 - p_m) * (1 - p_h)
    if pe >= 1.0 - 1e-12:                 # 双方边际全同标签：po 必然 = 1
        return 1.0 if po >= 1.0 - 1e-12 else 0.0
    return (po - pe) / (1 - pe)


@dataclass
class CalibrationReport:
    """judge（或任何机器判分器）的上岗校准报告。"""

    dim: str
    n: int
    agreement: float
    kappa: float
    pass_agreement: bool      # ≥ 0.85（粗筛门槛）
    pass_kappa: bool          # ≥ 0.80（随机一致性修正后的精评门槛）
    qualified: bool
    need_review_count: int = 0
    need_review_rate: float = 0.0


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
    cache_judgments: bool = True   # 相同 (query, criteria, output, trace) 只判一次
    _cache: dict = field(default_factory=dict, repr=False)

    def judge(self, query: str, criteria: list[str], output: str,
              trace: dict | None = None) -> dict:
        """判一条输出。返回 {"verdict": pass/fail/need_review, "evidence": [...]}。

        trace: 来自 AgentRun.trace_context() 的可选执行证据（tool_calls / retrieved / state），
               多轮用例还包含按顺序排列的 turns（每轮含 query / final_output）。
        任何调用失败都降级为 need_review —— 判分器故障不能污染跑批结果。
        """
        trace_key = (json.dumps(trace, ensure_ascii=False, sort_keys=True,
                                default=str) if trace is not None else "")
        # 缓存 key 必须纳入轨迹：同一句回答、背后工具返回不同，判定可能完全相反
        # （文章 Case B：工具都成功返回，回答却说「没数据」）。
        key = (query, tuple(criteria), output, trace_key)
        if self.cache_judgments and key in self._cache:
            return self._cache[key]
        turns = trace.get("turns") if trace is not None else None
        query_heading = "末轮用户问题" if turns else "用户问题"
        answer_heading = "末轮 Agent 回答" if turns else "Agent 回答"
        target_query = turns[-1]["query"] if turns else query
        user = ("【判分标准】\n" + "\n".join(f"- {c}" for c in criteria)
                + f"\n\n【{query_heading}】\n{target_query}"
                + f"\n\n【{answer_heading}】\n{output or '（空输出）'}")
        if turns:
            dialogue = "\n\n".join(
                f"第 {i} 轮\n用户问题：{turn['query']}\n"
                f"Agent 回答：{turn['final_output'] or '（空输出）'}"
                for i, turn in enumerate(turns, start=1)
            )
            user += f"\n\n【多轮对话】（按发生顺序，含末轮）\n{dialogue}"
        if trace is not None:
            execution_trace = {k: v for k, v in trace.items() if k != "turns"}
            if execution_trace:
                evidence = json.dumps(execution_trace, ensure_ascii=False, default=str)
                if len(evidence) > 3000:
                    evidence = evidence[:3000] + "…（已截断）"
                user += f"\n\n【执行证据】（工具调用与返回值 / 召回片段 / 结束状态）\n{evidence}"
        try:
            raw = self.client.chat(JUDGE_SYSTEM_PROMPT, user)
            result = _parse_verdict(raw)
        except Exception as e:  # noqa: BLE001 - 自定义 client 的任何异常都降级
            # 只捕获 JudgeError 时，自定义 client 抛 RuntimeError/ValueError 会
            # 冒泡到 harness 中断整批；与「任何调用失败都降级」的承诺相悖。
            self.last_errors.append(f"{type(e).__name__}: {e}")
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
    """judge 上岗校准。人工金标只允许 pass/fail，机器另可输出 need_review。

    门槛：一致率 ≥85% 且三类别 Cohen's κ ≥0.8。
    need_review 是独立类别，保留在总样本中且始终与人工标签不一致。
    """
    if len(machine) != len(human) or not machine:
        raise ValueError("金标集样本数为空或长度不一致")
    human_labels = ("pass", "fail")
    machine_labels = (*human_labels, "need_review")
    for side, values, allowed in (("machine", machine, machine_labels),
                                  ("human", human, human_labels)):
        for i, value in enumerate(values):
            if value not in allowed:
                raise ValueError(f"{side}[{i}] 标签非法: {value!r}，仅允许 {', '.join(allowed)}")
    n = len(machine)
    if n < min_samples:
        raise ValueError(
            f"金标集不足 {min_samples} 条（当前 {n}），不能给 judge 上岗"
            "——小样本校准出来的 judge 是过拟合的")
    machine_counts, human_counts = Counter(machine), Counter(human)
    hits = sum(m == h for m, h in zip(machine, human))
    ag = hits / n
    # 将观测一致率与随机一致率都乘以 n²，避免浮点误差影响退化情形及门槛判断。
    expected = sum(machine_counts[label] * human_counts[label] for label in machine_labels)
    kp = (hits * n - expected) / (n * n - expected) if expected != n * n else 1.0
    ok_ag, ok_kp = ag >= 0.85, kp >= 0.80
    need_review_count = machine_counts["need_review"]
    return CalibrationReport(
        dim="semantic_judge",
        n=n,
        agreement=round(ag, 4),
        kappa=round(kp, 4),
        pass_agreement=ok_ag,
        pass_kappa=ok_kp,
        qualified=ok_ag and ok_kp,
        need_review_count=need_review_count,
        need_review_rate=round(need_review_count / n, 4),
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
    "agreement_rate", "cohen_kappa", "CalibrationReport",
    "JUDGE_SYSTEM_PROMPT",
]
