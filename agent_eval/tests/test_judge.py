"""P2 语义判分单测：judge 降级 / 校准门槛 / harness 集成（层级、跳过、失败、need_review）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from harness import (AgentAdapter, AgentRun, BatchRunner, gate_decision)
from evalset import EvalCase
from judge import (JudgeError, OpenAICompatClient, SemanticJudge,
                   _parse_verdict, calibrate_judge, load_judge_from_env,
                   JUDGE_SYSTEM_PROMPT)


class FakeClient:
    """可编程假客户端：按提示词里的关键词决定返回。"""

    def __init__(self, reply=None, fail=False):
        self.reply = reply
        self.fail = fail
        self.calls = []

    def chat(self, system, user):
        self.calls.append({"system": system, "user": user})
        if self.fail:
            raise JudgeError("网络超时")
        return self.reply


class StubAdapter(AgentAdapter):
    def run(self, query, context):
        return AgentRun(query=query, final_output=context.get("out", "好的，已为你查询到 3 款商品"))


def _case(**kw):
    kw.setdefault("judge_criteria", ["回答必须包含商品数量"])
    return EvalCase(case_id=kw.pop("id", "c1"), query="推荐个充电宝",
                    severity=kw.pop("severity", "major"), **kw)


# ---------- 输出解析 ----------

def test_parse_plain_json():
    r = _parse_verdict('{"verdict": "pass", "evidence": ["提到了 3 款"]}')
    assert r["verdict"] == "pass" and r["evidence"] == ["提到了 3 款"]


def test_parse_with_code_fence_and_noise():
    r = _parse_verdict('好的，结果如下：\n```json\n{"verdict": "fail", "evidence": []}\n```')
    assert r["verdict"] == "fail"


def test_parse_invalid_verdict_becomes_need_review():
    assert _parse_verdict('{"verdict": "maybe", "evidence": []}')["verdict"] == "need_review"


def test_parse_garbage_becomes_need_review():
    assert _parse_verdict("我不是 JSON")["verdict"] == "need_review"


# ---------- judge 行为 ----------

def test_judge_failure_degrades_to_need_review():
    j = SemanticJudge(client=FakeClient(fail=True))
    r = j.judge("q", ["标准"], "回答")
    assert r["verdict"] == "need_review"
    assert "judge 调用降级" in r["evidence"][0]
    assert len(j.last_errors) == 1          # 失败留档


def test_judge_prompt_contains_criteria_query_output():
    c = FakeClient(reply='{"verdict": "pass", "evidence": []}')
    j = SemanticJudge(client=c)
    j.judge("问题Q", ["标准A", "标准B"], "回答R")
    user = c.calls[0]["user"]
    assert "标准A" in user and "标准B" in user and "问题Q" in user and "回答R" in user
    assert c.calls[0]["system"] == JUDGE_SYSTEM_PROMPT


# ---------- 校准 ----------

def test_calibrate_passes_on_perfect_labels():
    human = ["pass"] * 180 + ["fail"] * 20
    rep = calibrate_judge(list(human), human)
    assert rep.qualified and rep.agreement == 1.0 and rep.spearman == 1.0


def test_calibrate_below_agreement_gate_not_qualified():
    human = (["pass"] * 150 + ["fail"] * 50)
    machine = (["pass"] * 150 + ["fail"] * 10 + ["pass"] * 40)   # 一致率 80%
    rep = calibrate_judge(machine, human)
    assert not rep.qualified and rep.agreement == 0.8


def test_calibrate_need_review_counts_as_mismatch():
    human = ["pass"] * 200
    machine = ["pass"] * 199 + ["need_review"]
    rep = calibrate_judge(machine, human)
    assert rep.agreement == pytest.approx(0.995)   # need_review 按不一致计
    assert rep.pass_agreement                       # 但仍高于 85% 门槛


def test_calibrate_rejects_small_goldset():
    with pytest.raises(ValueError, match="不足 200"):
        calibrate_judge(["pass"] * 199, ["pass"] * 199)


def test_calibrate_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        calibrate_judge(["pass"], ["pass", "fail"])


# ---------- harness 集成 ----------

def test_l0_never_runs_judge():
    j = SemanticJudge(client=FakeClient(), calibrated=True)
    runner = BatchRunner(StubAdapter(), judge=j)
    o = runner.run_case(_case(), {"out": "随便"}, level="L0")
    assert j.client.calls == []          # L0 一个 LLM 调用都不能有
    assert o.assertions[-1].kind == "semantic" and o.assertions[-1].skipped
    assert o.passed                       # 跳过不影响判定


def test_uncalibrated_judge_is_skipped_at_l1():
    j = SemanticJudge(client=FakeClient())   # calibrated=False
    runner = BatchRunner(StubAdapter(), judge=j)
    o = runner.run_case(_case(), {}, level="L1")
    assert j.client.calls == []
    assert o.assertions[-1].skipped and "未通过金标校准" in o.assertions[-1].detail


def test_calibrated_judge_fail_fails_case_at_l1():
    reply = json.dumps({"verdict": "fail", "evidence": ["没提商品数量"]}, ensure_ascii=False)
    j = SemanticJudge(client=FakeClient(reply=reply), calibrated=True)
    runner = BatchRunner(StubAdapter(), judge=j)
    o = runner.run_case(_case(), {"out": "我不知道"}, level="L1")
    sem = [a for a in o.assertions if a.kind == "semantic"][0]
    assert not o.passed and not sem.skipped
    assert "语义判分不达标" in sem.detail
    assert sem.layer.value == "component"    # 语义失败归因 component


def test_calibrated_judge_pass_passes_case_at_l1():
    reply = json.dumps({"verdict": "pass", "evidence": []}, ensure_ascii=False)
    j = SemanticJudge(client=FakeClient(reply=reply), calibrated=True)
    runner = BatchRunner(StubAdapter(), judge=j)
    o = runner.run_case(_case(), {}, level="L1")
    assert o.passed


def test_need_review_does_not_hard_fail():
    reply = json.dumps({"verdict": "need_review", "evidence": ["证据不足"]}, ensure_ascii=False)
    j = SemanticJudge(client=FakeClient(reply=reply), calibrated=True)
    runner = BatchRunner(StubAdapter(), judge=j)
    o = runner.run_case(_case(), {}, level="L2")
    assert o.passed                          # 转人工但不硬判
    assert "证据不足转人工" in [a for a in o.assertions if a.kind == "semantic"][0].detail


def test_no_judge_criteria_no_semantic_assertion():
    j = SemanticJudge(client=FakeClient(), calibrated=True)
    runner = BatchRunner(StubAdapter(), judge=j)
    o = runner.run_case(EvalCase(case_id="c2", query="q", severity="minor"), {}, level="L1")
    assert all(a.kind != "semantic" for a in o.assertions)


def test_run_all_level_flows_to_semantic_and_report_stats():
    ok_reply = json.dumps({"verdict": "pass", "evidence": []}, ensure_ascii=False)
    bad_reply = json.dumps({"verdict": "fail", "evidence": ["不达标"]}, ensure_ascii=False)
    j = SemanticJudge(client=FakeClient(), calibrated=True,
                      cache_judgments=False)   # 两次用例内容相同，关缓存避免串判定
    runner = BatchRunner(StubAdapter(), judge=j)
    cases = [_case(id="c1"), _case(id="c2")]
    # 用两轮调用分别模拟 pass/fail
    j.client.reply = ok_reply
    r1 = runner.run_all(cases[:1], level="L1")
    j.client.reply = bad_reply
    r2 = runner.run_all(cases[1:], level="L1")
    assert r1.semantic_stats()["failed"] == 0 and r1.semantic_stats()["judged"] == 1
    assert r2.semantic_stats()["failed"] == 1
    assert "P2 语义判分" in r2.text()


def test_semantic_fail_in_blocker_blocks_gate():
    reply = json.dumps({"verdict": "fail", "evidence": ["不达标"]}, ensure_ascii=False)
    j = SemanticJudge(client=FakeClient(reply=reply), calibrated=True)
    runner = BatchRunner(StubAdapter(), judge=j)
    r = runner.run_all([_case(id="b1", severity="blocker")], level="L1")
    ok, reasons = gate_decision(r)
    assert not ok and any("blocker" in x for x in reasons)


# ---------- 环境变量加载 ----------

def test_load_judge_from_env_missing(monkeypatch):
    for k in ("EVAL_MODEL_URL", "EVAL_MODEL_NAME", "EVAL_MODEL_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert load_judge_from_env() is None


def test_load_judge_from_env_full(monkeypatch):
    monkeypatch.setenv("EVAL_MODEL_URL", "https://api.example.com/v1/chat/completions")
    monkeypatch.setenv("EVAL_MODEL_NAME", "test-model")
    monkeypatch.setenv("EVAL_MODEL_KEY", "sk-test")
    j = load_judge_from_env()
    assert isinstance(j, SemanticJudge)
    assert j.client.model == "test-model" and j.client.api_key == "sk-test"


def test_openai_client_direct_opener():
    c = OpenAICompatClient("https://api.example.com/v1/chat/completions", "k", "m")
    assert c.base_url.endswith("chat/completions")


# ---------- judge 缓存与重试 ----------

def test_judge_cache_hits_on_identical_input():
    c = FakeClient(reply='{"verdict": "pass", "evidence": []}')
    j = SemanticJudge(client=c)
    a = j.judge("q", ["标准"], "回答")
    b = j.judge("q", ["标准"], "回答")
    assert a == b and len(c.calls) == 1       # pass^k 复跑不重复烧 token


def test_judge_cache_does_not_hide_different_output():
    c = FakeClient(reply='{"verdict": "pass", "evidence": []}')
    j = SemanticJudge(client=c)
    j.judge("q", ["标准"], "回答A")
    j.judge("q", ["标准"], "回答B")
    assert len(c.calls) == 2


def test_judge_need_review_not_cached():
    bad = FakeClient(reply='{"verdict": "need_review", "evidence": ["证据不足"]}')
    j = SemanticJudge(client=bad)
    j.judge("q", ["标准"], "回答")
    bad.reply = '{"verdict": "pass", "evidence": []}'
    assert j.judge("q", ["标准"], "回答")["verdict"] == "pass"
    assert len(bad.calls) == 2                  # 网络/证据恢复后允许重判


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {
            "content": '{"verdict": "pass", "evidence": []}'}}]}).encode("utf-8")


def _patched_opener(monkeypatch, fail_times):
    """让 opener.open 前 fail_times 次抛超时，之后返回正常响应。"""
    import judge as jmod
    state = {"n": 0}

    class Opener:
        def open(self, req, timeout=None):
            state["n"] += 1
            if state["n"] <= fail_times:
                raise OSError("瞬时超时")
            return _FakeResp()

    monkeypatch.setattr(jmod.urllib.request, "build_opener", lambda *a: Opener())
    return state


def test_judge_retry_then_succeeds(monkeypatch):
    state = _patched_opener(monkeypatch, fail_times=2)
    c = OpenAICompatClient("https://x/v1/chat/completions", "k", "m", retries=2)
    j = SemanticJudge(client=c)
    assert j.judge("q", ["标准"], "回答")["verdict"] == "pass"
    assert state["n"] == 3


def test_judge_all_retries_fail_degrades(monkeypatch):
    state = _patched_opener(monkeypatch, fail_times=99)
    c = OpenAICompatClient("https://x/v1/chat/completions", "k", "m", retries=1)
    j = SemanticJudge(client=c)
    r = j.judge("q", ["标准"], "回答")
    assert r["verdict"] == "need_review" and "重试" in r["evidence"][0]
    assert state["n"] == 2


# ---------- 断言显式状态 ----------

def test_result_status_is_explicit_not_parsed_from_detail():
    from harness import AssertionResult
    assert AssertionResult("x", True, "").result_status() == "passed"
    assert AssertionResult("x", False, "失败").result_status() == "failed"
    assert AssertionResult("x", True, "跳过", skipped=True).result_status() == "skipped"
    assert AssertionResult("x", True, "随便什么文案", skipped=True,
                           status="need_review").result_status() == "need_review"


def test_semantic_stats_and_review_cases_use_status():
    reply = json.dumps({"verdict": "need_review", "evidence": ["证据不足"]},
                       ensure_ascii=False)
    j = SemanticJudge(client=FakeClient(reply=reply), calibrated=True)
    runner = BatchRunner(StubAdapter(), judge=j)
    r = runner.run_all([_case()], level="L1")
    assert r.semantic_stats()["need_review"] == 1
    assert len(r.review_cases()) == 1
    assert "待复核" in r.text()


def test_repeat_keeps_multi_turn_trajectories():
    from harness import write_snapshots
    import tempfile
    j = SemanticJudge(client=FakeClient(reply='{"verdict": "pass", "evidence": []}'),
                      calibrated=True)
    runner = BatchRunner(StubAdapter(), judge=j)
    c = EvalCase(case_id="m1", query="q1", severity="major",
                 turns=[{"query": "q2", "response_contains": ["好的"]}])
    r = runner.run_all([c], level="L1", repeat=3)
    assert r.outcomes[0].runs is not None and len(r.outcomes[0].runs) == 2
    with tempfile.TemporaryDirectory() as d:
        n = write_snapshots(f"{d}/s.jsonl", r)
        assert n == 2, f"复跑后快照应保留 2 轮，实际 {n}"


def test_snapshot_records_semantic_evidence():
    from harness import write_snapshots
    import tempfile
    reply = json.dumps({"verdict": "fail", "evidence": ["没给数量"]}, ensure_ascii=False)
    j = SemanticJudge(client=FakeClient(reply=reply), calibrated=True)
    runner = BatchRunner(StubAdapter(), judge=j)
    r = runner.run_all([_case()], level="L1")
    with tempfile.TemporaryDirectory() as d:
        p = f"{d}/s.jsonl"
        write_snapshots(p, r)
        rec = json.loads(open(p, encoding="utf-8").readline())
        assert rec["semantic"][0]["status"] == "failed"
        assert "语义判分不达标" in rec["semantic"][0]["detail"]
