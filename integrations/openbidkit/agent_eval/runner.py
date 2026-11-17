"""分层判卷 runner。

对应文档方法二 4.1–4.3：
- 全量 Query 埋点采集（零判定成本，只做记录）
- 多模型投票粗筛（3 个不同系列的模型，2/3 否决）
- 重点 Query 按评分细则分维度精评（路由决定跑哪些）
- 低分 + 用户投诉样本交人工深度评判

数据流：埋点 → 粗筛 → 精评 → 人工 → 结果归因 → 回流补用例
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from evaluator import DimEvaluator, Router, composite_score, majority_vote
from models import CaseResult, DimResult, Snapshot, Verdict


@dataclass
class SifterVote:
    model_name: str
    verdict: Verdict


class Sifter:
    """粗筛。3 个不同系列的模型投票，2/3 否决才判 FAIL。

    为什么要 3 个不同系列：同系列模型的相关性高，投票形同虚设。
    这个约束文档没写死，但它是「多模型投票」能成立的前提。
    """

    def __init__(self, clients: list[tuple[str, object]]) -> None:
        if len(clients) != 3:
            raise ValueError("粗筛需要恰好 3 个模型")
        names = [n for n, _ in clients]
        if len(set(names)) != 3:
            raise ValueError("3 个模型必须来自不同系列")
        self.clients = clients

    def sift(self, snapshot: Snapshot, judge_fn: Callable[..., Verdict]) -> Verdict:
        votes = [judge_fn(name, client, snapshot) for name, client in self.clients]
        return majority_vote(votes)


@dataclass
class PipelineResult:
    trace_id: str
    stage: str                       # passed_sift / failed_sift / need_review
    composite: Optional[float] = None
    dim_results: list[DimResult] = field(default_factory=list)
    first_error: Optional[str] = None
    human_queue: bool = False


class EvalPipeline:
    """把四层判卷串起来。"""

    # 低于该综合分转人工（设计值，落地校准）
    HUMAN_THRESHOLD = 0.6

    def __init__(self, sifter: Sifter, evaluators: dict[str, DimEvaluator], router: Router) -> None:
        self.sifter = sifter
        self.evaluators = evaluators
        self.router = router

    def run(self, snapshot: Snapshot, route_hint: str, user_complaint: bool = False) -> PipelineResult:
        # 第 1 层：粗筛
        verdict = self.sifter.sift(snapshot, self._vote_stub)
        if verdict == Verdict.PASS:
            return PipelineResult(trace_id=snapshot.trace_id, stage="passed_sift")

        # 第 2 层：精评（路由决定维度）
        dims = self.router.route(snapshot.query, route_hint)
        results: list[DimResult] = []
        for dim in dims:
            ev = self.evaluators[dim]
            results.append(ev.evaluate(snapshot))

        case = CaseResult(trace_id=snapshot.trace_id, dim_results=results)
        score = composite_score(results)

        # 第 3 层：低分或投诉 → 人工
        if score < self.HUMAN_THRESHOLD or user_complaint:
            case.routed_to_human = True
            return PipelineResult(
                trace_id=snapshot.trace_id,
                stage="need_review",
                composite=score,
                dim_results=results,
                human_queue=True,
            )

        return PipelineResult(
            trace_id=snapshot.trace_id,
            stage="failed_sift",
            composite=score,
            dim_results=results,
        )

    def _vote_stub(self, model_name, client, snapshot) -> Verdict:
        """真实接入时，这里调粗筛提示词。演示用固定逻辑。"""
        # 演示：输出为空直接 FAIL，否则 PASS —— 替换成真实 judge 即可
        if not snapshot.final_output.strip():
            return Verdict.FAIL
        return Verdict.PASS

    def replay_reflow(self, failed: PipelineResult) -> dict:
        """缺陷样本回流（方法一 3.7）。

        failed: 精评或人工判失败的样本
        返回一条补录用例的建议，交给方法一的资产生成阶段。
        """
        return {
            "source_trace": failed.trace_id,
            "suggested_query": "（从 trace 提取原始 Query）",
            "first_error": failed.first_error,
            "suggest_dims": [d.dim for d in failed.dim_results if d.score < 1.0],
            "priority": "high" if failed.human_queue else "normal",
        }
