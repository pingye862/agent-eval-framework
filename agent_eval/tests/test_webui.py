"""Web 控制台单测：报告序列化 / 用例字段保真 / 临时文件清理 / HTTP 适配器配置。

之前 server.py 全靠手工 E2E，这里把最容易静默出错的部分补齐。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "agent_eval"))
sys.path.insert(0, str(ROOT / "webui"))

import pytest

import server
from harness import AgentAdapter, AgentRun, BatchRunner
from evalset import EvalCase


class StubAdapter(AgentAdapter):
    def run(self, query, context):
        return AgentRun(query=query, final_output="ok")


# ---------- 报告序列化 ----------

def test_serialize_report_exposes_status_and_skipped():
    c = EvalCase(case_id="c1", query="q", severity="major",
                 judge_criteria=["必须回答完整"])
    r = BatchRunner(StubAdapter()).run_all([c], level="L1")   # 无 judge → 跳过
    d = server.serialize_report(r, True, [], 1.0)
    a = [x for x in d["cases"][0]["assertions"] if x["kind"] == "semantic"][0]
    assert a["skipped"] is True and a["status"] == "skipped"
    assert d["semantic_stats"]["skipped"] == 1


def test_serialize_report_failed_semantic_status():
    from judge import SemanticJudge

    class C:
        def chat(self, system, user):
            return json.dumps({"verdict": "fail", "evidence": ["不达标"]},
                              ensure_ascii=False)

    j = SemanticJudge(client=C(), calibrated=True)
    c = EvalCase(case_id="c1", query="q", severity="major",
                 judge_criteria=["必须回答完整"])
    r = BatchRunner(StubAdapter(), judge=j).run_all([c], level="L1")
    d = server.serialize_report(r, False, [], 1.0)
    a = [x for x in d["cases"][0]["assertions"] if x["kind"] == "semantic"][0]
    assert a["ok"] is False and a["status"] == "failed"


# ---------- 用例 CRUD 字段保真 ----------

def test_save_evalset_keeps_unknown_fields(tmp_path):
    p = tmp_path / "e.json"
    p.write_text("[]", encoding="utf-8")
    case = {"id": "c1", "query": "q", "severity": "major",
            "judge_criteria": ["标准A"], "context_overrides": {"vip": True},
            "persona": "测试用户"}
    server.save_evalset_cases(p, [case])
    saved = json.loads(p.read_text(encoding="utf-8"))[0]
    assert saved["judge_criteria"] == ["标准A"]
    assert saved["context_overrides"] == {"vip": True}
    assert saved["persona"] == "测试用户"


def test_save_evalset_cleans_tmp_on_validation_failure(tmp_path):
    p = tmp_path / "e.json"
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        server.save_evalset_cases(p, [{"id": "x", "query": "q"}])   # 缺 severity
    assert not (tmp_path / "e.json.tmp").exists(), "校验失败不能留临时文件"
    assert json.loads(p.read_text(encoding="utf-8")) == [], "原文件不能被破坏"


def test_validate_case_normalizes_judge_criteria_string():
    c = server._validate_case(
        {"id": "c1", "query": "q", "severity": "major",
         "judge_criteria": "标准一\n\n 标准二 \n"}, [])
    assert c["judge_criteria"] == ["标准一", "标准二"]


def test_validate_case_rejects_duplicate_id():
    with pytest.raises(ValueError, match="已存在"):
        server._validate_case({"id": "c1", "query": "q", "severity": "major"},
                              [{"id": "c1"}])


def test_resolve_evalset_path_rejects_unregistered(tmp_path):
    with pytest.raises(ValueError, match="未注册"):
        server.resolve_evalset_path(str(tmp_path / "evil.json"))


# ---------- 通用 HTTP 适配器 ----------

def test_generic_http_adapter_uses_configured_method():
    cfg = {"url": "http://127.0.0.1:9/x", "method": "PUT",
           "response_mapping": {"final_output": "data.answer"}}
    a = server.GenericHttpAdapter(cfg)
    assert a.method == "PUT"


def test_generic_http_adapter_default_method_post():
    a = server.GenericHttpAdapter({"url": "http://127.0.0.1:9/x"})
    assert a.method == "POST"


def test_generic_http_adapter_failure_becomes_agent_error():
    a = server.GenericHttpAdapter({"url": "http://127.0.0.1:9/x",
                                   "timeout": 1})
    run = a.run("q", {})
    assert run.error and "HTTP 调用失败" in run.error


def test_build_custom_adapter_rejects_empty_file():
    with pytest.raises(ValueError, match=r"\.py"):
        server.build_custom_adapter({"adapter_type": "python", "python": {"file": ""}})


def test_build_custom_adapter_rejects_non_py(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.py"):
        server.build_custom_adapter({"adapter_type": "python",
                                     "python": {"file": str(f)}})


# ---------- 覆盖账本公共接口 ----------

def test_ledger_units_is_public_readonly_view():
    from coverage import CoverageLedger
    from models import CoverageUnit, NotCoveredReason
    led = CoverageLedger()
    led.add(CoverageUnit(unit_id="U1", description="d", business_line="b",
                         not_covered_reason=NotCoveredReason.PENDING_DATA))
    units = led.units()
    assert len(units) == 1 and units[0].unit_id == "U1"
    units.clear()
    assert len(led.units()) == 1, "units() 返回副本，外部改不动账本"


# ---------- demo 并发竞态修复（ThreadingHTTPServer 下全局可变状态） ----------

def test_build_adapter_does_not_touch_globals():
    """构建红/绿两种 adapter 都不再改 agent.BUGS / NOTIFY_LOG 全局。"""
    import agent as demo_agent
    before_bugs = dict(demo_agent.BUGS)
    before_log_len = len(demo_agent.NOTIFY_LOG)
    server.build_adapter("demo", fixed=True)
    server.build_adapter("demo", fixed=False)
    assert demo_agent.BUGS == before_bugs
    assert len(demo_agent.NOTIFY_LOG) == before_log_len


def test_scoped_adapters_isolated_in_concurrent_batches():
    """红绿两个批次并发跑：BUGS 开关互不可见，结果各自稳定（回归竞态）。"""
    from concurrent.futures import ThreadPoolExecutor
    from evalset import load_evalset

    cases = load_evalset(str(server.DEMO_DIR / "evalset.json"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        red = pool.submit(lambda: server.BatchRunner(
            server.build_adapter("demo", fixed=False)).run_all(cases))
        green = pool.submit(lambda: server.BatchRunner(
            server.build_adapter("demo", fixed=True)).run_all(cases))
        red_r, green_r = red.result(), green.result()
    # 带缺陷批次保持拦截（成功率显著低于修复批次），且互不串扰
    assert green_r.success_rate == 1.0
    assert red_r.success_rate < green_r.success_rate


def test_demo_health_returns_true():
    """进程内 demo 探活恒绿（此前 .health() 直接 AttributeError）。"""
    assert server.build_adapter("demo", fixed=True).health() is True


# ---------- 数据飞轮增量：报告指纹 / 失败回流 / 数据集 ----------

def test_serialize_report_exposes_fingerprint_and_perf():
    from harness import ToolCallRecord
    class PerfAdapter(server.AgentAdapter):
        def run(self, query, context):
            return server.AgentRun(query=query, final_output="ok",
                                   latency_ms=100.0,
                                   token_usage={"total": 20},
                                   tool_calls=[ToolCallRecord("t", result="V1")])
    c = EvalCase(case_id="c1", query="q", severity="major", dataset="smoke")
    r = BatchRunner(PerfAdapter()).run_all([c], dataset="smoke", env="staging")
    d = server.serialize_report(r, True, [], 1.0)
    assert d["fingerprint"]["env"] == "staging"
    assert d["fingerprint"]["dataset"] == "smoke" and d["fingerprint"]["run_id"]
    assert d["perf"]["total_tokens"] == 20
    assert d["cases"][0]["tool_results"] == ["V1"]
    assert d["cases"][0]["dataset"] == "smoke"


def test_serialize_failures_are_candidates_not_answers():
    class BadAdapter(server.AgentAdapter):
        def run(self, query, context):
            return server.AgentRun(query=query, final_output="错误答案")
    c = EvalCase(case_id="c1", query="q", severity="blocker",
                 response_contains=["正确"])
    r = BatchRunner(BadAdapter()).run_all([c])
    from harness import collect_failures
    f = collect_failures(r)[0]
    assert f["suggested_case"]["id"] == "REG-c1"
    # 实际输出只进 actual（证据），绝不能变成新用例的 response_contains
    assert "错误答案" not in json.dumps(f["suggested_case"], ensure_ascii=False)


def test_safe_datasets_handles_bad_path():
    assert server._safe_datasets("/definitely/not/here.json") == {}


def test_promote_endpoint_rejects_unregistered_path(tmp_path):
    """回灌端点走评估集路径白名单，任意文件写入被拒绝。"""
    import harness
    fake = tmp_path / "f.json"
    src = tmp_path / "e.json"
    src.write_text("[]", encoding="utf-8")
    fake.write_text(json.dumps([{"case_id": "x", "suggested_case": {
        "id": "REG-x", "query": "q", "severity": "major"}}], ensure_ascii=False),
        encoding="utf-8")
    with pytest.raises(ValueError, match="未注册"):
        server.resolve_evalset_path(str(src))
    # 直接调库函数仍可用（CLI 走这条），端点层才做白名单
    out, n = harness.promote_failures(fake, src)
    assert n == 1 and out.name == "e.promoted.json"


def test_validate_case_keeps_flywheel_fields():
    """用例编辑表单不能静默丢掉 dataset / 证据接地字段（历史上出过这个 bug）。"""
    c = server._validate_case({
        "id": "c1", "query": "q", "severity": "major",
        "dataset": "baseline",
        "tool_result_used": [{"tool": "t", "min_values": 1}],
        "output_grounded_in": {"min_values": 2},
    }, [])
    assert c["dataset"] == "baseline"
    assert c["tool_result_used"] == [{"tool": "t", "min_values": 1}]
    assert c["output_grounded_in"] == {"min_values": 2}


def test_serialize_report_exposes_undetermined():
    """报告序列化必须暴露 undetermined 口径，且保留全部旧键。"""
    c = EvalCase(case_id="b1", query="q", severity="blocker",
                 judge_criteria=["须回答完整"])
    r = BatchRunner(StubAdapter()).run_all([c], level="L1")   # 无 judge → 全 skipped
    d = server.serialize_report(r, True, [], 1.0)
    for key in ("total", "passed", "success_rate", "blockers_failed",
                "fingerprint", "perf", "semantic_stats", "cases"):
        assert key in d
    assert d["undetermined"] == 1 and d["undetermined_total"] == 1
    assert d["determined_total"] == 0
    assert d["undetermined_cases"] == ["b1"]
    assert d["blockers_undetermined"] == ["b1"]
    assert d["cases"][0]["case_status"] == "undetermined"


def test_demo_evalset_label_count():
    """注册表标签里的用例数必须与实际 evalset.json 一致。"""
    assert "9 条" in server.EVALSETS["demo"]["label"]
