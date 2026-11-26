"""门禁可靠性回归：undetermined 第三态 / 判定正确性 / 指纹与快照健壮性。

本文件是「绿灯里不许混着没测过的用例」这一承诺的回归锁，集中放置便于审计。
每项对应一次代码审查里可复现的误放行 / 误拦截 / 数据失真。
"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from harness import (AgentAdapter, AgentRun, AssertionResult, BatchRunner,
                     CaseOutcome, EvalReport, ToolCallRecord, _dedup_pairs,
                     _judge_accepts_trace, _last_run_of, _normalize_verdict,
                     assert_case, compare_reports, gate_decision,
                     report_to_dict, write_snapshots)
from evalset import EvalCase, load_evalset
from judge import SemanticJudge
from models import AttributionLayer


class StubAdapter(AgentAdapter):
    def run(self, query: str, context: dict) -> AgentRun:
        return AgentRun(query=query, final_output="ok")


def _case(cid="c1", **kw):
    query = kw.pop("query", "q")
    severity = kw.pop("severity", "major")
    return EvalCase(case_id=cid, query=query, severity=severity, **kw)


# ---------- B 组：undetermined 第三态 ----------

def test_all_skipped_blocker_is_undetermined():
    """只写 judge_criteria 的 blocker，judge 未上岗时不能算通过、不能算进成功率。"""
    c = _case("b1", severity="blocker", judge_criteria=["必须回答完整"])
    r = BatchRunner(StubAdapter()).run_all([c], level="L1")
    o = r.outcomes[0]
    assert o.case_status == "undetermined" and o.undetermined is True
    assert r.passed == 0 and r.success_rate == 0.0
    assert r.determined_total == 0 and r.undetermined_total == 1
    assert [x.case.case_id for x in r.undetermined_cases()] == ["b1"]
    assert r.blockers_undetermined() and not r.blockers_failed()


def test_gate_rejects_blocker_undetermined():
    """blocker 未有效测试 → 门禁拒绝，理由必须点明「未有效测试」。"""
    c = _case("b1", severity="blocker", judge_criteria=["须回答完整"])
    ok, reasons = gate_decision(BatchRunner(StubAdapter()).run_all([c], level="L1"))
    assert ok is False
    assert any("未有效测试" in x for x in reasons)


def test_non_blocker_undetermined_blocks_gate_under_zero_miss():
    """零漏审口径：非 blocker 的不确定用例同样阻断门禁，不区分 severity。

    通过率分母口径保持不变（undetermined 仍不计入）；变的是放行条件 ——
    历史行为「只标注不阻断」已收敛为显式 strict_review=False 的兼容开关。
    """
    cases = [
        _case("m1", judge_criteria=["x"]),
        _case("ok", response_contains=["ok"]),
    ]
    r = BatchRunner(StubAdapter()).run_all(cases, level="L1")
    assert r.undetermined_total == 1 and r.determined_total == 1
    assert r.passed == 1 and r.success_rate == 1.0
    assert [x.case.case_id for x in r.review_queue()] == ["m1"]
    ok, reasons = gate_decision(r)
    assert ok is False
    assert any("零漏审" in x and "m1" in x for x in reasons)
    assert "未定 1 条" in r.text()
    ok_legacy, reasons_legacy = gate_decision(r, strict_review=False)
    assert ok_legacy is True and reasons_legacy == []


def test_need_review_case_excluded_from_rate():
    """need_review 是弃判，不计入 judged，也不计入通过率分子分母。"""
    class NeedReviewJudge:
        calibrated = True

        def judge(self, query, criteria, output, trace=None):
            return {"verdict": "need_review", "evidence": ["证据不足"]}

    cases = [_case("m1", judge_criteria=["x"]),
             _case("ok", response_contains=["ok"])]
    r = BatchRunner(StubAdapter(), judge=NeedReviewJudge()).run_all(cases, level="L1")
    assert r.undetermined_total == 1 and r.determined_total == 1
    assert r.success_rate == 1.0
    assert r.semantic_stats()["judged"] == 0
    assert r.semantic_stats()["need_review"] == 1
    assert [x.case.case_id for x in r.review_cases()] == ["m1"]


def test_mixed_skipped_with_effective_assertion_still_passed():
    """有有效断言时 case_status 仍为 passed，但零漏审下必须进人工审核队列。

    这正是旧口径的漏审点：P1 全过会把 skipped 的语义断言掩盖掉。
    """
    c = _case(response_contains=["ok"], judge_criteria=["x"])
    o = BatchRunner(StubAdapter()).run_case(c, level="L1")
    assert o.unverified and o.effective
    assert o.case_status == "passed" and o.passed is True
    assert o.review_required is True
    assert any("semantic:skipped" in x for x in o.review_reasons)


def test_case_status_three_states():
    """三态判定：failed / undetermined / passed。"""
    def outcome(assertions):
        return CaseOutcome(_case(), None, assertions, True, None, None)

    failed = [AssertionResult("no_error", True, ""),
              AssertionResult("must_call", False, "缺工具")]
    undet = [AssertionResult("no_error", True, ""),
             AssertionResult("semantic", True, "", skipped=True, status="skipped")]
    passed = [AssertionResult("no_error", True, ""),
              AssertionResult("response_contains", True, "")]
    assert outcome(failed).case_status == "failed"
    assert outcome(undet).case_status == "undetermined"
    assert outcome(passed).case_status == "passed"


def test_empty_report_gate_rejects():
    """手工构造的空报告直接交给门禁也必须拒绝（纵深防御）。"""
    ok, reasons = gate_decision(EvalReport(level="L0", outcomes=[]))
    assert ok is False and any("空报告" in x for x in reasons)


def test_run_all_rejects_empty_cases():
    with pytest.raises(ValueError, match="空跑批"):
        BatchRunner(StubAdapter()).run_all([])


@pytest.mark.parametrize("bad", [3, {"a": 1}, ["smoke", 3], ("smoke", 3)])
def test_run_all_rejects_bad_dataset_type(bad):
    """dataset 只接受 str 或全 str 数组；int / dict / 混合数组不能静默误处理。"""
    with pytest.raises(ValueError, match="dataset"):
        BatchRunner(StubAdapter()).run_all([_case()], dataset=bad)


# ---------- A 组：判定正确性 ----------

class PriceAdapter(AgentAdapter):
    def run(self, query, context):
        return AgentRun(query=query, final_output="价格是 100 元",
                        tool_calls=[ToolCallRecord("price", result="100")])


def test_tool_result_used_explicit_values_dedup():
    """显式 values 重复值不能凑满 min_values（与 output_grounded_in 同口径）。"""
    c = _case(tool_result_used=[
        {"tool": "price", "values": ["100", "100", "100"], "min_values": 3}])
    o = BatchRunner(PriceAdapter()).run_case(c)
    assert not o.passed
    assert any("tool_result_used" in d for d in o.failed_details())


def test_output_grounded_in_dedup_and_shared_helper():
    """三处去重口径统一；归一化后同值（5,999 / 5999）也算重复。"""
    c = _case(output_grounded_in={"values": ["100", "100", 100], "min_values": 3})
    o = BatchRunner(PriceAdapter()).run_case(c)
    assert not o.passed
    assert len(_dedup_pairs([("5,999", "5999"), ("5999", "5999"),
                             ("５９９９", "5999")])) == 1


@pytest.mark.parametrize("raw", [None, "maybe", {"verdict": "maybe"},
                                  {"verdict": "pass", "evidence": "不是数组"},
                                  42])
def test_custom_judge_bad_verdict_degrades_need_review(raw):
    """自定义 judge 返回值非法一律降级 need_review，绝不硬判 failed 误拦截 blocker。"""
    class BadJudge:
        calibrated = True

        def judge(self, query, criteria, output, trace=None):
            return raw

    c = _case("b1", severity="blocker", judge_criteria=["x"])
    o = BatchRunner(StubAdapter(), judge=BadJudge()).run_all([c], level="L1").outcomes[0]
    sem = [a for a in o.assertions if a.kind == "semantic"][0]
    assert sem.result_status() == "need_review"
    assert o.case_status == "undetermined"  # 未被误判为「回答不合格」


def test_custom_judge_exception_does_not_abort_batch():
    """判分器抛任何异常都不中断整批，后续用例照常执行。"""
    class BoomJudge:
        calibrated = True

        def judge(self, query, criteria, output, trace=None):
            raise RuntimeError("judge boom")

    cases = [_case("m1", judge_criteria=["x"]),
             _case("ok", response_contains=["ok"])]
    r = BatchRunner(StubAdapter(), judge=BoomJudge()).run_all(cases, level="L1")
    assert len(r.outcomes) == 2
    assert r.outcomes[0].case_status == "undetermined"
    assert r.outcomes[1].case_status == "passed"


def test_judge_accepts_trace_keyword_only_signature():
    """ctx=None 签名不得被误判为可接 trace；三参签名仍走三参调用。"""
    class CtxJudge:
        calibrated = True

        def judge(self, query, criteria, output, ctx=None):
            return {"verdict": "pass", "evidence": []}

    class ThreeJudge:
        calibrated = True

        def judge(self, query, criteria, output):
            return {"verdict": "pass", "evidence": []}

    class TraceJudge:
        calibrated = True

        def judge(self, query, criteria, output, trace=None):
            assert isinstance(trace, dict) and "tool_calls" in trace
            return {"verdict": "pass", "evidence": []}

    assert _judge_accepts_trace(CtxJudge()) is False
    assert _judge_accepts_trace(ThreeJudge()) is False
    assert _judge_accepts_trace(TraceJudge()) is True
    c = _case(judge_criteria=["x"])
    for j in (CtxJudge(), ThreeJudge(), TraceJudge()):
        o = BatchRunner(StubAdapter(), judge=j).run_all([c], level="L1").outcomes[0]
        assert o.passed is True


def test_normalize_verdict_whitelist():
    assert _normalize_verdict({"verdict": "pass", "evidence": []})["verdict"] == "pass"
    assert _normalize_verdict({"verdict": "FAIL", "evidence": []})["verdict"] == "need_review"
    assert _normalize_verdict({"verdict": "fail", "evidence": "x"})["verdict"] == "need_review"


@pytest.mark.parametrize("workers", [1, 4])
def test_adapter_exception_isolated_single_and_concurrent(workers):
    """一条坏用例不能毁整批：单线程与并发路径都转成 no_error 失败。"""
    class BoomAdapter(AgentAdapter):
        def run(self, query, context):
            if query == "bad":
                raise RuntimeError("adapter boom")
            return AgentRun(query=query, final_output="ok")

    cases = [_case("bad", query="bad"),
             _case("good", query="good", response_contains=["ok"])]
    r = BatchRunner(BoomAdapter()).run_all(cases, max_workers=workers)
    assert len(r.outcomes) == 2
    assert r.outcomes[0].case_status == "failed"
    assert r.outcomes[0].attribution == "system"
    assert r.outcomes[1].case_status == "passed"


def test_compare_reports_both_legacy_without_hash_uncomparable():
    """两侧都缺 case_hash 的旧报告不能判「提升可合并」。"""
    def rep(rate):
        return {"success_rate": rate,
                "cases": [{"id": "c1", "passed": True, "flaky": False}]}

    d = compare_reports(rep(0.70), rep(0.95))
    assert d["missing_fingerprints"] is True
    assert d["evalset_mismatch"] is True
    assert d["verdict"].startswith("invalid")
    assert "缺少用例定义指纹" in d["verdict"]


@pytest.mark.parametrize("bad_id", [[1, 2], {"a": 1}, 123, True])
def test_evalset_id_non_string_rejected(tmp_path, bad_id):
    """id 非字符串：给清晰 ValueError，不再在去重时抛 unhashable TypeError。"""
    p = tmp_path / "e.json"
    p.write_text(json.dumps([{"id": bad_id, "query": "q", "severity": "blocker"}]),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必填字段或类型错误"):
        load_evalset(str(p))


def test_evalset_required_query_type_rejected(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps([{"id": "c1", "query": 123, "severity": "blocker"}]),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必填字段或类型错误"):
        load_evalset(str(p))


# ---------- A 组：指纹 / 性能 / 快照 ----------

class MultiVersionAdapter(AgentAdapter):
    def __init__(self, version, first_latency=10.0, last_latency=90.0):
        self.version = version
        self.first_latency = first_latency
        self.last_latency = last_latency

    def run(self, query, context):
        return AgentRun(query=query, final_output="ok")

    def run_turns(self, turns, context):
        return [
            AgentRun(query=turns[0]["query"], final_output="ok",
                     latency_ms=self.first_latency, token_usage={"total": 5}),
            AgentRun(query=turns[1]["query"], final_output="ok",
                     versions={"model": self.version},
                     latency_ms=self.last_latency, token_usage={"total": 50}),
        ]


def test_multiturn_last_turn_versions_change_config_sig():
    """末轮模型版本变化必须改变 config_sig，且顶层与逐题版本口径一致。"""
    c = _case(turns=[{"query": "追问"}])
    r1 = BatchRunner(MultiVersionAdapter("v2")).run_all([c])
    r2 = BatchRunner(MultiVersionAdapter("v9")).run_all([c])
    assert r1.fingerprint()["config_sig"] != r2.fingerprint()["config_sig"]
    d = report_to_dict(r1)
    assert d["fingerprint"]["versions"] == {"model": "v2"}
    assert d["cases"][0]["versions"] == {"model": "v2"}


def test_perf_summary_uses_last_run():
    """perf_summary 与逐题性能都取末轮，不再首轮/末轮自相矛盾。"""
    c = _case(turns=[{"query": "追问"}])
    r = BatchRunner(MultiVersionAdapter("v1")).run_all([c])
    assert r.perf_summary()["avg_latency_ms"] == 90.0
    assert r.perf_summary()["total_tokens"] == 50
    d = report_to_dict(r)
    assert d["cases"][0]["latency_ms"] == 90.0
    assert d["cases"][0]["token_usage"] == {"total": 50}
    assert _last_run_of(r.outcomes[0]).latency_ms == 90.0


def test_snapshot_nonserializable_fields(tmp_path):
    """datetime / set / 自定义对象 / 非法 dict key 都能落盘且可 json.loads。"""
    class Weird:
        pass

    class SnapAdapter(AgentAdapter):
        def run(self, query, context):
            return AgentRun(
                query=query, final_output="ok",
                state={"ts": datetime.datetime(2026, 1, 1),
                       "tags": {"a", "b"}, "obj": Weird()},
                versions={"model": "v1"},
                tool_calls=[ToolCallRecord("t", {"k": {1, 2}})],
                retrieved=[{"chunk": 1}])

    p = tmp_path / "snap.jsonl"
    r = BatchRunner(SnapAdapter()).run_all([_case()])
    assert write_snapshots(p, r) == 1
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d["case_status"] == "passed" and d["tool_calls"][0]["name"] == "t"


def test_snapshot_atomic_write_no_partial_file(tmp_path):
    """序列化在写盘之前全部完成；失败时不留下半批残文件。"""
    class Bad:
        def __str__(self):
            raise RuntimeError("cannot str")

    class BadAdapter(AgentAdapter):
        def run(self, query, context):
            return AgentRun(query=query, final_output="ok", state={"x": Bad()})

    p = tmp_path / "snap.jsonl"
    r = BatchRunner(BadAdapter()).run_all([_case()])
    with pytest.raises(RuntimeError):
        write_snapshots(p, r)
    assert not p.exists()


def test_snapshot_session_id_is_effective_value(tmp_path):
    """不传 context_overrides 时，自动生成的 session_id 仍写进快照。"""
    p = tmp_path / "snap.jsonl"
    r = BatchRunner(StubAdapter()).run_all([_case()])
    write_snapshots(p, r)
    d = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert d["session_id"].startswith("eval-")
    assert d["session_id"] == r.outcomes[0].session_id


def test_empty_runs_via_batch_runner(tmp_path):
    """run_turns 返回 [] 且走 BatchRunner.run_case：不炸、failed、快照写 0 条。"""
    class EmptyAdapter(AgentAdapter):
        def run(self, query, context):
            return AgentRun(query=query, final_output="ok")

        def run_turns(self, turns, context):
            return []

    c = _case(turns=[{"query": "追问"}])
    r = BatchRunner(EmptyAdapter()).run_all([c])
    o = r.outcomes[0]
    assert o.case_status == "failed" and o.attribution == "system"
    assert write_snapshots(tmp_path / "s.jsonl", r) == 0


# ---------- judge 客户端异常降级 ----------

def test_client_runtime_error_degrades_need_review():
    class BoomClient:
        def chat(self, system, user):
            raise RuntimeError("network down")

    j = SemanticJudge(client=BoomClient(), calibrated=True)
    out = j.judge("q", ["c"], "o")
    assert out["verdict"] == "need_review"
    assert j.last_errors and "RuntimeError" in j.last_errors[-1]


def test_report_to_dict_exposes_undetermined_fields():
    """报告保留全部旧键，并新增 undetermined 系列字段供 Web / CI 消费。"""
    c = _case(judge_criteria=["x"])
    d = report_to_dict(BatchRunner(StubAdapter()).run_all([c], level="L1"))
    for key in ("total", "passed", "success_rate", "blockers_failed",
                "fingerprint", "perf", "semantic_stats", "cases"):
        assert key in d
    assert d["undetermined"] == 1 and d["undetermined_total"] == 1
    assert d["determined_total"] == 0
    assert d["undetermined_cases"] == ["c1"]
    assert d["cases"][0]["case_status"] == "undetermined"
