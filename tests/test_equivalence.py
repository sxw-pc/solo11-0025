from __future__ import annotations

import json

import pytest

from conftest import DICT_YAML, ROOT, read_report
from koujing.compare import compare_report, compare_reports, summarise
from koujing.dict_load import dictionary_from_document, load_dictionary
from koujing.report import build_payload, render_markdown
from koujing.sql_parse import parse_sql

EQUIVALENT_DIR = ROOT / "fixtures" / "reports_equivalent"

BATCH = {
    "batch_id": "20260101T000000000000Z-abcdef12",
    "created_at": "2026-01-01T00:00:00Z",
    "dict_fingerprint": "a" * 64,
    "report_count": 3,
    "counts": {"consistent": 2, "drift": 1, "missing_field": 0, "unregistered": 0},
    "parser_mode": "fallback",
}


def _metric(**overrides):
    metric = {
        "metric": "active_user",
        "owner": "growth_analytics",
        "description": "daily active user",
        "source_tables": ["fact_login"],
        "source_fields": ["user_id", "login_at", "event_type", "status"],
        "aggregation": "count_distinct",
        "filter_expr": "event_type = 'login' and status = 'ok'",
        "granularity": "day",
        "version": "1.0.0",
    }
    metric.update(overrides)
    return metric


def _reordered_dictionary():
    return dictionary_from_document({"metrics": [
        _metric(filter_expr="status = 'ok' AND event_type = 'login'")
    ]})


def test_and_reordering_is_equivalent_not_drift(dictionary):
    finding = compare_report(
        "fixture.sql",
        parse_sql(read_report("ok_active_user.sql"), mode="fallback"),
        dictionary,
    )[0]
    assert finding.category == "consistent"
    # The fixture already spells the terms in dictionary order, so there is no
    # spelling annotation; the equivalence check is covered below.
    assert finding.annotations == ()


def test_report_written_in_reversed_and_order_is_consistent(dictionary):
    sql = (
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE status = 'ok' AND event_type = 'login' GROUP BY DATE(login_at);"
    )
    finding = compare_report("fixture.sql", parse_sql(sql, mode="fallback"), dictionary)[0]
    assert finding.category == "consistent"
    assert finding.differences == ()
    assert len(finding.annotations) == 1
    annotation = finding.annotations[0]
    assert annotation["field"] == "filter_expr"
    assert annotation["kind"] == "equivalent_spelling"
    assert annotation["label"] == "等价但写法不同"
    assert annotation["expected_surface"] == "event_type = 'login' and status = 'ok'"
    assert annotation["actual_surface"] == "status = 'ok' and event_type = 'login'"


def test_equivalent_spelling_is_machine_readable_in_report_json(dictionary):
    sql = (
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE (status = 'ok' AND event_type = 'login') GROUP BY DATE(login_at);"
    )
    findings = [
        compare_report("fixture.sql", parse_sql(sql, mode="fallback"), dictionary)[0]
    ]
    payload = build_payload(BATCH, findings)
    item = payload["findings"][0]
    assert item["category"] == "consistent"
    assert item["annotations"][0]["kind"] == "equivalent_spelling"
    assert item["annotations"][0]["label"] == "等价但写法不同"
    markdown = render_markdown(BATCH, findings)
    assert "等价但写法不同" in markdown


def test_real_deviation_is_labelled_in_machine_readable_output(dictionary):
    sql = (
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE status <> 'ok' AND event_type = 'login' GROUP BY DATE(login_at);"
    )
    finding = compare_report("fixture.sql", parse_sql(sql, mode="fallback"), dictionary)[0]
    assert finding.category == "drift"
    difference = next(item for item in finding.differences if item["field"] == "filter_expr")
    assert difference["kind"] == "real_deviation"
    assert difference["label"] == "真实偏差"
    assert finding.annotations == ()


def test_reordered_or_chain_within_conjunction_stays_drift(dictionary):
    sql = (
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE (status = 'ok' OR event_type = 'login') AND login_at > '2020-01-01' "
        "GROUP BY DATE(login_at);"
    )
    reversed_sql = (
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE (event_type = 'login' OR status = 'ok') AND login_at > '2020-01-01' "
        "GROUP BY DATE(login_at);"
    )
    parsed = parse_sql(sql, mode="fallback")
    reordered = parse_sql(reversed_sql, mode="fallback")
    assert parsed.filter_expr != reordered.filter_expr


def test_dictionary_fingerprint_ignores_and_ordering_only():
    first = dictionary_from_document({"metrics": [_metric()]})
    second = _reordered_dictionary()
    third = dictionary_from_document({"metrics": [
        _metric(filter_expr="event_type = 'login' and status = 'refunded'")
    ]})
    assert first.metrics[0].filter_expr == second.metrics[0].filter_expr
    assert first.metrics[0].filter_expr_surface != second.metrics[0].filter_expr_surface
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != third.fingerprint


def test_both_parsers_normalise_equivalent_spellings_identically():
    pytest.importorskip("sqlglot")
    sql = (
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE status = 'ok' AND event_type = 'login' GROUP BY DATE(login_at);"
    )
    reversed_sql = (
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE event_type = 'login' AND status = 'ok' GROUP BY DATE(login_at);"
    )
    for statement in (sql, reversed_sql):
        primary = parse_sql(statement, mode="primary")
        fallback = parse_sql(statement, mode="fallback")
        assert primary.filter_expr == fallback.filter_expr
        assert primary.filter_expr_surface == fallback.filter_expr_surface
    # Equivalent spellings share one comparison form but keep their own surface.
    first = parse_sql(sql, mode="primary")
    second = parse_sql(reversed_sql, mode="primary")
    assert first.filter_expr == second.filter_expr
    assert first.filter_expr_surface != second.filter_expr_surface


