"""评估器骨架 + 一致率校准。

对应文档：
- 4.4 分维度精评：6 个维度，5 个有可用提示词（意图理解 / 知识召回 / 表达质量 /
  信息准确性 / 兜底质量），实时数据一致性不走离线评测
- 4.5 权重与路由归一化
- 5.2 金标集校准：机器评判先过一致率门槛，才准上岗；季度复校
- 文档 2.2：证据不足输出「需复核」，不硬判
"""
from __future__ import annotations

import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from models import CaseResult, DimResult, Snapshot, Verdict


# ---------- 可评测维度（实时数据一致性明确排除） ----------

EVAL_DIMS: dict[str, dict] = {
    "intent":        {"name": "意图理解",   "weight": 0.30},
    "retrieval":     {"name": "知识召回",   "weight": 0.25},
    "expression":    {"name": "表达质量",   "weight": 0.10},
    "info_accuracy": {"name": "信息准确性", "weight": 0.25},
    "fallback":      {"name": "兜底质量",   "weight": 0.10},
}
# 权重之和 = 1.00。文档第六节指出原型代码用了 0.2/0.2/0.2/0.2/0.2，与正文不符，
# 此处以正文 30/25/25/10/10 为准（见方案 v2.0 第 3 章口径统一说明）。


class LLMClient(ABC):
    """LLM 调用接口。真实模型接入时实现它。"""

    @abstractmethod
    def judge(self, dim: str, prompt_version: str, snapshot: Snapshot) -> tuple[Verdict, list[str]]:
        """返回（判定，证据列表）。证据必须引用 snapshot 中的具体字段。"""


@dataclass
class DimEvaluator:
    """单维度评估器。内部持有一个 LLM 客户端 + 提示词版本。"""

    dim: str
    prompt_version: str
    client: LLMClient
    calibrated: bool = False     # 未校准不允许参与门禁
    spearman: float = 0.0
    agreement: float = 0.0

    def evaluate(self, snapshot: Snapshot) -> DimResult:
        if not self.calibrated:
            raise RuntimeError(
                f"维度 {self.dim} 未通过金标集校准，不能参与评分"
                "（文档 5.2：先校准再托管）"
            )
        verdict, evidence = self.client.judge(self.dim, self.prompt_version, snapshot)
        w = EVAL_DIMS[self.dim]["weight"]
        return DimResult(
            dim=self.dim,
            verdict=verdict,
            score=verdict.value,
            triggered=True,
            evidence=evidence,
            weight=w,
        )


# ---------- 路由：决定一条 Query 该跑哪些维度 ----------

ROUTE_TABLE: dict[str, list[str]] = {
    # route -> 该跑的维度。路由是前置的，不跑全量维度。
    "product_consult":  ["intent", "retrieval", "expression", "info_accuracy"],
    "price_query":      ["intent", "expression"],       # 实时数据维度缺席，走报表
    "small_talk":       ["intent", "expression"],
    "unanswerable":     ["intent", "fallback"],
}


class Router:
    """前置路由：一条 Query 只触发相关维度，其余维度标记 triggered=False。"""

    def route(self, query: str, route_hint: str) -> list[str]:
        dims = ROUTE_TABLE.get(route_hint)
        if not dims:
            # 未登记的路由 —— 保守起见全维度跑，并标记需复核
            return list(EVAL_DIMS.keys())
        return dims


# ---------- 权重归一化 + 综合分 ----------

def normalize_weights(results: list[DimResult]) -> list[DimResult]:
    """方法二 4.5：没触发的维度不参与计算，剩余权重等比例放大到 1.0。"""
    triggered = [d for d in results if d.triggered]
    total = sum(d.weight for d in triggered)
    if total == 0:
        return triggered
    for d in triggered:
        d.weight = d.weight / total
    return triggered


def composite_score(results: list[DimResult]) -> float:
    """综合分 = sum(score × weight)。权重先经 normalize_weights 归一。"""
    normed = normalize_weights(results)
    if not normed:
        raise ValueError("没有触发的维度，无法计算综合分")
    return round(sum(d.score * d.weight for d in normed), 4)


# ---------- 金标集校准 ----------

def spearman(xs: list[float], ys: list[float]) -> float:
    """秩相关。衡量机器给出的好坏**排序**是否与人工一致。"""
    n = len(xs)
    if n != len(ys) or n < 2:
        raise ValueError("金标集样本数不足或长度不一致")
    rx = _ranks(xs)
    ry = _ranks(ys)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1 - (6 * d2) / (n * (n * n - 1))


def _ranks(vals: list[float]) -> list[float]:
    """带并列的秩（平均法）。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def agreement_rate(machine: list[float], human: list[float]) -> float:
    """一致率 = 完全一致样本数 / 总数（粗筛用的口径）。"""
    if len(machine) != len(human) or not machine:
        raise ValueError("样本数不一致或为空")
    hits = sum(1 for m, h in zip(machine, human) if m == h)
    return hits / len(machine)


@dataclass
class CalibrationReport:
    dim: str
    n: int
    agreement: float
    spearman: float
    pass_agreement: bool      # ≥ 0.85（粗筛门槛）
    pass_spearman: bool       # ≥ 0.80（精评门槛）
    qualified: bool


def calibrate(dim: str, machine_scores: list[float], human_scores: list[float]) -> CalibrationReport:
    """金标集校准。门槛来自文档 5.2：一致率 ≥85%，Spearman ≥0.8。"""
    if len(machine_scores) < 200:
        # 文档 5.2：金标集 200-500 条。少于 200 不给上岗，防止小样本过拟合。
        raise ValueError(f"金标集不足 200 条（当前 {len(machine_scores)}），不能校准")
    ag = agreement_rate(machine_scores, human_scores)
    sp = spearman(machine_scores, human_scores)
    ok_ag = ag >= 0.85
    ok_sp = sp >= 0.80
    return CalibrationReport(
        dim=dim,
        n=len(machine_scores),
        agreement=round(ag, 4),
        spearman=round(sp, 4),
        pass_agreement=ok_ag,
        pass_spearman=ok_sp,
        qualified=ok_ag and ok_sp,
    )


# ---------- 证据不足的降级 ----------

def needs_review(result: CaseResult, reason: str) -> CaseResult:
    """文档 2.2：证据不足 → 「需复核」，不硬判。打上标记后转人工队列。"""
    result.routed_to_human = True
    for d in result.dim_results:
        if not d.evidence:
            d.need_review = True
            d.evidence.append(f"NEED_REVIEW: {reason}")
    return result


def majority_vote(votes: list[Verdict]) -> Verdict:
    """粗筛投票。2/3 否决规则：至少 2 个判 FAIL 才算 FAIL。"""
    if len(votes) != 3:
        raise ValueError("粗筛投票必须是 3 个模型")
    fails = sum(1 for v in votes if v == Verdict.FAIL)
    if fails >= 2:
        return Verdict.FAIL
    if fails == 1:
        return Verdict.PARTIAL
    return Verdict.PASS


def mean(scores: list[float]) -> float:
    return statistics.mean(scores) if scores else 0.0


__all__ = [
    "EVAL_DIMS", "LLMClient", "DimEvaluator", "Router",
    "normalize_weights", "composite_score",
    "spearman", "agreement_rate", "calibrate", "CalibrationReport",
    "needs_review", "majority_vote", "mean",
    "Optional",
]
