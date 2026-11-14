"""Render deterministic Markdown, JSON and self-contained HTML reports."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Iterable

from .compare import (
    MIGRATION_STATUS_LABELS,
    MIGRATION_STATUSES,
    DictionaryEvolution,
    Finding,
)
from .config import CATEGORIES, CATEGORY_LABELS, CATEGORY_ORDER, relative_display, safe_batch_component


def _as_dict(finding: Finding | dict[str, Any]) -> dict[str, Any]:
    return finding.to_dict() if isinstance(finding, Finding) else dict(finding)


def normalise_findings(findings: Iterable[Finding | dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for finding in findings:
        item = _as_dict(finding)
        item["report"] = _display_report(str(item.get("report", "")))
        evidence = item.get("evidence")
        if isinstance(evidence, dict):
            evidence = dict(evidence)
            evidence["report"] = item["report"]
            item["evidence"] = evidence
        items.append(item)
    return sorted(
        items,
        key=lambda item: (
            item.get("report", ""),
            CATEGORY_ORDER.get(item.get("category", ""), 99),
            item.get("metric", ""),
            item.get("line_start", 0),
            item.get("summary", ""),
        ),
    )


def build_payload(batch: dict[str, Any], findings: Iterable[Finding | dict[str, Any]],
                  evolution: DictionaryEvolution | None = None) -> dict[str, Any]:
    items = normalise_findings(findings)
    counts = {name: 0 for name in CATEGORIES}
    for item in items:
        category = item.get("category", "")
        counts[category] = counts.get(category, 0) + 1
    batch_payload = {
        "batch_id": batch["batch_id"],
        "created_at": batch["created_at"],
        "dict_fingerprint": batch["dict_fingerprint"],
        "report_count": int(batch.get("report_count", 0)),
        "parser_mode": batch.get("parser_mode", "auto"),
        "counts": counts,
    }
    payload = {
        "schema_version": 1,
        "generator": "koujing",
        "batch": batch_payload,
        "summary": counts,
        "findings": items,
    }
    # Added only for explicit cross-dictionary checks, so the default payload
    # (and therefore report.json) stays byte-for-byte unchanged.
    if evolution is not None:
        evolution_payload = evolution.to_dict()
        evolution_payload["changes"] = [
            dict(item, report=_display_report(str(item.get("report", ""))))
            for item in evolution_payload["changes"]
        ]
        payload["dictionary_evolution"] = evolution_payload
    return payload


def render_json(batch: dict[str, Any], findings: Iterable[Finding | dict[str, Any]],
                evolution: DictionaryEvolution | None = None) -> str:
    return json.dumps(build_payload(batch, findings, evolution), ensure_ascii=False,
                      indent=2, sort_keys=True) + "\n"


def _display_report(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return relative_display(path)
    return value.replace("\\", "/")


def _difference_lines(item: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for difference in item.get("differences", []):
        expected = json.dumps(difference.get("expected"), ensure_ascii=False)
        actual = json.dumps(difference.get("actual"), ensure_ascii=False)
        lines.append(f"  - `{difference.get('field')}`: 期望 {expected} / 实际 {actual}")
    return lines


def _evolution_lines(evolution: DictionaryEvolution) -> list[str]:
    statuses = evolution.counts()
    by_metric = evolution.by_metric()
    lines = [
        "## 口径演进",
        "",
        f"- 基准字典指纹：`{evolution.baseline_fingerprint}`",
        f"- 当前字典指纹：`{evolution.current_fingerprint}`",
        "",
        "### 迁移状态汇总",
        "",
        "| 状态 | 含义 | 数量 |",
        "| --- | --- | ---: |",
    ]
    for status in MIGRATION_STATUSES:
        lines.append(
            f"| `{status}` | {MIGRATION_STATUS_LABELS[status]} | {statuses[status]} |"
        )
    lines.extend(["", "### 按指标汇总", ""])
    if not by_metric:
        lines.append("_无_")
    else:
        header = "| 指标 | " + " | ".join(
            MIGRATION_STATUS_LABELS[status] for status in MIGRATION_STATUSES
        ) + " |"
        separator = "| --- | " + " | ".join(
            "---:" for _ in MIGRATION_STATUSES
        ) + " |"
        lines.extend([header, separator])
        for metric in sorted(by_metric):
            counts = by_metric[metric]
            cells = " | ".join(str(counts[status]) for status in MIGRATION_STATUSES)
            lines.append(f"| `{metric}` | {cells} |")
    lines.extend(["", "### 变动条目", ""])
    changes = evolution.changes()
    if not changes:
        lines.append("_无变动_")
    else:
        lines.extend([
            "| 报表 | 指标 | 基准结论 | 当前结论 | 状态 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for item in changes:
            suffix = f"（第 {item.occurrence} 处）" if item.occurrence > 1 else ""
            lines.append(
                "| `{}` | `{}`{} | `{}` | `{}` | `{}` |".format(
                    _display_report(item.report),
                    item.metric,
                    suffix,
                    item.baseline_category or "—",
                    item.current_category or "—",
                    MIGRATION_STATUS_LABELS[item.status],
                )
            )
    lines.append("")
    return lines


def render_markdown(batch: dict[str, Any], findings: Iterable[Finding | dict[str, Any]],
                    evolution: DictionaryEvolution | None = None) -> str:
    payload = build_payload(batch, findings, evolution)
    batch_data = payload["batch"]
    lines = [
        "# 口径校对报告",
        "",
        f"- 批次号：`{batch_data['batch_id']}`",
        f"- 生成时间（UTC）：{batch_data['created_at']}",
        f"- 字典指纹：`{batch_data['dict_fingerprint']}`",
        f"- 报表数量：{batch_data['report_count']}",
        f"- 解析器模式：{batch_data['parser_mode']}",
        "",
        "## 结论摘要",
        "",
        "| 类别 | 含义 | 数量 |",
        "| --- | --- | ---: |",
    ]
    for category in CATEGORIES:
        lines.append(f"| `{category}` | {CATEGORY_LABELS[category]} | {payload['summary'][category]} |")
    for category in CATEGORIES:
        group = [item for item in payload["findings"] if item.get("category") == category]
        lines.extend(["", f"## {CATEGORY_LABELS[category]} (`{category}`) — {len(group)}", ""])
        if not group:
            lines.append("_无_")
            continue
        for item in group:
            report = _display_report(item.get("report", ""))
            lines.append(f"### `{report}` → `{item.get('metric', '')}`")
            lines.append("")
            lines.append(f"- 摘要：{item.get('summary', '')}")
            lines.append(f"- 证据行：第 {item.get('line_start', 1)}–{item.get('line_end', 1)} 行")
            lines.append(f"- 解析器：`{item.get('parser_source', 'unknown')}`")
            if item.get("differences"):
                lines.extend(_difference_lines(item))
            else:
                lines.append("- 差异：无")
            warnings = item.get("evidence", {}).get("warnings", [])
            if warnings:
                lines.append("- 解析警告：" + "；".join(str(warning) for warning in warnings))
            lines.append("")
    if evolution is not None:
        lines.extend(_evolution_lines(evolution))
    lines.append("")
    return "\n".join(lines)


def _html_finding(item: dict[str, Any]) -> str:
    differences = item.get("differences", [])
    if differences:
        diff_html = "<ul>" + "".join(
            "<li><code>{}</code> 期望 <code>{}</code> / 实际 <code>{}</code></li>".format(
                html.escape(str(diff.get("field"))),
                html.escape(json.dumps(diff.get("expected"), ensure_ascii=False)),
                html.escape(json.dumps(diff.get("actual"), ensure_ascii=False)),
            )
            for diff in differences
        ) + "</ul>"
    else:
        diff_html = "<p class=\"muted\">无差异</p>"
    warnings = item.get("evidence", {}).get("warnings", [])
    warning_html = ""
    if warnings:
        warning_html = "<p class=\"warn\">解析警告：" + html.escape("；".join(map(str, warnings))) + "</p>"
    return (
        "<article class=\"finding\">"
        f"<h3><code>{html.escape(_display_report(item.get('report', '')))}</code>"
        f" → <code>{html.escape(item.get('metric', ''))}</code></h3>"
        f"<p>{html.escape(item.get('summary', ''))}</p>"
        f"<p class=\"muted\">第 {item.get('line_start', 1)}–{item.get('line_end', 1)} 行 · "
        f"解析器 {html.escape(item.get('parser_source', 'unknown'))}</p>"
        f"{diff_html}{warning_html}</article>"
    )


def _html_evolution(evolution: DictionaryEvolution) -> str:
    statuses = evolution.counts()
    by_metric = evolution.by_metric()
    metric_table = (
        "<table><thead><tr><th>指标</th>"
        + "".join(f"<th>{html.escape(MIGRATION_STATUS_LABELS[status])}</th>"
                  for status in MIGRATION_STATUSES)
        + "</tr></thead><tbody>"
        + ("".join(
            "<tr><td><code>{}</code></td>{}</tr>".format(
                html.escape(metric),
                "".join(f"<td>{by_metric[metric][status]}</td>"
                        for status in MIGRATION_STATUSES),
            )
            for metric in sorted(by_metric)
        ) or '<tr><td colspan="8" class="muted">无</td></tr>')
        + "</tbody></table>"
    )
    change_rows = "".join(
        "<tr><td><code>{}</code></td><td><code>{}</code></td><td><code>{}</code></td>"
        "<td><code>{}</code></td><td>{}</td></tr>".format(
            html.escape(_display_report(item.report)),
            html.escape(item.metric),
            html.escape(item.baseline_category or "—"),
            html.escape(item.current_category or "—"),
            html.escape(MIGRATION_STATUS_LABELS[item.status]),
        )
        for item in evolution.changes()
    )
    change_table = (
        "<table><thead><tr><th>报表</th><th>指标</th><th>基准结论</th>"
        "<th>当前结论</th><th>状态</th></tr></thead><tbody>"
        + (change_rows or '<tr><td colspan="5" class="muted">无变动</td></tr>')
        + "</tbody></table>"
    )
    status_pills = "".join(
        f'<span class="pill">{html.escape(MIGRATION_STATUS_LABELS[status])} '
        f'{statuses[status]}</span>' for status in MIGRATION_STATUSES
    )
    return (
        '<section id="dictionary-evolution"><h2>口径演进</h2>'
        f'<p class="muted">基准指纹 <code>{html.escape(evolution.baseline_fingerprint[:16])}…</code>'
        f' · 当前指纹 <code>{html.escape(evolution.current_fingerprint[:16])}…</code></p>'
        f'<p>{status_pills}</p>'
        f'<h3>按指标汇总</h3>{metric_table}'
        f'<h3>变动条目</h3>{change_table}</section>'
    )


def render_html(batch: dict[str, Any], findings: Iterable[Finding | dict[str, Any]],
                evolution: DictionaryEvolution | None = None) -> str:
    payload = build_payload(batch, findings, evolution)
    batch_data = payload["batch"]
    evolution_html = _html_evolution(evolution) if evolution is not None else ""
    sections: list[str] = []
    toc: list[str] = []
    for category in CATEGORIES:
        group = [item for item in payload["findings"] if item.get("category") == category]
        anchor = f"group-{category}"
        toc.append(
            f'<a href="#{anchor}">{html.escape(CATEGORY_LABELS[category])}'
            f' <span>{len(group)}</span></a>'
        )
        cards = "".join(_html_finding(item) for item in group)
        empty_card = '<p class="muted">无</p>'
        sections.append(
            f'<section id="{anchor}"><h2>{html.escape(CATEGORY_LABELS[category])} '
            f'<span class="pill">{len(group)}</span></h2>{cards or empty_card}</section>'
        )
    style = """