@pytest.mark.parametrize("name", [
    "ok_active_user_reordered.sql",
    "ok_active_user_duplicate_and.sql",
])
def test_equivalent_fixtures_are_consistent(dictionary, name):
    sql = (EQUIVALENT_DIR / name).read_text(encoding="utf-8")
    finding = compare_report(f"fixtures/reports_equivalent/{name}",
                             parse_sql(sql, mode="fallback"), dictionary)[0]
    assert finding.category == "consistent"
    assert finding.annotations[0]["label"] == "等价但写法不同"


def test_reordered_or_fixture_is_drift(dictionary):
    sql = (EQUIVALENT_DIR / "drift_active_user_or_reordered.sql").read_text(encoding="utf-8")
    finding = compare_report("fixtures/reports_equivalent/drift_active_user_or_reordered.sql",
                             parse_sql(sql, mode="fallback"), dictionary)[0]
    assert finding.category == "drift"
    difference = next(item for item in finding.differences if item["field"] == "filter_expr")
    assert difference["kind"] == "real_deviation"
    assert difference["label"] == "真实偏差"


def test_fallback_parsers_agree_on_equivalent_fixture_batch(dictionary):
    reports = {}
    for path in sorted(EQUIVALENT_DIR.glob("*.sql")):
        reports[path.name] = parse_sql(path.read_text(encoding="utf-8"), mode="fallback")
    # The two equivalent fixtures collapse to the same filter form.
    assert reports["ok_active_user_reordered.sql"].filter_expr == (
        reports["ok_active_user_duplicate_and.sql"].filter_expr
    )
    assert (reports["drift_active_user_or_reordered.sql"].filter_expr
            != reports["ok_active_user_reordered.sql"].filter_expr)


def test_fixture_dictionary_still_loads_with_surface_metadata():
    loaded = load_dictionary(DICT_YAML, yaml_backend="lite")
    active = loaded.by_name()["active_user"]
    assert active.filter_expr == "event_type = 'login' and status = 'ok'"
    assert active.filter_expr_surface == active.filter_expr
    assert "filter_expr_surface" not in active.to_dict()
    assert "filter_expr_surface" not in loaded.metrics[0].to_dict()


def test_both_parsers_agree_on_unsafe_and_neutral_constructs():
    pytest.importorskip("sqlglot")
    statements = [
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE login_at BETWEEN '2020-01-01' AND '2020-02-01' AND status = 'ok' "
        "GROUP BY DATE(login_at);",
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE CASE WHEN status = 'ok' AND event_type = 'login' THEN 1 ELSE 0 END = 1 "
        "GROUP BY DATE(login_at);",
        "SELECT COUNT(DISTINCT user_id) AS active_user FROM fact_login "
        "WHERE status = 'ok' OR event_type = 'login' GROUP BY DATE(login_at);",
    ]
    for statement in statements:
        primary = parse_sql(statement, mode="primary")
        fallback = parse_sql(statement, mode="fallback")
        assert primary.filter_expr == fallback.filter_expr
        assert primary.filter_expr_surface == fallback.filter_expr_surface
        assert primary.comparable() == fallback.comparable()


def test_both_parsers_produce_identical_findings_on_equivalent_batch(dictionary):
    pytest.importorskip("sqlglot")
    names = [
        "ok_active_user_reordered.sql",
        "ok_active_user_duplicate_and.sql",
        "drift_active_user_or_reordered.sql",
    ]
    findings_by_mode = {}
    for mode in ("primary", "fallback"):
        findings = []
        for name in names:
            sql = (EQUIVALENT_DIR / name).read_text(encoding="utf-8")
            finding = compare_report(
                f"fixtures/reports_equivalent/{name}", parse_sql(sql, mode=mode), dictionary
            )[0]
            findings.append(finding)
        findings_by_mode[mode] = findings

    def signature(finding):
        return (
            finding.metric,
            finding.category,
            [item["field"] for item in finding.differences],
            [item["field"] for item in finding.annotations],
        )

    assert [signature(item) for item in findings_by_mode["primary"]] == [
        signature(item) for item in findings_by_mode["fallback"]
    ]
    assert [item.category for item in findings_by_mode["fallback"]] == [
        "consistent", "consistent", "drift"
    ]


def test_new_equivalent_cases_stack_on_top_of_baseline_counts(dictionary):
    baseline = ["drift_active_user.sql", "missing_field.sql", "ok_active_user.sql",
                "ok_order_gmv.sql", "unregistered_metric.sql", "weird_syntax.sql"]
    pairs = [(f"fixtures/reports/{name}",
              parse_sql(read_report(name), mode="fallback")) for name in baseline]
    baseline_findings = compare_reports(pairs, dictionary)
    assert summarise(baseline_findings) == {
        "consistent": 3, "drift": 1, "missing_field": 1, "unregistered": 1
    }
    equivalent = compare_report(
        "fixtures/reports_equivalent/ok_active_user_reordered.sql",
        parse_sql((EQUIVALENT_DIR / "ok_active_user_reordered.sql").read_text(encoding="utf-8"),
                  mode="fallback"),
        dictionary,
    )[0]
    overlaid = summarise(baseline_findings + [equivalent])
    assert overlaid == {
        "consistent": 4, "drift": 1, "missing_field": 1, "unregistered": 1
    }
