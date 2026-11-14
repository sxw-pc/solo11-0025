from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from koujing.dict_load import load_dictionary  # noqa: E402
from koujing.sql_parse import parse_sql  # noqa: E402

FIXTURES = ROOT / "fixtures"
DICT_YAML = FIXTURES / "dict" / "metrics.yaml"
DICT_MIN = FIXTURES / "dict" / "metrics_min.yaml"
DICT_RATIO = FIXTURES / "dict" / "metrics_ratio.yaml"
REPORT_DIR = FIXTURES / "reports"
RATIO_REPORT_DIR = FIXTURES / "ratio_reports"


@pytest.fixture(scope="session")
def dictionary():
    return load_dictionary(DICT_YAML, yaml_backend="lite")


@pytest.fixture(scope="session")
def ratio_dictionary():
    return load_dictionary(DICT_RATIO, yaml_backend="lite")


@pytest.fixture(scope="session")
def reports_fixture():
    return {path.name: path.read_text(encoding="utf-8")
            for path in sorted(REPORT_DIR.glob("*.sql"))}


@pytest.fixture
def parsed(request, reports_fixture):
    name, mode = request.param
    return parse_sql(reports_fixture[name], mode=mode)


def read_report(name: str) -> str:
    return (REPORT_DIR / name).read_text(encoding="utf-8")


def write_dict(tmp_path: Path, text: str, suffix: str = ".yaml") -> Path:
    path = tmp_path / f"dict{suffix}"
    path.write_text(text, encoding="utf-8")
    return path


def valid_metric(**overrides):
    metric = {
        "metric": "sample_metric",
        "owner": "sample_owner",
        "description": "synthetic fixture metric",
        "source_tables": ["fact_sample"],
        "source_fields": ["amount", "event_at"],
        "aggregation": "sum",
        "filter_expr": "status = 'ok'",
        "granularity": "day",
        "version": "1.0.0",
    }
    metric.update(overrides)
    return metric


def write_report_dir(tmp_path: Path, reports: dict[str, str]) -> Path:
    directory = tmp_path / "reports"
    directory.mkdir()
    for name, sql in reports.items():
        (directory / name).write_text(sql, encoding="utf-8")
    return directory
