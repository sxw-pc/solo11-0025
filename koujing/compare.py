"""Structural comparison between declared metric semantics and parsed SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .config import ALL_CATEGORIES, CATEGORIES, CATEGORY_ORDER
from .dict_load import Dictionary, Metric, MetricRatio
from .sql_parse.common import extract_identifiers
from .sql_parse.model import ParsedQuery, RatioSpec
from .sql_parse.ratio import ratio_fields


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
              expected: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "report": "",
        "metric": metric,
        "aggregate_alias": alias,
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
             alias: str) -> Finding:
    evidence = _evidence(metric, parsed, observed, differences, alias, expected)
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


def _side_differences(expected: MetricRatio, actual: RatioSpec
                      ) -> tuple[list[dict[str, Any]], bool]:
    """Compare numerator/denominator and report swapped operands explicitly."""
    differences: list[dict[str, Any]] = []
    expected_n = expected.numerator
    expected_d = expected.denominator
    actual_n = actual.numerator
    actual_d = actual.denominator
    swapped = (
        expected_n.expression == actual_d.expression
        and expected_d.expression == actual_n.expression
        and expected_n.expression != expected_d.expression
    )
    if swapped:
        differences.append(_difference(
            "ratio.operand_order",
            ["numerator", "denominator"],
            ["denominator", "numerator"],
        ))

    def normalised_aggregation(value: str) -> str:
        # An average ratio's operands are per-row fields; the implied ``avg``
        # is applied by the outer AVG, so ``avg`` and ``row`` are equivalent.
        if expected.kind == "average_ratio" and actual.kind == "average_ratio":
            return "avg" if value in {"avg", "row"} else value
        return value

    sides = (
        ("numerator", expected_n, actual_n),
        ("denominator", expected_d, actual_d),
    )
    for name, expected_side, actual_side in sides:
        aggregation_expected = expected_side.aggregation
        aggregation_actual = actual_side.aggregation
        if normalised_aggregation(aggregation_expected) != normalised_aggregation(aggregation_actual):
            differences.append(_difference(f"ratio.{name}.aggregation",
                                           aggregation_expected, aggregation_actual))
        if expected_side.expression != actual_side.expression:
            differences.append(_difference(f"ratio.{name}.expression",
                                           expected_side.expression,
                                           actual_side.expression))
    return differences, swapped


def _undetermined_differences(actual: RatioSpec) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = [
        _difference("ratio.undetermined_reason", "", "; ".join(actual.unsupported))
    ]
    for name, part in (("numerator", actual.numerator),
                       ("denominator", actual.denominator)):
        if part.supported:
            continue
        detail = _difference(f"ratio.{name}.expression",
                             None, part.expression)
        detail.update({
            "supported": False,
            "aggregation": part.aggregation or None,
            "reason": next((reason for reason in actual.unsupported
                            if reason.startswith(name)), ""),
        })
        differences.append(detail)
    return differences


def _compare_ratio_metric(report: str, parsed: ParsedQuery, aggregate,
                          metric: Metric) -> Finding:
    """Compare a declared ratio metric against a parsed ratio projection."""
    ratio_spec = aggregate.ratio
    assert ratio_spec is not None and metric.ratio is not None
    observed_fields = _observed_fields(parsed, aggregate)
    observed = {
        "tables": list(parsed.tables),
        "source_fields": list(observed_fields),
        "select_fields": list(parsed.select_fields),
        "aggregation": aggregate.function,
        "filter_expr": parsed.filter_expr,
        "granularity": parsed.granularity,
        "group_by": list(parsed.group_by),
        "aggregate_expression": aggregate.expression,
        "ratio": ratio_spec.to_dict(),
    }
    expected = metric.to_dict()

    if not ratio_spec.supported:
        differences = _undetermined_differences(ratio_spec)
        return _finding(report, "undetermined", metric.metric, parsed,
                        "报表中的派生比率超出可解析子集，无法判定",
                        differences, observed, expected, aggregate.alias)

    expected_fields = tuple(sorted(metric.source_fields))
    missing = sorted(set(expected_fields) - set(observed_fields))
    extra = sorted(set(observed_fields) - set(expected_fields))
    differences: list[dict[str, Any]] = []
    if parsed.tables != metric.source_tables:
        differences.append(_difference("source_tables", list(metric.source_tables),
                                       list(parsed.tables)))
    if missing or extra:
        detail = _difference("source_fields", list(expected_fields), list(observed_fields))
        detail["missing_in_report"] = missing
        detail["undeclared_in_report"] = extra
        differences.append(detail)
    if ratio_spec.kind != metric.ratio.kind:
        differences.append(_difference("ratio.kind", metric.ratio.kind,
                                       ratio_spec.kind))
    side_differences, swapped = _side_differences(metric.ratio, ratio_spec)
    differences.extend(side_differences)
    if ratio_spec.zero_division != metric.ratio.zero_division:
        differences.append(_difference("ratio.zero_division",
                                       metric.ratio.zero_division,
                                       ratio_spec.zero_division))
    if parsed.filter_expr != metric.filter_expr:
        differences.append(_difference("filter_expr", metric.filter_expr,
                                       parsed.filter_expr))
    if parsed.granularity != metric.granularity:
        differences.append(_difference("granularity", metric.granularity,
                                       parsed.granularity))

    if missing or extra:
        category = "missing_field"
        summary = "字段与口径字典不一致"
    elif differences:
        category = "drift"
        if swapped:
            summary = "分子与分母与口径字典对调"
        elif any(item["field"] == "ratio.kind" for item in differences):
            summary = "比率类型（平均比率/总体比率）与口径字典不一致"
        elif any(item["field"] == "ratio.zero_division" for item in differences):
            summary = "零分母处理策略与口径字典不一致"
        else:
            summary = "分子、分母或聚合方式与口径字典不一致"
    else:
        category = "consistent"
        summary = "与口径字典一致"
    return _finding(report, category, metric.metric, parsed, summary, differences,
                    observed, expected, aggregate.alias)


def _compare_metric(report: str, parsed: ParsedQuery, aggregate,
                    metric: Metric) -> Finding:
    if metric.ratio is not None and aggregate.ratio is None:
        return _compare_shape_mismatch(
            report, parsed, aggregate, metric,
            expected_shape="ratio", actual_shape=aggregate.function,
        )
    if metric.ratio is None and aggregate.ratio is not None:
        return _compare_shape_mismatch(
            report, parsed, aggregate, metric,
            expected_shape=metric.aggregation, actual_shape="ratio",
        )
    if metric.ratio is not None and aggregate.ratio is not None:
        return _compare_ratio_metric(report, parsed, aggregate, metric)
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
                    observed, expected, aggregate.alias)


def _compare_shape_mismatch(report: str, parsed: ParsedQuery, aggregate, metric: Metric,
                            *, expected_shape: str, actual_shape: str) -> Finding:
    """Report a ratio-vs-single-aggregate mismatch without losing the alias."""
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
        "ratio": aggregate.ratio.to_dict() if aggregate.ratio is not None else None,
    }
    differences: list[dict[str, Any]] = [
        _difference("aggregation", expected_shape, actual_shape)
    ]
    if parsed.tables != metric.source_tables:
        differences.append(_difference("source_tables", list(metric.source_tables),
                                       list(parsed.tables)))
    if missing or extra:
        detail = _difference("source_fields", list(expected_fields), list(observed_fields))
        detail["missing_in_report"] = missing
        detail["undeclared_in_report"] = extra
        differences.append(detail)
    if parsed.filter_expr != metric.filter_expr:
        differences.append(_difference("filter_expr", metric.filter_expr,
                                       parsed.filter_expr))
    if parsed.granularity != metric.granularity:
        differences.append(_difference("granularity", metric.granularity,
                                       parsed.granularity))
    if missing or extra:
        category = "missing_field"
        summary = "字段与口径字典不一致"
    else:
        category = "drift"
        summary = ("报表使用单聚合而字典声明派生比率" if expected_shape == "ratio"
                   else "报表使用派生比率而字典声明单聚合")
    return _finding(report, category, metric.metric, parsed, summary, differences,
                    observed, metric.to_dict(), aggregate.alias)


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
    if aggregate.ratio is not None:
        observed["ratio"] = aggregate.ratio.to_dict()
    differences = [_difference("metric", None, aggregate.alias)]
    if aggregate.ratio is not None and not aggregate.ratio.supported:
        differences.append(_difference("ratio.undetermined_reason", "",
                                       "; ".join(aggregate.ratio.unsupported)))
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
    """Legacy four-category summary; extra categories are counted separately."""
    counts = {category: 0 for category in CATEGORIES}
    for finding in findings:
        if finding.category not in counts:
            continue
        counts[finding.category] += 1
    return counts


def summarise_extra(findings: Iterable[Finding]) -> dict[str, int]:
    """Counts for additive categories (currently ``undetermined``) only.

    Kept separate from :func:`summarise` so legacy four-category summaries stay
    byte-identical for batches that contain no derived ratio metrics.
    """
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.category in CATEGORIES:
            continue
        counts[finding.category] = counts.get(finding.category, 0) + 1
    return counts


def failing_categories(findings: Iterable[Finding], selected: Iterable[str]) -> list[str]:
    """Return selected categories that actually occurred, in stable order."""
    selected_set = set(selected)
    occurred = {finding.category for finding in findings}
    return [category for category in ALL_CATEGORIES
            if category in selected_set and category in occurred]
