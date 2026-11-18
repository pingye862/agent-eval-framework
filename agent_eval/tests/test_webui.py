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
