"""数据飞轮增量能力单测（P0-1/2/3 + P1-4/5/6）。

对应文章《Agent 数据飞轮平台的选型和实践》里，本框架在自身定位内
补齐的五环：轨迹证据 / 失败回流 / 数据集分层 / 性能对比 / 证据接地。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from harness import (AgentAdapter, AgentRun, BatchRunner, ToolCallRecord,
                     _merged_trace, _judge_accepts_trace, _norm_value,
                     report_to_dict, collect_failures, export_failures,
                     promote_failures, compare_reports, format_compare,
                     write_snapshots, _case_diff)
from evalset import EvalCase, load_evalset, case_to_dict, dataset_counts
from judge import SemanticJudge, JudgeError, JUDGE_SYSTEM_PROMPT


class FakeClient:
    def __init__(self, reply='{"verdict": "pass", "evidence": []}'):
        self.reply = reply
        self.calls = []

    def chat(self, system, user):
        self.calls.append({"system": system, "user": user})
        return self.reply


class EvidenceAdapter(AgentAdapter):
    """可编程：返回指定输出、工具调用（含返回值）、性能指标。"""

    def __init__(self, output="", tools=None, latency_ms=None, token_usage=None,
                 retrieved=None):
        self.output = output
        self.tools = tools or []
        self.latency_ms = latency_ms
        self.token_usage = token_usage or {}
        self.retrieved = retrieved or []

    def run(self, query, context):
        return AgentRun(query=query, final_output=self.output,
                        tool_calls=self.tools, retrieved=self.retrieved,
                        latency_ms=self.latency_ms, token_usage=self.token_usage)


def _case(cid="c1", **kw):
    query = kw.pop("query", "q")
    severity = kw.pop("severity", "major")
    return EvalCase(case_id=cid, query=query, severity=severity, **kw)


# ---------- P0-1：judge 轨迹证据 + 缓存 key ----------

def test_judge_prompt_contains_trace_evidence():
    c = FakeClient()
    j = SemanticJudge(client=c)
    trace = {"tool_calls": [{"name": "query_price", "args": {"model": "x"},
                             "ok": True, "result": {"price": "5999"}}],
             "retrieved": ["kb-001"], "state": {}}
    j.judge("多少钱", ["必须给出价格"], "5999 元", trace=trace)
    user = c.calls[0]["user"]
    assert "【执行证据】" in user and "5999" in user and "query_price" in user
    assert "工具调用成功" in JUDGE_SYSTEM_PROMPT   # 裁判被明确告知别只看状态


def test_judge_cache_key_includes_trace():
    """同一句回答、背后工具返回不同 → 必须重判，不能命中旧缓存。"""
    c = FakeClient()
    j = SemanticJudge(client=c)
    t1 = {"tool_calls": [{"name": "t", "result": "A16"}]}
    t2 = {"tool_calls": [{"name": "t", "result": "A17"}]}
    j.judge("q", ["标准"], "一样的回答", trace=t1)
    j.judge("q", ["标准"], "一样的回答", trace=t2)
    assert len(c.calls) == 2


def test_judge_cache_still_hits_without_trace():
    c = FakeClient()
    j = SemanticJudge(client=c)
    j.judge("q", ["标准"], "回答")
    j.judge("q", ["标准"], "回答")
    assert len(c.calls) == 1


def test_judge_backward_compatible_with_3arg_impl():
    """第三方自定义 judge 仍是 3 参数时，harness 不得强传第 4 个。"""
    class OldJudge:
        calibrated = True

        def judge(self, query, criteria, output):
            return {"verdict": "pass", "evidence": []}

    assert not _judge_accepts_trace(OldJudge())
    o = BatchRunner(EvidenceAdapter(output="ok"), judge=OldJudge()).run_case(
        _case(judge_criteria=["标准"]), level="L1")
    assert o.passed


def test_merged_trace_collects_all_turns():
    """多轮：前几轮的工具结果也要进证据（文章 Case B 的子任务）。"""
    runs = [
        AgentRun(query="q1", final_output="查询中",
                 tool_calls=[ToolCallRecord("query_price", result={"price": "4599"})]),
        AgentRun(query="q2", final_output="汇总 4599", retrieved=["kb-1"]),
    ]
    t = _merged_trace(runs)
    assert len(t["tool_calls"]) == 1 and t["tool_calls"][0]["result"] == {"price": "4599"}
    assert t["retrieved"] == ["kb-1"]


def test_harness_passes_trace_to_judge():
    called = {}

    class TraceJudge:
        calibrated = True

        def judge(self, query, criteria, output, trace=None):
            called["trace"] = trace
            return {"verdict": "pass", "evidence": []}

    a = EvidenceAdapter(output="5999 元", tools=[ToolCallRecord(
        "query_price", result={"price": "5999"})])
    BatchRunner(a, judge=TraceJudge()).run_case(
        _case(judge_criteria=["必须给出价格"]), level="L1")
    assert called["trace"]["tool_calls"][0]["result"] == {"price": "5999"}


# ---------- P1-6：证据接地断言 ----------

def test_norm_value_normalizes_formats():
    assert _norm_value("5,999 元") == "5999元"
    assert _norm_value("６.１ 英寸") == "6.1英寸"
    assert _norm_value("A16") == "a16"


def test_tool_result_used_passes_with_value_normalization():
    a = EvidenceAdapter(output="当前售价 5,999 元", tools=[ToolCallRecord(
        "query_price", result={"price": 5999, "currency": "CNY"})])
    r = BatchRunner(a).run_case(
        _case(tool_result_used=[{"tool": "query_price", "min_values": 1}]))
    assert r.passed


def test_tool_result_used_catches_case_b():
    """文章 Case B：工具成功返回，回答却没采用结果 —— 老断言全过，只有它抓得住。"""
    a = EvidenceAdapter(output="已为您完成价格查询，具体结果请稍后查看。",
                        tools=[ToolCallRecord("query_price",
                                              result={"price": "4599"})])
    r = BatchRunner(a).run_case(_case(
        must_call=[{"tool": "query_price"}],
        tool_result_used=[{"tool": "query_price", "min_values": 1}]))
    assert not r.passed
    kinds = [x.kind for x in r.assertions if not x.ok]
    assert kinds == ["tool_result_used"]          # must_call 是通过的
    assert "未被回答采用" in r.failed_details()[0]


def test_tool_result_used_skipped_when_adapter_reports_nothing():
    """渐进接入红线：不可观测 ≠ Agent 做错了。"""
    a = EvidenceAdapter(output="随便", tools=[ToolCallRecord("query_price")])
    r = BatchRunner(a).run_case(
        _case(tool_result_used=[{"tool": "query_price"}]))
    assert r.passed
    gr = [x for x in r.assertions if x.kind == "tool_result_used"][0]
    assert gr.result_status() == "skipped" and "未上报返回值" in gr.detail


def test_tool_result_used_fails_when_tool_not_called():
    a = EvidenceAdapter(output="没调工具")
    r = BatchRunner(a).run_case(
        _case(tool_result_used=[{"tool": "query_price"}]))
    assert not r.passed
    assert "未被调用" in r.failed_details()[0]


def test_output_grounded_in_pass_and_fail():
    tools = [ToolCallRecord("t", result={"a": "A16", "b": "6.1 英寸"})]
    ok = BatchRunner(EvidenceAdapter(output="A16 芯片，6.1 英寸屏", tools=tools)).run_case(
        _case(output_grounded_in={"min_values": 2}))
    assert ok.passed
    bad = BatchRunner(EvidenceAdapter(output="我不知道", tools=tools)).run_case(
        _case(output_grounded_in={"min_values": 2}))
    assert not bad.passed and "缺乏执行证据支撑" in bad.failed_details()[0]


def test_output_grounded_in_supports_retrieved_and_explicit_values():
    r = BatchRunner(EvidenceAdapter(output="依据 kb-001 得到结论")).run_case(
        _case(output_grounded_in={"min_values": 1, "values": ["kb-001"],
                                  "sources": ["tool_results"]}))
    assert r.passed
    r2 = BatchRunner(EvidenceAdapter(output="依据 kb-001 得到结论",
                                     retrieved=["kb-001"])).run_case(
        _case(output_grounded_in={"min_values": 1,
                                  "sources": ["retrieved"]}))
    assert r2.passed
    # 召回里有证据、回答却不用 → 抓得住（文章「结果已返回但没被使用」）
    r3 = BatchRunner(EvidenceAdapter(output="没有数据",
                                     retrieved=["kb-001"])).run_case(
        _case(output_grounded_in={"min_values": 1,
                                  "sources": ["retrieved"]}))
    assert not r3.passed


def test_output_grounded_in_skipped_without_any_evidence():
    r = BatchRunner(EvidenceAdapter(output="无证据")).run_case(
        _case(output_grounded_in={"min_values": 1}))
    assert r.passed
    assert [x.result_status() for x in r.assertions
            if x.kind == "output_grounded_in"] == ["skipped"]


def test_grounding_assertion_layer_is_trajectory():
    a = EvidenceAdapter(output="型号 V1", tools=[ToolCallRecord("t", result="V1")])
    r = BatchRunner(a).run_case(_case(tool_result_used=[{"tool": "t"}]))
    assert r.passed and r.attribution is None    # 通过时不归因
    bad = BatchRunner(EvidenceAdapter(output="型号错误",
                      tools=[ToolCallRecord("t", result="V1")])).run_case(
        _case(tool_result_used=[{"tool": "t"}]))
    assert bad.attribution == "trajectory"


# ---------- P0-3：dataset 分层 + 运行指纹 ----------

def test_load_evalset_dataset_filter(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps([
        {"id": "a", "query": "q", "severity": "major", "dataset": "smoke"},
        {"id": "b", "query": "q", "severity": "major", "dataset": "regression"},
        {"id": "c", "query": "q", "severity": "major"},   # 默认 regression
    ], ensure_ascii=False), encoding="utf-8")
    assert [x.case_id for x in load_evalset(p, dataset="smoke")] == ["a"]
    assert [x.case_id for x in load_evalset(p, dataset=["regression"])] == ["b", "c"]
    assert len(load_evalset(p)) == 3
    with pytest.raises(ValueError, match="没有 dataset"):
        load_evalset(p, dataset="baseline")


def test_dataset_counts(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps([
        {"id": "a", "query": "q", "severity": "major"},
        {"id": "b", "query": "q", "severity": "major", "dataset": "smoke"},
    ]), encoding="utf-8")
    assert dataset_counts(p) == {"regression": 1, "smoke": 1}


def test_run_all_dataset_filter_and_empty_guard():
    cases = [_case("a", dataset="smoke"), _case("b", dataset="baseline")]
    r = BatchRunner(EvidenceAdapter(output="ok")).run_all(cases, dataset="smoke")
    assert r.total == 1 and r.dataset == "smoke"
    with pytest.raises(ValueError, match="拒绝空跑批"):
        BatchRunner(EvidenceAdapter(output="ok")).run_all(cases, dataset="nope")


def test_fingerprint_env_and_config_sig():
    a = EvidenceAdapter(output="ok")
    r1 = BatchRunner(a).run_all([_case()], env="local", dataset="regression")
    r2 = BatchRunner(a).run_all([_case()], env="local", dataset="regression")
    r3 = BatchRunner(a).run_all([_case()], env="staging", dataset="regression")
    f1, f2, f3 = r1.fingerprint(), r2.fingerprint(), r3.fingerprint()
    assert f1["run_id"] and f1["run_id"] != f2["run_id"]
    assert f1["config_sig"] == f2["config_sig"]      # 同配置可比
    assert f1["config_sig"] != f3["config_sig"]      # 换环境指纹必变
    assert f1["env"] == "local" and f3["env"] == "staging"


def test_fingerprint_reads_eval_env(monkeypatch):
    monkeypatch.setenv("EVAL_ENV", "preprod")
    r = BatchRunner(EvidenceAdapter(output="ok")).run_all([_case()])
    assert r.env == "preprod"
    monkeypatch.delenv("EVAL_ENV")


def test_perf_summary():
    a = EvidenceAdapter(output="ok", latency_ms=100.0,
                        token_usage={"prompt": 10, "completion": 5, "total": 15})
    r = BatchRunner(a).run_all([_case()])
    p = r.perf_summary()
    assert p["avg_latency_ms"] == 100.0 and p["total_tokens"] == 15
    assert p["cases_with_latency"] == 1


def test_report_text_shows_fingerprint_and_perf():
    a = EvidenceAdapter(output="ok", latency_ms=100.0, token_usage={"total": 15})
    t = BatchRunner(a).run_all([_case()]).text()
    assert "运行指纹" in t and "性能指标" in t


def test_snapshot_records_new_fields(tmp_path):
    a = EvidenceAdapter(output="ok", tools=[ToolCallRecord("t", result="V1")],
                        latency_ms=88.0, token_usage={"total": 7})
    r = BatchRunner(a).run_all([_case(dataset="smoke")],
                               env="preprod", dataset="smoke")
    p = tmp_path / "s.jsonl"
    write_snapshots(p, r)
    rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert rec["run_id"] == r.run_id and rec["env"] == "preprod"
    assert rec["dataset"] == "smoke" and rec["case_dataset"] == "smoke"
    assert rec["tool_calls"][0]["result"] == "V1"
    assert rec["latency_ms"] == 88.0 and rec["token_usage"] == {"total": 7}


def test_case_to_dict_roundtrip(tmp_path):
    c = _case("x", dataset="baseline", judge_criteria=["标准"],
              tool_result_used=[{"tool": "t"}])
    p = tmp_path / "e.json"
    p.write_text(json.dumps([case_to_dict(c)], ensure_ascii=False), encoding="utf-8")
    loaded = load_evalset(p)[0]
    assert loaded.dataset == "baseline" and loaded.tool_result_used == [{"tool": "t"}]


# ---------- P0-2：失败样本回流 ----------

def _failing_report():
    a = EvidenceAdapter(output="已为您完成价格查询，具体结果请稍后查看。",
                        tools=[ToolCallRecord("query_price", result={"price": "4599"})],
                        latency_ms=100.0, token_usage={"total": 20})
    c = _case("c9", severity="blocker",
              must_call=[{"tool": "query_price"}],
              tool_result_used=[{"tool": "query_price", "min_values": 1}])
    return BatchRunner(a).run_all([c], env="staging")


def test_collect_failures_content():
    r = _failing_report()
    f = collect_failures(r)[0]
    assert f["case_id"] == "c9" and f["env"] == "staging"
    assert f["failed_assertions"][0]["kind"] == "tool_result_used"
    assert f["actual"]["tool_calls"][0]["result"] == {"price": "4599"}
    s = f["suggested_case"]
    assert s["id"] == "REG-c9" and s["dataset"] == "regression"
    assert any("依据" in x for x in s["judge_criteria"])
    assert "review_hint" in f     # 人工确认是必须环节


def test_export_and_promote_failures(tmp_path):
    r = _failing_report()
    eset = tmp_path / "e.json"
    eset.write_text(json.dumps([case_to_dict(_case("orig"))], ensure_ascii=False),
                    encoding="utf-8")
    fp = tmp_path / "f.json"
    assert export_failures(fp, r) == 1
    out, n = promote_failures(fp, eset)
    assert n == 1 and out.name == "e.promoted.json"
    assert len(json.loads(eset.read_text(encoding="utf-8"))) == 1   # 原文件没动
    merged = json.loads(out.read_text(encoding="utf-8"))
    assert [x["id"] for x in merged] == ["orig", "REG-c9"]


def test_promote_avoids_id_collision_and_validates(tmp_path):
    r = _failing_report()
    eset = tmp_path / "e.json"
    fp = tmp_path / "f.json"
    export_failures(fp, r)
    eset.write_text(json.dumps([case_to_dict(_case("REG-c9"))],
                               ensure_ascii=False), encoding="utf-8")
    out, n = promote_failures(fp, eset, overwrite=True)
    assert n == 1
    assert [x["id"] for x in json.loads(out.read_text(encoding="utf-8"))] == [
        "REG-c9", "REG-c9-2"]
    # 半成品整集不许落盘
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"id": "x"}], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        promote_failures(fp, bad)
    assert not (tmp_path / "bad.json.promote.tmp").exists()


def test_promote_rejects_empty_selection(tmp_path):
    r = _failing_report()
    fp = tmp_path / "f.json"
    export_failures(fp, r)
    with pytest.raises(ValueError, match="没有选中"):
        promote_failures(fp, tmp_path / "e.json", ids=["不存在的id"])


# ---------- P1-5：逐题 diff + 环境校验 ----------

def test_report_to_dict_full_fields():
    a = EvidenceAdapter(output="5999 元", tools=[ToolCallRecord(
        "query_price", result={"price": "5999"})],
        latency_ms=100.0, token_usage={"total": 20})
    d = report_to_dict(BatchRunner(a).run_all([_case()]), gate_ok=True)
    c = d["cases"][0]
    assert c["final_output"] == "5999 元" and c["tool_results"] == [{"price": "5999"}]
    assert c["latency_ms"] == 100.0 and c["dataset"] == "regression"
    assert d["fingerprint"]["env"] == "local" and d["perf"]["total_tokens"] == 20


def test_case_diff_detects_output_and_assertion_changes():
    old = {"final_output": "没有数据", "tool_calls": ["t"],
           "assertions": [{"kind": "tool_result_used", "status": "failed"}]}
    new = {"final_output": "4599 元", "tool_calls": ["t"],
           "assertions": [{"kind": "tool_result_used", "status": "passed"}]}
    ch = _case_diff(old, new)
    fields = [x["field"] for x in ch]
    assert "final_output" in fields
    assert any(f.startswith("assertion:") for f in fields)
    assert "tool_calls" not in fields


def test_compare_reports_case_diffs_and_perf():
    def rep(rate, out, lat, tok, env="local", passed=True):
        return {"success_rate": rate, "env": env,
                "perf": {"avg_latency_ms": lat, "total_tokens": tok},
                "cases": [{"id": "c1", "case_hash": "h-c1", "severity": "major",
                           "passed": passed, "flaky": False, "final_output": out}]}
    d = compare_reports(rep(0.75, "没查到", 100, 1000, passed=False),
                        rep(1.0, "4599 元", 260, 2400))
    assert d["case_diffs"][0]["id"] == "c1"
    assert d["perf_delta"]["avg_latency_ms"]["delta"] == 160.0
    assert d["perf_delta"]["total_tokens"]["delta"] == 1400
    assert d["verdict"].startswith("promote")
    assert "逐题变化" in format_compare(d) and "性能变化" in format_compare(d)


def test_compare_reports_env_mismatch_invalidates():
    def rep(env, rate):
        return {"success_rate": rate, "env": env,
                "cases": [{"id": "c1", "case_hash": "h-c1", "severity": "major",
                           "passed": True, "flaky": False}]}
    d = compare_reports(rep("staging", 0.70), rep("production", 0.95))
    assert d["env_mismatch"] is True
    assert d["verdict"].startswith("invalid") and "环境不一致" in d["verdict"]
    assert "环境不一致" in format_compare(d)


def test_compare_reports_legacy_format_still_works():
    """旧报告（无 fingerprint / perf / final_output）不能炸。"""
    def rep(rate):
        return {"total": 10, "passed": round(rate * 10), "success_rate": rate,
                "cases": [{"id": f"c{i}", "case_hash": f"h-c{i}", "severity": "major",
                           "passed": True, "flaky": False} for i in range(10)]}
    d = compare_reports(rep(0.70), rep(0.85))
    assert d["case_diffs"] == [] and d["perf_delta"] == {}
    assert d["verdict"].startswith("promote")


def test_compare_rejects_removed_failed_cases():
    old = {"success_rate": 0.7, "cases": [
        {"id": str(i), "case_hash": f"h-{i}", "passed": i < 7} for i in range(10)]}
    new = {"success_rate": 1.0, "cases": old["cases"][:7]}
    diff = compare_reports(old, new)
    assert diff["delta_pp"] == 30.0
    assert diff["removed_cases"] == ["7", "8", "9"]
    assert diff["evalset_mismatch"] and diff["verdict"].startswith("invalid")


def test_compare_rejects_changed_expectation_with_same_id():
    runner = BatchRunner(EvidenceAdapter(output="ok"))
    old = report_to_dict(runner.run_all([_case(response_contains=["missing"])]))
    new = report_to_dict(runner.run_all([_case(response_contains=["ok"])]))
    assert old["success_rate"] == 0 and new["success_rate"] == 1
    diff = compare_reports(old, new)
    assert diff["changed_cases"] == [old["cases"][0]["id"]]
    assert diff["verdict"].startswith("invalid")


def test_repeat_exports_first_failed_trajectory():
    class RepeatAdapter(AgentAdapter):
        calls = 0

        def run(self, query, context):
            if context["is_last_turn"]:
                self.calls += 1
            return AgentRun(query=query, final_output=(
                "ok" if self.calls < 2 else f"bad-{self.calls}"))

    case = _case(severity="blocker", turns=[{
        "query": "follow-up", "response_contains": ["ok"]}])
    report = BatchRunner(RepeatAdapter()).run_all([case], repeat=3)
    outcome = report.outcomes[0]
    assert not outcome.passed and outcome.flaky and outcome.pass_runs == 1
    assert len(outcome.runs) == 2
    failure = collect_failures(report)[0]
    assert failure["actual"]["final_output"] == "bad-2"
    assert failure["failed_assertions"][0]["kind"] == "response_contains"
    assert outcome.first_error and outcome.attribution == "trajectory"


# ---------- evalset 新字段校验 ----------

def test_evalset_rejects_bad_new_fields(tmp_path):
    p = tmp_path / "e.json"
    base = {"id": "a", "query": "q", "severity": "major"}
    bad_variants = [
        {**base, "dataset": 123},
        {**base, "tool_result_used": [{"name": "t"}]},
        {**base, "tool_result_used": [{"tool": "t", "min_values": 0}]},
        {**base, "output_grounded_in": {"sources": ["bad_source"]}},
        {**base, "output_grounded_in": "not-a-dict"},
    ]
    for item in bad_variants:
        p.write_text(json.dumps([item], ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError):
            load_evalset(p)


def test_evalset_accepts_valid_new_fields(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps([{
        "id": "a", "query": "q", "severity": "major", "dataset": "baseline",
        "tool_result_used": [{"tool": "t", "min_values": 2, "values": ["V1"]}],
        "output_grounded_in": {"min_values": 1,
                               "sources": ["tool_results", "retrieved"]},
    }], ensure_ascii=False), encoding="utf-8")
    c = load_evalset(p)[0]
    assert c.dataset == "baseline" and c.tool_result_used[0]["min_values"] == 2
    assert c.output_grounded_in["min_values"] == 1
