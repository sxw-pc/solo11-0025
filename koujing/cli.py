"""Command line entry points: check, history, export and serve."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from . import __version__
from .compare import Finding, compare_reports, failing_categories, summarise, summarise_extra
from .config import (
    CATEGORIES,
    DEFAULT_DB,
    DEFAULT_DICT,
    DEFAULT_HOST,
    DEFAULT_OUT,
    DEFAULT_PORT,
    FAILABLE_CATEGORIES,
    PROJECT_ROOT,
    PARSER_MODES,
    relative_display,
)
from .dict_load import DictValidationError, Dictionary, load_dictionary
from .report import write_reports
from .server import create_server
from .sql_parse import parse_sql
from .store import Store, make_batch_id, utc_iso


class CliError(RuntimeError):
    pass


@dataclass
class CheckResult:
    batch: dict
    findings: list[Finding]
    files: dict[str, Path]


def _report_label(path: Path, reports_root: Path) -> str:
    for base in (PROJECT_ROOT, reports_root):
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except (ValueError, OSError):
            continue
    return path.name


def discover_reports(report_dir: str | Path) -> tuple[list[Path], str]:
    root = Path(report_dir)
    if not root.exists():
        raise CliError(f"报表目录不存在: {report_dir}")
    if not root.is_dir():
        raise CliError(f"报表路径不是目录: {report_dir}")
    reports = sorted(path for path in root.rglob("*.sql") if path.is_file())
    if not reports:
        raise CliError(f"报表目录中没有 .sql 文件: {report_dir}")
    return reports, str(root)


def _parse_fail_on(values: Iterable[str] | None) -> list[str]:
    selected: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            category = part.strip()
            if not category:
                continue
            if category not in FAILABLE_CATEGORIES:
                raise CliError(
                    f"未知的 --fail-on 类别: {category}; "
                    f"可选 {', '.join(FAILABLE_CATEGORIES)}"
                )
            if category not in selected:
                selected.append(category)
    return selected


def run_check(report_dir: str | Path, *, dict_path: str | Path = DEFAULT_DICT,
              db_path: str | Path = DEFAULT_DB, out_dir: str | Path = DEFAULT_OUT,
              parser_mode: str = "auto", now: datetime | None = None,
              progress: bool = False) -> CheckResult:
    """Run a full check. Raises DictValidationError or CliError on bad input."""
    if parser_mode not in PARSER_MODES:
        raise CliError(f"未知解析器模式: {parser_mode}")
    reports, reports_root = discover_reports(report_dir)
    dictionary: Dictionary = load_dictionary(dict_path)
    parsed_reports: list[tuple[str, object]] = []
    for report_path in reports:
        try:
            sql = report_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CliError(f"无法读取报表 {report_path.name}: {exc}") from None
        parsed = parse_sql(sql, mode=parser_mode)
        parsed_reports.append((_report_label(report_path, Path(reports_root)), parsed))
        if progress:
            print(f"parsed {report_path.name} via {parsed.source}", file=sys.stderr)
    findings = compare_reports(parsed_reports, dictionary)
    counts = summarise(findings)
    extra_counts = summarise_extra(findings)
    batch_id = make_batch_id(dictionary.fingerprint, now)
    batch = {
        "batch_id": batch_id,
        "created_at": utc_iso(now),
        "dict_fingerprint": dictionary.fingerprint,
        "report_count": len(reports),
        "counts": counts,
        "parser_mode": parser_mode,
    }
    if extra_counts:
        batch["extra_counts"] = extra_counts
    store = Store(db_path)
    store.save_batch(
        batch,
        findings,
        dictionary.raw_text,
        source_path=relative_display(dictionary.source_path),
    )
    files = write_reports(batch, findings, out_dir)
    return CheckResult(batch=batch, findings=findings, files=files)


def _print_summary(result: CheckResult) -> None:
    batch = result.batch
    counts = " ".join(f"{name}={batch['counts'][name]}" for name in CATEGORIES)
    extra = batch.get("extra_counts") or {}
    if "undetermined" in extra:
        counts += f" undetermined={extra['undetermined']}"
    print(f"batch={batch['batch_id']} reports={batch['report_count']} {counts}")
    print(
        "reports: "
        + ", ".join(f"{name}={relative_display(path)}" for name, path in result.files.items())
    )


def _print_dict_errors(exc: DictValidationError) -> None:
    print("口径字典校验失败:", file=sys.stderr)
    for error in exc.errors:
        location = []
        if error.get("metric"):
            location.append(f"metric={error['metric']}")
        if error.get("field"):
            location.append(f"field={error['field']}")
        suffix = f" ({', '.join(location)})" if location else ""
        print(f"- [{error.get('code')}] {error.get('message')}{suffix}", file=sys.stderr)


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        fail_on = _parse_fail_on(args.fail_on)
        result = run_check(
            args.reports,
            dict_path=args.dict,
            db_path=args.db,
            out_dir=args.out,
            parser_mode=args.parser,
        )
    except DictValidationError as exc:
        _print_dict_errors(exc)
        return 2
    except CliError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # parser failure in strict primary mode
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        _print_summary(result)
    blocked = failing_categories(result.findings, fail_on)
    if blocked:
        if not args.quiet:
            print(f"阻断类别命中: {', '.join(blocked)}", file=sys.stderr)
        return 1
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    store = Store(args.db)
    batches = store.list_batches(args.limit)
    if not batches:
        print("暂无批次")
        return 0
    for batch in batches:
        counts = batch["counts"]
        count_text = " ".join(f"{name}={counts.get(name, 0)}" for name in CATEGORIES)
        print(
            f"{batch['batch_id']}  {batch['created_at']}  "
            f"reports={batch['report_count']}  {count_text}"
        )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from .report import write_reports as write

    store = Store(args.db)
    try:
        batch = store.get_batch(args.batch_id)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    if batch is None:
        print(f"错误: 批次不存在: {args.batch_id}", file=sys.stderr)
        return 1
    findings = store.get_findings(args.batch_id)
    files = write(batch, findings, args.out)
    print(f"batch={args.batch_id}")
    for name, path in files.items():
        print(f"{name}={relative_display(path)}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        server = create_server(host=DEFAULT_HOST, port=args.port, db_path=args.db)
    except OSError as exc:
        print(f"错误: 无法启动本地服务: {exc}", file=sys.stderr)
        return 1
    actual_port = server.server_address[1]
    print(f"口径校对台只读视图: http://{DEFAULT_HOST}:{actual_port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="koujing",
        description="离线指标口径字典与 SQL 报表口径一致性核对工具",
    )
    parser.add_argument("--version", action="version", version=f"koujing {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="解析报表目录并生成核对批次")
    check.add_argument("reports", help="包含 .sql 报表的目录")
    check.add_argument("--dict", default=str(DEFAULT_DICT), help="口径字典 YAML/JSON")
    check.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    check.add_argument("--out", default=str(DEFAULT_OUT), help="报告输出根目录")
    check.add_argument("--parser", choices=PARSER_MODES, default="auto",
                       help="auto 优先 sqlglot；fallback 强制标准库解析器")
    check.add_argument("--fail-on", action="append", default=[], metavar="CATEGORY",
                       help="命中这些结论时退出码为 1，可逗号分隔")
    check.add_argument("--quiet", action="store_true", help="仅输出退出码")
    check.set_defaults(func=_cmd_check)

    history = subparsers.add_parser("history", help="列出最近的核对批次")
    history.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    history.add_argument("--limit", type=int, default=20, help="最多显示批次数")
    history.set_defaults(func=_cmd_history)

    export = subparsers.add_parser("export", help="按批次号重新导出报告")
    export.add_argument("batch_id")
    export.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    export.add_argument("--out", default=str(DEFAULT_OUT), help="报告输出根目录")
    export.set_defaults(func=_cmd_export)

    serve = subparsers.add_parser("serve", help="启动本地只读查看页")
    serve.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="仅监听 127.0.0.1")
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:
        return 0
