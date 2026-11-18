"""测试执行器（harness）：把被测 Agent 接进来的那一层。

你的项目只需要做一件事：实现 AgentAdapter，把每次调用包装成 AgentRun。
其余的（P1 断言、批量执行、失败归因、门禁判定、报告）都由这里负责。

能力覆盖：断言三类型 / 分层执行 / 合并门禁 / 链路第一错归因。
"""
from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from attribution import FirstErrorFinder, TraceStep
from evalset import EvalCase
from models import AttributionLayer


def _import_judge():
    """延迟导入 judge（可选模块：无 LLM 时框架照常跑 P1）。"""
    try:
        import judge as _j
        return _j
    except ImportError:
        return None


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


# ---------- P1 断言器 ----------

@dataclass
class AssertionResult:
    kind: str            # must_call / must_not_call / response_contains / state_after / no_error / semantic
    ok: bool
    detail: str
    layer: AttributionLayer = AttributionLayer.TRAJECTORY
    skipped: bool = False   # True = 未参与判定（如 judge 未校准被跳过），不影响 passed


def assert_case(case: EvalCase, runs: AgentRun | list[AgentRun]) -> list[AssertionResult]:
    """对一次（或多轮）运行执行全部 P1 断言。失败会带中文 detail，直接进报告。

    多轮（runs 为列表且 >1 条）时的断言语义：
    - no_error: 每一轮都不能抛异常
    - must_call / must_not_call: 看全程工具调用的并集（"整个会话里必须/禁止调用过"）
    - response_contains（案例级）: 看最后一轮的最终输出（"聊完之后要给出…"）
    - turns[i].response_contains（轮次级）: 看第 i+1 轮当轮输出
    - state_after: 看最后一轮结束后的系统状态
    """
    runs = [runs] if isinstance(runs, AgentRun) else list(runs)
    multi = len(runs) > 1
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
    results.append(AssertionResult("no_error", True, ""))

    # 多轮：全程并集；单轮：本条轨迹
    if multi:
        called: list[str] = []
        args_by_tool: dict[str, dict] = {}
        for r in runs:
            for t in r.tool_calls:
                if t.name not in called:
                    called.append(t.name)
                args_by_tool.setdefault(t.name, {}).update(t.args or {})
    else:
        called = [t.name for t in runs[0].tool_calls]
        args_by_tool = {t.name: (t.args or {}) for t in runs[0].tool_calls}
    final_run = runs[-1]

    for req in case.must_call:
        tool = req.get("tool", "")
        ok = tool in called
        detail = ""
        if not ok:
            detail = f"缺少工具调用: {tool}"
        else:
            # 参数断言：required_args 里的键必须存在（值正确性属 P2）
            missing = [a for a in req.get("required_args", [])
                       if a not in args_by_tool.get(tool, {})]
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
        ok = kw in (final_run.final_output or "")
        results.append(AssertionResult(
            "response_contains", ok,
            "" if ok else f"输出缺少关键词: {kw}（实际: {final_run.final_output[:60]!r}）"))

    # 轮次级断言：turns[i] 对应 runs[i+1]（runs[0] 即 case.query 是第 1 轮）
    if multi:
        for j, turn_spec in enumerate(case.turns):
            turn_run = runs[j + 1] if j + 1 < len(runs) else final_run
            for kw in turn_spec.get("response_contains", []):
                ok = kw in (turn_run.final_output or "")
                results.append(AssertionResult(
                    "response_contains", ok,
                    "" if ok else f"第 {j + 2} 轮输出缺少关键词: {kw}"
                                  f"（实际: {turn_run.final_output[:60]!r}）"))

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

    def semantic_stats(self) -> dict:
        """P2 语义判分汇总: judged(实判) / failed / skipped(跳过) / need_review(转人工)。"""
        stats = {"judged": 0, "failed": 0, "skipped": 0, "need_review": 0}
        for o in self.outcomes:
            for a in o.assertions:
                if a.kind != "semantic":
                    continue
                if a.skipped:
                    stats["skipped"] += 1
                elif not a.ok:
                    stats["failed"] += 1
                    stats["judged"] += 1
                elif "证据不足转人工" in a.detail:
                    stats["need_review"] += 1
                    stats["judged"] += 1
                else:
                    stats["judged"] += 1
        return stats

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
        sem = self.semantic_stats()
        if any(o.case.judge_criteria for o in self.outcomes):
            lines.append(f"P2 语义判分: 实判 {sem['judged']} / 失败 {sem['failed']} / "
                         f"转人工 {sem['need_review']} / 跳过 {sem['skipped']}")
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
        all_runs: list[AgentRun]
        if case.turns:
            # 多轮：case.query 是第 1 轮，case.turns 是第 2 轮起的后续轮次
            turn_specs = [{"query": case.query}, *case.turns]
            all_runs = self.adapter.run_turns(turn_specs, merged)
        else:
            all_runs = [self.adapter.run(case.query, merged)]
        results = assert_case(case, all_runs)
        results += self._semantic_assertions(case, all_runs, level)
        passed = all(r.ok for r in results if not r.skipped)

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
        return CaseOutcome(case, all_runs[0], results, passed, first_error, attribution,
                           runs=(all_runs if len(all_runs) > 1 else None))

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
                                    AttributionLayer.COMPONENT, skipped=True)]
        final_output = runs[-1].final_output
        r = self.judge.judge(case.query, case.judge_criteria, final_output)
        verdict = r.get("verdict", "need_review")
        evidence = r.get("evidence", [])
        if verdict == "pass":
            return [AssertionResult("semantic", True, "", AttributionLayer.COMPONENT)]
        if verdict == "need_review":
            return [AssertionResult("semantic", True,
                                    f"judge 证据不足转人工: {'; '.join(evidence[:3])}",
                                    AttributionLayer.COMPONENT, skipped=True)]
        return [AssertionResult("semantic", False,
                                f"语义判分不达标: {'; '.join(evidence[:3])}",
                                AttributionLayer.COMPONENT)]

    def _run_repeats(self, case: EvalCase, repeat: int,
                     context: dict | None, level: str = "L0") -> CaseOutcome:
        """复跑聚合：pass^k 口径（全过才算过），断言明细取第一次运行。"""
        outcomes = [self.run_case(case, context, level=level) for _ in range(repeat)]
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
            outcomes = [self.run_case(c, context, level=level) for c in cases]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [(i, pool.submit(self._run_repeats, c, repeat, context, level))
                           for i, c in enumerate(cases)]
                outcomes: list[Optional[CaseOutcome]] = [None] * len(cases)
                for i, fut in futures:
                    outcomes[i] = fut.result()
        return EvalReport(level=level, outcomes=outcomes, repeat=repeat)


