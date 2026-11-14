"""Structural comparison between declared metric semantics and parsed SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .config import CATEGORIES, CATEGORY_ORDER
from .dict_load import Dictionary, Metric
from .sql_parse.common import extract_identifiers
from .sql_parse.model import ParsedQuery, normalise_alias


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
    aggregate_index: int = 0

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
            "aggregate_index": self.aggregate_index,
            "differences": [dict(item) for item in self.differences],
            "evidence": self.evidence,
        }


def _difference(field: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {"field": field, "expected": expected, "actual": actual}


def _observed_fields(parsed: ParsedQuery, aggregate) -> tuple[str, ...]:
    # Attribute aggregate arguments to their own alias only. ``dimension_fields``
    # contains columns from non-aggregate projections, so sibling aggregates can
    # never contaminate this metric while report-level filter and GROUP BY
    # columns are still counted because they apply to every aggregate.
    fields = set(aggregate.fields)
    fields |= set(parsed.dimension_fields)
    fields |= set(extract_identifiers(parsed.filter_expr))
    fields |= set(extract_identifiers(" ".join(parsed.group_by)))
    return tuple(sorted(fields))


def _evidence(metric: str, parsed: ParsedQuery, observed: dict[str, Any],
              differences: list[dict[str, Any]], alias: str,
              expected: dict[str, Any] | None, aggregate_index: int) -> dict[str, Any]:
    return {
        "report": "",
        "metric": metric,
        "aggregate_alias": alias,
        "aggregate_index": aggregate_index,
        "parser_source": parsed.source,
        "line_range": [parsed.line_start, parsed.line_end],
        "expected": expected,
        "observed": observed,
        "differences": differences,
        "warnings": list(parsed.warnings),
    }


def _finding(report: str, category: str, metric: str, parsed: ParsedQuery,
             summary: str, differences: list[dict[str, Any]],
             observed: dict[str, Any], expected: dict[str, Any] | None,
             alias: str, aggregate_index: int) -> Finding:
    evidence = _evidence(metric, parsed, observed, differences, alias, expected,
                         aggregate_index)
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
        aggregate_index=aggregate_index,
    )


def _compare_metric(report: str, parsed: ParsedQuery, aggregate, metric: Metric,
                    aggregate_index: int) -> Finding:
    observed_fields = _observed_fields(parsed, aggregate)
    expected_fields = tuple(sorted(metric.source_fields))
    missing = sorted(set(expected_fields) - set(observed_fields))
    extra = sorted(set(observed_fields) - set(expected_fields))
    observed = {
        "tables": list(parsed.tables),
        "source_fields": list(observed_fields),
        "select_fields": list(parsed.select_fields),
        "dimension_fields": list(parsed.dimension_fields),
        "aggregation": aggregate.function,
        "filter_expr": parsed.filter_expr,
        "granularity": parsed.granularity,
        "group_by": list(parsed.group_by),
        "aggregate_expression": aggregate.expression,
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

    expected = metric.to_dict()
    if missing or extra:
        category = "missing_field"
        summary = "字段与口径字典不一致"
    elif differences:
        category = "drift"
        summary = "聚合、过滤或粒度与口径字典不一致"
    else:
        category = "consistent"
        summary = "与口径字典一致"
    return _finding(report, category, metric.metric, parsed, summary, differences,
                    observed, expected, normalise_alias(aggregate.alias), aggregate_index)


def _compare_unregistered(report: str, parsed: ParsedQuery, aggregate,
                          aggregate_index: int) -> Finding:
    alias = normalise_alias(aggregate.alias)
    observed = {
        "tables": list(parsed.tables),
        "select_fields": list(parsed.select_fields),
        "dimension_fields": list(parsed.dimension_fields),
        "source_fields": list(_observed_fields(parsed, aggregate)),
        "aggregation": aggregate.function,
        "filter_expr": parsed.filter_expr,
        "granularity": parsed.granularity,
        "group_by": list(parsed.group_by),
        "aggregate_expression": aggregate.expression,
    }
    differences = [_difference("metric", None, alias)]
    return _finding(report, "unregistered", alias, parsed,
                    "报表中的聚合别名未在口径字典登记", differences,
                    observed, None, alias, aggregate_index)


def compare_report(report: str, parsed: ParsedQuery, dictionary: Dictionary) -> list[Finding]:
    """Compare every aggregate alias in one report against the dictionary.

    All aggregates are compared independently and in declaration order, even
    when two projections share the same normalised alias. Alias lookup uses the
    same normalisation as the parsers so case and quoting differences cannot
    silently drop or misclassify a projection.
    """
    registry = {normalise_alias(name): metric for name, metric in dictionary.by_name().items()}
    findings: list[Finding] = []
    for index, aggregate in enumerate(parsed.aggregates):
        alias = normalise_alias(aggregate.alias)
        if not alias:
            continue
        metric = registry.get(alias)
        if metric is None:
            findings.append(_compare_unregistered(report, parsed, aggregate, index))
        else:
            findings.append(_compare_metric(report, parsed, aggregate, metric, index))
    return findings


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Fixed sorting key: report, category, metric, source line, occurrence."""
    return sorted(
        findings,
        key=lambda item: (
            item.report,
            CATEGORY_ORDER.get(item.category, 99),
            item.metric,
            item.line_start,
            item.line_end,
            item.summary,
            int(item.evidence.get("aggregate_index", 0)),
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
