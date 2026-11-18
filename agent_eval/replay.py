"""快照与单变量回放。

对应文档：
- 快照：锁定召回结果 / 知识片段 / 提示词版本，回放的确定性来源
- 回放三种情形：换提示词 / 换模型 / 换知识库 —— 一次只改一个
- 回放门禁四档：提升可合并 / 保持不合并 / 显著下降需排查 / 环境不稳本次作废
  （文档第六节指出原型缺「下降但未超阈值」一档，此处已补齐）
"""
from __future__ import annotations

import copy
from dataclasses import replace
from typing import Callable, Optional

from models import GateVerdict, Snapshot


class ReplayError(Exception):
    pass


class SnapshotStore:
    """快照库。按 trace_id 存取，写入后不可变（用 dataclasses.replace 派生新快照）。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}

    def save(self, snap: Snapshot) -> None:
        if snap.trace_id in self._snapshots:
            raise ReplayError(f"trace {snap.trace_id} 已存在，快照不可覆盖")
        self._snapshots[snap.trace_id] = copy.deepcopy(snap)

    def get(self, trace_id: str) -> Snapshot:
        if trace_id not in self._snapshots:
            raise KeyError(f"trace {trace_id} 不在快照库中")
        return copy.deepcopy(self._snapshots[trace_id])

    def derive(self, trace_id: str, **changes) -> Snapshot:
        """从既有快照派生一个改了变量的新快照。这就是「只改一个变量」的机械保证。"""
        if not changes:
            raise ReplayError("至少要改一个变量")
        if len(changes) > 1:
            raise ReplayError(
                f"一次只允许改一个变量，收到 {len(changes)} 个：{list(changes)}"
            )
        return replace(self.get(trace_id), **changes)


class ReplayRunner:
    """执行回放并对结果打门禁。"""

    # 阈值是设计值，落地前必须用真实数据校准（文档第六节第 8 条）
    PROMOTE_GAIN = 0.10        # 提升 ≥ 10pp → 可合并
    INVESTIGATE_DROP = 0.20    # 下降 ≥ 20pp → 排查
    NOISE_BAND = 0.03          # |差值| ≤ 3pp 视为噪声 → 作废重跑

    def __init__(self, agent_fn: Callable[[Snapshot], float]) -> None:
        """
        agent_fn: 给一个快照，跑一遍被测 Agent，返回该维度的得分（0~1）。
                  实际项目里这一步会调真实链路并采样 judge，这里抽象掉。
        """
        self.agent_fn = agent_fn

    def run(self, base: Snapshot, **changes) -> tuple[Snapshot, float]:
        derived = SnapshotStore()
        derived.save(base)
        variant = derived.derive(base.trace_id, **changes)
        return variant, self.agent_fn(variant)

    def gate(self, baseline_score: float, replay_score: float) -> GateVerdict:
        """四档门禁（补齐了文档第六节指出的缺档）。"""
        delta = replay_score - baseline_score

        if abs(delta) <= self.NOISE_BAND:
            # 噪声带内 —— 无法归因，本次作废，重跑或加样本
            return GateVerdict.INVALID
        if delta >= self.PROMOTE_GAIN:
            return GateVerdict.PROMOTE
        if delta <= -self.INVESTIGATE_DROP:
            return GateVerdict.INVESTIGATE
        # 剩下的一档：下降超过噪声带但未到排查线 —— 保持不合并，等更多证据
        return GateVerdict.HOLD