# ---------- 门禁判定（baseline 由调用方传入） ----------

def gate_decision(report: EvalReport, baseline_rate: Optional[float] = None,
                  drop_pp: float = 2.0) -> tuple[bool, list[str]]:
    """返回 (是否放行, 触发的规则列表)。blocker 一票否决，不参与百分比稀释。

    P2 语义断言失败的 blocker 用例同样一票否决（语义失败已在 passed 里，无需特判）。
    """
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


# ---------- 快照落盘与报告对比（回放门禁接入主链路） ----------

def _runs_of(outcome: CaseOutcome) -> list[AgentRun]:
    """取出一次执行的全部轮次轨迹（多轮取 runs，单轮兜底 [run]）。"""
    if outcome.runs:
        return outcome.runs
    return [outcome.run] if outcome.run else []


def write_snapshots(path: str | Path, report: EvalReport) -> int:
    """把本次跑批的执行轨迹追加落成 JSONL 快照（回放的确定性来源）。

    每轮一条记录：query / tool_calls / retrieved / final_output / state /
    versions / error / 判定结果。返回写入条数。同一文件可多次追加（历史留档）。
    """
    records: list[dict] = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    for o in report.outcomes:
        turns = _runs_of(o)
        for i, r in enumerate(turns):
            records.append({
                "ts": ts,
                "level": report.level,
                "case_id": o.case.case_id,
                "severity": o.case.severity,
                "turn": f"{i + 1}/{len(turns)}",
                "session_id": (o.case.context_overrides or {}).get("session_id", ""),
                "query": r.query,
                "tool_calls": [{"name": t.name, "args": t.args} for t in r.tool_calls],
                "retrieved": r.retrieved,
                "final_output": r.final_output,
                "state": r.state,
                "versions": r.versions,
                "error": r.error,
                "passed": o.passed,
            })
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(records)


def compare_reports(old: dict, new: dict) -> dict:
    """对比两份跑批报告 JSON，返回差异摘要 + 回放四档判定。

    四档判定复用 ReplayRunner 的阈值：
    |Δ|≤3pp 视为噪声本次作废；Δ≥+10pp 提升可合并；Δ≤-20pp 显著下降需排查；
    其余为保持不合并。
    """
    from replay import ReplayRunner          # 延迟导入，避免循环依赖

    old_map = {c["id"]: c for c in old.get("cases", [])}
    new_map = {c["id"]: c for c in new.get("cases", [])}
    common = sorted(set(old_map) & set(new_map))

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

    delta = (new.get("success_rate") or 0.0) - (old.get("success_rate") or 0.0)
    rr = ReplayRunner(agent_fn=lambda s: 0.0)     # 只为复用阈值
    if abs(delta) <= rr.NOISE_BAND:
        verdict = "invalid｜噪声带内，无法归因，本次作废（复跑或加样本）"
    elif delta >= rr.PROMOTE_GAIN:
        verdict = "promote｜提升可合并"
    elif delta <= -rr.INVESTIGATE_DROP:
        verdict = "investigate｜显著下降需排查"
    else:
        verdict = "hold｜保持不合并，等更多证据"
    if blockers_new_fail:
        verdict = f"investigate｜显著下降需排查（blocker 新增失败: {blockers_new_fail}）"

    return {
        "old_rate": old.get("success_rate"),
        "new_rate": new.get("success_rate"),
        "delta_pp": round(delta * 100, 1),
        "newly_failing": newly_failing,
        "newly_fixed": newly_fixed,
        "flaky_worse": flaky_worse,
        "flaky_fixed": flaky_fixed,
        "added_cases": sorted(set(new_map) - set(old_map)),
        "removed_cases": sorted(set(old_map) - set(new_map)),
        "verdict": verdict,
    }


def format_compare(diff: dict) -> str:
    """把 compare_reports 的结果渲染成可读文本。"""
    lines = ["-" * 62, "与上次报告对比:"]
    old_r, new_r = diff["old_rate"], diff["new_rate"]
    if old_r is not None and new_r is not None:
        lines.append(f"  成功率: {old_r:.1%} → {new_r:.1%} ({diff['delta_pp']:+.1f}pp)")
    for label, key in [("新增失败", "newly_failing"), ("修复", "newly_fixed"),
                       ("flaky 恶化", "flaky_worse"), ("flaky 消除", "flaky_fixed"),
                       ("新增用例", "added_cases"), ("移除用例", "removed_cases")]:
        if diff[key]:
            lines.append(f"  {label}: {diff[key]}")
    lines.append(f"  回放判定: {diff['verdict']}")
    return "\n".join(lines)
