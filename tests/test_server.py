from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timezone

import pytest

from conftest import DICT_YAML, REPORT_DIR, ROOT
from koujing.cli import run_check
from koujing.server import create_server


@pytest.fixture()
def viewer(tmp_path):
    result = run_check(
        REPORT_DIR,
        dict_path=DICT_YAML,
        db_path=tmp_path / "viewer.db",
        out_dir=tmp_path / "out",
        parser_mode="fallback",
        now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    server = create_server(port=0, db_path=tmp_path / "viewer.db")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1], result.batch["batch_id"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port, path, method="GET", host=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host} if host else {}
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    headers_out = dict(response.getheaders())
    status = response.status
    connection.close()
    return status, body, headers_out


def test_static_page_and_health_are_local(viewer):
    _, port, _ = viewer
    status, body, headers = request(port, "/")
    assert status == 200
    assert "口径校对台" in body.decode("utf-8")
    assert "http://" not in body.decode("utf-8")
    assert headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert request(port, "/app.js")[0] == 200
    assert request(port, "/style.css")[0] == 200
    health = json.loads(request(port, "/api/health")[1])
    assert health == {"ok": True, "read_only": True}


def test_batch_list_and_grouped_detail(viewer):
    _, port, batch_id = viewer
    status, body, _ = request(port, "/api/batches")
    assert status == 200
    batches = json.loads(body)["batches"]
    assert batches[0]["batch_id"] == batch_id
    status, body, _ = request(port, f"/api/batches/{batch_id}")
    assert status == 200
    payload = json.loads(body)
    assert payload["summary"]["drift"] == 1
    assert set(payload["groups"]) == {"consistent", "drift", "missing_field", "unregistered"}
    assert all(item["report"].startswith("fixtures/reports/") for item in payload["findings"])
    assert str(ROOT) not in body.decode("utf-8")


def test_finding_evidence_and_json_download(viewer):
    _, port, batch_id = viewer
    status, body, _ = request(port, f"/api/batches/{batch_id}/findings/1")
    assert status == 200
    finding = json.loads(body)["finding"]
    assert "evidence" in finding and "differences" in finding
    status, body, headers = request(port, f"/api/batches/{batch_id}/report.json")
    assert status == 200
    assert "attachment" in headers["Content-Disposition"]
    report = json.loads(body)
    assert report["batch"]["batch_id"] == batch_id
    assert len(report["findings"]) == 6


def test_out_of_range_finding_is_404(viewer):
    _, port, batch_id = viewer
    assert request(port, f"/api/batches/{batch_id}/findings/99")[0] == 404
    assert request(port, f"/api/batches/{batch_id}/findings/abc")[0] == 404
    assert request(port, "/api/batches/20260101T000000000000Z-deadbeef")[0] == 404


def test_write_methods_are_rejected_and_audited(viewer):
    server, port, _ = viewer
    before = len(server.audit_entries)
    status, body, headers = request(port, "/api/batches", method="POST")
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert json.loads(body)["error"] == "read_only"
    assert any(entry["kind"] == "method" for entry in server.audit_entries[before:])


def test_non_local_host_is_rejected_and_audited(viewer):
    server, port, _ = viewer
    before = len(server.audit_entries)
    status, body, _ = request(port, "/api/health", host="not-local-host")
    assert status == 403
    assert json.loads(body)["error"] == "forbidden_host"
    assert any(entry["kind"] == "host" for entry in server.audit_entries[before:])


def test_path_traversal_is_rejected_and_audited(viewer):
    server, port, _ = viewer
    before = len(server.audit_entries)
    status, body, _ = request(port, "/api/batches/../../secret")
    assert status == 403
    assert json.loads(body)["error"] == "forbidden_path"
    assert any(entry["kind"] == "path" for entry in server.audit_entries[before:])


def test_head_is_read_only_and_unknown_paths_are_404(viewer):
    _, port, _ = viewer
    status, body, headers = request(port, "/", method="HEAD")
    assert status == 200
    assert body == b""
    assert int(headers["Content-Length"]) > 0
    assert request(port, "/static/secret")[0] == 404


def test_server_refuses_non_loopback_bind(tmp_path):
    with pytest.raises(ValueError):
        create_server(host="not-local-host", port=0, db_path=tmp_path / "server.db")