body{margin:0;background:#f4f6f8;color:#17202a;font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:32px 20px 64px}header{background:#102a43;color:#fff;padding:28px 20px}
header div{max-width:1040px;margin:auto}h1{margin:0 0 8px;font-size:26px}h2{margin-top:34px;border-bottom:1px solid #cbd5df;padding-bottom:6px}
.meta{color:#d9e2ec;font-size:13px}.toc{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}.toc a{background:#fff;border:1px solid #cbd5df;border-radius:999px;padding:6px 12px;color:#102a43;text-decoration:none}
.toc span,.pill{background:#e6edf3;border-radius:999px;padding:1px 8px;font-size:12px}.finding{background:#fff;border:1px solid #d9e2ec;border-radius:10px;padding:16px 18px;margin:12px 0}
.finding h3{margin:0 0 4px;font-size:16px}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px;background:#eef2f6;padding:1px 4px;border-radius:4px}
.muted{color:#627d98}.warn{color:#9a6700;background:#fff8e1;padding:8px;border-radius:6px}footer{color:#627d98;margin-top:40px;font-size:12px}
"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>口径校对报告 {html.escape(batch_data['batch_id'])}</title>
<style>{style}</style></head><body>
<header><div><h1>口径校对报告</h1>
<p class="meta">批次 {html.escape(batch_data['batch_id'])} · {html.escape(batch_data['created_at'])} · 报表 {batch_data['report_count']} 份 · 指纹 {html.escape(batch_data['dict_fingerprint'][:16])}…</p></div></header>
<main><nav class="toc">{''.join(toc)}</nav>{''.join(sections)}{evolution_html}
<footer>由 koujing 本地生成；报告仅含相对项目路径，不含原始 SQL 执行结果。</footer></main></body></html>
"""


def write_reports(batch: dict[str, Any], findings: Iterable[Finding | dict[str, Any]],
                  out_root: str | Path,
                  evolution: DictionaryEvolution | None = None) -> dict[str, Path]:
    """Write report.md, report.json and report.html under out/<batch_id>/."""
    batch_id = safe_batch_component(str(batch["batch_id"]))
    target = Path(out_root) / batch_id
    target.mkdir(parents=True, exist_ok=True)
    items = list(findings)
    files = {
        "markdown": target / "report.md",
        "json": target / "report.json",
        "html": target / "report.html",
    }
    files["markdown"].write_text(render_markdown(batch, items, evolution), encoding="utf-8")
    files["json"].write_text(render_json(batch, items, evolution), encoding="utf-8")
    files["html"].write_text(render_html(batch, items, evolution), encoding="utf-8")
    return files
