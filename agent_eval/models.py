"""数据模型。

覆盖单元 / 三层评测 / 未覆盖原因 6 类 / 快照 / 回放门禁四档。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class EvalLayer(enum.Enum):
    """三层评测。系统级默认，轨迹级需可观测，组件级需用户确认。"""

    SYSTEM = "system"
    TRAJECTORY = "trajectory"
    COMPONENT = "component"


class NotCoveredReason(enum.Enum):
    """覆盖账本中未覆盖单元的 6 类原因。只标注，不删除。"""

    PENDING_CLARIFICATION = "待澄清"          # 依赖澄清结果
    PENDING_DATA = "待补数据"                  # 证据缺口待补
    NOT_OBSERVABLE = "不可观测"                # 轨迹字段缺失
    SCOPE_EXCLUDED = "范围排除"                # 用户明确排除
    RISK_WAIVED = "风险豁免"                   # 接受风险不测
    NOT_APPLICABLE = "不适用"                  # 场景不存在


class AttributionLayer(enum.Enum):
    """主归因层。一个失败只归因一层。"""

    SYSTEM = "system"        # 召回 / 知识片段 / 快照等系统能力
    TRAJECTORY = "trajectory"  # 意图识别、工具选择、执行路径
    COMPONENT = "component"  # 单个组件的行为（提示词、评估器本身）


class GateVerdict(enum.Enum):
    """回放门禁四档判定。"""

    PROMOTE = "提升可合并"
    HOLD = "保持不合并"
    INVESTIGATE = "显著下降需排查"
    INVALID = "环境不稳本次作废"


@dataclass
class CoverageUnit:
    """覆盖单元：评测的最小设计单位。"""

    unit_id: str
    description: str
    business_line: str
    layer: EvalLayer = EvalLayer.SYSTEM
    covered_by: list[str] = field(default_factory=list)   # 用例 id 列表
    not_covered_reason: Optional[NotCoveredReason] = None
    risk_level: str = "normal"   # low / normal / high


@dataclass
class Snapshot:
    """快照。回放的确定性来源。"""

    trace_id: str
    query: str
    retrieved: list[str] = field(default_factory=list)    # 锁定的召回结果
    kb_version: str = ""
    prompt_version: str = ""
    model_version: str = ""
    tool_desc_version: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_output: str = ""
