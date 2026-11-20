"""通用案例库一键跑批。

默认零配置：跑本目录的 evalset.json（11 条通用能力用例），配 ReferenceAdapter
（纯标准库参考实现），应该全绿 + 门禁放行——用来确认「框架与案例库都正常」。

接自己的系统时只改一处：把 reference_agent.ReferenceAdapter 换成你的 adapter，
或者复制本文件、把 import 换掉。

用法：
    python3 run_eval.py                                  # 全量 11 条（L0）
    python3 run_eval.py --dataset smoke                  # 只跑 3 条冒烟用例
    python3 run_eval.py --level L1                       # 加 P2 语义判分（需 EVAL_MODEL_*）
    python3 run_eval.py --evalset evalset.mine.json      # 跑 render.py 生成的自家集
    python3 run_eval.py --demo-undetermined              # 演示 undetermined 如何拦门禁
    python3 run_eval.py --report-json r.json --compare old.json
    python3 run_eval.py --export-failures f.json

拷到框架外面跑：
    AGENT_EVAL_SRC=/path/to/agent-eval/agent_eval python3 run_eval.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 框架源码位置：优先环境变量（整目录拷到别处时用），否则取仓库内的 agent_eval/
_SRC = os.environ.get("AGENT_EVAL_SRC") or os.path.normpath(
    os.path.join(HERE, "..", ".."))
for p in (_SRC, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_eval_lib import (BatchRunner, load_evalset, gate_decision,  # noqa: E402
                            load_judge_from_env, report_to_dict,
                            compare_reports, format_compare, export_failures)
from reference_agent import ReferenceAdapter, describe_protocol  # noqa: E402


def _print_dimensions(cases: list) -> None:
    """按 route 聚一下，让人一眼看到这次覆盖了哪些能力维度。"""
    from collections import Counter
    counts = Counter(c.route for c in cases)
    print("覆盖维度：" + "、".join(f"{k}×{v}" for k, v in sorted(counts.items())))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="通用 Agent 评测案例库（可跑在任何实现了标准动作协议的系统上）")
    ap.add_argument("--evalset", default=os.path.join(HERE, "evalset.json"),
                    help="评估集 JSON（默认本目录 evalset.json）")
    ap.add_argument("--dataset", default=None,
                    help="只跑指定分层：smoke / regression / baseline")
    ap.add_argument("--level", default="L0", choices=["L0", "L1", "L2"],
                    help="评测层级：L0 只 P1 规则断言；L1/L2 加 P2 语义判分")
    ap.add_argument("--env", default=None, help="环境标识（默认读 EVAL_ENV，再兜底 local）")
    ap.add_argument("--gate", action="store_true",
                    help="显式门禁模式（退出码始终由门禁判定决定：0 放行 / 1 拦截）")
    ap.add_argument("--baseline", type=float, default=None,
                    help="历史基线成功率（0~1），用于下降判定")
    ap.add_argument("--report-json", default=None, help="把报告写成 JSON，供下次对比")
    ap.add_argument("--compare", default=None, help="与历史报告 JSON 做逐题 diff")
    ap.add_argument("--export-failures", default=None, help="导出失败样本（候选回归用例）")
    ap.add_argument("--demo-undetermined", action="store_true",
                    help="演示用：跑只含 judge_criteria 的反面教材集，观察 undetermined 拦截")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="逐条打印结果（用例 / 维度 / 有效断言数）")
    args = ap.parse_args()

    evalset = (os.path.join(HERE, "evalset.undetermined-demo.json")
               if args.demo_undetermined else args.evalset)
    if args.demo_undetermined and args.level == "L0":
        print("说明：该集合只有语义标准、没有 P1 断言，L0 下必然 undetermined。\n")

    cases = load_evalset(evalset, dataset=args.dataset)
    print(f"评估集：{os.path.basename(evalset)} ｜ 用例：{len(cases)} 条")
    _print_dimensions(cases)
    proto = describe_protocol()
    print(f"协议：标准动作 {proto['standard_tools']} ｜ 回显令牌 {proto['echo_token']}\n")

    judge = load_judge_from_env() if args.level != "L0" else None
    if args.level != "L0" and judge is None:
        print("⚠ 未配置 EVAL_MODEL_URL/NAME/KEY，P2 语义判分将跳过（P1 照常跑）\n")
    elif args.level != "L0" and not judge.calibrated:
        print("⚠ judge 未通过金标校准，语义判分跳过并标注（这是预期行为，不硬判）\n")

    report = BatchRunner(ReferenceAdapter(), judge=judge).run_all(
        cases, level=args.level, dataset=args.dataset, env=args.env)
    print(report.text())

    if args.verbose:
        mark = {"passed": "✓", "failed": "✗", "undetermined": "⚠"}
        print("\n逐条结果：")
        for o in report.outcomes:
            ok_n = sum(1 for a in o.effective if a.ok)
            print(f"  {mark.get(o.case_status, '?')} {o.case.case_id} "
                  f"[{o.case.severity}/{o.case.dataset}] {o.case.route}"
                  f" ｜ 有效断言 {ok_n}/{len(o.effective)}")

    if report.undetermined_total:
        print(f"\n⚠ 有 {report.undetermined_total} 条用例未被有效测试（undetermined），"
              "不计入成功率分母：")
        for o in report.undetermined_cases():
            why = "；".join(f"[{a.kind}] {a.detail}" for a in o.unverified[:2])
            print(f"  - {o.case.case_id}: {why}")

    ok, reasons = gate_decision(report, baseline_rate=args.baseline)
    if args.gate:
        print("\n[门禁模式] 下方判定直接决定进程退出码：0 放行 / 1 拦截")
    print("\n门禁判定:", "✅ 放行合并" if ok else "⛔ 拦截合并")
    for r in reasons:
        print(f"  - {r}")

    if args.report_json or args.compare:
        payload = report_to_dict(report, gate_ok=ok, gate_reasons=reasons,
                                 baseline=args.baseline)
        if args.report_json:
            with open(args.report_json, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"报告 JSON: {args.report_json}")
        if args.compare:
            with open(args.compare, encoding="utf-8") as f:
                old = json.load(f)
            print(format_compare(compare_reports(old, payload)))

    if args.export_failures:
        n = export_failures(args.export_failures, report)
        print(f"失败样本已导出 {n} 条 → {args.export_failures}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
