"""测试执行器（harness）：把被测 Agent 接进来的那一层。

你的项目只需要做一件事：实现 AgentAdapter，把每次调用包装成 AgentRun。
其余的（P1 断言、批量执行、失败归因、门禁判定、报告）都由这里负责。

能力覆盖：断言五类型 / 分层执行 / 失败样本回流 / 合并门禁 /
链路第一错与主归因层 / 数据集分层 / 运行指纹 / 版本逐题对比。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from attribution import FirstErrorFinder, TraceStep, single_attribution
from evalset import EvalCase, case_to_dict, load_evalset
from models import AttributionLayer


def _import_judge():
    """延迟导入 judge（可选模块：无 LLM 时框架照常跑 P1）。"""
    try:
        import judge as _j
        return _j
    except ImportError:
        return None


def _merged_trace(runs: list[AgentRun]) -> dict:
    """多轮执行证据合并（文章 Case B 的子任务往往发生在前几轮）。"""
    calls: list[dict] = []
    retrieved: list[str] = []
    state = {}
    for r in runs:
        for t in r.tool_calls:
            calls.append({"name": t.name, "args": t.args,
                          "ok": t.ok, "result": _safe_value(t.result)})
        retrieved.extend(r.retrieved or [])
        if r.state:
            state = _safe_value(r.state)
    trace = {"tool_calls": calls, "retrieved": retrieved, "state": state}
    if len(runs) > 1:
        trace["turns"] = [{"query": r.query, "final_output": r.final_output} for r in runs]
    return trace


def _judge_accepts_trace(judge) -> bool:
    """能力探测：调用处按关键字 trace= 传第 4 个参数，需真实校验能否绑定。

    旧实现用「位置参数 ≥ 4」推断，会把 judge(self,q,c,o,ctx=None) 误判为可接收
    trace，实际调用抛 TypeError 中断整批。改用 bind 试绑定四参关键字调用；
    3 参签名 / 命名参数不是 trace 的签名都会正确返回 False，走三参调用。
    """
    try:
        sig = inspect.signature(judge.judge)
        sig.bind("q", ["c"], "o", trace=None)
    except (TypeError, ValueError):
        return False
    return True


def _normalize_verdict(raw) -> dict:
    """把自定义 judge 的返回值收敛成 {verdict, evidence}。

    自定义 judge 是公开扩展点，返回值不受 judge.py 的 _parse_verdict 保护：
    非 dict / 非法 verdict / evidence 非 list 一律降级 need_review，对齐
    judge.py:161-163 与 JUDGE_SYSTEM_PROMPT「证据不足不硬判」。判分器故障
    不能被当成「回答不合格」而误拦截 blocker。
    """
    if not isinstance(raw, dict):
        return {"verdict": "need_review",
                "evidence": [f"judge 返回值非对象: {raw!r}"[:160]]}
    verdict = raw.get("verdict")
    if verdict not in ("pass", "fail", "need_review"):
        return {"verdict": "need_review",
                "evidence": [f"judge verdict 非法: {verdict!r}"]}
    evidence = raw.get("evidence") or []
    if not isinstance(evidence, list):
        # 契约不符（而非仅仅格式宽松）：降级 need_review，避免拿可疑返回值硬判
        return {"verdict": "need_review",
                "evidence": [f"judge evidence 非数组: {type(evidence).__name__}"]}
    return {"verdict": verdict, "evidence": [str(e) for e in evidence]}


# ---------- 你的 Agent 要返回的东西 ----------

@dataclass
class ToolCallRecord:
    name: str
    args: dict = field(default_factory=dict)
    result: object = None     # 工具返回值（可选）：从 trace 里拿到就填，
                              # 证据接地断言与 judge 判分都靠它。调用与返回值天然对齐，
                              # 不用维护「调用列表 / 结果列表」两套下标。
    ok: bool = True           # 工具调用是否成功（可选）


@dataclass
class AgentRun:
    """一次 Agent 调用的完整轨迹。adapter 负责从你的项目里拼出它。"""
    query: str
    final_output: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)      # chunk id
    state: dict = field(default_factory=dict)               # 副作用后的系统状态
    versions: dict = field(default_factory=dict)            # model/prompt/tool_desc/kb
    error: Optional[str] = None                             # Agent 自身抛异常时填
    latency_ms: Optional[float] = None                      # 端到端耗时（实验对比用）
    token_usage: dict = field(default_factory=dict)         # {"prompt":n,"completion":n,"total":n}

    def trace_context(self) -> dict:
        """打包给 LLM judge 的执行证据。只含可核验事实，不含判分标准。

        文章里「工具调用成功 ≠ 任务完成」的两个 Case（读文档未刷新、子任务
        结果未回传），靠的就是把这层证据递到裁判面前。
        """
        calls = []
        for t in self.tool_calls:
            calls.append({"name": t.name, "args": t.args,
                          "ok": t.ok, "result": _safe_value(t.result)})
        return {
            "tool_calls": calls,
            "retrieved": list(self.retrieved or []),
            "state": _safe_value(self.state),
        }


def _safe_value(obj, _depth: int = 0):
    """把任意工具返回值收敛成可 JSON 序列化、且长度可控的证据片段。"""
    if _depth > 4:
        return "<嵌套过深，已截断>"
    if obj is None or isinstance(obj, (str, int, float, bool)):
        if isinstance(obj, str) and len(obj) > 400:
            return obj[:400] + "…（已截断）"
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe_value(v, _depth + 1)
                for k, v in list(obj.items())[:20]}
    if isinstance(obj, (list, tuple)):
        return [_safe_value(v, _depth + 1) for v in list(obj)[:20]]
    return str(obj)[:200]


class AgentAdapter(ABC):
    """接入协议：你的项目实现这一个类，就完成了全部接入。"""

    @abstractmethod
    def run(self, query: str, context: dict) -> AgentRun:
        ...

    def run_turns(self, turns: list[dict], context: dict) -> list[AgentRun]:
        """多轮对话默认实现：同一 session 逐轮调用 run()。

        turns: [{"query": ...}, ...]（已含第 1 轮）。context 里会注入
        session_id / turn_index，被测服务若维护会话状态即可串起上下文；
        需要更精细的会话控制（如显式 history）时，adapter 覆写此方法。
        """
        sid = context.get("session_id") or f"eval-{uuid.uuid4().hex[:12]}"
        runs: list[AgentRun] = []
        for i, t in enumerate(turns):
            merged = {**context, "session_id": sid, "turn_index": i,
                      "is_last_turn": i == len(turns) - 1}
            runs.append(self.run(t["query"], merged))
        return runs


# ---------- P1 断言：证据接地（工具返回值是否真的被回答采用） ----------

def _norm_value(v: object) -> str:
    """值归一化：全角转半角 / 去千分位逗号 / 去空白 / 小写。

    让 "5,999" 与 "5999 元"、"６.１" 与 "6.1" 都能互相命中。
    只用于「证据值是否出现在回答里」的字面匹配，不做语义判断（那是 P2 的事）。
    """
    s = str(v)
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
                for c in s)
    s = s.replace(",", "").replace("，", "")
    return "".join(s.split()).lower()


def _iter_scalars(obj, _depth: int = 0):
    """递归展开任意工具返回值，只取标量（字符串 / 数字）。"""
    if _depth > 4 or obj is None or isinstance(obj, bool):
        return
    if isinstance(obj, (str, int, float)):
        yield obj
    elif isinstance(obj, dict):
        for v in list(obj.values())[:30]:
            yield from _iter_scalars(v, _depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in list(obj)[:30]:
            yield from _iter_scalars(v, _depth + 1)


def _dedup_pairs(pairs) -> list[tuple[str, str]]:
    """证据值按归一化结果去重的唯一实现。

    tool_result_used 的显式 values / output_grounded_in / _evidence_pairs 三条
    路径必须共用它：否则重复值（"100","100","100" 或归一化后同值）能凑满
    min_values，把接地失败的样本静默判成通过。
    """
    uniq: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw, n in pairs:
        if not n or n in seen:
            continue
        seen.add(n)
        uniq.append((raw, n))
    return uniq


def _evidence_pairs(obj) -> list[tuple[str, str]]:
    """从返回值抽出 (原值, 归一化值) 候选，过滤太短/太长的噪音，去重。"""
    pairs: list[tuple[str, str]] = []
    for v in _iter_scalars(obj):
        raw = str(v).strip()
        n = _norm_value(raw)
        if len(n) < 2 or len(n) > 80:
            continue
        pairs.append((raw[:60], n))
    return _dedup_pairs(pairs)


def _grounding_assertions(case: EvalCase, runs: list[AgentRun]) -> list[AssertionResult]:
    """证据接地两类断言。

    tool_result_used: 指定工具的返回值必须被最终回答采用
    output_grounded_in: 回答中至少要出现 N 个来自执行证据的值

    对应文章里「工具调用成功 ≠ 任务完成」的两个 Case：
    - 文档读取未刷新（本轮没读，凭旧上下文编结论）
    - 子 Agent 结果未回传（工具全成功，结果没被汇总，回答却说「没数据」）

    渐进接入红线：adapter 没上报任何 tool result 时一律 skipped，
    绝不把「不可观测」误判成「Agent 做错了」。
    """
    if not case.tool_result_used and not case.output_grounded_in:
        return []

    results: list[AssertionResult] = []
    all_calls = [t for r in runs for t in r.tool_calls]
    all_retrieved = [x for r in runs for x in (r.retrieved or [])]
    final_output = (runs[-1].final_output or "") if runs else ""
    norm_out = _norm_value(final_output)

    for spec in case.tool_result_used:
        tool = str(spec.get("tool") or "")
        min_values = int(spec.get("min_values", 1))
        explicit = spec.get("values") or []
        matched = [t for t in all_calls if t.name == tool]
        if explicit:
            # 与 _evidence_pairs / output_grounded_in 共用去重口径：
            # 重复值（含归一化后同值）不能凑满 min_values
            pairs = _dedup_pairs([(str(v)[:60], _norm_value(v)) for v in explicit])
        else:
            pairs = []
            for t in matched:
                if t.result is not None:
                    pairs += _evidence_pairs(t.result)
            pairs = _dedup_pairs(pairs)
        if not matched:
            results.append(AssertionResult(
                "tool_result_used", False,
                f"工具 {tool} 未被调用，无返回值可供回答采用",
                AttributionLayer.TRAJECTORY))
            continue
        if not pairs:
            results.append(AssertionResult(
                "tool_result_used", True,
                f"工具 {tool} 已调用但 adapter 未上报返回值，证据接地断言跳过"
                "（在 ToolCallRecord.result 填充即可启用）",
                AttributionLayer.TRAJECTORY, skipped=True, status="skipped"))
            continue
        hits = [raw for raw, n in pairs if n in norm_out]
        ok = len(hits) >= min_values
        results.append(AssertionResult(
            "tool_result_used", ok,
            "" if ok else (f"工具 {tool} 的返回值未被回答采用: "
                           f"命中 {len(hits)}/{min_values}，候选 {[p[0] for p in pairs[:5]]}，"
                           f"实际回答 {final_output[:60]!r}"),
            AttributionLayer.TRAJECTORY))

    spec = case.output_grounded_in
    if spec:
        min_values = int(spec.get("min_values", 1))
        sources = spec.get("sources") or ["tool_results", "retrieved"]
        explicit = spec.get("values") or []
        pairs: list[tuple[str, str]] = []
        if explicit:
            pairs += [(str(v)[:60], _norm_value(v)) for v in explicit]
        else:
            if "tool_results" in sources:
                for t in all_calls:
                    if t.result is not None:
                        pairs += _evidence_pairs(t.result)
            if "retrieved" in sources:
                pairs += [(str(x)[:60], _norm_value(x)) for x in all_retrieved]
        # 与 tool_result_used / _evidence_pairs 共用同一去重实现
        uniq = _dedup_pairs(pairs)
        if not uniq:
            results.append(AssertionResult(
                "output_grounded_in", True,
                "无可用执行证据（tool result / retrieved 均未上报），接地断言跳过",
                AttributionLayer.TRAJECTORY, skipped=True, status="skipped"))
        else:
            hits = [raw for raw, n in uniq if n in norm_out]
            ok = len(hits) >= min_values
            results.append(AssertionResult(
                "output_grounded_in", ok,
                "" if ok else (f"回答缺乏执行证据支撑: 命中 {len(hits)}/{min_values}，"
                               f"候选 {[p[0] for p in uniq[:5]]}，"
                               f"实际回答 {final_output[:60]!r}"),
                AttributionLayer.TRAJECTORY))
    return results


# ---------- 断言器 ----------

@dataclass
class AssertionResult:
    kind: str            # must_call / must_not_call / response_contains / state_after / no_error / semantic
    ok: bool
    detail: str
    layer: AttributionLayer = AttributionLayer.TRAJECTORY
    skipped: bool = False   # True = 未参与判定（如 judge 未校准被跳过），不影响 passed
    status: str = ""        # 显式状态：passed / failed / skipped / need_review；空则按 ok/skipped 推导

    def result_status(self) -> str:
        """状态单一来源。禁止用 detail 文本反推状态（文案一改就静默失真）。"""
        if self.status:
            return self.status
        if self.skipped:
            return "skipped"
        return "passed" if self.ok else "failed"


def _tool_assertions(must_call: list[dict], must_not_call: list[dict],
                     calls: list[ToolCallRecord], prefix: str = "") -> list[AssertionResult]:
    args_by_tool: dict[str, list[dict]] = {}
    for call in calls:
        args_by_tool.setdefault(call.name, []).append(call.args or {})

    results: list[AssertionResult] = []
    for req in must_call:
        tool = req.get("tool", "")
        matching_args = args_by_tool.get(tool)
        if not matching_args:
            detail = f"缺少工具调用: {tool}"
        else:
            required = set(req.get("required_args", []))
            missing = min((required.difference(args) for args in matching_args), key=len)
            detail = (f"工具 {tool} 单次调用缺少参数: {sorted(missing)}"
                      if missing else "")
        results.append(AssertionResult("must_call", not detail, prefix + detail if detail else ""))

    for req in must_not_call:
        tool = req.get("tool", "")
        ok = tool not in args_by_tool
        results.append(AssertionResult(
            "must_not_call", ok, "" if ok else f"{prefix}出现禁止调用: {tool}"))
    return results


def assert_case(case: EvalCase, runs: AgentRun | list[AgentRun]) -> list[AssertionResult]:
    """执行全部 P1 断言，失败详情直接进报告。

    案例级工具断言看全程，案例级输出和状态断言只看末轮；
    turns[i] 的工具和输出断言只看第 i+2 轮。
    """
    runs = [runs] if isinstance(runs, AgentRun) else list(runs)
    turns = case.turns or []
    expected_turns = 1 + len(turns)
    multi = expected_turns > 1 or len(runs) > 1
    results: list[AssertionResult] = []

    # Agent 自己崩了，后面不用看
    errored = [(i, r) for i, r in enumerate(runs) if r.error]
    if errored:
        for i, r in errored:
            prefix = f"第 {i + 1} 轮 " if multi else ""
            results.append(AssertionResult("no_error", False,
                                           f"{prefix}Agent 抛异常: {r.error}",
                                           AttributionLayer.SYSTEM))
        return results
    if len(runs) < expected_turns:
        return [AssertionResult("no_error", False,
                                f"缺少第 {len(runs) + 1} 轮运行结果"
                                f"（应有 {expected_turns} 轮，实际 {len(runs)} 轮）",
                                AttributionLayer.SYSTEM)]
    results.append(AssertionResult("no_error", True, ""))

    # 空轨迹防御：即使将来在 early-return 之前插入新分支也不会裸索引
    final_run = runs[-1] if runs else AgentRun(query="", final_output="")
    results.extend(_tool_assertions(case.must_call, case.must_not_call,
                                    [call for run in runs for call in run.tool_calls]))

    for kw in case.response_contains:
        ok = kw in (final_run.final_output or "")
        results.append(AssertionResult(
            "response_contains", ok,
            "" if ok else f"输出缺少关键词: {kw}（实际: {final_run.final_output[:60]!r}）"))

    for j, turn_spec in enumerate(turns, start=1):
        turn_run = runs[j]
        prefix = f"第 {j + 1} 轮"
        results.extend(_tool_assertions(turn_spec.get("must_call", []),
                                        turn_spec.get("must_not_call", []),
                                        turn_run.tool_calls, f"{prefix} "))
        output = turn_run.final_output or ""
        for kw in turn_spec.get("response_contains", []):
            ok = kw in output
            results.append(AssertionResult(
                "response_contains", ok,
                "" if ok else f"{prefix}输出缺少关键词: {kw}（实际: {output[:60]!r}）"))

    for item in case.state_after:
        path = item.get("path", "")
        actual = final_run.state
        for part in path.split("."):
            if isinstance(actual, dict):
                actual = actual.get(part)
            else:
                actual = None
                break
        if "value" in item:
            ok, expect_desc = actual == item["value"], f"值 = {item['value']!r}"
        elif "min_length" in item:
            ok = isinstance(actual, (list, dict, str)) and len(actual) >= item["min_length"]
            expect_desc = f"长度 ≥ {item['min_length']}"
        else:
            ok, expect_desc = actual is not None, "非空"
        results.append(AssertionResult(
            "state_after", ok,
            "" if ok else f"状态 {path} 期望 {expect_desc}，实际 {actual!r}",
            AttributionLayer.SYSTEM))

    results += _grounding_assertions(case, runs)
    return results


# ---------- 批量执行与报告 ----------

@dataclass
class CaseOutcome:
    case: EvalCase
    run: Optional[AgentRun]
    assertions: list[AssertionResult]
    passed: bool
    first_error: Optional[TraceStep]
    attribution: Optional[str]
    pass_runs: int = 1          # repeat>1 时：通过次数（pass^k 口径 = 全过才算过）
    total_runs: int = 1
    runs: Optional[list[AgentRun]] = None   # 多轮对话时：每一轮的 AgentRun（单轮为 None）
    session_id: str = ""        # 实际生效的会话标识（快照按会话归并回放）

    @property
    def flaky(self) -> bool:
        """有通过有失败 —— 非确定性信号，进报告单独列示。"""
        return 0 < self.pass_runs < self.total_runs

    @property
    def unverified(self) -> list[AssertionResult]:
        """声明了但没验成：skipped / need_review 的断言。"""
        return [a for a in self.assertions
                if a.result_status() in ("skipped", "need_review")]

    @property
    def review_reasons(self) -> list[str]:
        """零漏审口径：本条用例的全部不确定信号。

        与 case_status 解耦 —— 即便 P1 断言全过、case_status 判 passed，
        只要存在 skipped / need_review / flaky / undetermined，就必须人工确认。
        """
        from review import review_reasons as _collect
        return _collect(self)

    @property
    def review_required(self) -> bool:
        """是否需要人工审核。空 = 全自动判定完毕，可放行。"""
        return bool(self.review_reasons)

    @property
    def effective(self) -> list[AssertionResult]:
        """真正参与判定的断言。no_error 是结构性占位（Agent 跑通），不算判分依据。"""
        return [a for a in self.assertions
                if a.result_status() in ("passed", "failed")
                and a.kind != "no_error"]

    @property
    def undetermined(self) -> bool:
        """没有任何有效断言、且存在被跳过的断言 —— 这条用例其实没测过。"""
        return (not any(a.result_status() == "failed" for a in self.assertions)
                and bool(self.unverified) and not self.effective)

    @property
    def case_status(self) -> str:
        """三态判定：passed / failed / undetermined。

        - 任一断言 failed → failed（与 passed 原义一致）
        - 有 skipped/need_review 但无任何有效断言 → undetermined（不计入通过率）
        - 有 skipped/need_review 但存在有效断言 → 按有效断言判（仅标注不阻断）
        """
        if any(a.result_status() == "failed" for a in self.assertions):
            return "failed"
        return "undetermined" if self.undetermined else "passed"

    def failed_details(self) -> list[str]:
        details = [f"[{a.kind}] {a.detail}" for a in self.assertions if not a.ok]
        if self.flaky:
            details.append(f"[flaky] 复跑 {self.total_runs} 次仅通过 {self.pass_runs} 次")
        return details


@dataclass
class EvalReport:
    level: str
    outcomes: list[CaseOutcome]
    repeat: int = 1
    run_id: str = ""            # 一次跑批的唯一标识，快照/报告里贯穿
    env: str = "local"          # 生产 / 预发 / 本地：不同环境的结果不可比
    dataset: str = ""           # 本次跑的数据集标签（空 = 全量）
    ts: str = ""                # 跑批时间戳

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def determined_total(self) -> int:
        """有效判定过的用例数（排除 undetermined）—— 通过率的分母。"""
        return sum(1 for o in self.outcomes if o.case_status != "undetermined")

    @property
    def undetermined_total(self) -> int:
        """未有效测试的用例数（全 skipped / need_review / 无有效断言）。"""
        return sum(1 for o in self.outcomes if o.case_status == "undetermined")

    def undetermined_cases(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.case_status == "undetermined"]

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.case_status == "passed")

    @property
    def success_rate(self) -> float:
        # 分母用有效判定数：未测过的用例不能算进成功率，也不能稀释分母
        return self.passed / self.determined_total if self.determined_total else 0.0

    def fingerprint(self) -> dict:
        """运行指纹：回答「这两份报告能不能放在一起比」。

        文章踩坑 1：生产与预发共用应用标识 → 评估任务混入不同环境的 Trace，
        结果不可比较。指纹把环境 / 数据集 / 层级 / 版本四要素固化下来。
        """
        # vs 来自 versions_summary（已遍历全部轮次），因此末轮 model/prompt
        # 版本变化会改变 config_sig —— 换版本的两份报告不再被误判为可比。
        vs = self.versions_summary()
        sig = hashlib.sha256(json.dumps(
            {"env": self.env, "dataset": self.dataset, "level": self.level,
             "repeat": self.repeat, "versions": vs},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
        return {
            "run_id": self.run_id, "env": self.env,
            "dataset": self.dataset or "all", "level": self.level,
            "repeat": self.repeat, "ts": self.ts,
            "versions": vs, "config_sig": sig,
        }

    def perf_summary(self) -> dict:
        """延迟 / Token 汇总。实验对比时「改好了但慢了、贵了」必须看得见。

        一律取末轮，与 report_to_dict 的逐题性能口径一致（o.run 是首轮代表性轨迹，
        多轮场景下会与逐题字段自相矛盾）。
        """
        latencies = []
        tokens = []
        for o in self.outcomes:
            last = _last_run_of(o)
            if last and isinstance(last.latency_ms, (int, float)):
                latencies.append(last.latency_ms)
            u = (last.token_usage if last else None) or {}
            if isinstance(u, dict) and u:
                t = u.get("total")
                if not isinstance(t, (int, float)):
                    t = sum(v for v in u.values() if isinstance(v, (int, float)))
                tokens.append(t)
        out = {
            "cases_with_latency": len(latencies),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1)
            if latencies else None,
            "max_latency_ms": round(max(latencies), 1) if latencies else None,
            "cases_with_tokens": len(tokens),
            "total_tokens": int(sum(tokens)) if tokens else None,
        }
        return out

    def blockers_failed(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if not o.passed and o.case.severity == "blocker"]

    def blockers_undetermined(self) -> list[CaseOutcome]:
        """blocker 未有效测试 —— 门禁必须拒绝，不能当作绿灯。"""
        return [o for o in self.outcomes
                if o.case_status == "undetermined" and o.case.severity == "blocker"]

    def flaky_cases(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.flaky]

    def semantic_stats(self) -> dict:
        """P2 语义判分汇总: judged(实判) / failed / skipped(跳过) / need_review(转人工)。"""
        stats = {"judged": 0, "failed": 0, "skipped": 0, "need_review": 0}
        for o in self.outcomes:
            for a in o.assertions:
                if a.kind != "semantic":
                    continue
                s = a.result_status()
                if s == "skipped":
                    stats["skipped"] += 1
                elif s == "need_review":
                    # 弃判不是实判：不能计入 judged，否则校准/覆盖率会虚高
                    stats["need_review"] += 1
                elif s == "failed":
                    stats["failed"] += 1
                    stats["judged"] += 1
                else:
                    stats["judged"] += 1
        return stats

    def review_cases(self) -> list[CaseOutcome]:
        """judge 转人工的用例（不硬判，但审计必须看得见）。

        历史口径：只收 semantic + need_review。零漏审请用 review_queue()。
        """
        return [o for o in self.outcomes
                if any(a.kind == "semantic" and a.result_status() == "need_review"
                       for a in o.assertions)]

    def review_queue(self) -> list[CaseOutcome]:
        """零漏审审核队列：所有不确定用例，不区分 severity、不被有效断言掩盖。

        覆盖 skipped（judge 未校准 / 证据未上报）、need_review（judge 弃判）、
        undetermined（无有效断言）、flaky（复跑结果不一致）四类信号。
        """
        return [o for o in self.outcomes if o.review_required]

    def versions_summary(self) -> dict:
        """版本四要素汇总，供报告留档与版本对比。

        遍历全部轮次（多轮用例的末轮版本才代表本次生效配置），按轮次顺序覆盖；
        否则 config_sig 不含版本，换模型版本的两份报告会被误判为可比。
        """
        summary: dict = {}
        for o in self.outcomes:
            for r in _runs_of(o):
                if r.versions:
                    summary.update(_safe_value(r.versions))
        return summary

    def text(self) -> str:
        lines = [
            f"级别: {self.level} ｜ 用例: {self.total} ｜ 通过: {self.passed} ｜ "
            f"成功率: {self.success_rate:.1%}（有效判定 {self.determined_total} 条）"
            + (f" ｜ 每条复跑 {self.repeat} 次" if self.repeat > 1 else "")
        ]
        if self.undetermined_total:
            lines.append(f"⚠ 未定 {self.undetermined_total} 条（未有效测试，不参与通过率）: "
                         f"{[o.case.case_id for o in self.undetermined_cases()]}")
        fp = self.fingerprint()
        lines.append(f"运行指纹: run_id={fp['run_id'] or '未分配'} ｜ env={fp['env']} "
                     f"｜ dataset={fp['dataset']} ｜ config={fp['config_sig']}")
        perf = self.perf_summary()
        if perf["cases_with_latency"] or perf["cases_with_tokens"]:
            bits = []
            if perf["avg_latency_ms"] is not None:
                bits.append(f"平均耗时 {perf['avg_latency_ms']}ms")
            if perf["total_tokens"] is not None:
                bits.append(f"Token 合计 {perf['total_tokens']}")
            lines.append("性能指标: " + " ｜ ".join(bits))
        flaky = self.flaky_cases()
        if flaky:
            lines.append(f"⚠ 非确定性用例 {len(flaky)} 条: "
                         f"{[o.case.case_id for o in flaky]}")
        sem = self.semantic_stats()
        if any(o.case.judge_criteria for o in self.outcomes):
            lines.append(f"P2 语义判分: 实判 {sem['judged']} / 失败 {sem['failed']} / "
                         f"转人工 {sem['need_review']} / 跳过 {sem['skipped']}")
            for o in self.review_cases():
                lines.append(f"  ⚑ 待复核 {o.case.case_id}: "
                             + "; ".join(a.detail for a in o.assertions
                                         if a.kind == "semantic"
                                         and a.result_status() == "need_review"))
        bad = [o for o in self.outcomes if not o.passed]
        if bad:
            lines.append("-" * 62)
            for o in bad:
                lines.append(f"✗ {o.case.case_id} [{o.case.severity}] {o.case.query[:30]}")
                for d in o.failed_details():
                    lines.append(f"    {d}")
                if o.first_error:
                    lines.append(f"    第一错: {o.first_error.step_id} "
                                 f"({o.first_error.target}) → 主归因: {o.attribution}")
        return "\n".join(lines)


class BatchRunner:
    """分层跑批。L0 只 P1；L1/L2 加 P2 语义断言（judge 未校准时自动跳过并标注）。

    repeat > 1 时每条用例复跑多次（pass^k 口径：全过才算通过，部分通过记 flaky）；
    max_workers > 1 时用线程池并发执行（适用于 HTTP 型 adapter，进程内 adapter 注意线程安全）。
    """

    def __init__(self, adapter: AgentAdapter,
                 judge: Optional[object] = None,
                 judge_level: tuple = ("L1", "L2")) -> None:
        """judge: SemanticJudge 实例（可选）。judge_level: 哪些评测层级启用语义判分。"""
        self.adapter = adapter
        self.judge = judge
        self.judge_level = set(judge_level)

    def run_case(self, case: EvalCase, context: dict | None = None,
                 level: str = "L0") -> CaseOutcome:
        merged = {**(context or {}), **case.context_overrides}
        # 实际生效的会话标识写回 context 并留档：run_turns 自动生成后不再无人知晓，
        # 快照也因此能按真实会话归并回放（旧实现只读 context_overrides，恒为空）。
        effective_sid = merged.get("session_id") or f"eval-{uuid.uuid4().hex[:12]}"
        merged = {**merged, "session_id": effective_sid}
        all_runs: list[AgentRun]
        try:
            if case.turns:
                # 多轮：case.query 是第 1 轮，case.turns 是第 2 轮起的后续轮次
                turn_specs = [{"query": case.query}, *case.turns]
                all_runs = self.adapter.run_turns(turn_specs, merged)
            else:
                all_runs = [self.adapter.run(case.query, merged)]
        except Exception as e:  # noqa: BLE001 - 一条坏用例不能毁掉整批
            all_runs = [AgentRun(query=case.query, final_output="",
                                 error=f"adapter 异常: {e!r}")]
        results = assert_case(case, all_runs)
        if len(all_runs) >= 1 + len(case.turns):
            results += self._semantic_assertions(case, all_runs, level)
        passed = all(r.ok for r in results if not r.skipped)

        # 两个独立的归因口径，别混：
        # - first_error（链路第一错）：按断言链路顺序取第一个失败环节 —— 定位用
        # - attribution（主归因层）：按 single_attribution 的固定优先级
        #   system > trajectory > component 从全部失败断言中选 —— 定责用。
        #   例：must_call(trajectory) 与 state_after(system) 同时挂，主归因是 system：
        #   系统层（状态/知识/召回）是上游，修好它，下游轨迹错往往跟着消失。
        first_error, attribution = None, None
        if not passed:
            failed_layers = {r.layer for r in results if not r.ok}
            if failed_layers:
                attribution = single_attribution(
                    {layer: True for layer in failed_layers}).value
            steps = [
                TraceStep(step_id=f"{i}_{r.kind}", method="rule_check",
                          target=r.kind, ok=r.ok, detail=r.detail, layer=r.layer)
                for i, r in enumerate(results)
            ]
            first_error = FirstErrorFinder(steps).find()
        return CaseOutcome(case, all_runs[0] if all_runs else None,
                           results, passed, first_error, attribution,
                           runs=(all_runs if len(all_runs) > 1 else None),
                           session_id=effective_sid)

    def _semantic_assertions(self, case: EvalCase,
                             runs: list[AgentRun], level: str) -> list[AssertionResult]:
        """P2 语义断言。规则：

        - 只在 judge_level 层级生效，且用例声明了 judge_criteria
        - judge 未配置 / 未校准 → 产出 skipped=True 的占位断言（审计可见，不参与判定）
        - 校准过的 judge 判 fail → 语义断言失败，归因到 component 层（判分对象是
          提示词与回答行为）；need_review 同样标记但不硬判，转人工队列
        """
        if not case.judge_criteria:
            return []
        if level not in self.judge_level or self.judge is None or not self.judge.calibrated:
            why = (f"层级 {level} 不启用语义判分" if level not in self.judge_level
                   else ("未配置 judge" if self.judge is None else "judge 未通过金标校准，不能上岗"))
            return [AssertionResult("semantic", True,
                                    f"P2 语义断言跳过（{why}）: "
                                    f"{len(case.judge_criteria)} 条标准未判",
                                    AttributionLayer.COMPONENT, skipped=True,
                                    status="skipped")]
        final_output = runs[-1].final_output if runs else ""
        trace = _merged_trace(runs)
        try:
            if _judge_accepts_trace(self.judge):
                try:
                    r = self.judge.judge(case.query, case.judge_criteria,
                                         final_output, trace=trace)
                except TypeError:
                    # 双保险：能力探测误判时回退三参调用，不让整批中断
                    r = self.judge.judge(case.query, case.judge_criteria,
                                         final_output)
            else:
                r = self.judge.judge(case.query, case.judge_criteria, final_output)
        except Exception as e:  # noqa: BLE001 - 判分器故障不能毁整批
            r = {"verdict": "need_review",
                 "evidence": [f"judge 调用异常降级: {e!r}"]}
        r = _normalize_verdict(r)
        verdict = r["verdict"]
        evidence = r["evidence"]
        if verdict == "pass":
            return [AssertionResult("semantic", True, "", AttributionLayer.COMPONENT,
                                    status="passed")]
        if verdict == "need_review":
            return [AssertionResult("semantic", True,
                                    f"judge 证据不足转人工: {'; '.join(evidence[:3])}",
                                    AttributionLayer.COMPONENT, skipped=True,
                                    status="need_review")]
        return [AssertionResult("semantic", False,
                                f"语义判分不达标: {'; '.join(evidence[:3])}",
                                AttributionLayer.COMPONENT, status="failed")]

    def _run_repeats(self, case: EvalCase, repeat: int,
                     context: dict | None, level: str = "L0") -> CaseOutcome:
        """复跑聚合：全过才算过；优先保留首次失败的断言和轨迹。"""
        outcomes = [self.run_case(case, context, level=level) for _ in range(repeat)]
        pass_runs = sum(1 for o in outcomes if o.passed)
        base = next((o for o in outcomes if not o.passed), outcomes[0])
        return CaseOutcome(
            case=base.case, run=base.run, assertions=base.assertions,
            passed=(pass_runs == repeat),
            first_error=base.first_error, attribution=base.attribution,
            pass_runs=pass_runs, total_runs=repeat,
            runs=base.runs,          # 多轮轨迹必须透传，否则快照只剩第 1 轮
            session_id=base.session_id,
        )

    def run_all(self, cases: list[EvalCase], level: str = "L0",
                context: dict | None = None,
                repeat: int = 1, max_workers: int = 1,
                env: str | None = None,
                dataset: str | list[str] | None = None) -> EvalReport:
        """分层跑批。

        env:     环境标识（默认读 EVAL_ENV 环境变量，再兜底 local）。
                 生产/预发结果不可混比，compare_reports 会校验。
        dataset: 只跑指定数据集标签的用例（str 或列表）；None = 全量。
        """
        if repeat < 1 or max_workers < 1:
            raise ValueError("repeat 与 max_workers 必须 ≥ 1")
        # 空跑批无条件拒绝：dataset 过滤分支之外也要挡住（空报告会让门禁误放行）
        if not cases:
            raise ValueError("空跑批（0 条用例），拒绝生成空报告")
        if dataset is not None:
            if isinstance(dataset, str):
                wanted = {dataset}
            elif isinstance(dataset, (list, tuple, set)) and all(
                    isinstance(x, str) and x.strip() for x in dataset):
                wanted = set(dataset)
            else:
                raise ValueError("dataset 应为字符串或字符串数组"
                                 f"（实际 {type(dataset).__name__}）")
            cases = [c for c in cases if c.dataset in wanted]
            if not cases:
                raise ValueError(f"dataset={sorted(wanted)} 过滤后没有用例，拒绝空跑批"
                                 "（空报告会让门禁误放行）")
        if repeat == 1 and max_workers == 1:
            outcomes = []
            for c in cases:
                try:
                    outcomes.append(self.run_case(c, context, level=level))
                except Exception as e:  # noqa: BLE001 - 一条坏用例不毁整批
                    outcomes.append(_failed_outcome(c, f"框架执行异常: {e!r}"))
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [(i, pool.submit(self._run_repeats, c, repeat, context, level))
                           for i, c in enumerate(cases)]
                outcomes: list[CaseOutcome] = [None] * len(cases)  # type: ignore[list-item]
                for i, fut in futures:
                    try:
                        outcomes[i] = fut.result()
                    except Exception as e:  # noqa: BLE001
                        outcomes[i] = _failed_outcome(cases[i], f"框架执行异常: {e!r}")
        ds_label = (dataset if isinstance(dataset, str)
                    else ",".join(sorted(dataset)) if dataset else "")
        return EvalReport(
            level=level, outcomes=outcomes, repeat=repeat,
            run_id=uuid.uuid4().hex[:12],
            env=env or os.environ.get("EVAL_ENV") or "local",
            dataset=ds_label,
            ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )


# ---------- 门禁判定（baseline 由调用方传入） ----------

def _failed_outcome(case: EvalCase, message: str) -> CaseOutcome:
    """用例级异常兜底：转成 no_error 失败，一条坏用例不毁整批。"""
    run = AgentRun(query=case.query, final_output="", error=message)
    results = assert_case(case, [run])
    return CaseOutcome(case, run, results, False, None, None, session_id="")


def gate_decision(report: EvalReport, baseline_rate: Optional[float] = None,
                  drop_pp: float = 2.0, strict_review: bool = True,
                  signoffs=None) -> tuple[bool, list[str]]:
    """返回 (是否放行, 触发的规则列表)。blocker 一票否决，不参与百分比稀释。

    零漏审（strict_review=True，默认）：任何不确定用例未完成人工签核，
    一律拒绝放行，不区分 severity、不被有效断言掩盖。签核由
    review.load_signoffs() 提供且绑定内容指纹 —— 输出或轨迹变化后旧签核失效。

    纵深防御：空报告、全部用例未有效测试（undetermined）、blocker 未测，
    都不能当作绿灯放行。

    兼容口径：strict_review=False 回到历史行为（其余 undetermined 只标注、
    不阻断）。仅用于本地演示与回归对照，生产门禁不应关闭。
    """
    if report.total == 0:
        return False, ["空报告（0 条用例），拒绝放行"]
    if report.determined_total == 0:
        return False, ["全部用例未有效测试（undetermined），拒绝放行"]
    reasons: list[str] = []
    undetermined_blockers = report.blockers_undetermined()
    if undetermined_blockers:
        reasons.append(f"blocker 未有效测试 {len(undetermined_blockers)} 条: "
                       f"{[o.case.case_id for o in undetermined_blockers]}")
    blocked = report.blockers_failed()
    if blocked:
        reasons.append(f"blocker 样本失败 {len(blocked)} 条: "
                       f"{[o.case.case_id for o in blocked]}")
    if strict_review:
        from review import review_fingerprint
        pending: list[tuple[str, str]] = []
        for o in report.review_queue():
            fp = review_fingerprint(o)
            status = (signoffs.status(o.case.case_id, fp)
                      if signoffs is not None else "pending")
            if status != "approve":
                pending.append((o.case.case_id, status))
        if pending:
            detail = ", ".join(f"{cid}[{st}]" for cid, st in pending)
            reasons.append(f"零漏审：{len(pending)} 条不确定用例未完成人工确认"
                           f"（未签核/已驳回）: {detail}")
    if baseline_rate is not None:
        drop = (baseline_rate - report.success_rate) * 100
        if drop > drop_pp:
            reasons.append(f"成功率比基线下降 {drop:.1f}pp（> {drop_pp}pp）")
    return (len(reasons) == 0), reasons


# ---------- 快照落盘与报告对比（回放门禁接入主链路） ----------

def _runs_of(outcome: CaseOutcome) -> list[AgentRun]:
    """取出一次执行的全部轮次轨迹（多轮取 runs，单轮兜底 [run]）。"""
    if outcome.runs:
        return outcome.runs
    return [outcome.run] if outcome.run else []


def _last_run_of(outcome: CaseOutcome) -> Optional[AgentRun]:
    """版本 / 性能 / 报告 / 快照统一取末轮，避免首轮与逐题字段自相矛盾。"""
    runs = _runs_of(outcome)
    return runs[-1] if runs else None


def _snapshot_call(t: ToolCallRecord) -> dict:
    """快照里的工具调用：默认只写 name/args（与旧格式完全兼容），
    有返回值或调用失败时才补 result / ok 字段。"""
    d = {"name": t.name, "args": _safe_value(t.args)}
    if t.result is not None:
        d["result"] = _safe_value(t.result)
    if not t.ok:
        d["ok"] = False
    return d


def report_to_dict(report: EvalReport, gate_ok: bool | None = None,
                   gate_reasons: list[str] | None = None,
                   baseline: float | None = None) -> dict:
    """统一的报告 JSON 格式。CLI / Web 控制台 / 版本对比共用一份，避免格式漂移。

    逐题 diff 依赖这里输出 final_output / tool_calls / latency / token 全字段。
    """
    from review import review_fingerprint
    cases = []
    for o in report.outcomes:
        last = _last_run_of(o)
        cases.append({
            "id": o.case.case_id,
            "case_hash": hashlib.sha256(json.dumps(
                case_to_dict(o.case), sort_keys=True, ensure_ascii=False,
                default=str).encode("utf-8")).hexdigest(),
            "query": o.case.query,
            "severity": o.case.severity,
            "dataset": o.case.dataset,
            "route": o.case.route,
            "owner": o.case.owner,
            "passed": o.passed,
            "case_status": o.case_status,
            "pass_runs": o.pass_runs,
            "total_runs": o.total_runs,
            "flaky": o.flaky,
            "review_required": o.review_required,
            "review_reasons": o.review_reasons,
            "review_fingerprint": (review_fingerprint(o)
                                   if o.review_required else ""),
            "assertions": [
                {"kind": a.kind, "ok": a.ok, "detail": a.detail,
                 "layer": a.layer.value, "skipped": a.skipped,
                 "status": a.result_status()}
                for a in o.assertions
            ],
            "first_error": {
                "step_id": o.first_error.step_id,
                "target": o.first_error.target,
                "layer": o.first_error.layer.value,
                "detail": o.first_error.detail,
            } if o.first_error else None,
            "attribution": o.attribution,
            "final_output": (last.final_output if last else "")[:300],
            "tool_calls": [t.name for t in (last.tool_calls if last else [])],
            "tool_results": [_safe_value(t.result) for t in (last.tool_calls if last else [])],
            "retrieved": list(last.retrieved) if last else [],
            "versions": dict(last.versions) if last else {},
            "latency_ms": (last.latency_ms if last else None),
            "token_usage": dict(last.token_usage) if last else {},
        })
    d = {
        "total": report.total,
        "passed": report.passed,
        "success_rate": report.success_rate,
        "determined_total": report.determined_total,
        "undetermined": report.undetermined_total,
        "undetermined_total": report.undetermined_total,
        "undetermined_cases": [o.case.case_id for o in report.undetermined_cases()],
        "blockers_failed": [o.case.case_id for o in report.blockers_failed()],
        "blockers_undetermined": [o.case.case_id
                                  for o in report.blockers_undetermined()],
        "versions": report.versions_summary(),
        "fingerprint": report.fingerprint(),
        "perf": report.perf_summary(),
        "text_report": report.text(),
        "semantic_stats": report.semantic_stats(),
        "review_total": len(report.review_queue()),
        "review_queue": [o.case.case_id for o in report.review_queue()],
        "review_cases": [o.case.case_id for o in report.review_cases()],
        "cases": cases,
    }
    if gate_ok is not None:
        d["gate_ok"] = gate_ok
        d["gate_reasons"] = gate_reasons or []
        d["baseline"] = baseline
    return d


# ---------- 失败样本回流：让 Bad Case 变成下一次测试的输入 ----------

def collect_failures(report: EvalReport) -> list[dict]:
    """把本次失败用例加工成「候选回归样本」（不写盘，供导出/控制台预览）。

    每条含：实际轨迹 + 失败断言明细 + 建议的 judge_criteria 草稿 +
    一个可直接回灌评估集的 suggested_case。人工确认是必须环节——
    线上失败输出绝不能被直接当成标准答案。
    """
    out: list[dict] = []
    for o in report.outcomes:
        if o.passed:
            continue
        turns = _runs_of(o)
        last = turns[-1] if turns else None
        failed = [{"kind": a.kind, "status": a.result_status(), "detail": a.detail}
                  for a in o.assertions if a.result_status() == "failed"]
        kinds = {f["kind"] for f in failed}
        hints: list[str] = []
        if kinds & {"response_contains", "semantic", "tool_result_used",
                    "output_grounded_in"}:
            hints.append("回答中的事实性结论必须能在工具返回值或召回片段里找到依据，"
                         "不能编造、遗漏或答非所问")
        if kinds & {"must_call", "tool_result_used"}:
            hints.append("本轮必须真正调用所需工具并采用其返回结果，"
                         "不能只凭对话上下文推断作答")
        if kinds & {"state_after", "must_not_call"}:
            hints.append("副作用与禁用工具的行为必须符合业务规则，不能只让最终回答看起来合理")
        if o.flaky:
            hints.append("该用例复跑时结果不稳定，需保证多次执行结论一致")

        suggested = case_to_dict(o.case)
        suggested["id"] = f"REG-{o.case.case_id}"
        suggested["dataset"] = "regression"
        existing = list(suggested.get("judge_criteria") or [])
        suggested["judge_criteria"] = existing + [h for h in hints if h not in existing]
        if not suggested["judge_criteria"]:
            del suggested["judge_criteria"]

        out.append({
            "case_id": o.case.case_id,
            "query": o.case.query,
            "severity": o.case.severity,
            "dataset": o.case.dataset,
            "run_id": report.run_id,
            "env": report.env,
            "first_error": o.first_error.target if o.first_error else None,
            "attribution": o.attribution,
            "flaky": o.flaky,
            "failed_assertions": failed,
            "actual": {
                "final_output": (last.final_output if last else "")[:1000],
                "tool_calls": [
                    {"name": t.name, "args": t.args, "ok": t.ok,
                     "result": _safe_value(t.result)}
                    for r in turns for t in r.tool_calls
                ],
                "retrieved": [x for r in turns for x in (r.retrieved or [])],
                "state": _safe_value(last.state) if last else {},
                "latency_ms": last.latency_ms if last else None,
                "token_usage": dict(last.token_usage) if last else {},
            },
            "suggested_case": suggested,
            "review_hint": "人工确认 suggested_case（尤其 judge_criteria）后再回灌；"
                           "实际输出只是证据，不是标准答案",
        })
    return out


def export_failures(path: str | Path, report: EvalReport) -> int:
    """导出失败样本到 JSON 文件，返回条数。文件可反复覆盖（每次跑批一份）。"""
    items = collect_failures(report)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)


def promote_failures(failures_path: str | Path, evalset_path: str | Path,
                     ids: list[str] | None = None,
                     out_path: str | Path | None = None,
                     overwrite: bool = False) -> tuple[Path, int]:
    """把人工确认过的候选样本回灌进评估集。

    安全约束（默认不覆盖原文件）：
    - 不写回原 evalset，另存 <name>.promoted.json；显式 overwrite=True 才原地写
    - id 冲突自动加后缀避让，不静默覆盖既有用例
    - 合并结果整集跑 load_evalset 校验，半成品不许落盘
    - 写盘走临时文件 + replace，失败不留 .tmp
    """
    raw = json.loads(Path(failures_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("失败样本文件应为数组")
    picked = [x for x in raw if isinstance(x, dict) and
              (ids is None or x.get("case_id") in ids)]
    if not picked:
        raise ValueError("没有选中任何失败样本（ids 过滤后为空）")

    target = Path(evalset_path)
    existing = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
    if not isinstance(existing, list):
        raise ValueError(f"{target}: 评估集应为数组")
    seen = {str(x.get("id")) for x in existing if isinstance(x, dict)}

    added = 0
    for item in picked:
        c = dict(item.get("suggested_case") or {})
        if not c.get("id") or not c.get("query") or not c.get("severity"):
            raise ValueError(f"样本 {item.get('case_id')} 的 suggested_case 不完整，拒绝回灌")
        base = str(c["id"])
        cid, n = base, 2
        while cid in seen:
            cid = f"{base}-{n}"
            n += 1
        c["id"] = cid
        seen.add(cid)
        existing.append(c)
        added += 1

    # 整集校验：先写临时文件走 load_evalset，通过才落最终文件
    tmp = target.with_suffix(".json.promote.tmp")
    out = Path(out_path) if out_path else (
        target if overwrite else target.with_name(target.stem + ".promoted.json"))
    try:
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        load_evalset(tmp)                 # 半成品 / 重复 id / 非法断言直接抛错
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    finally:
        if tmp.exists():
            tmp.unlink()
    return out, added


def write_snapshots(path: str | Path, report: EvalReport) -> int:
    """把本次跑批的执行轨迹追加落成 JSONL 快照（回放的确定性来源）。

    每轮一条记录：query / tool_calls / retrieved / final_output / state /
    versions / error / 判定结果。返回写入条数。同一文件可多次追加（历史留档）。
    """
    records: list[dict] = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    for o in report.outcomes:
        turns = _runs_of(o)
        semantic = [
            {"kind": a.kind, "status": a.result_status(), "detail": a.detail}
            for a in o.assertions if a.kind == "semantic"
        ]
        for i, r in enumerate(turns):
            records.append({
                "ts": ts,
                "run_id": report.run_id,
                "env": report.env,
                "dataset": report.dataset or "all",
                "level": report.level,
                "case_id": o.case.case_id,
                "severity": o.case.severity,
                "case_dataset": o.case.dataset,
                "turn": f"{i + 1}/{len(turns)}",
                # 取实际生效的会话标识（run_case 写回），不再只读 context_overrides
                "session_id": o.session_id,
                "query": r.query,
                "tool_calls": [_snapshot_call(t) for t in r.tool_calls],
                # 动态字段统一收敛：datetime / set / 自定义对象 / 非法 dict key
                # 不再让裸 json.dumps 抛 TypeError 并留下半批残文件。
                # query / final_output / error 保留原值（字符串），避免截断回放证据。
                "retrieved": [str(x) for x in (r.retrieved or [])],
                "final_output": r.final_output,
                "state": _safe_value(r.state),
                "versions": _safe_value(r.versions),
                "latency_ms": r.latency_ms,
                "token_usage": _safe_value(r.token_usage),
                "error": r.error,
                "passed": o.passed,
                "case_status": o.case_status,
                "semantic": semantic if i == len(turns) - 1 else [],
            })
    # 先在内存把全部记录序列化成功，再一次追加写入：任一条失败都不会留下
    # 半批残文件（快照是回放的确定性来源）。default=str 作为最后兜底。
    payload = "".join(json.dumps(rec, ensure_ascii=False, default=str) + "\n"
                      for rec in records)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(payload)
    return len(records)


def _env_of(rep: dict) -> str:
    fp = rep.get("fingerprint") or {}
    return str(rep.get("env") or fp.get("env") or "")


def _case_diff(old_c: dict, new_c: dict) -> list[dict]:
    """单条用例的逐题变化清单。只看报告里真实存在的字段（兼容旧格式）。"""
    changes: list[dict] = []
    if old_c.get("passed") != new_c.get("passed"):
        changes.append({"field": "passed", "old": old_c.get("passed"),
                        "new": new_c.get("passed")})
    if "final_output" in old_c and "final_output" in new_c:
        a, b = old_c.get("final_output") or "", new_c.get("final_output") or ""
        if a != b:
            changes.append({"field": "final_output", "old": a[:120], "new": b[:120]})
    if "tool_calls" in old_c and "tool_calls" in new_c:
        a, b = old_c.get("tool_calls") or [], new_c.get("tool_calls") or []
        if a != b:
            changes.append({"field": "tool_calls", "old": a, "new": b})
    old_a = old_c.get("assertions") or []
    new_a = new_c.get("assertions") or []
    if old_a and new_a:
        om = {f"{x.get('kind')}#{i}": x.get("status") or ("skipped" if x.get("skipped")
              else ("passed" if x.get("ok") else "failed"))
              for i, x in enumerate(old_a)}
        nm = {f"{x.get('kind')}#{i}": x.get("status") or ("skipped" if x.get("skipped")
              else ("passed" if x.get("ok") else "failed"))
              for i, x in enumerate(new_a)}
        for k in sorted(set(om) | set(nm)):
            if om.get(k) != nm.get(k):
                changes.append({"field": f"assertion:{k}",
                                "old": om.get(k), "new": nm.get(k)})
    for field in ("latency_ms", "token_usage", "attribution"):
        if field in old_c and field in new_c and old_c.get(field) != new_c.get(field):
            changes.append({"field": field, "old": old_c.get(field),
                            "new": new_c.get(field)})
    return changes


def compare_reports(old: dict, new: dict) -> dict:
    """对比两份跑批报告 JSON，返回差异摘要 + 回放四档判定。

    相比只看通过率，这里补上三层：
    - 环境一致性（文章踩坑 1：生产/预发混跑 → 结果不可比，直接判作废）
    - 逐题 diff（同一 case 的回答 / 断言 / 工具调用到底变了什么）
    - 性能 delta（改好了但慢了、贵了，同样要能看见）

    四档判定复用 ReplayRunner 的阈值：
    |Δ|≤3pp 视为噪声本次作废；Δ≥+10pp 提升可合并；Δ≤-20pp 显著下降需排查；
    其余为保持不合并。
    """
    from replay import ReplayRunner          # 延迟导入，避免循环依赖

    old_map = {c["id"]: c for c in old.get("cases", [])}
    new_map = {c["id"]: c for c in new.get("cases", [])}
    common = sorted(set(old_map) & set(new_map))
    added_cases = sorted(new_map.keys() - old_map.keys())
    removed_cases = sorted(old_map.keys() - new_map.keys())
    changed_cases = [cid for cid in common
                     if old_map[cid].get("case_hash") != new_map[cid].get("case_hash")]
    # 旧格式报告可能两侧都没有 case_hash：None != None 为 False，会绕过
    # 「用例定义变化」检查，把不可比的两份报告判成可比（误放行）。
    # 只处理「两侧都缺」；一侧有一侧没有由 changed_cases（hash 不等）覆盖。
    missing_fingerprints = bool(common) and all(
        not old_map[cid].get("case_hash") and not new_map[cid].get("case_hash")
        for cid in common)
    evalset_mismatch = bool(added_cases or removed_cases or changed_cases
                            or missing_fingerprints)

    newly_failing = [cid for cid in common
                     if old_map[cid].get("passed") and not new_map[cid].get("passed")]
    newly_fixed = [cid for cid in common
                   if not old_map[cid].get("passed") and new_map[cid].get("passed")]
    flaky_worse = [cid for cid in common
                   if new_map[cid].get("flaky") and not old_map[cid].get("flaky")]
    flaky_fixed = [cid for cid in common
                   if old_map[cid].get("flaky") and not new_map[cid].get("flaky")]

    blockers_new_fail = [cid for cid in newly_failing
                         if new_map[cid].get("severity") == "blocker"]

    case_diffs = []
    for cid in common:
        ch = _case_diff(old_map[cid], new_map[cid])
        if ch:
            case_diffs.append({"id": cid, "changes": ch})

    old_env, new_env = _env_of(old), _env_of(new)
    env_mismatch = bool(old_env and new_env and old_env != new_env)

    old_perf = old.get("perf") or {}
    new_perf = new.get("perf") or {}
    perf_delta: dict = {}
    for k in ("avg_latency_ms", "max_latency_ms", "total_tokens"):
        a, b = old_perf.get(k), new_perf.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            perf_delta[k] = {"old": a, "new": b, "delta": round(b - a, 1)}

    delta = (new.get("success_rate") or 0.0) - (old.get("success_rate") or 0.0)
    rr = ReplayRunner(agent_fn=lambda s: 0.0)     # 只为复用阈值
    if env_mismatch:
        # 环境不一致时任何差值都不可信，优先于其他所有判定
        verdict = f"invalid｜环境不一致（{old_env} → {new_env}），两份报告不可比较"
    elif evalset_mismatch:
        why = "缺少用例定义指纹，无法确认两份报告用的是同一套评估集" if missing_fingerprints \
            else "用例新增、移除或定义变化"
        verdict = f"invalid｜评估集不一致（{why}），通过率不可比较"
    elif abs(delta) <= rr.NOISE_BAND:
        verdict = "invalid｜噪声带内，无法归因，本次作废（复跑或加样本）"
    elif delta >= rr.PROMOTE_GAIN:
        verdict = "promote｜提升可合并"
    elif delta <= -rr.INVESTIGATE_DROP:
        verdict = "investigate｜显著下降需排查"
    else:
        verdict = "hold｜保持不合并，等更多证据"
    if blockers_new_fail and not (env_mismatch or evalset_mismatch):
        verdict = f"investigate｜显著下降需排查（blocker 新增失败: {blockers_new_fail}）"

    return {
        "old_rate": old.get("success_rate"),
        "new_rate": new.get("success_rate"),
        "delta_pp": round(delta * 100, 1),
        "newly_failing": newly_failing,
        "newly_fixed": newly_fixed,
        "flaky_worse": flaky_worse,
        "flaky_fixed": flaky_fixed,
        "added_cases": added_cases,
        "removed_cases": removed_cases,
        "changed_cases": changed_cases,
        "evalset_mismatch": evalset_mismatch,
        "missing_fingerprints": missing_fingerprints,
        "case_diffs": case_diffs,
        "perf_delta": perf_delta,
        "env_mismatch": env_mismatch,
        "old_env": old_env, "new_env": new_env,
        "verdict": verdict,
    }


def format_compare(diff: dict) -> str:
    """把 compare_reports 的结果渲染成可读文本。"""
    lines = ["-" * 62, "与上次报告对比:"]
    old_r, new_r = diff["old_rate"], diff["new_rate"]
    if old_r is not None and new_r is not None:
        lines.append(f"  成功率: {old_r:.1%} → {new_r:.1%} ({diff['delta_pp']:+.1f}pp)")
    if diff.get("env_mismatch"):
        lines.append(f"  ⛔ 环境不一致: {diff['old_env']} → {diff['new_env']}（结果不可比较）")
    for label, key in [("新增失败", "newly_failing"), ("修复", "newly_fixed"),
                       ("flaky 恶化", "flaky_worse"), ("flaky 消除", "flaky_fixed"),
                       ("新增用例", "added_cases"), ("移除用例", "removed_cases"),
                       ("用例定义或指纹变化", "changed_cases")]:
        if diff.get(key):
            lines.append(f"  {label}: {diff[key]}")
    cd = diff.get("case_diffs") or []
    if cd:
        lines.append(f"  逐题变化 {len(cd)} 条:")
        for item in cd[:8]:
            fields = ", ".join(f"{c['field']}: {c['old']!r} → {c['new']!r}"
                               for c in item["changes"][:3])
            lines.append(f"    {item['id']}: {fields}")
        if len(cd) > 8:
            lines.append(f"    … 另有 {len(cd) - 8} 条")
    pd = diff.get("perf_delta") or {}
    if pd:
        label = {"avg_latency_ms": "平均耗时(ms)",
                 "max_latency_ms": "最大耗时(ms)", "total_tokens": "Token 合计"}
        lines.append("  性能变化: " + " ｜ ".join(
            f"{label.get(k, k)} {v['old']} → {v['new']} ({v['delta']:+})"
            for k, v in pd.items()))
    lines.append(f"  回放判定: {diff['verdict']}")
    return "\n".join(lines)
