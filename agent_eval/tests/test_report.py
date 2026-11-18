"""覆盖账本可视化单测：规格加载校验 / 文本渲染 / HTML 生成与内容完整性。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from coverage import CoverageLedger
from models import CoverageUnit, EvalLayer, NotCoveredReason
from report import build_ledger, render_coverage_html, render_coverage_text


def _spec(tmp_path, items=None):
    items = items if items is not None else [
        {"unit_id": "U001", "description": "商品推荐兜底", "business_line": "售前",
         "layer": "system", "covered_by": ["case-001"], "risk_level": "high"},
        {"unit_id": "U002", "description": "价格查询", "business_line": "售前",
         "layer": "trajectory", "covered_by": [], "not_covered_reason": "待补数据"},
        {"unit_id": "U003", "description": "多轮上下文", "business_line": "售后",
         "layer": "component", "covered_by": [], "not_covered_reason": "风险豁免",
         "risk_level": "high"},
    ]
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return p


# ---------- build_ledger ----------

def test_build_ledger_from_spec(tmp_path):
    led = build_ledger(_spec(tmp_path))
    r = led.report()
    assert r.total == 3 and r.covered == 1 and r.not_covered == 2
    assert r.by_reason == {"待补数据": 1, "风险豁免": 1}
    assert r.by_layer["system"] == {"covered": 1, "not": 0}
    assert r.uncovered_high_risk == ["U003"]       # 高风险豁免 = 审计不过


def test_build_ledger_rejects_bad_layer(tmp_path):
    items = [{"unit_id": "X", "description": "", "business_line": "",
              "layer": "wrong_layer", "covered_by": []}]
    with pytest.raises(ValueError, match="layer"):
        build_ledger(_spec(tmp_path, items))


def test_build_ledger_rejects_bad_reason(tmp_path):
    items = [{"unit_id": "X", "description": "", "business_line": "",
              "layer": "system", "covered_by": [], "not_covered_reason": "不知道"}]
    with pytest.raises(ValueError, match="not_covered_reason"):
        build_ledger(_spec(tmp_path, items))


def test_build_ledger_requires_unit_id(tmp_path):
    items = [{"description": "没有 id", "business_line": "", "layer": "system"}]
    with pytest.raises(KeyError):
        build_ledger(_spec(tmp_path, items))


# ---------- render_coverage_text ----------

def test_text_report_shows_rate_and_high_risk(tmp_path):
    led = build_ledger(_spec(tmp_path))
    txt = render_coverage_text(led.report())
    assert "覆盖率 33.3%" in txt
    assert "⛔ 高风险且未覆盖" in txt and "U003" in txt


def test_text_report_clean_when_no_high_risk(tmp_path):
    items = [{"unit_id": "A", "description": "", "business_line": "",
              "layer": "system", "covered_by": ["c1"]}]
    txt = render_coverage_text(build_ledger(_spec(tmp_path, items)).report())
    assert "高风险单元零豁免" in txt


# ---------- render_coverage_html ----------

def test_html_report_written_and_complete(tmp_path):
    led = build_ledger(_spec(tmp_path))
    out = render_coverage_html(led, tmp_path / "coverage.html")
    assert out.exists()
    html_text = out.read_text(encoding="utf-8")
    # 概览数字
    assert "覆盖率" in html_text and "33.3%" in html_text
    # 审计横幅（有高风险豁免 → 不通过）
    assert "审计不通过" in html_text
    # 三层拆解 + 6 类原因分布 + 明细表
    assert "system" in html_text and "trajectory" in html_text and "component" in html_text
    assert "待补数据" in html_text and "风险豁免" in html_text
    for uid in ("U001", "U002", "U003"):
        assert uid in html_text
    assert "已覆盖" in html_text and html_text.count("pill") >= 3
    # 单文件：无外链资源
    assert "http://" not in html_text.replace("http://www.w3.org", "") or "src=" not in html_text


def test_html_report_audit_pass_banner(tmp_path):
    items = [{"unit_id": "A", "description": "", "business_line": "",
              "layer": "system", "covered_by": ["c1"]}]
    led = build_ledger(_spec(tmp_path, items))
    out = render_coverage_html(led, tmp_path / "ok.html")
    assert "审计通过" in out.read_text(encoding="utf-8")


def test_html_escaping(tmp_path):
    items = [{"unit_id": "U<1>", "description": "<script>alert(1)</script>",
              "business_line": "a&b", "layer": "system",
              "covered_by": ["c&1"]}]
    out = render_coverage_html(build_ledger(_spec(tmp_path, items)),
                               tmp_path / "esc.html")
    text = out.read_text(encoding="utf-8")
    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text


# ---------- 与手工账本等价 ----------

def test_build_ledger_matches_manual(tmp_path):
    spec = build_ledger(_spec(tmp_path))
    manual = CoverageLedger()
    manual.add(CoverageUnit(unit_id="U001", description="商品推荐兜底",
                            business_line="售前", covered_by=["case-001"],
                            risk_level="high"))
    manual.add(CoverageUnit(unit_id="U002", description="价格查询",
                            business_line="售前", layer=EvalLayer.TRAJECTORY,
                            not_covered_reason=NotCoveredReason.PENDING_DATA))
    manual.add(CoverageUnit(unit_id="U003", description="多轮上下文",
                            business_line="售后", layer=EvalLayer.COMPONENT,
                            not_covered_reason=NotCoveredReason.RISK_WAIVED,
                            risk_level="high"))
    a, b = spec.report(), manual.report()
    assert (a.total, a.covered, a.by_reason) == (b.total, b.covered, b.by_reason)
