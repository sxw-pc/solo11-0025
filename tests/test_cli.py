from __future__ import annotations

import subprocess
import sys

from conftest import DICT_YAML, REPORT_DIR, ROOT, write_dict
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
