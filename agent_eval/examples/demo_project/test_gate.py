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
    """整卷判定：blocker 全过 + 成功率达标。真实项目里 baseline 从
    上次通过的门禁结果文件读取（如 .gate_baseline.json）。"""
    report = RUNNER.run_all(CASES, level="L0")
    ok, reasons = gate_decision(report, baseline_rate=None)
    assert ok, "；".join(reasons)
