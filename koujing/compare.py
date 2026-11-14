"""Structural comparison between declared metric semantics and parsed SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .config import CATEGORIES, CATEGORY_ORDER
from .dict_load import Dictionary, Metric
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


# ---------------------------------------------------------------------------
# Cross-dictionary comparison ("口径演进")
# ---------------------------------------------------------------------------

# Migration statuses are deliberately directional: moving from a milder
# conclusion to a more severe one is a tightening of the metric definition.
MIGRATION_STATUSES = (
    "unchanged",
    "tightened",
    "relaxed",
    "newly_registered",
    "dictionary_removed",
)
MIGRATION_STATUS_LABELS = {
    "unchanged": "未变",
    "tightened": "收紧",
    "relaxed": "放宽",
    "newly_registered": "新登记",
    "dictionary_removed": "字典移除",
}
MIGRATION_STATUS_ORDER = {
    name: index for index, name in enumerate(MIGRATION_STATUSES)
}
# Category severity follows the normal judgement priority. A finding moving
# towards a higher number means the current dictionary rejects more reports.
_CATEGORY_SEVERITY = {name: index for index, name in enumerate(CATEGORIES)}


@dataclass(frozen=True)
class Migration:
    """One aligned conclusion pair and its cross-version status."""

    report: str
    metric: str
    baseline_category: str
    current_category: str
    status: str
    occurrence: int
    aggregate_expression: str
    aggregate_function: str
    baseline_summary: str
    current_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": self.report,
            "metric": self.metric,
            "status": self.status,
            "baseline_category": self.baseline_category,
            "current_category": self.current_category,
            "occurrence": self.occurrence,
            "aggregate_expression": self.aggregate_expression,
            "aggregate_function": self.aggregate_function,
            "baseline_summary": self.baseline_summary,
            "current_summary": self.current_summary,
        }


def _aggregate_expression(finding: Finding) -> str:
    """Stable discriminator for several findings sharing report + metric.

    Findings are aligned by report path and metric name, but one report may
    contain the same metric alias several times with different aggregate
    arguments. The aggregate expression is independent of the dictionary and
    therefore identifies the report occurrence across both runs.
    """
    evidence = finding.evidence if isinstance(finding.evidence, dict) else {}
    observed = evidence.get("observed") or {}
    value = observed.get("aggregate_expression")
    if value is None:
        value = evidence.get("aggregate_expression", "")
    return str(value or "")


def _aggregate_function(finding: Finding) -> str:
    evidence = finding.evidence if isinstance(finding.evidence, dict) else {}
    observed = evidence.get("observed") or {}
    return str(observed.get("aggregation") or "")


def _alignment_group_key(finding: Finding) -> tuple[str, str, str, str]:
    """Stable cross-version key for one report occurrence.

    Report path and metric name form the public alignment key. Aggregate
    arguments/function are added as an occurrence discriminator because one
    report may emit the same metric alias several times with different
    aggregation parameters; those occurrences are then paired by source order,
    never by their (dictionary-dependent) conclusion.
    """
    return (
        finding.report,
        finding.metric,
        _aggregate_expression(finding),
        _aggregate_function(finding),
    )


def _occurrence_sort_key(finding: Finding) -> tuple[Any, ...]:
    # Only attributes that do not depend on the dictionary are used here.
    return (
        finding.line_start,
        finding.line_end,
        _aggregate_expression(finding),
        _aggregate_function(finding),
        finding.metric,
        finding.report,
    )


def _group_findings(
    findings: Iterable[Finding],
) -> dict[tuple[str, str, str, str], list[Finding]]:
    grouped: dict[tuple[str, str, str, str], list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(_alignment_group_key(finding), []).append(finding)
    for items in grouped.values():
        items.sort(key=_occurrence_sort_key)
    return grouped


def _migration_status(baseline: Finding, current: Finding,
                      baseline_metrics: set[str], current_metrics: set[str]) -> str:
    if current.metric in current_metrics and baseline.metric not in baseline_metrics:
        return "newly_registered"
    if baseline.metric in baseline_metrics and current.metric not in current_metrics:
        return "dictionary_removed"
    if baseline.category == current.category:
        return "unchanged"
    if _CATEGORY_SEVERITY.get(current.category, 99) > _CATEGORY_SEVERITY.get(
        baseline.category, 99
    ):
        return "tightened"
    return "relaxed"


def align_findings(baseline_findings: Iterable[Finding],
                   current_findings: Iterable[Finding],
                   baseline_dictionary: Dictionary,
                   current_dictionary: Dictionary) -> list[Migration]:
    """Pair baseline/current conclusions and assign a migration status.

    Alignment order is deterministic: report, metric, aggregate expression,
    aggregate function and then occurrence number. Duplicate occurrences of
    the same (report, metric, expression, function) tuple are paired by their
    independent source line order rather than by their category, so a changed
    conclusion cannot shuffle the pairing.
    """
    baseline_groups = _group_findings(baseline_findings)
    current_groups = _group_findings(current_findings)
    baseline_metrics = set(baseline_dictionary.metric_names())
    current_metrics = set(current_dictionary.metric_names())

    migrations: list[Migration] = []
    for group_key in sorted(baseline_groups):
        # Both runs parse the same reports and emit one finding per aggregate
        # alias, so every alignment group exists on both sides.
        baseline_items = baseline_groups[group_key]
        current_items = current_groups[group_key]
        expression, function = group_key[2], group_key[3]
        for index, (baseline, current) in enumerate(zip(baseline_items, current_items)):
            migrations.append(Migration(
                report=current.report,
                metric=current.metric,
                baseline_category=baseline.category,
                current_category=current.category,
                status=_migration_status(
                    baseline, current, baseline_metrics, current_metrics
                ),
                occurrence=index + 1,
                aggregate_expression=expression,
                aggregate_function=function,
                baseline_summary=baseline.summary,
                current_summary=current.summary,
            ))
    migrations.sort(key=lambda item: (
        item.report,
        item.metric,
        item.aggregate_expression,
        item.aggregate_function,
        item.occurrence,
        MIGRATION_STATUS_ORDER.get(item.status, 99),
    ))
    return migrations


def migration_counts(migrations: Iterable[Migration]) -> dict[str, int]:
    counts = {name: 0 for name in MIGRATION_STATUSES}
    for migration in migrations:
        counts[migration.status] = counts.get(migration.status, 0) + 1
    return counts


def migrations_by_metric(migrations: Iterable[Migration]) -> dict[str, dict[str, int]]:
    metrics: dict[str, dict[str, int]] = {}
    for migration in migrations:
        counts = metrics.setdefault(
            migration.metric, {name: 0 for name in MIGRATION_STATUSES}
        )
        counts[migration.status] = counts.get(migration.status, 0) + 1
    return metrics


@dataclass(frozen=True)
class DictionaryEvolution:
    """Cross-version comparison summary attached to a check report."""

    baseline_fingerprint: str
    current_fingerprint: str
    migrations: tuple[Migration, ...]

    def counts(self) -> dict[str, int]:
        return migration_counts(self.migrations)

    def by_metric(self) -> dict[str, dict[str, int]]:
        return migrations_by_metric(self.migrations)

    def changes(self) -> tuple[Migration, ...]:
        return tuple(item for item in self.migrations if item.status != "unchanged")

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_dict_fingerprint": self.baseline_fingerprint,
            "current_dict_fingerprint": self.current_fingerprint,
            "statuses": self.counts(),
            "by_metric": self.by_metric(),
            "changes": [item.to_dict() for item in self.changes()],
        }


def compare_dictionary_versions(baseline_findings: Iterable[Finding],
                                current_findings: Iterable[Finding],
                                baseline_dictionary: Dictionary,
                                current_dictionary: Dictionary) -> DictionaryEvolution:
    """Compare two dictionaries' conclusions over the same parsed reports."""
    migrations = align_findings(
        baseline_findings, current_findings, baseline_dictionary, current_dictionary
    )
    return DictionaryEvolution(
        baseline_fingerprint=baseline_dictionary.fingerprint,
        current_fingerprint=current_dictionary.fingerprint,
        migrations=tuple(migrations),
    )
