"""门面模块：把 agent_eval 的核心 API 聚合到一个入口，方便外部项目导入。"""
from harness import (AgentAdapter, AgentRun, ToolCallRecord, BatchRunner,
                     EvalReport, assert_case, gate_decision,  # noqa: F401
                     write_snapshots, compare_reports, format_compare,
                     report_to_dict, collect_failures, export_failures,
                     promote_failures)
from evalset import (EvalCase, load_evalset, case_to_dict,   # noqa: F401
                     dataset_counts, RECOMMENDED_DATASETS)
from coverage import CoverageLedger                               # noqa: F401
from replay import SnapshotStore, ReplayRunner                    # noqa: F401
from judge import (SemanticJudge, OpenAICompatClient,             # noqa: F401
                   calibrate_judge, load_judge_from_env,
                   agreement_rate, cohen_kappa, CalibrationReport)
from report import (build_ledger, render_coverage_html,                   # noqa: F401
                    render_coverage_text)
