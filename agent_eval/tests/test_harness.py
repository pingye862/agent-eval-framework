"""harness 单测：P1 断言、复跑聚合（pass^k / flaky）、并发顺序、门禁判定、多轮对话、快照与对比。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import (AgentAdapter, AgentRun, BatchRunner, ToolCallRecord,
                     assert_case, gate_decision, write_snapshots, compare_reports,
                     format_compare)
from evalset import EvalCase, load_evalset


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


# ---------- 归因口径：第一错（链路顺序）≠ 主归因层（优先级） ----------

def test_attribution_priority_over_first_error():
    """must_call(trajectory) 与 state_after(system) 同时挂：
    链路第一错是 must_call，但主归因层按 system > trajectory 优先级取 system。
    系统层是上游，修好它，下游轨迹错往往跟着消失。"""
    class Adapter(AgentAdapter):
        def run(self, query, context):
            return AgentRun(query=query, final_output="done",
                            state={"log": []})

    r = BatchRunner(Adapter()).run_case(
        _case(must_call=[{"tool": "run_sql"}],
              state_after=[{"path": "log", "min_length": 1}]))
    assert not r.passed
    assert r.first_error.target == "must_call"          # 链路第一错：定位用
    assert r.attribution == "system"                    # 主归因层：定责用


def test_attribution_single_layer_no_promotion():
    """只有 trajectory 层挂时，主归因就是 trajectory（不误抬到 system）。"""
    a = StubAdapter({"q": {"output": "好的", "tools": []}})
    r = BatchRunner(a).run_case(_case(must_call=[{"tool": "run_sql"}]))
    assert not r.passed
    assert r.attribution == "trajectory"


def test_attribution_semantic_fail_is_component():
    """语义断言失败归 component 层（判分对象是提示词与回答行为）。"""
    class FakeJudge:
        calibrated = True

        def judge(self, query, criteria, output):
            return {"verdict": "fail", "evidence": ["答非所问"]}

    a = StubAdapter({"q": {"output": "不知道"}})
    r = BatchRunner(a, judge=FakeJudge()).run_case(
        _case(judge_criteria=["必须回答商品数量"]), level="L1")
    assert not r.passed
    assert r.attribution == "component"


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


# ---------- 多轮对话 ----------

class TurnAdapter(AgentAdapter):
    """带会话记忆的假被测对象：记住同一 session 的历史轮次。"""

    def __init__(self, reply_by_turn=None):
        self.reply_by_turn = reply_by_turn or {}
        self.sessions: dict[str, list[str]] = {}
        self.seen_contexts: list[dict] = []

    def run(self, query: str, context: dict) -> AgentRun:
        sid = context.get("session_id", "default")
        idx = context.get("turn_index", 0)
        self.seen_contexts.append(dict(context))
        self.sessions.setdefault(sid, []).append(query)
        tools = self.reply_by_turn.get(idx, {}).get("tools", [])
        output = self.reply_by_turn.get(idx, {}).get("output", "好的")
        return AgentRun(query=query, final_output=output,
                        tool_calls=[ToolCallRecord(t, {}) for t in tools])


def _turn_case(**kw):
    kw.setdefault("case_id", "mt1")
    turns = kw.pop("turns")
    return EvalCase(case_id=kw.pop("case_id"), query=kw.pop("query"),
                    severity="major", turns=turns, **kw)


def test_multiturn_session_shared():
    """多轮用例：各轮共享同一 session_id，adapter 收到 turn_index。"""
    a = TurnAdapter()
    case = _turn_case(query="查销量", turns=[{"query": "再查库存"}])
    r = BatchRunner(a).run_all([case])
    assert r.passed == 1
    assert len(a.seen_contexts) == 2
    assert a.seen_contexts[0]["session_id"] == a.seen_contexts[1]["session_id"]
    assert a.seen_contexts[0]["turn_index"] == 0
    assert a.seen_contexts[1]["turn_index"] == 1
    assert a.seen_contexts[1]["is_last_turn"] is True


def test_multiturn_turn_level_contains():
    """轮次级 response_contains：断言落在对应轮次的输出上。"""
    a = TurnAdapter(reply_by_turn={
        0: {"output": "已为您查询，请问还需要什么"},
        1: {"output": "2024 年销量 130 台"},
    })
    case = _turn_case(query="查销量", turns=[
        {"query": "补充：2024 年", "response_contains": ["130"]},
    ])
    r = BatchRunner(a).run_all([case])
    assert r.outcomes[0].passed


def test_multiturn_turn_level_fail_message():
    """轮次级断言失败时，报错信息标注轮次。"""
    a = TurnAdapter(reply_by_turn={0: {"output": "第一轮回复"}, 1: {"output": "答非所问"}})
    case = _turn_case(query="查销量", turns=[
        {"query": "补充", "response_contains": ["130"]},
    ])
    r = BatchRunner(a).run_all([case])
    o = r.outcomes[0]
    assert not o.passed
    assert any("第 2 轮" in d for d in o.failed_details())


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("scope", ["single", "session", "turn"])
def test_required_args_must_be_present_on_one_call(complete, scope):
    matching = ToolCallRecord("run_sql", {"sql": None, **({"table": "t"} if complete else {})})
    partial = ToolCallRecord("run_sql", {"table": "t"})
    spec = {"must_call": [{"tool": "run_sql", "required_args": ["sql", "table"]}]}
    if scope == "single":
        case = _case(**spec)
        runs = [AgentRun("q", "ok", [matching, partial])]
    elif scope == "session":
        case = _case(turns=[{"query": "补充"}], **spec)
        runs = [AgentRun("q", "ok", [matching]), AgentRun("补充", "ok", [partial])]
    else:
        case = _case(turns=[{"query": "补充", **spec}])
        runs = [AgentRun("q", "ok"), AgentRun("补充", "ok", [matching, partial])]
    (result,) = [a for a in assert_case(case, runs) if a.kind == "must_call"]
    assert result.ok is complete
    if not complete:
        assert "缺少参数" in result.detail
        if scope == "turn":
            assert "第 2 轮" in result.detail


def test_multiturn_tool_assertions_only_inspect_their_own_turn():
    a = TurnAdapter(reply_by_turn={0: {"tools": ["lookup"]}, 1: {"tools": ["save"]}})
    case = _turn_case(query="查询", turns=[{
        "query": "补充", "must_call": [{"tool": "lookup"}, {"tool": "save"}],
        "must_not_call": [{"tool": "lookup"}, {"tool": "save"}],
    }])
    outcome = BatchRunner(a).run_case(case)
    actual = [(a.kind, a.ok) for a in outcome.assertions if a.kind.startswith("must_")]
    assert actual == [("must_call", False), ("must_call", True),
                      ("must_not_call", True), ("must_not_call", False)]
    assert len(outcome.failed_details()) == 2
    assert all("第 2 轮" in d for d in outcome.failed_details())


@pytest.mark.parametrize("count", [0, 1, 2])
def test_multiturn_missing_run_fails_without_falling_back(count):
    case = _case(turns=[{"query": "补充"}, {
        "query": "确认", "response_contains": ["已查到"],
        "must_call": [{"tool": "lookup"}], "must_not_call": [{"tool": "save"}],
    }])
    runs = [AgentRun(q, "已查到", [ToolCallRecord("lookup")]) for q in ("q", "补充")][:count]
    (result,) = assert_case(case, runs)
    assert not result.ok and result.kind == "no_error"
    assert f"第 {count + 1} 轮" in result.detail


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(("spec", "bad_field"), [
    ({"must_call": "lookup"}, "must_call"),
    ({"must_call": [{}]}, "must_call[0]"),
    ({"must_not_call": [{"tool": 42}]}, "must_not_call[0]"),
    ({"must_call": [{"tool": "lookup", "required_args": "sql"}]}, "must_call[0].required_args"),
    ({"must_call": [{"tool": "lookup", "required_args": [" "]}]}, "must_call[0].required_args"),
    ({"response_contains": [False]}, "response_contains[0]"),
])
def test_evalset_validates_assertions_at_both_scopes(tmp_path, nested, spec, bad_field):
    item = {"id": "c", "query": "查询", "severity": "major"}
    if nested:
        item["turns"] = [{"query": "补充", **spec}]
        bad_field = f"turns[0].{bad_field}"
    else:
        item.update(spec)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([item]), encoding="utf-8")
    with pytest.raises(ValueError) as err:
        load_evalset(path)
    assert bad_field in str(err.value)


def test_multiturn_must_call_union():
    """must_call 看全程并集：工具在第 1 轮调用、断言写在案例级，仍通过。"""
    a = TurnAdapter(reply_by_turn={0: {"tools": ["run_sql"], "output": "查询中"},
                                   1: {"output": "结果是 130"}})
    case = _turn_case(query="查销量", turns=[{"query": "补充"}],
                      must_call=[{"tool": "run_sql"}])
    r = BatchRunner(a).run_all([case])
    assert r.outcomes[0].passed


def test_multiturn_must_not_call_union_catches():
    """must_not_call 看全程并集：任何一轮出现禁止调用都算失败。"""
    a = TurnAdapter(reply_by_turn={0: {"output": "ok"},
                                   1: {"tools": ["delete_record"], "output": "已删除"}})
    case = _turn_case(query="查销量", turns=[{"query": "帮我删了"}],
                      must_not_call=[{"tool": "delete_record"}])
    r = BatchRunner(a).run_all([case])
    o = r.outcomes[0]
    assert not o.passed
    fe = o.first_error
    assert fe is not None and fe.target == "must_not_call"


def test_multiturn_response_contains_on_final_turn():
    """案例级 response_contains 只看最后一轮输出。"""
    a = TurnAdapter(reply_by_turn={0: {"output": "没有答案"}, 1: {"output": "最终答案是 130"}})
    case = _turn_case(query="查销量", turns=[{"query": "补充"}],
                      response_contains=["130"])
    r = BatchRunner(a).run_all([case])
    assert r.outcomes[0].passed


def test_multiturn_error_in_later_turn():
    """第 2 轮抛异常：no_error 失败，detail 标注轮次，归因 system。"""
    class BrokenTurn(TurnAdapter):
        def run(self, query, context):
            if context.get("turn_index") == 1:
                return AgentRun(query=query, final_output="", error="boom")
            return super().run(query, context)

    case = _turn_case(query="查销量", turns=[{"query": "补充"}])
    r = BatchRunner(BrokenTurn()).run_all([case])
    o = r.outcomes[0]
    assert not o.passed
    assert any("第 2 轮" in d for d in o.failed_details())
    assert o.attribution == "system"


# ---------- 快照落盘与报告对比 ----------

def test_write_snapshots_jsonl(tmp_path):
    """快照：每轮一条记录，字段齐全，追加模式累计。"""
    a = TurnAdapter(reply_by_turn={0: {"tools": ["run_sql"], "output": "查询中"},
                                   1: {"output": "130 元"}})
    case = _turn_case(query="查销量", turns=[{"query": "补充"}],
                      response_contains=["130"])
    r = BatchRunner(a).run_all([case])
    p = tmp_path / "snapshots.jsonl"
    n = write_snapshots(p, r)
    assert n == 2                                     # 两轮各一条
    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert [x["turn"] for x in lines] == ["1/2", "2/2"]
    assert lines[0]["tool_calls"] == [{"name": "run_sql", "args": {}}]
    assert lines[1]["query"] == "补充"
    assert lines[0]["passed"] is True
    n2 = write_snapshots(p, r)                        # 再写一次 = 追加
    assert n2 == 2 and len(p.read_text(encoding="utf-8").splitlines()) == 4


def test_compare_reports_and_format():
    """报告对比：成功率差值、新增失败/修复、flaky 变化、四档判定。"""

    def _dump(pairs):
        return {"total": len(pairs),
                "passed": sum(1 for *_x, p, _f in pairs if p),
                "success_rate": sum(1 for *_x, p, _f in pairs if p) / len(pairs),
                "cases": [{"id": cid, "severity": sev, "passed": p, "flaky": fl}
                          for cid, sev, p, fl in pairs]}

    old = _dump([("a", "blocker", True, False), ("b", "major", True, False),
                 ("c", "major", False, True)])
    new = _dump([("a", "blocker", False, False), ("b", "major", True, False),
                 ("c", "major", True, False)])
    d = compare_reports(old, new)
    assert d["newly_failing"] == ["a"] and d["newly_fixed"] == ["c"]
    assert d["flaky_fixed"] == ["c"]
    assert d["delta_pp"] == 0.0        # 新旧都是 2/3，总量持平；变化在结构不在总量
    # blocker 新增失败 → 无论差值多少都判 investigate
    assert d["verdict"].startswith("investigate")
    text = format_compare(d)
    assert "新增失败" in text and "回放判定" in text


def test_compare_reports_noise_band():
    """差值在噪声带内（≤3pp）→ 判 invalid 作废。"""

    def _dump(rate):
        return {"total": 10, "passed": round(rate * 10), "success_rate": rate,
                "cases": [{"id": f"c{i}", "severity": "major", "passed": True, "flaky": False}
                          for i in range(10)]}

    d = compare_reports(_dump(0.90), _dump(0.88))     # -2pp，噪声带内
    assert d["verdict"].startswith("invalid")


def test_compare_reports_promote():
    """提升 ≥10pp 且无 blocker 失败 → promote。"""

    def _dump(rate):
        return {"total": 10, "passed": round(rate * 10), "success_rate": rate,
                "cases": [{"id": f"c{i}", "severity": "major", "passed": True, "flaky": False}
                          for i in range(10)]}

    d = compare_reports(_dump(0.70), _dump(0.85))
    assert d["verdict"].startswith("promote")
