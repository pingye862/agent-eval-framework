"""评估器测试：权重归一化 / 综合分 / 金标集校准 / 2/3 投票 / 秩相关。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from evaluator import (
    EVAL_DIMS, composite_score, normalize_weights, calibrate,
    agreement_rate, majority_vote, spearman,
)
from models import DimResult, Verdict


def d(dim, score, triggered=True):
    return DimResult(dim=dim, verdict=Verdict(score), score=score,
                     triggered=triggered, weight=EVAL_DIMS[dim]["weight"])


def test_weights_sum_to_one():
    assert abs(sum(v["weight"] for v in EVAL_DIMS.values()) - 1.0) < 1e-9


def test_normalize_excludes_untriggered():
    rs = [d("intent", 1.0), d("retrieval", 1.0, triggered=False), d("expression", 0.0)]
    normed = normalize_weights(rs)
    # 意图 0.30 + 表达 0.10 归一 → 0.75 / 0.25
    got = {x.dim: round(x.weight, 4) for x in normed}
    assert got["intent"] == 0.75 and got["expression"] == 0.25


def test_composite_skips_untriggered_dims():
    # 兜底类 Query 只触发兜底维度 → 兜底 0 分就是综合分 0，不被其他维度稀释
    rs = [d("fallback", 0.0)] + [d(k, 1.0, triggered=False)
                                 for k in ("intent", "retrieval", "expression", "info_accuracy")]
    assert composite_score(rs) == 0.0


def test_composite_full_pass():
    rs = [d(k, 1.0) for k in EVAL_DIMS]
    assert composite_score(rs) == 1.0


def test_spearman_perfect_and_inverse():
    assert spearman([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0


def test_spearman_with_ties():
    # 人工打分常见 0/0.5/1，并列很多 —— 秩要用平均法
    s = spearman([1, 1, 0], [1, 1, 0])
    assert s == 1.0


def test_agreement_rate():
    assert agreement_rate([1, 0, 1, 1], [1, 0, 0, 1]) == 0.75


def test_calibrate_rejects_small_golden_set():
    # 金标集 < 200 条不允许上岗（文档 5.2）
    with pytest.raises(ValueError):
        calibrate("intent", [1.0] * 100, [1.0] * 100)


def test_majority_vote_two_thirds_rule():
    assert majority_vote([Verdict.FAIL, Verdict.FAIL, Verdict.PASS]) == Verdict.FAIL
    assert majority_vote([Verdict.FAIL, Verdict.PASS, Verdict.PASS]) == Verdict.PARTIAL
    assert majority_vote([Verdict.PASS] * 3) == Verdict.PASS
    with pytest.raises(ValueError):
        majority_vote([Verdict.PASS, Verdict.PASS])
