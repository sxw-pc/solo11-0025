from __future__ import annotations

import json

import pytest

from conftest import REPORT_DIR
from koujing.compare import compare_reports
from koujing.report import (
    build_payload,
    render_html,
    render_json,
    render_markdown,
    write_reports,
)
from koujing.sql_parse import parse_sql


def make_findings(dictionary):
    reports = []
    for path in sorted(REPORT_DIR.glob("*.sql")):
        parsed = parse_sql(path.read_text(encoding="utf-8"), mode="fallback")
        reports.append((f"fixtures/reports/{path.name}", parsed))
    return compare_reports(reports, dictionary)


def batch_dict():
    return {
        "batch_id": "20260101T000000000000Z-abcdef12",
        "created_at": "2026-01-01T00:00:00Z",
        "dict_fingerprint": "a" * 64,
        "report_count": 6,
        "counts": {"consistent": 3, "drift": 1, "missing_field": 1, "unregistered": 1},
        "parser_mode": "fallback",
    }


def test_payload_has_summary_and_machine_evidence(dictionary):
    payload = build_payload(batch_dict(), make_findings(dictionary))
    assert payload["schema_version"] == 1
    assert payload["summary"] == {
        "consistent": 3, "drift": 1, "missing_field": 1, "unregistered": 1
    }
    assert len(payload["findings"]) == 6
    for finding in payload["findings"]:
        assert finding["report"].startswith("fixtures/reports/")
        assert finding["line_range"] == [finding["line_start"], finding["line_end"]]
        assert "evidence" in finding
        assert "\x1b" not in json.dumps(finding, ensure_ascii=False)


def test_json_render_is_parseable_and_deterministic(dictionary):
    findings = make_findings(dictionary)
    first = render_json(batch_dict(), findings)
    second = render_json(batch_dict(), findings)
    assert first == second
    payload = json.loads(first)
    assert payload["batch"]["batch_id"] == batch_dict()["batch_id"]
    assert first.endswith("\n")


def test_markdown_groups_are_present(dictionary):
    markdown = render_markdown(batch_dict(), make_findings(dictionary))
    for label in ("一致 (`consistent`)", "偏差 (`drift`)",
                  "缺失字段 (`missing_field`)", "未登记 (`unregistered`)"):
        assert label in markdown
    assert "fixtures/reports/drift_active_user.sql" in markdown
    assert "D:\\" not in markdown
    assert "count_distinct" in markdown


def test_html_is_self_contained_and_escaped(dictionary):
    document = render_html(batch_dict(), make_findings(dictionary))
    assert document.startswith("<!DOCTYPE html>")
    assert "http://" not in document
    assert "https://" not in document
    assert "<script" not in document
    assert "fixtures/reports/ok_order_gmv.sql" in document
    assert "内容安全" not in document


def test_write_reports_creates_three_files(dictionary, tmp_path):
    files = write_reports(batch_dict(), make_findings(dictionary), tmp_path)
    assert set(files) == {"markdown", "json", "html"}
    target = tmp_path / batch_dict()["batch_id"]
    assert files["markdown"] == target / "report.md"
    assert all(path.is_file() and path.stat().st_size > 0 for path in files.values())
    assert (target / "report.html").read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_absolute_report_paths_are_rendered_relative(dictionary, tmp_path):
    finding = make_findings(dictionary)[0].to_dict()
    finding["report"] = str(tmp_path / "reports" / "secret.sql")
    finding["evidence"]["report"] = finding["report"]
    payload = build_payload(batch_dict(), [finding])
    assert payload["findings"][0]["report"] == "secret.sql"
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


def test_unsafe_batch_id_is_rejected(dictionary, tmp_path):
    unsafe = dict(batch_dict(), batch_id="../escape")
    with pytest.raises(ValueError):
        write_reports(unsafe, make_findings(dictionary), tmp_path)


def evolution_fixture(dictionary):
    from koujing.compare import compare_dictionary_versions
    from conftest import DICT_MIN
    from koujing.dict_load import load_dictionary

    current = make_findings(dictionary)
    baseline_dict = load_dictionary(DICT_MIN, yaml_backend="lite")
    reports = []
    for path in sorted(REPORT_DIR.glob("*.sql")):
        parsed = parse_sql(path.read_text(encoding="utf-8"), mode="fallback")
        reports.append((f"fixtures/reports/{path.name}", parsed))
    baseline = compare_reports(reports, baseline_dict)
    return compare_dictionary_versions(baseline, current, baseline_dict, dictionary)


def test_default_payload_has_no_evolution_key(dictionary):
    assert "dictionary_evolution" not in build_payload(batch_dict(), make_findings(dictionary))


def test_json_payload_appends_evolution_without_changing_existing_fields(dictionary):
    payload = build_payload(batch_dict(), make_findings(dictionary),
                            evolution_fixture(dictionary))
    assert payload["schema_version"] == 1
    assert payload["summary"] == {
        "consistent": 3, "drift": 1, "missing_field": 1, "unregistered": 1
    }
    assert len(payload["findings"]) == 6
    evolution = payload["dictionary_evolution"]
    assert evolution["baseline_dict_fingerprint"]
    assert evolution["current_dict_fingerprint"]
    assert set(evolution["statuses"]) == {
        "unchanged", "tightened", "relaxed", "newly_registered", "dictionary_removed"
    }
    assert evolution["changes"]
    for change in evolution["changes"]:
        assert set(change) >= {
            "report", "metric", "baseline_category", "current_category", "status"
        }
        assert change["report"].startswith("fixtures/reports/")


def test_markdown_has_evolution_section_with_status_counts_and_changes(dictionary):
    markdown = render_markdown(batch_dict(), make_findings(dictionary),
                               evolution_fixture(dictionary))
    assert "## 口径演进" in markdown
    assert "### 迁移状态汇总" in markdown
    assert "### 按指标汇总" in markdown
    assert "### 变动条目" in markdown
    assert "| `newly_registered` | 新登记 |" in markdown
    assert "fixtures/reports/ok_order_gmv.sql" in markdown


def test_html_has_evolution_section(dictionary):
    document = render_html(batch_dict(), make_findings(dictionary),
                           evolution_fixture(dictionary))
    assert 'id="dictionary-evolution"' in document
    assert "口径演进" in document
    assert "http://" not in document
    assert "https://" not in document


def test_evolution_does_not_change_report_paths_or_batch(dictionary, tmp_path):
    plain_files = write_reports(batch_dict(), make_findings(dictionary), tmp_path / "a")
    evo_files = write_reports(batch_dict(), make_findings(dictionary),
                              tmp_path / "b", evolution_fixture(dictionary))
    assert {name: path.name for name, path in plain_files.items()} == {
        name: path.name for name, path in evo_files.items()
    }
    assert plain_files["json"].relative_to(tmp_path / "a") == \
        evo_files["json"].relative_to(tmp_path / "b")
    plain = json.loads(plain_files["json"].read_text(encoding="utf-8"))
    evo = json.loads(evo_files["json"].read_text(encoding="utf-8"))
    assert plain["batch"] == evo["batch"]
    assert plain["findings"] == evo["findings"]
    assert plain["summary"] == evo["summary"]
