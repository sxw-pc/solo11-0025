from __future__ import annotations

import json
import subprocess
import sys

from conftest import DICT_MIN, DICT_YAML, REPORT_DIR, ROOT, write_dict
from koujing.cli import main
from koujing.store import Store


def check_args(tmp_path, *extra):
    return [
        "check", str(REPORT_DIR),
        "--dict", str(DICT_YAML),
        "--db", str(tmp_path / "koujing.db"),
        "--out", str(tmp_path / "out"),
        *extra,
    ]


def out_batches(tmp_path):
    return sorted((tmp_path / "out").iterdir())


def test_default_check_writes_reports_and_returns_zero(tmp_path, capsys):
    exit_code = main(check_args(tmp_path))
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "drift=1" in captured.out
    assert "missing_field=1" in captured.out
    batches = out_batches(tmp_path)
    assert len(batches) == 1
    assert {path.name for path in batches[0].iterdir()} == {
        "report.md", "report.json", "report.html"
    }
    assert Store(tmp_path / "koujing.db").count_batches() == 1


def test_fail_on_returns_nonzero_and_quiet_suppresses_stdout(tmp_path, capsys):
    code = main(check_args(tmp_path, "--fail-on", "drift,missing_field", "--quiet"))
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""


def test_unknown_fail_on_category_is_a_clean_error(tmp_path, capsys):
    code = main(check_args(tmp_path, "--fail-on", "banana"))
    captured = capsys.readouterr()
    assert code == 2
    assert "未知的 --fail-on" in captured.err
    assert "Traceback" not in captured.err


def test_invalid_dictionary_returns_two(tmp_path, capsys):
    bad_dict = write_dict(tmp_path, "metrics:\n  - metric: only_name\n")
    args = check_args(tmp_path)
    args[3] = str(bad_dict)
    code = main(args)
    captured = capsys.readouterr()
    assert code == 2
    assert "口径字典校验失败" in captured.err
    assert "missing_field" in captured.err


def test_missing_report_directory_returns_two(tmp_path, capsys):
    code = main([
        "check", str(tmp_path / "absent"), "--dict", str(DICT_YAML),
        "--db", str(tmp_path / "db.sqlite"), "--out", str(tmp_path / "out"),
    ])
    assert code == 2
    assert "报表目录不存在" in capsys.readouterr().err


def test_history_and_export_roundtrip(tmp_path, capsys):
    assert main(check_args(tmp_path)) == 0
    batch_id = out_batches(tmp_path)[0].name
    capsys.readouterr()
    assert main(["history", "--db", str(tmp_path / "koujing.db")]) == 0
    history = capsys.readouterr().out
    assert batch_id in history
    assert "drift=1" in history
    export_out = tmp_path / "exported"
    assert main([
        "export", batch_id, "--db", str(tmp_path / "koujing.db"),
        "--out", str(export_out),
    ]) == 0
    assert (export_out / batch_id / "report.json").is_file()


def test_export_missing_batch_returns_one(tmp_path, capsys):
    code = main([
        "export", "20260101T000000000000Z-deadbeef",
        "--db", str(tmp_path / "empty.db"), "--out", str(tmp_path / "out"),
    ])
    assert code == 1
    assert "批次不存在" in capsys.readouterr().err


def test_history_empty_database(tmp_path, capsys):
    assert main(["history", "--db", str(tmp_path / "empty.db")]) == 0
    assert "暂无批次" in capsys.readouterr().out


def test_module_entrypoint_and_repeat_batches(tmp_path):
    command = [
        sys.executable, "-m", "koujing", *check_args(tmp_path),
        "--parser", "fallback",
    ]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert len(out_batches(tmp_path)) == 2
    assert Store(tmp_path / "koujing.db").count_batches() == 2


def test_baseline_dict_appends_evolution_and_keeps_paths(tmp_path, capsys):
    extra = ["--baseline-dict", str(DICT_MIN)]
    code = main(check_args(tmp_path, *extra))
    captured = capsys.readouterr()
    assert code == 0
    assert "baseline_dict=" in captured.out
    batches = out_batches(tmp_path)
    assert len(batches) == 1
    assert {path.name for path in batches[0].iterdir()} == {
        "report.md", "report.json", "report.html"
    }
    batch_id = batches[0].name
    assert batch_id.endswith("-18c1de24")  # current dictionary fingerprint
    report = (batches[0] / "report.md").read_text(encoding="utf-8")
    assert "## 口径演进" in report
    store = Store(tmp_path / "koujing.db")
    assert store.count_batches() == 1
    batch = store.get_batch(batch_id)
    assert batch["dict_fingerprint"].startswith("18c1de24")
    stored = store.get_findings(batch_id)
    assert len(stored) == 6


def test_baseline_dict_does_not_change_batch_or_default_report_paths(tmp_path):
    from datetime import datetime, timezone

    from koujing.cli import run_check

    fixed_now = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
    plain = run_check(
        REPORT_DIR, dict_path=DICT_YAML, db_path=tmp_path / "plain.db",
        out_dir=tmp_path / "plain", parser_mode="fallback", now=fixed_now,
    )
    baseline = run_check(
        REPORT_DIR, dict_path=DICT_YAML, db_path=tmp_path / "baseline.db",
        out_dir=tmp_path / "baseline", parser_mode="fallback", now=fixed_now,
        baseline_dict_path=DICT_MIN,
    )
    assert plain.batch["batch_id"] == baseline.batch["batch_id"]
    assert plain.files["json"].parent.name == baseline.files["json"].parent.name
    plain_payload = json.loads(plain.files["json"].read_text(encoding="utf-8"))
    baseline_payload = json.loads(baseline.files["json"].read_text(encoding="utf-8"))
    assert plain_payload["batch"] == baseline_payload["batch"]
    assert plain_payload["findings"] == baseline_payload["findings"]
    assert plain_payload["summary"] == baseline_payload["summary"]
    assert "dictionary_evolution" not in plain_payload
    assert "dictionary_evolution" in baseline_payload


def test_invalid_baseline_dictionary_returns_two(tmp_path, capsys):
    bad = write_dict(tmp_path, "metrics:\n  - metric: only_name\n")
    code = main(check_args(tmp_path, "--baseline-dict", str(bad)))
    captured = capsys.readouterr()
    assert code == 2
    assert "口径字典校验失败" in captured.err
    assert not (tmp_path / "out").exists()


def test_quiet_suppresses_evolution_output(tmp_path, capsys):
    code = main(check_args(tmp_path, "--baseline-dict", str(DICT_MIN), "--quiet"))
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert (tmp_path / "out").exists()
