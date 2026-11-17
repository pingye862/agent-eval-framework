"""归因引擎。

对应文档：
- 方法一 3.6：系统、轨迹、组件三层中只认一个主归因层
- 方法二 4.8：沿处理链路找第一个出错的环节
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from models import AttributionLayer


@dataclass
class TraceStep:
    """链路上的一步。method 用来区分「环节检查」和「结果检查」。"""

    step_id: str
    method: str          # rule_check / llm_vote / snapshot_diff ...
    target: str          # 检查的对象：意图识别 / 工具选择 / 最终回复 ...
    ok: Optional[bool]   # None 表示该步没有产出可信结论
    detail: str = ""
    layer: AttributionLayer = AttributionLayer.TRAJECTORY


class FirstErrorFinder:
    """沿链路找第一个出错的环节。order 就是链路顺序，索引即优先级。"""

    def __init__(self, steps: list[TraceStep]) -> None:
        self.steps = steps

    def find(self) -> Optional[TraceStep]:
        for s in self.steps:
            if s.ok is False:
                return s
            # ok is None 且带 detail 的，视为证据不足，记入但不中断——
            # 由调用方决定是否降级为「需复核」
        return None

    def inconclusive(self) -> list[TraceStep]:
        return [s for s in self.steps if s.ok is None]


def single_attribution(candidates: dict[AttributionLayer, bool]) -> AttributionLayer:
    """三层只认一个主归因层。

    candidates: 层 -> 该层是否有问题
    返回优先级最高的有问题的层。顺序固定为 system > trajectory > component，
    因为系统层（召回 / 知识 / 快照）是上游，修好它，下游的轨迹错误往往跟着消失。
    """
    order = [AttributionLayer.SYSTEM, AttributionLayer.TRAJECTORY, AttributionLayer.COMPONENT]
    hits = [k for k in order if candidates.get(k)]
    if not hits:
        raise ValueError("没有任何层被标记为有问题，不能归因")
    return hits[0]


def blame_once(errors: list[tuple[str, AttributionLayer]]) -> list[tuple[str, AttributionLayer]]:
    """去重：同一 layer 的多个错误只保留第一个。

    这是对「一个失败只归因一次」的直接实现——
    即使轨迹层和组件层同时报错，也只报主归因层的那一个。
    """
    if not errors:
        return []
    seen: set[AttributionLayer] = set()
    out: list[tuple[str, AttributionLayer]] = []
    for step_id, layer in errors:
        if layer in seen:
            continue
        seen.add(layer)
        out.append((step_id, layer))
    return out
