"""覆盖账本测试：6 类原因 / 已覆盖不能豁免 / 未标注不能审计通过 / 高风险零豁免。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from coverage import CoverageLedger
from models import CoverageUnit, EvalLayer, NotCoveredReason


def u(uid, risk="normal", layer=EvalLayer.SYSTEM):
    return CoverageUnit(unit_id=uid, description="", business_line="导购",
                        layer=layer, risk_level=risk)


def test_report_counts_reasons():
    led = CoverageLedger()
    led.add(u("A"))
    led.add(u("B"))
    led.add(u("C"))
    led.mark_not_covered("A", NotCoveredReason.PENDING_CLARIFICATION)
    led.mark_not_covered("B", NotCoveredReason.NOT_OBSERVABLE)
    led.mark_not_covered("C", NotCoveredReason.SCOPE_EXCLUDED)
    r = led.report()
    assert r.total == 3 and r.covered == 0 and r.not_covered == 3
    assert r.by_reason == {"待澄清": 1, "不可观测": 1, "范围排除": 1}


def test_covered_unit_cannot_be_waived():
    led = CoverageLedger()
    led.add(u("A"))
    led._units["A"].covered_by = ["case-1"]
    with pytest.raises(ValueError):
        led.mark_not_covered("A", NotCoveredReason.RISK_WAIVED)


def test_uncovered_without_reason_fails_audit():
    led = CoverageLedger()
    led.add(u("A"))
    with pytest.raises(ValueError):
        led.report()


def test_high_risk_uncovered_fails_audit():
    led = CoverageLedger()
    led.add(u("A", risk="high"))
    led.mark_not_covered("A", NotCoveredReason.RISK_WAIVED)
    assert led.audit_pass() is False


def test_layer_breakdown():
    led = CoverageLedger()
    led.add(u("A", layer=EvalLayer.SYSTEM))
    led.add(u("B", layer=EvalLayer.TRAJECTORY))
    led._units["A"].covered_by = ["c1"]
    led.mark_not_covered("B", NotCoveredReason.PENDING_DATA)
    r = led.report()
    assert r.by_layer["system"]["covered"] == 1
    assert r.by_layer["trajectory"]["not"] == 1
