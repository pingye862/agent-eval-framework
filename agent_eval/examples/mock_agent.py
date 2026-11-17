"""虚构的数码商品导购 Agent（文档统一示例）。

带可控缺陷开关，用来演示归因链路。这不是被测系统的真实实现，
只是让归因引擎有事可做——真实接入时换成你的 Agent 链路。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from attribution import FirstErrorFinder, TraceStep, single_attribution
from models import AttributionLayer


class MockAgent:
    """每类缺陷一个开关，全关 = 完美 Agent。"""

    def __init__(self,
                 intent_bug=False,       # 意图识别错
                 tool_bug=False,         # 工具选择错
                 retrieval_bug=False,    # 知识召回错
                 expression_bug=False):  # 表达问题
        self.intent_bug = intent_bug
        self.tool_bug = tool_bug
        self.retrieval_bug = retrieval_bug
        self.expression_bug = expression_bug

    def run(self, query: str) -> list[TraceStep]:
        steps = []
        steps.append(TraceStep(
            "s1_intent", "rule_check", "意图识别",
            ok=(not self.intent_bug),
            detail="意图识别错" if self.intent_bug else "",
            layer=AttributionLayer.TRAJECTORY,
        ))

        steps.append(TraceStep(
            "s2_tool", "rule_check", "工具选择",
            ok=(not self.tool_bug),
            detail="调用了降价查询工具而非参数查询" if self.tool_bug else "",
            layer=AttributionLayer.TRAJECTORY,
        ))

        steps.append(TraceStep(
            "s3_retrieval", "snapshot_diff", "知识召回",
            ok=(not self.retrieval_bug),
            detail="召回了过期的 kb-20260901 片段" if self.retrieval_bug else "",
            layer=AttributionLayer.SYSTEM,
        ))

        steps.append(TraceStep(
            "s4_expression", "llm_vote", "表达质量",
            ok=(not self.expression_bug),
            detail="称呼混乱" if self.expression_bug else "",
            layer=AttributionLayer.COMPONENT,
        ))

        return steps

    def diagnose(self, query: str) -> dict:
        """跑一遍链路 → 找第一错 → 单归因。这就是方法二 4.8 + 方法一 3.6 的落地。"""
        steps = self.run(query)
        finder = FirstErrorFinder(steps)
        first = finder.find()
        if first is None:
            return {"query": query, "verdict": "PASS", "attribution": None}

        layer_votes = {AttributionLayer.SYSTEM: False,
                       AttributionLayer.TRAJECTORY: False,
                       AttributionLayer.COMPONENT: False}
        for s in steps:
            if s.ok is False:
                layer_votes[s.layer] = True

        return {
            "query": query,
            "verdict": "FAIL",
            "first_error_step": first.step_id,
            "first_error_target": first.target,
            "detail": first.detail,
            "attribution": single_attribution(layer_votes).value,
        }


if __name__ == "__main__":
    print("=== 场景 1：召回坏了 + 工具也选错 ===")
    agent = MockAgent(retrieval_bug=True, tool_bug=True)
    r = agent.diagnose("iPhone 15 的屏幕尺寸是多少")
    print(f"  第一错 : {r['first_error_step']} ({r['first_error_target']})")
    print(f"  主归因 : {r['attribution']}")
    print("  → 单归因优先 system 层：召回是上游，修好它，")
    print("    下游的工具选择错误可能跟着消失。不重复扣分。")

    print("\n=== 场景 2：修好召回后重跑（单变量修复验证）===")
    agent2 = MockAgent(retrieval_bug=False, tool_bug=True)
    r2 = agent2.diagnose("iPhone 15 的屏幕尺寸是多少")
    print(f"  判定   : {r2['verdict']}")
    print(f"  第一错 : {r2['first_error_step']} ({r2['first_error_target']})")
    print(f"  主归因 : {r2['attribution']}")
    print("  → 剩下的错误确实在轨迹层，这次修工具选择。")

    print("\n=== 场景 3：完美 Agent ===")
    print(MockAgent().diagnose("耳机推荐一下"))
