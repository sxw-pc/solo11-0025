"""Structural comparison between declared metric semantics and parsed SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .config import CATEGORIES, CATEGORY_ORDER
from .dict_load import Dictionary, Metric
from .relationship import UNDECIDABLE, evaluate_join
from .sql_parse.common import extract_identifiers
from .sql_parse.model import ParsedQuery


@dataclass(frozen=True)
class Finding:
    report: str
    category: str
    metric: str
    parser_source: str
    line_start: int
    line_end: int
    summary: str
    evidence: dict[str, Any]
    differences: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": self.report,
            "category": self.category,
            "metric": self.metric,
            "parser_source": self.parser_source,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "line_range": [self.line_start, self.line_end],
            "summary": self.summary,
            "differences": [dict(item) for item in self.differences],
            "evidence": self.evidence,
        }


def _difference(field: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {"field": field, "expected": expected, "actual": actual}


def _observed_fields(parsed: ParsedQuery, aggregate) -> tuple[str, ...]:
    # Keep report dimensions and filter/group columns, but attribute aggregate
    # arguments to their own alias so unrelated metrics do not contaminate the
    # field comparison in multi-metric reports.
    other_aggregate_fields = {
        field for other in parsed.aggregates if other is not aggregate for field in other.fields
    }
    fields = set(aggregate.fields)
    fields |= set(parsed.select_fields) - other_aggregate_fields
    fields |= set(extract_identifiers(parsed.filter_expr))
    fields |= set(extract_identifiers(" ".join(parsed.group_by)))
    return tuple(sorted(fields))


def _evidence(metric: str, parsed: ParsedQuery, observed: dict[str, Any],
              differences: list[dict[str, Any]], alias: str,
              expected: dict[str, Any] | None,
              join_verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence = {
        "report": "",
        "metric": metric,
        "aggregate_alias": alias,
        "parser_source": parsed.source,
        "line_range": [parsed.line_start, parsed.line_end],
        "expected": expected,
        "observed": observed,
        "differences": differences,
        "warnings": list(parsed.warnings),
        "observed_joins": [join.to_dict() for join in parsed.joins],
    }
    if join_verdict is not None:
        evidence["join_verdict"] = join_verdict
    return evidence


def _finding(report: str, category: str, metric: str, parsed: ParsedQuery,
             summary: str, differences: list[dict[str, Any]],
             observed: dict[str, Any], expected: dict[str, Any] | None,
             alias: str, join_verdict: dict[str, Any] | None = None) -> Finding:
    evidence = _evidence(metric, parsed, observed, differences, alias, expected,
                         join_verdict)
    evidence["report"] = report
    return Finding(
        report=report,
        category=category,
        metric=metric,
        parser_source=parsed.source,
        line_start=parsed.line_start,
        line_end=parsed.line_end,
        summary=summary,
        evidence=evidence,
        differences=tuple(dict(item) for item in differences),
    )


def _compare_metric(report: str, parsed: ParsedQuery, aggregate,
                    metric: Metric) -> Finding:
    observed_fields = _observed_fields(parsed, aggregate)
    expected_fields = tuple(sorted(metric.source_fields))
    missing = sorted(set(expected_fields) - set(observed_fields))
    extra = sorted(set(observed_fields) - set(expected_fields))
    observed = {
        "tables": list(parsed.tables),
        "source_fields": list(observed_fields),
        "select_fields": list(parsed.select_fields),
        "aggregation": aggregate.function,
        "filter_expr": parsed.filter_expr,
        "granularity": parsed.granularity,
        "group_by": list(parsed.group_by),
        "aggregate_expression": aggregate.expression,
        "joins": [join.to_dict() for join in parsed.joins],
    }
    differences: list[dict[str, Any]] = []
    if parsed.tables != metric.source_tables:
        differences.append(_difference("source_tables", list(metric.source_tables),
                                       list(parsed.tables)))
    if missing or extra:
        detail = _difference("source_fields", list(expected_fields), list(observed_fields))
        detail["missing_in_report"] = missing
        detail["undeclared_in_report"] = extra
        differences.append(detail)
    if aggregate.function != metric.aggregation:
        differences.append(_difference("aggregation", metric.aggregation,
                                       aggregate.function))
    if parsed.filter_expr != metric.filter_expr:
        differences.append(_difference("filter_expr", metric.filter_expr,
                                       parsed.filter_expr))
    if parsed.granularity != metric.granularity:
        differences.append(_difference("granularity", metric.granularity,
                                       parsed.granularity))

    # Optional relationship contract: only metrics that explicitly declare one
    # are checked; legacy metrics keep the original comparison untouched.
    verdict = evaluate_join(parsed, metric.join_contract)
    verdict_dict = verdict.to_dict() if verdict is not None else None
    join_failed = verdict is not None and not verdict.provable
    if join_failed:
        join_difference = _difference(
            "join_contract",
            metric.join_contract.to_dict(),
            {
                "join_type": verdict.actual_join_type,
                "pairs": [dict(item) for item in verdict.actual_pairs],
                "status": verdict.status,
            },
        )
        join_difference["verdict"] = verdict.status
        join_difference["reasons"] = list(verdict.reasons)
        if verdict.missing_pairs:
            join_difference["missing_key_pairs"] = [
                dict(item) for item in verdict.missing_pairs
            ]
        if verdict.mismatched_pairs:
            join_difference["wrong_column_pairs"] = [
                dict(item) for item in verdict.mismatched_pairs
            ]
        if verdict.uncovered_key_columns:
            join_difference["uncovered_unique_key_columns"] = list(
                verdict.uncovered_key_columns
            )
        if verdict.parser_reasons:
            join_difference["parser_reasons"] = list(verdict.parser_reasons)
        differences.append(join_difference)

    expected = metric.to_dict()
    if missing or extra:
        category = "missing_field"
        summary = "字段与口径字典不一致"
    elif differences:
        category = "drift"
        if join_failed:
            summary = ("连接关系无法证明为多对一" if verdict.status == UNDECIDABLE
                       else "连接关系与口径字典契约不一致")
        else:
            summary = "聚合、过滤或粒度与口径字典不一致"
    else:
        category = "consistent"
        summary = "与口径字典一致"
    return _finding(report, category, metric.metric, parsed, summary, differences,
                    observed, expected, aggregate.alias, verdict_dict)


def _compare_unregistered(report: str, parsed: ParsedQuery, aggregate) -> Finding:
    observed = {
        "tables": list(parsed.tables),
        "select_fields": list(parsed.select_fields),
        "aggregation": aggregate.function,
        "filter_expr": parsed.filter_expr,
        "granularity": parsed.granularity,
        "group_by": list(parsed.group_by),
        "aggregate_expression": aggregate.expression,
    }
    differences = [_difference("metric", None, aggregate.alias)]
    return _finding(report, "unregistered", aggregate.alias, parsed,
                    "报表中的聚合别名未在口径字典登记", differences,
                    observed, None, aggregate.alias)


def compare_report(report: str, parsed: ParsedQuery, dictionary: Dictionary) -> list[Finding]:
    """Compare every aggregate alias in one report against the dictionary."""
    registry = dictionary.by_name()
    findings: list[Finding] = []
    for aggregate in parsed.aggregates:
        if not aggregate.alias:
            continue
        metric = registry.get(aggregate.alias)
        if metric is None:
            findings.append(_compare_unregistered(report, parsed, aggregate))
        else:
            findings.append(_compare_metric(report, parsed, aggregate, metric))
    return findings


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Fixed sorting key: report, category, metric, source line."""
    return sorted(
        findings,
        key=lambda item: (
            item.report,
            CATEGORY_ORDER.get(item.category, 99),
            item.metric,
            item.line_start,
            item.line_end,
            item.summary,
        ),
    )


def compare_reports(reports: Iterable[tuple[str, ParsedQuery]],
                    dictionary: Dictionary) -> list[Finding]:
    findings: list[Finding] = []
    for report, parsed in sorted(reports, key=lambda item: item[0]):
        findings.extend(compare_report(report, parsed, dictionary))
    return sort_findings(findings)


def summarise(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {category: 0 for category in CATEGORIES}
    for finding in findings:
        counts[finding.category] = counts.get(finding.category, 0) + 1
    return counts


def failing_categories(findings: Iterable[Finding], selected: Iterable[str]) -> list[str]:
    """Return selected categories that actually occurred, in stable order."""
    selected_set = set(selected)
    occurred = {finding.category for finding in findings}
    return [category for category in CATEGORIES if category in selected_set and category in occurred]
