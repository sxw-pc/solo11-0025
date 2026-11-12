from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import REPORT_DIR
from koujing.compare import compare_reports
from koujing.sql_parse import parse_sql
from koujing.store import Store, make_batch_id, utc_iso


def build_batch(batch_id="20260101T000000000000Z-abcdef12"):
    return {
        "batch_id": batch_id,
        "created_at": "2026-01-01T00:00:00Z",
        "dict_fingerprint": "abcdef12" + "0" * 56,
        "report_count": 6,
        "counts": {"consistent": 3, "drift": 1, "missing_field": 1, "unregistered": 1},
        "parser_mode": "fallback",
    }


def build_findings(dictionary):
    reports = []
    for path in sorted(REPORT_DIR.glob("*.sql")):
        parsed = parse_sql(path.read_text(encoding="utf-8"), mode="fallback")
        reports.append((path.name, parsed))
    return compare_reports(reports, dictionary)


def test_schema_is_idempotent_and_batch_roundtrips(dictionary, tmp_path):
    path = tmp_path / "store.db"
    store = Store(path)
    Store(path).initialise()
    batch = build_batch()
    assert store.save_batch(batch, build_findings(dictionary), "raw dictionary") is True
    loaded = store.get_batch(batch["batch_id"])
    assert loaded["counts"] == batch["counts"]
    assert loaded["parser_mode"] == "fallback"
    findings = store.get_findings(batch["batch_id"])
    assert len(findings) == 6
    assert findings[0]["line_range"] == [findings[0]["line_start"], findings[0]["line_end"]]
    assert findings[0]["evidence"]["report"] == findings[0]["report"]


def test_duplicate_batch_write_is_idempotent(dictionary, tmp_path):
    store = Store(tmp_path / "store.db")
    batch = build_batch()
    findings = build_findings(dictionary)
    assert store.save_batch(batch, findings, "raw") is True
    assert store.save_batch(batch, findings, "raw") is False
    assert store.save_batch(batch, findings[:1], "different") is False
    assert len(store.get_findings(batch["batch_id"])) == 6
    assert store.count_batches() == 1
    assert store.get_dict_snapshot(batch["dict_fingerprint"])["content"] == "raw"


def test_batch_listing_category_and_metric_queries(dictionary, tmp_path):
    store = Store(tmp_path / "store.db")
    first = build_batch("20260101T000000000000Z-abcdef12")
    second = build_batch("20260102T000000000000Z-abcdef12")
    second["created_at"] = "2026-01-02T00:00:00Z"
    store.save_batch(first, build_findings(dictionary), "raw")
    store.save_batch(second, build_findings(dictionary), "raw")
    assert [item["batch_id"] for item in store.list_batches()] == [
        second["batch_id"], first["batch_id"]
    ]
    assert store.category_counts(first["batch_id"])["drift"] == 1
    assert len(store.findings_by_category(first["batch_id"], "unregistered")) == 1
    history = store.metric_history("active_user")
    assert len(history) == 6
    assert {item["category"] for item in history} == {"consistent", "drift"}


def test_in_memory_store_supports_roundtrip(dictionary):
    store = Store(":memory:")
    batch = build_batch()
    store.save_batch(batch, build_findings(dictionary), "raw")
    assert store.get_batch(batch["batch_id"])["batch_id"] == batch["batch_id"]
    assert len(store.get_findings(batch["batch_id"])) == 6


def test_dictionary_snapshot_is_preserved(dictionary, tmp_path):
    store = Store(tmp_path / "store.db")
    batch = build_batch()
    store.save_batch(batch, [], "verbatim: dictionary", source_path="fixtures/dict/metrics.yaml")
    snapshot = store.get_dict_snapshot(batch["dict_fingerprint"])
    assert snapshot["content"] == "verbatim: dictionary"
    assert snapshot["source_path"] == "fixtures/dict/metrics.yaml"


def test_missing_batch_and_invalid_identifier(dictionary, tmp_path):
    store = Store(tmp_path / "store.db")
    assert store.get_batch("20260101T000000000000Z-deadbeef") is None
    with pytest.raises(ValueError):
        store.get_findings("../escape")
    with pytest.raises(ValueError):
        store.save_batch(build_batch("../escape"), [], "raw")


def test_batch_id_and_timestamp_are_clock_injectable():
    moment = datetime(2026, 3, 4, 5, 6, 7, 890123, tzinfo=timezone.utc)
    batch_id = make_batch_id("abcdef123456", moment)
    assert batch_id == "20260304T050607890123Z-abcdef12"
    assert utc_iso(moment) == "2026-03-04T05:06:07Z"
    assert make_batch_id("abcdef123456", moment) == make_batch_id("abcdef123456", moment)
