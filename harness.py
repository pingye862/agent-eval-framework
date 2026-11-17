"""测试执行器（harness）：把被测 Agent 接进来的那一层。

你的项目只需要做一件事：实现 AgentAdapter，把每次调用包装成 AgentRun。
其余的（P1 断言、批量执行、失败归因、门禁判定、报告）都由这里负责。

对应方案：4.5 断言三类型 / 6 分层执行 / 7.1 合并门禁 / 方法二 4.8 链路第一错。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from attribution import FirstErrorFinder, TraceStep
from evalset import EvalCase
from models import AttributionLayer


# ---------- 你的 Agent 要返回的东西 ----------

@dataclass
class ToolCallRecord:
    name: str
    args: dict = field(default_factory=dict)


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


class AgentAdapter(ABC):
    """接入协议：你的项目实现这一个类，就完成了全部接入。"""

    @abstractmethod
    def run(self, query: str, context: dict) -> AgentRun:
        ...


# ---------- P1 断言器 ----------

@dataclass
class AssertionResult:
    kind: str            # must_call / must_not_call / response_contains / state_after / no_error
    ok: bool
    detail: str
    layer: AttributionLayer = AttributionLayer.TRAJECTORY


def assert_case(case: EvalCase, run: AgentRun) -> list[AssertionResult]:
    """对一次运行执行全部 P1 断言。失败会带中文 detail，直接进报告。"""
    results: list[AssertionResult] = []

    # Agent 自己崩了，后面不用看
    if run.error:
        results.append(AssertionResult("no_error", False, f"Agent 抛异常: {run.error}",
                                       AttributionLayer.SYSTEM))
        return results
    results.append(AssertionResult("no_error", True, ""))

    called = [t.name for t in run.tool_calls]

    for req in case.must_call:
        tool = req.get("tool", "")
        ok = tool in called
        detail = ""
        if not ok:
            detail = f"缺少工具调用: {tool}"
        else:
            # 参数断言：required_args 里的键必须存在（值正确性属 P2）
            missing = [a for a in req.get("required_args", [])
                       if a not in next((t.args for t in run.tool_calls if t.name == tool), {})]
            if missing:
                ok = False
                detail = f"工具 {tool} 缺少参数: {missing}"
        results.append(AssertionResult("must_call", ok, detail))

    for req in case.must_not_call:
        tool = req.get("tool", "")
        ok = tool not in called
        results.append(AssertionResult(
            "must_not_call", ok,
            "" if ok else f"出现禁止调用: {tool}"))

    for kw in case.response_contains:
        ok = kw in (run.final_output or "")
        results.append(AssertionResult(
            "response_contains", ok,
            "" if ok else f"输出缺少关键词: {kw}（实际: {run.final_output[:60]!r}）"))

    for item in case.state_after:
        path = item.get("path", "")
        actual = run.state
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

    @property
    def flaky(self) -> bool:
        """有通过有失败 —— 非确定性信号，进报告单独列示。"""
        return 0 < self.pass_runs < self.total_runs

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

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def success_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def blockers_failed(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if not o.passed and o.case.severity == "blocker"]

    def flaky_cases(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.flaky]

    def versions_summary(self) -> dict:
        """版本四要素汇总（取最后一次记录到的值），供报告留档与版本对比。"""
        summary: dict = {}
        for o in self.outcomes:
            if o.run and o.run.versions:
                summary.update(o.run.versions)
        return summary

    def text(self) -> str:
        lines = [
            f"级别: {self.level} ｜ 用例: {self.total} ｜ 通过: {self.passed} ｜ "
            f"成功率: {self.success_rate:.1%}"
            + (f" ｜ 每条复跑 {self.repeat} 次" if self.repeat > 1 else "")
        ]
        flaky = self.flaky_cases()
        if flaky:
            lines.append(f"⚠ 非确定性用例 {len(flaky)} 条: "
                         f"{[o.case.case_id for o in flaky]}")
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

    def __init__(self, adapter: AgentAdapter) -> None:
        self.adapter = adapter

    def run_case(self, case: EvalCase, context: dict | None = None) -> CaseOutcome:
        merged = {**(context or {}), **case.context_overrides}
        run = self.adapter.run(case.query, merged)
        results = assert_case(case, run)
        passed = all(r.ok for r in results)

        first_error, attribution = None, None
        if not passed:
            steps = [
                TraceStep(step_id=f"{i}_{r.kind}", method="rule_check",
                          target=r.kind, ok=r.ok, detail=r.detail, layer=r.layer)
                for i, r in enumerate(results)
            ]
            first_error = FirstErrorFinder(steps).find()
            if first_error:
                attribution = first_error.layer.value
        return CaseOutcome(case, run, results, passed, first_error, attribution)

    def _run_repeats(self, case: EvalCase, repeat: int,
                     context: dict | None) -> CaseOutcome:
        """复跑聚合：pass^k 口径（全过才算过），断言明细取第一次运行。"""
        outcomes = [self.run_case(case, context) for _ in range(repeat)]
        pass_runs = sum(1 for o in outcomes if o.passed)
        base = outcomes[0]
        return CaseOutcome(
            case=base.case, run=base.run, assertions=base.assertions,
            passed=(pass_runs == repeat),
            first_error=base.first_error, attribution=base.attribution,
            pass_runs=pass_runs, total_runs=repeat,
        )

    def run_all(self, cases: list[EvalCase], level: str = "L0",
                context: dict | None = None,
                repeat: int = 1, max_workers: int = 1) -> EvalReport:
        if repeat < 1 or max_workers < 1:
            raise ValueError("repeat 与 max_workers 必须 ≥ 1")
        if repeat == 1 and max_workers == 1:
            outcomes = [self.run_case(c, context) for c in cases]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [(i, pool.submit(self._run_repeats, c, repeat, context))
                           for i, c in enumerate(cases)]
                outcomes: list[Optional[CaseOutcome]] = [None] * len(cases)
                for i, fut in futures:
                    outcomes[i] = fut.result()
        return EvalReport(level=level, outcomes=outcomes, repeat=repeat)


# ---------- 门禁判定（对应方案 7.1，baseline 由调用方传入） ----------

def gate_decision(report: EvalReport, baseline_rate: Optional[float] = None,
                  drop_pp: float = 2.0) -> tuple[bool, list[str]]:
    """返回 (是否放行, 触发的规则列表)。blocker 一票否决，不参与百分比稀释。"""
    reasons: list[str] = []
    blocked = report.blockers_failed()
    if blocked:
        reasons.append(f"blocker 样本失败 {len(blocked)} 条: "
                       f"{[o.case.case_id for o in blocked]}")
    if baseline_rate is not None:
        drop = (baseline_rate - report.success_rate) * 100
        if drop > drop_pp:
            reasons.append(f"成功率比基线下降 {drop:.1f}pp（> {drop_pp}pp）")
    return (len(reasons) == 0), reasons
