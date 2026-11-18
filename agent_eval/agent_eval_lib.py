"""门面模块：把 agent_eval 的核心 API 聚合到一个入口，方便外部项目导入。"""
from harness import (AgentAdapter, AgentRun, ToolCallRecord, BatchRunner,
                     EvalReport, assert_case, gate_decision,
                     write_snapshots, compare_reports, format_compare)  # noqa: F401
from evalset import EvalCase, load_evalset                        # noqa: F401
from coverage import CoverageLedger                               # noqa: F401
from replay import SnapshotStore, ReplayRunner                    # noqa: F401
from judge import (SemanticJudge, OpenAICompatClient,             # noqa: F401
                   calibrate_judge, load_judge_from_env,
                   agreement_rate, spearman, CalibrationReport)
from report import (build_ledger, render_coverage_html,                   # noqa: F401
                    render_coverage_text)
