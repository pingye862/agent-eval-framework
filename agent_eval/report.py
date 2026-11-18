"""覆盖账本可视化：账本 → 单文件 HTML 报告（零依赖，内联 SVG）。

覆盖率不是报个百分数就完事——审计要能回答三个问题：
1. 按层拆（system / trajectory / component），哪一层欠账？
2. 未覆盖的 6 类原因各占多少？「风险豁免」是不是被滥用了？
3. 高风险且未覆盖的单元是哪几个？（审计红线，一个都不能有）

用法::

    from coverage import CoverageLedger
    from models import CoverageUnit
    from report import render_coverage_html

    ledger = CoverageLedger()
    ledger.add(CoverageUnit(unit_id="U001", description="...", business_line="售前"))
    ...
    render_coverage_html(ledger, "coverage.html")

也支持从 JSON 规格文件直接构建::

    from report import build_ledger
    ledger = build_ledger("coverage_spec.json")
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Optional

from coverage import CoverageLedger, CoverageReport
from models import CoverageUnit, EvalLayer, NotCoveredReason

_VALID_LAYERS = {e.value for e in EvalLayer}
_VALID_REASONS = {e.value for e in NotCoveredReason}

_C = {
    "green": "#16a34a", "red": "#dc2626", "amber": "#d97706",
    "ink": "#1e293b", "muted": "#64748b", "line": "#e2e8f0",
    "bg": "#f8fafc", "blue": "#2563eb",
}


def build_ledger(spec: str | Path) -> CoverageLedger:
    """从 JSON 规格构建账本。规格格式::

        [{"unit_id": "U001", "description": "商品推荐兜底",
          "business_line": "售前", "layer": "system",
          "covered_by": ["case-001"], "not_covered_reason": null,
          "risk_level": "high"}, ...]

    未覆盖的单元必须给 6 类原因之一，账本才完整（审计不过会直接抛错）。
    """
    raw = json.loads(Path(spec).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{spec}: 覆盖规格应为数组")
    ledger = CoverageLedger()
    for i, item in enumerate(raw):
        layer = item.get("layer", "system")
        if layer not in _VALID_LAYERS:
            raise ValueError(f"{spec} 第 {i} 条 layer 非法: {layer!r}（允许 {sorted(_VALID_LAYERS)}）")
        reason = item.get("not_covered_reason")
        if reason is not None and reason not in _VALID_REASONS:
            raise ValueError(f"{spec} 第 {i} 条 not_covered_reason 非法: {reason!r}（允许 {sorted(_VALID_REASONS)}）")
        ledger.add(CoverageUnit(
            unit_id=str(item["unit_id"]),
            description=str(item.get("description", "")),
            business_line=str(item.get("business_line", "")),
            layer=EvalLayer(layer),
            covered_by=list(item.get("covered_by") or []),
            not_covered_reason=NotCoveredReason(reason) if reason else None,
            risk_level=str(item.get("risk_level", "normal")),
        ))
    return ledger


# ---------- 文本版（CLI / 日志用） ----------

def render_coverage_text(report: CoverageReport) -> str:
    r = report
    rate = r.covered / r.total if r.total else 0.0
    lines = [f"覆盖账本: 单元 {r.total} ｜ 已覆盖 {r.covered} ｜ 未覆盖 {r.not_covered}"
             f" ｜ 覆盖率 {rate:.1%}"]
    if r.by_layer:
        lines.append("按层:")
        for layer, b in sorted(r.by_layer.items()):
            t = b["covered"] + b["not"]
            lines.append(f"  {layer:<12} {b['covered']}/{t} "
                         f"({'█' * b['covered']}{'░' * b['not']})")
    if r.by_reason:
        lines.append(f"未覆盖原因: {r.by_reason}")
    if r.uncovered_high_risk:
        lines.append(f"⛔ 高风险且未覆盖（审计不过）: {r.uncovered_high_risk}")
    else:
        lines.append("✅ 高风险单元零豁免")
    return "\n".join(lines)


# ---------- HTML 版（审计报告用） ----------

def _bar(label: str, covered: int, not_cov: int, color: str) -> str:
    total = covered + not_cov or 1
    w = lambda n: round(n / total * 100, 1)
    return (f'<div class="row"><span class="lbl">{html.escape(label)}</span>'
            f'<div class="track">'
            f'<div class="fill" style="width:{w(covered)}%;background:{color}"></div>'
            f'<div class="fill gap" style="width:{w(not_cov)}%;background:#fecaca"></div>'
            f'</div><span class="num">{covered}/{total}</span></div>')


def _donut_reason(by_reason: dict, not_covered: int) -> str:
    """未覆盖原因分布 → 横向条形列表（比饼图可读，6 类原因文字长）。"""
    rows = []
    palette = ["#dc2626", "#d97706", "#2563eb", "#7c3aed", "#0891b2", "#64748b"]
    for i, (reason, n) in enumerate(sorted(by_reason.items(), key=lambda kv: -kv[1])):
        pct = round(n / not_covered * 100, 1) if not_covered else 0
        rows.append(
            f'<div class="row"><span class="lbl">{html.escape(reason)}</span>'
            f'<div class="track"><div class="fill" style="width:{pct}%;'
            f'background:{palette[i % len(palette)]}"></div></div>'
            f'<span class="num">{n} 条 · {pct}%</span></div>')
    return "".join(rows)


def render_coverage_html(ledger: CoverageLedger, out_path: str | Path,
                         title: str = "覆盖账本审计报告") -> Path:
    """账本 → 单文件 HTML。直接双击打开即可审计，无外部资源依赖。"""
    r = ledger.report()
    rate = r.covered / r.total if r.total else 0.0
    audit_ok = ledger.audit_pass()
    units = sorted(ledger.units(), key=lambda u: u.unit_id)

    layer_rows = "".join(
        _bar(f"{layer}", b["covered"], b["not"],
             {"system": _C["blue"], "trajectory": "#7c3aed",
              "component": "#0891b2"}.get(layer, _C["blue"]))
        for layer, b in sorted(r.by_layer.items()))
    reason_rows = _donut_reason(r.by_reason, r.not_covered)

    unit_rows = []
    for u in units:
        if u.covered_by:
            status = f'<span class="pill ok">已覆盖</span>'
            detail = html.escape("、".join(u.covered_by))
        elif u.not_covered_reason:
            risk = " ⛔高风险" if u.risk_level == "high" else ""
            status = f'<span class="pill bad">{u.not_covered_reason.value}{risk}</span>'
            detail = html.escape(u.description)
        else:
            status = '<span class="pill bad">未标注原因</span>'
            detail = html.escape(u.description)
        unit_rows.append(
            f"<tr><td>{html.escape(u.unit_id)}</td><td>{html.escape(u.description)}</td>"
            f"<td>{html.escape(u.business_line)}</td><td>{u.layer.value}</td>"
            f"<td>{status}</td><td class='dim'>{detail}</td></tr>")

    high = "".join(f"<li>{html.escape(uid)}</li>" for uid in r.uncovered_high_risk) or "<li>无</li>"
    banner = (f'<div class="banner ok">✅ 审计通过：高风险单元零豁免</div>'
              if audit_ok else
              f'<div class="banner bad">⛔ 审计不通过：存在高风险且未覆盖的单元，'
              f'先处理再谈覆盖率</div>')

    page = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
     margin:0;background:{_C['bg']};color:{_C['ink']}}}
.wrap{{max-width:920px;margin:0 auto;padding:32px 24px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:{_C['muted']};font-size:13px;margin-bottom:20px}}
.cards{{display:flex;gap:12px;margin-bottom:16px}}
.card{{flex:1;background:#fff;border:1px solid {_C['line']};border-radius:10px;
      padding:14px 16px;text-align:center}}
.card .v{{font-size:26px;font-weight:700}} .card .k{{font-size:12px;color:{_C['muted']}}}
.banner{{border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:14px}}
.banner.ok{{background:#dcfce7;color:#14532d}} .banner.bad{{background:#fee2e2;color:#7f1d1d}}
section{{background:#fff;border:1px solid {_C['line']};border-radius:10px;
        padding:16px 18px;margin-bottom:16px}}
section h2{{font-size:15px;margin:0 0 12px}}
.row{{display:flex;align-items:center;gap:10px;margin:7px 0}}
.lbl{{width:110px;flex:none;font-size:13px;text-align:right;color:{_C['ink']}}}
.track{{flex:1;height:14px;background:#f1f5f9;border-radius:7px;overflow:hidden;
       display:flex}}
.fill{{height:100%}} .fill.gap{{}}
.num{{width:90px;flex:none;font-size:12px;color:{_C['muted']}}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 9px;border-bottom:1px solid {_C['line']};text-align:left;
      vertical-align:top}}
th{{color:{_C['muted']};font-weight:600;font-size:12px}}
.dim{{color:{_C['muted']}}}
.pill{{padding:2px 9px;border-radius:99px;font-size:12px;white-space:nowrap}}
.pill.ok{{background:#dcfce7;color:#166534}} .pill.bad{{background:#fee2e2;color:#991b1b}}
ul{{margin:0;padding-left:20px;font-size:13px}}
</style></head><body><div class="wrap">
<h1>{html.escape(title)}</h1>
<div class="sub">生成时间 {time.strftime('%Y-%m-%d %H:%M')} ｜ agent-eval-framework</div>
{banner}
<div class="cards">
<div class="card"><div class="v">{r.total}</div><div class="k">覆盖单元总数</div></div>
<div class="card"><div class="v" style="color:{_C['green']}">{r.covered}</div><div class="k">已覆盖</div></div>
<div class="card"><div class="v" style="color:{_C['red']}">{r.not_covered}</div><div class="k">未覆盖</div></div>
<div class="card"><div class="v" style="color:{_C['blue']}">{rate:.1%}</div><div class="k">覆盖率</div></div>
</div>
<section><h2>按层拆解（system / trajectory / component）</h2>{layer_rows or '<div class="dim">账本为空</div>'}</section>
<section><h2>未覆盖原因分布（6 类，只标注不删除）</h2>{reason_rows or '<div class="dim">没有未覆盖单元</div>'}</section>
<section><h2>高风险且未覆盖（审计红线）</h2><ul>{high}</ul></section>
<section><h2>覆盖单元明细</h2>
<table><tr><th>单元ID</th><th>描述</th><th>业务线</th><th>层</th><th>状态</th><th>覆盖用例 / 说明</th></tr>
{''.join(unit_rows) or '<tr><td colspan="6" class="dim">账本为空</td></tr>'}
</table></section>
</div></body></html>"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")
    return p


def render_run_report_html(report, out_path: str | Path,
                           title: str = "Agent 评测报告") -> Path:
    """顺手把跑批报告也渲染成 HTML（同一套样式），审计留档用。"""
    sem = report.semantic_stats()
    rows = []
    for o in report.outcomes:
        sem_cell = ""
        for a in o.assertions:
            if a.kind == "semantic":
                s = a.result_status()
                cls = {"passed": "pill ok", "failed": "pill bad",
                       "skipped": "pill dim", "need_review": "pill dim"}[s]
                label = {"passed": "通过", "failed": "不达标",
                         "skipped": "跳过未判", "need_review": "转人工"}[s]
                sem_cell = f'<span class="{cls}">{label}</span>'
                break
        rows.append(
            f"<tr><td>{html.escape(o.case.case_id)}</td>"
            f"<td>{html.escape(o.case.query[:40])}</td>"
            f"<td>{html.escape(o.case.severity)}</td>"
            f"<td>{'<span class=\"pill ok\">✓</span>' if o.passed else '<span class=\"pill bad\">✗</span>'}</td>"
            f"<td>{sem_cell or '<span class=\"dim\">—</span>'}</td>"
            f"<td class='dim'>{html.escape('; '.join(o.failed_details())[:120])}</td></tr>")
    page = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:-apple-system,'PingFang SC',sans-serif;background:{_C['bg']};
color:{_C['ink']};margin:0}} .wrap{{max-width:960px;margin:0 auto;padding:32px 24px}}
h1{{font-size:22px}} table{{width:100%;border-collapse:collapse;font-size:13px;
background:#fff;border-radius:10px}}
th,td{{padding:7px 9px;border-bottom:1px solid {_C['line']};text-align:left}}
th{{color:{_C['muted']};font-size:12px}}
.pill{{padding:2px 9px;border-radius:99px;font-size:12px}}
.pill.ok{{background:#dcfce7;color:#166534}} .pill.bad{{background:#fee2e2;color:#991b1b}}
.pill.dim,.dim{{color:{_C['muted']}}}</style></head><body><div class="wrap">
<h1>{html.escape(title)}</h1>
<p>级别 {report.level} ｜ 用例 {report.total} ｜ 通过 {report.passed}
｜ 成功率 {report.success_rate:.1%} ｜ P2 语义: 实判 {sem['judged']} 失败 {sem['failed']}
转人工 {sem['need_review']} 跳过 {sem['skipped']}</p>
<table><tr><th>用例</th><th>Query</th><th>严重度</th><th>结果</th><th>语义判分</th><th>失败明细</th></tr>
{''.join(rows)}</table></div></body></html>"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")
    return p


def _cli() -> None:
    """命令行：python3 report.py --spec coverage.json --out coverage.html"""
    import argparse
    ap = argparse.ArgumentParser(description="覆盖账本可视化")
    ap.add_argument("--spec", required=True, help="覆盖规格 JSON 文件")
    ap.add_argument("--out", default="coverage.html", help="输出 HTML 路径")
    ap.add_argument("--text", action="store_true", help="同时打印文本版摘要")
    a = ap.parse_args()
    ledger = build_ledger(a.spec)
    rep = ledger.report()
    if a.text:
        print(render_coverage_text(rep))
    p = render_coverage_html(ledger, a.out)
    print(f"HTML 报告已生成: {p}")


if __name__ == "__main__":
    _cli()
