"""归因引擎测试：单归因 / 链路第一错 / blame_once 去重。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from attribution import FirstErrorFinder, TraceStep, blame_once, single_attribution
from models import AttributionLayer


def test_first_error_picks_earliest():
    steps = [
        TraceStep("s1", "rule_check", "意图识别", ok=True),
        TraceStep("s2", "llm_vote", "工具选择", ok=False, detail="选错工具"),
        TraceStep("s3", "rule_check", "最终回复", ok=False),
    ]
    assert FirstErrorFinder(steps).find().step_id == "s2"


def test_first_error_none_when_all_pass():
    steps = [TraceStep("s1", "rule_check", "意图识别", ok=True)]
    assert FirstErrorFinder(steps).find() is None


def test_inconclusive_steps_collected():
    steps = [
        TraceStep("s1", "rule_check", "意图识别", ok=None),
        TraceStep("s2", "rule_check", "回复", ok=True),
    ]
    assert [s.step_id for s in FirstErrorFinder(steps).inconclusive()] == ["s1"]


def test_single_attribution_prefers_system():
    got = single_attribution({
        AttributionLayer.SYSTEM: True,
        AttributionLayer.TRAJECTORY: True,
        AttributionLayer.COMPONENT: True,
    })
    assert got == AttributionLayer.SYSTEM


def test_single_attribution_falls_through():
    got = single_attribution({AttributionLayer.COMPONENT: True})
    assert got == AttributionLayer.COMPONENT


def test_single_attribution_rejects_empty():
    with pytest.raises(ValueError):
        single_attribution({})


def test_blame_once_keeps_one_per_layer():
    errors = [
        ("s1", AttributionLayer.SYSTEM),
        ("s2", AttributionLayer.TRAJECTORY),
        ("s3", AttributionLayer.TRAJECTORY),   # 同层第二条，应被去掉
    ]
    out = blame_once(errors)
    assert out == [("s1", AttributionLayer.SYSTEM), ("s2", AttributionLayer.TRAJECTORY)]
