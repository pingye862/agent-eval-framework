"""零漏审回归：审核队列 / 人工签核 / 内容指纹 / 门禁闭环。

本文件锁住的是「凡不确定必有人确认、未确认不放行」这条质量红线，
与 test_safeguards.py 的通过率口径回归互为补充。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from harness import (AgentAdapter, AgentRun, AssertionResult, BatchRunner,
                     CaseOutcome, gate_decision, report_to_dict)
from evalset import EvalCase
from review import (ReviewSignoff, SignoffSet, build_review_queue,
                    export_review_queue, load_signoffs, review_fingerprint,
                    review_stats)


class StubAdapter(AgentAdapter):
    def __init__(self, output="ok"):
        self.output = output

    def run(self, query, context):
        return AgentRun(query=query, final_output=self.output)


class NeedReviewJudge:
    calibrated = True

    def judge(self, query, criteria, output, trace=None):
        return {"verdict": "need_review", "evidence": ["证据不足"]}


def _case(cid="c1", **kw):
    query = kw.pop("query", "q")
    severity = kw.pop("severity", "major")
    return EvalCase(case_id=cid, query=query, severity=severity, **kw)


def _report(cases, output="ok", level="L1", judge=None):
    return BatchRunner(StubAdapter(output), judge=judge).run_all(cases, level=level)


def _mixed_report(output="ok"):
    """一条不确定（judge 未配置）+ 一条全自动通过，保证 determined_total > 0。"""
    return _report([_case("m1", judge_criteria=["x"]),
                    _case("ok", response_contains=["ok"])], output=output)


# ---------- A 组：队列口径 ----------

def test_full_auto_case_not_in_review_queue():
    """全自动判定完毕的用例不进队列，零漏审不应误拦截干净报告。"""
    r = _report([_case("a", response_contains=["ok"])])
    assert [o.case.case_id for o in r.review_queue()] == []
    ok, reasons = gate_decision(r)
    assert ok is True and reasons == []


def test_skipped_semantic_hidden_by_effective_assertion_still_queued():
    """旧漏审点：P1 全过把 skipped 语义断言掩盖，新口径仍必须进队列。"""
    r = _report([_case("s1", response_contains=["ok"], judge_criteria=["x"])])
    o = r.outcomes[0]
    assert o.case_status == "passed"
    assert o.review_required is True
    assert any("semantic:skipped" in x for x in o.review_reasons)
    assert [x.case.case_id for x in r.review_queue()] == ["s1"]


def test_need_review_enters_queue():
    r = _report([_case("n1", judge_criteria=["x"]),
                 _case("ok", response_contains=["ok"])], judge=NeedReviewJudge())
    ids = [o.case.case_id for o in r.review_queue()]
    assert "n1" in ids
    assert any("semantic:need_review" in x
               for x in r.review_queue()[0].review_reasons)


def test_undetermined_enters_queue():
    r = _mixed_report()
    o = r.review_queue()[0]
    assert o.case.case_id == "m1" and o.case_status == "undetermined"
    assert any("undetermined" in x for x in o.review_reasons)


def test_flaky_enters_queue_even_without_unverified_assertions():
    o = CaseOutcome(_case("f1"), AgentRun(query="q", final_output="ok"),
                    [AssertionResult("response_contains", True, "")],
                    True, None, None, pass_runs=1, total_runs=2)
    assert o.flaky and o.review_required
    assert any("flaky" in x for x in o.review_reasons)


# ---------- B 组：签核与门禁 ----------

def test_pending_signoff_blocks_gate():
    r = _mixed_report()
    ok, reasons = gate_decision(r)
    assert ok is False
    assert any("零漏审" in x and "m1" in x for x in reasons)


def test_approve_signoff_unlocks_gate():
    r = _mixed_report()
    fp = review_fingerprint(r.review_queue()[0])
    signoffs = SignoffSet([ReviewSignoff("m1", fp, "approve", "AP")])
    ok, reasons = gate_decision(r, signoffs=signoffs)
    assert ok is True and reasons == []


def test_reject_signoff_blocks_gate():
    r = _mixed_report()
    fp = review_fingerprint(r.review_queue()[0])
    signoffs = SignoffSet([ReviewSignoff("m1", fp, "reject", "AP")])
    ok, reasons = gate_decision(r, signoffs=signoffs)
    assert ok is False and any("零漏审" in x for x in reasons)


def test_partial_signoff_still_blocks():
    r = _report([_case("m1", judge_criteria=["x"]),
                 _case("m2", judge_criteria=["x"]),
                 _case("ok", response_contains=["ok"])])
    queue = build_review_queue(r)
    assert len(queue) == 2
    fp = queue[0]["fingerprint"]
    signoffs = SignoffSet([ReviewSignoff("m1", fp, "approve", "AP")])
    ok, reasons = gate_decision(r, signoffs=signoffs)
    assert ok is False and any("m2" in x for x in reasons)


def test_fingerprint_change_invalidates_old_signoff():
    """输出变化后旧签核失效，必须用新报告重新确认。"""
    r1 = _mixed_report(output="ok")
    r2 = _mixed_report(output="完全不同输出")
    fp1 = review_fingerprint(r1.review_queue()[0])
    fp2 = review_fingerprint(r2.review_queue()[0])
    assert fp1 != fp2
    signoffs = SignoffSet([ReviewSignoff("m1", fp1, "approve", "AP")])
    ok, reasons = gate_decision(r2, signoffs=signoffs)
    assert ok is False and any("零漏审" in x for x in reasons)


def test_strict_review_false_is_explicit_legacy_escape():
    r = _mixed_report()
    ok, reasons = gate_decision(r, strict_review=False)
    assert ok is True and reasons == []


# ---------- C 组：导出 / 回读 / 校验 ----------

def test_export_and_signoff_roundtrip(tmp_path):
    r = _mixed_report()
    path = tmp_path / "queue.json"
    assert export_review_queue(path, r) == 1
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw[0]["status"] == "pending" and raw[0]["reasons"]
    assert raw[0]["actual"]["final_output"] == "ok"
    raw[0].update({"decision": "approve", "reviewer": "AP",
                   "ts": "2026-11-26T17:00:00", "note": "已核对证据"})
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    signoffs = load_signoffs(path)
    assert signoffs.approved("m1", raw[0]["fingerprint"])
    ok, _ = gate_decision(r, signoffs=signoffs)
    assert ok is True


def test_empty_decision_counts_as_pending(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps([
        {"case_id": "m1", "fingerprint": "abc", "decision": "",
         "reviewer": "", "status": "pending"}
    ], ensure_ascii=False), encoding="utf-8")
    s = load_signoffs(path)
    assert s.status("m1", "abc") == "pending"


def test_invalid_decision_rejected(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps([
        {"case_id": "m1", "fingerprint": "abc", "decision": "approved",
         "reviewer": "AP"}
    ], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="decision 非法"):
        load_signoffs(path)


def test_incomplete_signoff_rejected(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps([
        {"case_id": "m1", "fingerprint": "abc", "decision": "approve",
         "reviewer": ""}
    ], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="签核信息不完整"):
        load_signoffs(path)


def test_review_stats():
    r = _mixed_report()
    assert review_stats(build_review_queue(r)) == {
        "total": 1, "pending": 1, "approve": 0, "reject": 0}


def test_report_to_dict_exposes_review_fields():
    r = _mixed_report()
    d = report_to_dict(r)
    assert d["review_total"] == 1 and d["review_queue"] == ["m1"]
    case = next(c for c in d["cases"] if c["id"] == "m1")
    assert case["review_required"] is True
    assert case["review_fingerprint"] and case["review_reasons"]
