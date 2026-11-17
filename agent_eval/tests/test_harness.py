"""harness 单测：P1 断言、复跑聚合（pass^k / flaky）、并发顺序、门禁判定。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import (AgentAdapter, AgentRun, BatchRunner, ToolCallRecord,
                     gate_decision)
from evalset import EvalCase


class StubAdapter(AgentAdapter):
    """可编程的假被测对象：按 (query, 调用序号) 决定行为。"""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = 0

    def run(self, query: str, context: dict) -> AgentRun:
        self.calls += 1
        behavior = self.script.get(query, {"output": "ok", "tools": []})
        if "error" in behavior:
            return AgentRun(query=query, final_output="", error=behavior["error"])
        return AgentRun(
            query=query,
            final_output=behavior.get("output", "ok"),
            tool_calls=[ToolCallRecord(name=t) for t in behavior.get("tools", [])],
        )


def _case(cid="c1", **kw):
    query = kw.pop("query", "q")
    severity = kw.pop("severity", "major")
    return EvalCase(case_id=cid, query=query, severity=severity, **kw)


# ---------- P1 断言 ----------

def test_must_call_pass_and_fail():
    a = StubAdapter({"q": {"output": "好的", "tools": ["run_sql"]}})
    ok = BatchRunner(a).run_case(_case(must_call=[{"tool": "run_sql"}]))
    assert ok.passed
    bad = BatchRunner(a).run_case(_case(must_call=[{"tool": "other_tool"}]))
    assert not bad.passed
    assert any("缺少工具调用" in d for d in bad.failed_details())


def test_must_call_required_args_missing():
    """required_args 查键存在性：工具调了但缺参数也算失败。"""
    class AdapterWithArgs(AgentAdapter):
        def run(self, query, context):
            return AgentRun(query=query, final_output="x",
                            tool_calls=[ToolCallRecord("run_sql", {"table": "t"})])
    r = BatchRunner(AdapterWithArgs()).run_case(
        _case(must_call=[{"tool": "run_sql", "required_args": ["sql"]}]))
    assert not r.passed
    assert any("缺少参数" in d for d in r.failed_details())


def test_agent_error_short_circuits():
    """Agent 抛异常：只产生 no_error 一条失败断言（system 层），其余断言短路。"""
    a = StubAdapter({"q": {"error": "boom"}})
    r = BatchRunner(a).run_case(
        _case(response_contains=["答案"], must_call=[{"tool": "x"}]))
    assert not r.passed
    assert [x.kind for x in r.assertions] == ["no_error"]
    assert r.attribution == "system"


def test_must_not_call_and_contains():
    a = StubAdapter({"q": {"output": "含答案关键词", "tools": ["delete_record"]}})
    r = BatchRunner(a).run_case(
        _case(response_contains=["答案"], must_not_call=[{"tool": "delete_record"}]))
    assert not r.passed
    assert sum(1 for x in r.assertions if not x.ok) == 1   # 只有 must_not_call 挂


def test_state_after_value_and_min_length():
    class Stateful(AgentAdapter):
        def run(self, query, context):
            return AgentRun(query=query, final_output="done", state={"log": ["a", "b"]})
    ok = BatchRunner(Stateful()).run_case(
        _case(state_after=[{"path": "log", "min_length": 2}]))
    assert ok.passed
    bad = BatchRunner(Stateful()).run_case(
        _case(state_after=[{"path": "log", "value": ["a"]}]))
    assert not bad.passed


# ---------- 复跑聚合：pass^k / flaky ----------

def test_repeat_all_pass():
    a = StubAdapter({"q": {"output": "ok"}})
    r = BatchRunner(a).run_all([_case()], repeat=3)
    assert r.outcomes[0].passed and r.outcomes[0].pass_runs == 3
    assert not r.outcomes[0].flaky


def test_repeat_flaky_detected():
    """第1次过、第2次挂 → pass^k 判失败 + flaky 标记。"""
    a = StubAdapter()
    results = iter([True, False])
    orig = a.run

    def flaky_run(query, context):
        a.calls += 1
        ok = next(results)
        return AgentRun(query=query, final_output="ok" if ok else "")

    a.run = flaky_run
    r = BatchRunner(a).run_all([_case(response_contains=["ok"])], repeat=2)
    o = r.outcomes[0]
    assert not o.passed and o.pass_runs == 1 and o.total_runs == 2
    assert o.flaky
    assert "flaky" in r.text()
    assert len(r.flaky_cases()) == 1


def test_repeat_baseline_not_diluted():
    """成功率按用例数（pass^k）计，不按复跑次数稀释：4 条 1 失败 → 75%。"""
    a = StubAdapter()
    calls = {"n": 0}

    def run(query, context):
        calls["n"] += 1
        return AgentRun(query=query, final_output="" if calls["n"] == 5 else "ok")

    a.run = run
    cases = [_case(f"c{i}", response_contains=["ok"]) for i in range(4)]
    r = BatchRunner(a).run_all(cases, repeat=2)   # 第5次调用挂 → 某条 fail 一次
    assert r.passed == 3 and abs(r.success_rate - 0.75) < 1e-9
    assert len(r.flaky_cases()) == 1


# ---------- 并发 ----------

def test_concurrent_order_preserved():
    a = StubAdapter({f"q{i}": {"output": f"out{i}"} for i in range(8)})
    cases = [_case(f"c{i}", query=f"q{i}") for i in range(8)]
    r = BatchRunner(a).run_all(cases, max_workers=4)
    assert [o.case.case_id for o in r.outcomes] == [f"c{i}" for i in range(8)]
    assert r.passed == 8


def test_invalid_params_rejected():
    try:
        BatchRunner(StubAdapter()).run_all([_case()], repeat=0)
        assert False, "repeat=0 应抛错"
    except ValueError:
        pass


# ---------- 门禁 ----------

def test_gate_blocker_one_vote_veto():
    """blocker 失败一票否决，即使总体成功率仍高。"""
    a = StubAdapter({"q": {"output": "正常输出"}, "bad": {"output": "不包含关键词"}})
    cases = [_case("b1", severity="blocker", response_contains=["要求词"]),
             _case("m1", query="q")]
    r = BatchRunner(a).run_all(cases)
    ok, reasons = gate_decision(r, baseline_rate=1.0)
    assert not ok
    assert any("blocker" in x for x in reasons)


def test_gate_baseline_drop():
    """成功率回落超过 drop_pp 拦截：75% vs 基线 100% → 拦；基线 75% → 放行。"""
    a = StubAdapter({"q": {"output": "包含答案"}, "bad": {"output": "不含"}})
    r = BatchRunner(a).run_all([_case("m1", response_contains=["答案"]),
                                _case("m2", response_contains=["答案"]),
                                _case("m3", response_contains=["答案"]),
                                _case("bad", query="bad", response_contains=["答案"])])
    ok, reasons = gate_decision(r, baseline_rate=1.0)
    assert not ok and any("下降" in x for x in reasons)
    ok2, _ = gate_decision(r, baseline_rate=0.75)        # 持平基线 → 放行
    assert ok2
