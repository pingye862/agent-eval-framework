"""命令行跑批 + 报告。

用法：
    python3 run_eval.py            # 正常跑（带缺陷开关的当前状态）
    python3 run_eval.py --fixed    # 修复全部缺陷后跑（门禁变绿）
    python3 run_eval.py --gate     # 门禁模式：不达标 exit 1（CI 里用这个）
    python3 run_eval.py --level L1 # 启用 P2 语义判分（需先 export EVAL_MODEL_*，
                                   # 且金标校准通过后 judge 才会真正上岗）
    python3 run_eval.py --coverage # 同时生成覆盖账本审计报告（coverage.html）
    python3 run_eval.py --dataset regression          # 只跑回归集
    python3 run_eval.py --report-json report.json    # 存报告 JSON 供版本对比
    python3 run_eval.py --compare old.json           # 与上次报告逐题对比
    python3 run_eval.py --export-failures f.json     # 导出失败样本（候选回归用例）
    python3 run_eval.py --promote-failures f.json    # 人工确认后回灌评估集
    python3 run_eval.py --export-review q.json       # 导出零漏审人工审核队列
    python3 run_eval.py --signoffs q.json            # 用已签核的队列跑门禁

零漏审默认开启：凡 skipped / need_review / undetermined / flaky 的用例，
必须在审核队列里填写 decision=approve|reject + reviewer 后才能放行。
"""
import sys
import os
import argparse
import json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent_eval_lib import (BatchRunner, gate_decision, load_evalset,
                            load_judge_from_env, build_ledger,
                            render_coverage_html, render_coverage_text,
                            report_to_dict, compare_reports, format_compare,
                            export_failures, promote_failures,
                            export_review_queue, load_signoffs,
                            build_review_queue, review_stats)

HERE = os.path.dirname(__file__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evalset", default=os.path.join(HERE, "evalset.json"))
    ap.add_argument("--gate", action="store_true",
                    help="显式门禁模式：打印放行依据（退出码始终由门禁判定决定，0 放行 / 1 拦截）")
    ap.add_argument("--fixed", action="store_true", help="演示用：修复全部缺陷")
    ap.add_argument("--baseline", type=float, default=None,
                    help="历史基线成功率（0~1），用于下降判定")
    ap.add_argument("--level", default="L0", choices=["L0", "L1", "L2"],
                    help="评测层级：L0 只 P1；L1/L2 加 P2 语义判分（需 EVAL_MODEL_*）")
    ap.add_argument("--force-uncalibrated", action="store_true",
                    help="仅本地演示：未做金标校准也放行 judge。生产环境禁止使用")
    ap.add_argument("--coverage", action="store_true",
                    help="生成覆盖账本审计报告 coverage.html")
    ap.add_argument("--dataset", default=None,
                    help="只跑指定数据集标签（smoke / regression / baseline，可自定义）")
    ap.add_argument("--env", default=None,
                    help="环境标识（默认读 EVAL_ENV，再兜底 local）；不同环境报告不可比")
    ap.add_argument("--report-json", default=None,
                    help="把报告写成 JSON，供 --compare 做版本对比")
    ap.add_argument("--compare", default=None,
                    help="与这份历史报告 JSON 对比（逐题 diff + 性能 delta + 四档判定）")
    ap.add_argument("--export-failures", default=None,
                    help="导出失败样本到 JSON（含实际轨迹 + 建议判分标准草稿）")
    ap.add_argument("--promote-failures", default=None,
                    help="把确认过的失败样本回灌评估集（默认另存 .promoted.json）")
    ap.add_argument("--promote-overwrite", action="store_true",
                    help="配合 --promote-failures：原地覆盖评估集（默认关闭，更安全）")
    ap.add_argument("--export-review", default=None,
                    help="导出零漏审人工审核队列 JSON（填写 decision/reviewer 后作为签核回读）")
    ap.add_argument("--signoffs", default=None,
                    help="已人工签核的审核队列 JSON 路径；未签核项将阻断门禁")
    ap.add_argument("--no-strict-review", action="store_true",
                    help="关闭零漏审门禁（仅本地演示/回归对照，生产禁用）")
    args = ap.parse_args()

    if args.promote_failures:
        out, n = promote_failures(args.promote_failures, args.evalset,
                                  overwrite=args.promote_overwrite)
        print(f"已回灌 {n} 条 → {out}")
        if not args.promote_overwrite:
            print("（默认另存，未覆盖原评估集；确认无误后替换，或加 --promote-overwrite）")
        return 0

    judge = load_judge_from_env() if args.level != "L0" else None
    if args.level != "L0":
        if judge is None:
            print("⚠ 未配置 EVAL_MODEL_URL/NAME/KEY，P2 语义判分将跳过（P1 照常跑）")
        elif not judge.calibrated:
            if args.force_uncalibrated:
                print("⚠ 仅为本地演示放行未校准 judge——生产环境必须先跑金标集校准"
                      "（calibrate_judge：一致率 ≥85% 且 Cohen's κ ≥0.8）")
                judge.mark_calibrated()
            else:
                print("⚠ judge 未通过金标校准，P2 语义判分跳过并标注。"
                      "想看判分效果可加 --force-uncalibrated（仅限本地演示）")

    cases = load_evalset(args.evalset, dataset=args.dataset)
    adapter_cls = __import__("adapter").ShoppingAgentAdapter
    # --fixed 不再改 agent.BUGS 全局（多线程跑批有竞态），改为按 context 注入
    context = ({"bugs": {k: False for k in __import__("agent").BUGS}}
               if args.fixed else None)
    report = BatchRunner(adapter_cls(), judge=judge).run_all(
        cases, level=args.level, context=context,
        dataset=args.dataset, env=args.env)
    print(report.text())

    if args.coverage:
        spec = os.path.join(HERE, "coverage_spec.json")
        if os.path.exists(spec):
            ledger = build_ledger(spec)
            print("\n" + render_coverage_text(ledger.report()))
            out = render_coverage_html(ledger, os.path.join(HERE, "coverage.html"))
            print(f"覆盖账本审计报告: {out}")
        else:
            print(f"\n（未找到 {spec}，跳过覆盖账本）")

    signoffs = load_signoffs(args.signoffs) if args.signoffs else None
    queue = build_review_queue(report, signoffs)
    if queue:
        stats = review_stats(queue)
        print(f"\n[零漏审] 待人工确认 {stats['total']} 条："
              f"未签核 {stats['pending']} / 通过 {stats['approve']} / 驳回 {stats['reject']}")
        if args.no_strict_review:
            print("⚠ 已关闭零漏审门禁，上述未确认项不会阻断放行（生产禁用）")
    ok, reasons = gate_decision(report, baseline_rate=args.baseline,
                                strict_review=not args.no_strict_review,
                                signoffs=signoffs)
    if args.gate:
        print("\n[门禁模式] 下方判定直接决定进程退出码：0 放行 / 1 拦截")
    print("\n门禁判定:", "✅ 放行合并" if ok else "⛔ 拦截合并")
    for r in reasons:
        print(f"  - {r}")
    if not ok:
        print("\n（修复 agent.py 顶部的 BUGS 开关后重跑，观察门禁从红变绿）")

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
        if n:
            print("人工确认 suggested_case 后用 --promote-failures 回灌评估集")
    if args.export_review:
        n = export_review_queue(args.export_review, report, signoffs)
        print(f"人工审核队列已导出 {n} 条 → {args.export_review}")
        if n:
            print("逐条填写 decision=approve|reject、reviewer、note 后，"
                  "用 --signoffs 回读；未签核项一律阻断门禁")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
