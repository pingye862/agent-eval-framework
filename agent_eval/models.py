"""数据模型。

对应文档：
- 覆盖单元 / 三层评测 —— 方法一 3.2
- 用例资产 —— 方法一 3.3
- 未覆盖原因 6 类 —— 方法一 3.8
- 快照 —— 方法二 4.9
- 判定 0/1 或 0/0.5/1 —— 方法二 4.6
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class EvalLayer(enum.Enum):
    """三层评测。方法一 3.2：系统级默认，轨迹级需可观测，组件级需用户确认。"""

    SYSTEM = "system"
    TRAJECTORY = "trajectory"
    COMPONENT = "component"


class NotCoveredReason(enum.Enum):
    """覆盖账本中未覆盖单元的 6 类原因。方法一 3.8：只标注，不删除。"""

    PENDING_CLARIFICATION = "待澄清"          # 依赖澄清结果
    PENDING_DATA = "待补数据"                  # 证据缺口待补
    NOT_OBSERVABLE = "不可观测"                # 轨迹字段缺失
    SCOPE_EXCLUDED = "范围排除"                # 用户明确排除
    RISK_WAIVED = "风险豁免"                   # 接受风险不测
    NOT_APPLICABLE = "不适用"                  # 场景不存在


class AttributionLayer(enum.Enum):
    """主归因层。一个失败只归因一层（方法一 3.6 / 方法二 4.8）。"""

    SYSTEM = "system"        # 召回 / 知识片段 / 快照等系统能力
    TRAJECTORY = "trajectory"  # 意图识别、工具选择、执行路径
    COMPONENT = "component"  # 单个组件的行为（提示词、评估器本身）


class Verdict(enum.Enum):
    """单维度判定。方法二 4.6：0/1，或人工评判的 0/0.5/1。"""

    PASS = 1.0
    PARTIAL = 0.5
    FAIL = 0.0


class GateVerdict(enum.Enum):
    """回放门禁。方法二 4.11 的四档。"""

    PROMOTE = "提升可合并"
    HOLD = "保持不合并"
    INVESTIGATE = "显著下降需排查"
    INVALID = "环境不稳本次作废"


@dataclass
class CoverageUnit:
    """覆盖单元：评测的最小设计单位（方法一 3.2）。"""

    unit_id: str
    description: str
    business_line: str
    layer: EvalLayer = EvalLayer.SYSTEM
    covered_by: list[str] = field(default_factory=list)   # 用例 id 列表
    not_covered_reason: Optional[NotCoveredReason] = None
    risk_level: str = "normal"   # low / normal / high


@dataclass
class TestCase:
    """用例资产。方法一 3.3。"""

    case_id: str
    unit_id: str
    query: str
    expected_behavior: str
    layer: EvalLayer
    dims: list[str] = field(default_factory=list)         # 关联的评估维度
    expected_tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)     # 引用字段，禁止无证据预期


@dataclass
class Snapshot:
    """快照（方法二 4.9）。回放的确定性来源。"""

    trace_id: str
    query: str
    retrieved: list[str] = field(default_factory=list)    # 锁定的召回结果
    kb_version: str = ""
    prompt_version: str = ""
    model_version: str = ""
    tool_desc_version: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_output: str = ""


@dataclass
class DimResult:
    """单维度评估结果。"""

    dim: str
    verdict: Verdict
    score: float                 # 1.0 / 0.5 / 0.0
    triggered: bool = True       # 路由是否触发该维度
    evidence: list[str] = field(default_factory=list)
    need_review: bool = False    # 证据不足，转人工（文档 2.2）
    weight: float = 0.0


@dataclass
class CaseResult:
    """一条 Query 的综合评估结果。"""

    trace_id: str
    dim_results: list[DimResult] = field(default_factory=list)
    composite: Optional[float] = None        # 路由归一化后的综合分
    first_error_step: Optional[str] = None   # 链路第一错（方法二 4.8）
    attribution: Optional[AttributionLayer] = None
    routed_to_human: bool = False
    human_score: Optional[float] = None

    def triggered_results(self) -> list[DimResult]:
        return [d for d in self.dim_results if d.triggered]
