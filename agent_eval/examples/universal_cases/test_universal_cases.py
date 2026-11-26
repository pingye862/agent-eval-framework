"""通用案例库自检：schema 合法、参考实现全绿、undetermined 能拦门禁。

单跑：
    python3 -m pytest test_universal_cases.py -q
    python3 test_universal_cases.py
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.environ.get("AGENT_EVAL_SRC") or os.path.normpath(
    os.path.join(HERE, "..", ".."))
for p in (_SRC, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_eval_lib import BatchRunner, gate_decision, load_evalset  # noqa: E402
from reference_agent import ReferenceAdapter  # noqa: E402

FULL = os.path.join(HERE, "evalset.json")
DEMO = os.path.join(HERE, "evalset.undetermined-demo.json")


def _run(path=FULL, dataset=None):
    cases = load_evalset(path, dataset=dataset)
    report = BatchRunner(ReferenceAdapter()).run_all(
        cases, level="L0", env="selfcheck", dataset=dataset)
    return report


def test_full_evalset_runs_all_green():
    """通过率口径全绿，但零漏审仍要求人工确认未判项。

    UC-011 只配了 P1 关键词 + judge_criteria；L0 无 judge，语义标准未判
    （skipped）。旧口径把这条算作「全绿」，新口径必须进审核队列并签核后才能放行。
    """
    from agent_eval_lib import (ReviewSignoff, SignoffSet,
                                review_fingerprint)
    report = _run()
    assert report.total == 11
    assert report.undetermined_total == 0
    assert report.passed == 11
    assert report.success_rate == 1.0
    assert [o.case.case_id for o in report.review_queue()] == ["UC-011"]
    ok, reasons = gate_decision(report)
    assert not ok and any("UC-011" in r for r in reasons)
    o = report.review_queue()[0]
    signoffs = SignoffSet([ReviewSignoff("UC-011", review_fingerprint(o),
                                         "approve", "selfcheck")])
    ok2, reasons2 = gate_decision(report, signoffs=signoffs)
    assert ok2, reasons2


def test_every_case_carries_a_real_assertion():
    """防止退化：任何用例只剩 no_error（结构性占位）就会变成 undetermined。"""
    report = _run()
    weak = [o.case.case_id for o in report.outcomes if not o.effective]
    assert weak == [], f"这些用例没有任何有效 P1/passed 断言: {weak}"


def test_smoke_subset_is_three_cases():
    report = _run(dataset="smoke")
    assert report.total == 3
    assert report.passed == 3


def test_dataset_layers_add_up():
    smoke = len(load_evalset(FULL, dataset="smoke"))
    regression = len(load_evalset(FULL, dataset="regression"))
    baseline = len(load_evalset(FULL, dataset="baseline"))
    assert (smoke, regression, baseline) == (3, 7, 1)
    assert smoke + regression + baseline == len(load_evalset(FULL))


def test_undetermined_demo_blocks_gate():
    """只有 judge_criteria、没有 P1 断言的 blocker 用例必须被拦，不能算通过。"""
    report = _run(DEMO)
    assert report.total == 1
    assert report.undetermined_total == 1
    assert report.determined_total == 0
    assert report.success_rate == 0.0
    ok, reasons = gate_decision(report)
    assert not ok
    assert any("未有效测试" in r for r in reasons), reasons


def test_render_roundtrip_keeps_cases_valid(tmp_path):
    """用等价 profile 渲染，产物仍合法且行为不变。"""
    from render import _load_profile, _rewrite

    prof = _load_profile(os.path.join(HERE, "profile.example.json"))
    raw = json.loads(open(FULL, encoding="utf-8").read())
    out = _rewrite(raw, prof, {"tool": 0, "path": 0, "value": 0})
    dest = tmp_path / "evalset.mine.json"
    dest.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    cases = load_evalset(str(dest))
    assert len(cases) == 11
    report = BatchRunner(ReferenceAdapter()).run_all(cases, level="L0")
    assert report.passed == 11


def test_render_applies_real_aliases(tmp_path):
    """真实改名后，案例里的工具名与期望值都应被替换。"""
    from render import _rewrite

    prof = {
        "tool_aliases": {"lookup": "search_kb", "compute": "run_calc"},
        "state_paths": {"audit_log": "audit.entries"},
        "arg_aliases": {"query": "keyword"},
        "value_aliases": {"ECHO-42": "TOKEN-9", "ALPHA-7": "DOC-123", "84": "126"},
    }
    raw = json.loads(open(FULL, encoding="utf-8").read())
    out = _rewrite(raw, prof, {"tool": 0, "path": 0, "value": 0})
    dest = tmp_path / "evalset.renamed.json"
    dest.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    cases = {c.case_id: c for c in load_evalset(str(dest))}
    assert [x["tool"] for x in cases["UC-003"].must_call] == ["search_kb"]
    assert cases["UC-003"].must_call[0]["required_args"] == ["keyword"]
    assert cases["UC-003"].response_contains == ["DOC-123"]
    assert cases["UC-004"].response_contains == ["126"]
    assert cases["UC-006"].state_after[0]["path"] == "audit.entries"
    assert cases["UC-001"].query.endswith("TOKEN-9，不要添加其他内容")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
