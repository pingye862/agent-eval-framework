"""CI 门禁（L0）：这就是接入你项目 CI 的那个文件。

GitHub Actions / Jenkins / GitLab CI 里加一步 `pytest test_gate.py`，
红 = 不许合并。blocker 一票否决，不参与百分比稀释。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest

from agent_eval_lib import BatchRunner, gate_decision, load_evalset
import adapter

CASES = load_evalset(os.path.join(os.path.dirname(__file__), "evalset.json"))
RUNNER = BatchRunner(adapter.ShoppingAgentAdapter())

# 演示用：GATE_FIXED=1 pytest test_gate.py 可观察门禁变绿。
# 真实项目不需要这个开关——修好了就是修好了。
if os.environ.get("GATE_FIXED"):
    import agent as _agent
    for _k in _agent.BUGS:
        _agent.BUGS[_k] = False


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_case(case):
    """逐条断言：失败信息直接可读，不用翻日志。"""
    outcome = RUNNER.run_case(case)
    assert outcome.passed, "\n".join(outcome.failed_details())


def test_gate_overall():
    """整卷判定：blocker 全过 + 成功率达标 + 零漏审签核齐备。

    真实项目里 baseline 从上次通过的门禁结果文件读取（如 .gate_baseline.json），
    签核文件也必须由人工产出（--export-review 导出后逐条确认），
    下面自动 approve 仅用于让 GATE_FIXED=1 的演示能变绿，不可照搬到 CI。
    """
    from agent_eval_lib import ReviewSignoff, SignoffSet, review_fingerprint
    report = RUNNER.run_all(CASES, level="L0")
    queue = report.review_queue()
    if queue:
        ok_pending, reasons_pending = gate_decision(report, baseline_rate=None)
        assert not ok_pending
        assert any("零漏审" in r for r in reasons_pending)
    signoffs = SignoffSet([
        ReviewSignoff(o.case.case_id, review_fingerprint(o), "approve", "ci-demo")
        for o in queue
    ])
    ok, reasons = gate_decision(report, baseline_rate=None, signoffs=signoffs)
    assert ok, "；".join(reasons)
