"""回放测试：单变量保证 / 快照不可覆盖 / 四档门禁（含补齐的缺档）。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models import GateVerdict, Snapshot
from replay import ReplayError, ReplayRunner, SnapshotStore


def snap(tid="t1"):
    return Snapshot(trace_id=tid, query="iPhone 15 多少钱",
                    retrieved=["kb-1"], kb_version="kb-20261031",
                    prompt_version="p-v3", model_version="m-a")


def test_derive_single_variable_ok():
    store = SnapshotStore()
    store.save(snap())
    v = store.derive("t1", prompt_version="p-v4")
    assert v.prompt_version == "p-v4"
    assert v.kb_version == "kb-20261031"   # 其他变量保持锁定


def test_derive_rejects_multi_variable():
    store = SnapshotStore()
    store.save(snap())
    with pytest.raises(ReplayError):
        store.derive("t1", prompt_version="p-v4", model_version="m-b")


def test_derive_rejects_zero_variable():
    store = SnapshotStore()
    store.save(snap())
    with pytest.raises(ReplayError):
        store.derive("t1")


def test_snapshot_not_overwritable():
    store = SnapshotStore()
    store.save(snap())
    with pytest.raises(ReplayError):
        store.save(snap())


def test_gate_promote():
    r = ReplayRunner(lambda s: 0.0)
    assert r.gate(0.50, 0.65) == GateVerdict.PROMOTE      # +15pp


def test_gate_investigate():
    r = ReplayRunner(lambda s: 0.0)
    assert r.gate(0.80, 0.55) == GateVerdict.INVESTIGATE  # -25pp


def test_gate_hold_fills_the_missing_band():
    # 文档第六节指出的缺档：下降超过噪声带但未到排查线
    r = ReplayRunner(lambda s: 0.0)
    assert r.gate(0.80, 0.70) == GateVerdict.HOLD         # -10pp


def test_gate_invalid_in_noise_band():
    r = ReplayRunner(lambda s: 0.0)
    assert r.gate(0.80, 0.79) == GateVerdict.INVALID      # -1pp，环境不稳


def test_replay_runs_only_derived_snapshot():
    calls = []
    runner = ReplayRunner(lambda s: calls.append(s.prompt_version) or 0.5)
    base = snap()
    variant, score = runner.run(base, prompt_version="p-v4")
    assert variant.prompt_version == "p-v4"
    assert calls == ["p-v4"]   # 只跑变体，基线分数是外部传入的既有值
    assert score == 0.5
