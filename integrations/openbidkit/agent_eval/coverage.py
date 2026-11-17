"""覆盖账本。

对应文档方法一 3.2 / 3.8：
- 覆盖单元是评测的最小设计单位
- 没有用例承接的单元**留在账本里**，标 6 类原因之一，不从范围里删除
- 覆盖率要能审计：按层、按业务线都能拆出来
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from models import CoverageUnit, EvalLayer, NotCoveredReason


@dataclass
class CoverageReport:
    total: int
    covered: int
    not_covered: int
    by_reason: dict[str, int]
    by_layer: dict[str, dict[str, int]]
    uncovered_high_risk: list[str]   # 高风险且未覆盖 —— 必须处理


class CoverageLedger:
    """覆盖账本。审计用，不参与打分。"""

    def __init__(self) -> None:
        self._units: dict[str, CoverageUnit] = {}

    def add(self, unit: CoverageUnit) -> None:
        self._units[unit.unit_id] = unit

    def mark_not_covered(self, unit_id: str, reason: NotCoveredReason) -> None:
        """标注未覆盖原因。已覆盖的单元不允许再标——覆盖优先于豁免。"""
        unit = self._units[unit_id]
        if unit.covered_by:
            raise ValueError(f"单元 {unit_id} 已有覆盖用例，不能标未覆盖")
        unit.not_covered_reason = reason

    def report(self) -> CoverageReport:
        by_reason: dict[str, int] = defaultdict(int)
        by_layer: dict[str, dict[str, int]] = defaultdict(lambda: {"covered": 0, "not": 0})
        uncovered_high_risk: list[str] = []

        covered = 0
        for u in self._units.values():
            bucket = by_layer[u.layer.value]
            if u.covered_by:
                covered += 1
                bucket["covered"] += 1
            else:
                reason = u.not_covered_reason
                if reason is None:
                    # 未覆盖又没标原因 —— 账本不完整，审计不过
                    raise ValueError(f"单元 {u.unit_id} 未覆盖且未标注原因")
                by_reason[reason.value] += 1
                bucket["not"] += 1
                if u.risk_level == "high":
                    uncovered_high_risk.append(u.unit_id)

        return CoverageReport(
            total=len(self._units),
            covered=covered,
            not_covered=len(self._units) - covered,
            by_reason=dict(by_reason),
            by_layer={k: dict(v) for k, v in by_layer.items()},
            uncovered_high_risk=uncovered_high_risk,
        )

    def audit_pass(self) -> bool:
        """审计通过 = 账本完整 + 高风险单元零豁免。"""
        r = self.report()
        return not r.uncovered_high_risk
