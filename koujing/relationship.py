"""Static many-to-one relationship checking against explicit join contracts.

The checker answers one narrow question: *given the contract the metric
dictionary explicitly declares, can the join traversed by the report SQL be
proved many-to-one purely from the query text?*

It never executes SQL, never estimates row counts and never treats the
declared candidate unique key as proof that the right table's data is
actually unique.  The verdict therefore distinguishes "provable under the
declaration" from "cannot be decided", and every failure carries concrete
evidence (expected versus actual key pairings, missing components, the
rejected predicate shape, ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .dict_load import JoinContract
from .sql_parse.model import JoinSpec, ParsedQuery

# Stable machine-readable verdict codes.
PROVABLE = "many_to_one_provable"
UNPROVABLE = "not_provable"
UNDECIDABLE = "undecidable"

# Failure reason codes; wording is kept here so reports stay consistent.
REASON_NO_CONTRACT = "no_join_contract_declared"
REASON_SINGLE_TABLE = "query_has_no_join"
REASON_PARSER_UNDECIDABLE = "join_not_within_bounded_syntax"
REASON_MULTIPLE_JOIN_SPECS = "multiple_join_specs"
REASON_JOIN_TYPE_MISMATCH = "join_type_mismatch"
REASON_ROLE_MISMATCH = "preserved_side_mismatch"
REASON_TABLE_ROLE_MISMATCH = "table_role_mismatch"
REASON_MISSING_KEY_PAIR = "missing_key_pair"
REASON_WRONG_COLUMN = "join_column_mismatch"
REASON_KEY_NOT_COVERED = "unique_key_component_not_covered"

REASON_MESSAGES = {
    REASON_NO_CONTRACT: "指标未声明连接契约，沿用原比对规则",
    REASON_SINGLE_TABLE: "报表未包含连接，无需关系核对",
    REASON_PARSER_UNDECIDABLE: "连接超出单层两表等值合取的有界语法，无法判定关系",
    REASON_MULTIPLE_JOIN_SPECS: "查询包含多个连接，超出有界语法，无法判定关系",
    REASON_JOIN_TYPE_MISMATCH: "实际连接类型与契约声明不一致",
    REASON_ROLE_MISMATCH: "实际保留侧与契约声明的左右角色不符",
    REASON_TABLE_ROLE_MISMATCH: "实际连接的表角色与契约声明不一致",
    REASON_MISSING_KEY_PAIR: "联合键分量缺失：契约声明的等值配对未在 ON 条件中出现",
    REASON_WRONG_COLUMN: "连接到错误列：实际配对列与契约声明不一致",
    REASON_KEY_NOT_COVERED: "右表候选唯一键的分量未被连接配对完整覆盖",
}

# Wording for the reason codes emitted by the parser layer (sql_parse.joins).
PARSER_REASON_MESSAGES = {
    "subquery_in_from": "FROM 中存在子查询，超出单层两表有界语法",
    "not_exactly_two_tables": "无法识别为恰好两张物理表的连接",
    "unsupported_join_type": "连接类型不受支持（仅支持 INNER / LEFT）",
    "comma_or_cross_join": "逗号隐式连接或 CROSS JOIN 无法表达等值关系",
    "or_in_join_predicate": "ON 条件包含 OR，不是纯等值合取",
    "non_equality_conjunct": "ON 条件包含非等值连接项",
    "unqualified_column_in_on": "ON 条件中的列未限定表别名，无法绑定端点",
    "column_alias_not_bound": "ON 条件中的别名无法绑定到物理表",
    "join_endpoint_is_not_a_column": "连接端点不是简单的限定列",
    "more_than_one_join": "查询包含多个连接，超出单层两表有界语法",
    "missing_on_clause": "连接缺少可核对的 ON 等值条件（如 USING/NATURAL）",
}


@dataclass(frozen=True)
class JoinVerdict:
    """Outcome of checking one parsed query against one join contract."""

    status: str
    provable: bool
    reasons: tuple[str, ...] = ()
    expected_join_type: str = ""
    actual_join_type: str = ""
    expected_pairs: tuple[dict[str, str], ...] = ()
    actual_pairs: tuple[dict[str, str], ...] = ()
    missing_pairs: tuple[dict[str, str], ...] = ()
    mismatched_pairs: tuple[dict[str, Any], ...] = ()
    extra_pairs: tuple[dict[str, str], ...] = ()
    unique_key: tuple[str, ...] = ()
    uncovered_key_columns: tuple[str, ...] = ()
    parser_reasons: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provable": self.provable,
            "reasons": list(self.reasons),
            "reason_messages": [REASON_MESSAGES.get(code, PARSER_REASON_MESSAGES.get(code, code))
                                for code in self.reasons],
            "expected_join_type": self.expected_join_type,
            "actual_join_type": self.actual_join_type,
            "expected_pairs": [dict(item) for item in self.expected_pairs],
            "actual_pairs": [dict(item) for item in self.actual_pairs],
            "missing_pairs": [dict(item) for item in self.missing_pairs],
            "mismatched_pairs": [dict(item) for item in self.mismatched_pairs],
            "extra_pairs": [dict(item) for item in self.extra_pairs],
            "right_unique_key": list(self.unique_key),
            "uncovered_key_columns": list(self.uncovered_key_columns),
            "parser_reasons": list(self.parser_reasons),
            "detail": self.detail,
        }


def _pair_dict(left_table: str, left_column: str,
               right_table: str, right_column: str) -> dict[str, str]:
    return {
        "left_table": left_table,
        "left_column": left_column,
        "right_table": right_table,
        "right_column": right_column,
    }


def _expected_pairs(contract: JoinContract) -> tuple[dict[str, str], ...]:
    return tuple(
        _pair_dict(pair.left_table, pair.left_column, pair.right_table, pair.right_column)
        for pair in contract.on
    )


def _actual_pairs(join: JoinSpec) -> tuple[dict[str, str], ...]:
    return tuple(
        _pair_dict(item.left.table, item.left.column, item.right.table, item.right.column)
        for item in join.equalities
    )


def _reject(reason: str, *, expected: tuple[dict[str, str], ...] = (),
            actual: tuple[dict[str, str], ...] = (),
            missing: tuple[dict[str, str], ...] = (),
            mismatched: tuple[dict[str, Any], ...] = (),
            extra: tuple[dict[str, str], ...] = (),
            unique_key: tuple[str, ...] = (),
            uncovered: tuple[str, ...] = (),
            expected_type: str = "", actual_type: str = "",
            parser_reasons: tuple[str, ...] = ()) -> JoinVerdict:
    return JoinVerdict(
        status=UNDECIDABLE if reason == REASON_PARSER_UNDECIDABLE else UNPROVABLE,
        provable=False,
        reasons=(reason,),
        expected_join_type=expected_type,
        actual_join_type=actual_type,
        expected_pairs=expected,
        actual_pairs=actual,
        missing_pairs=missing,
        mismatched_pairs=mismatched,
        extra_pairs=extra,
        unique_key=unique_key,
        uncovered_key_columns=uncovered,
        parser_reasons=parser_reasons,
        detail=REASON_MESSAGES.get(reason, ""),
    )


def evaluate_join(parsed: ParsedQuery, contract: JoinContract | None) -> JoinVerdict | None:
    """Evaluate the relationship of *parsed* under the explicit *contract*.

    Returns ``None`` when the metric declares no contract: callers keep the
    legacy comparison untouched in that case.
    """
    if contract is None:
        return None
    expected = _expected_pairs(contract)
    if not parsed.joins:
        return JoinVerdict(
            status=UNPROVABLE,
            provable=False,
            reasons=(REASON_SINGLE_TABLE,),
            expected_join_type=contract.join_type,
            expected_pairs=expected,
            unique_key=contract.right_unique_key,
            detail=REASON_MESSAGES[REASON_SINGLE_TABLE],
        )
    if len(parsed.joins) != 1:
        return _reject(REASON_MULTIPLE_JOIN_SPECS, expected=expected,
                       expected_type=contract.join_type,
                       unique_key=contract.right_unique_key)
    join = parsed.joins[0]
    if not join.decidable:
        return JoinVerdict(
            status=UNDECIDABLE,
            provable=False,
            reasons=(REASON_PARSER_UNDECIDABLE,),
            expected_join_type=contract.join_type,
            actual_join_type=join.join_type,
            expected_pairs=expected,
            unique_key=contract.right_unique_key,
            parser_reasons=join.undecidable_reasons,
            detail=REASON_MESSAGES[REASON_PARSER_UNDECIDABLE],
        )

    # Join type (which side rows are preserved for LEFT joins) must match.
    if join.join_type != contract.join_type:
        return _reject(
            REASON_JOIN_TYPE_MISMATCH,
            expected=expected, actual=_actual_pairs(join),
            expected_type=contract.join_type, actual_type=join.join_type,
            unique_key=contract.right_unique_key,
        )
    # Physical table roles, including the preserved side, must match.
    if (join.left_table, join.right_table) != (contract.left_table, contract.right_table):
        swapped = (join.left_table == contract.right_table
                   and join.right_table == contract.left_table)
        reason = REASON_ROLE_MISMATCH if swapped else REASON_TABLE_ROLE_MISMATCH
        return _reject(
            reason,
            expected=expected, actual=_actual_pairs(join),
            expected_type=contract.join_type, actual_type=join.join_type,
            unique_key=contract.right_unique_key,
        )

    actual = _actual_pairs(join)
    actual_signatures = {
        (item["left_table"], item["left_column"], item["right_table"], item["right_column"])
        for item in actual
    }
    expected_signatures = {
        (item["left_table"], item["left_column"], item["right_table"], item["right_column"])
        for item in expected
    }
    missing = tuple(item for item in expected
                    if tuple(item[key] for key in
                             ("left_table", "left_column", "right_table", "right_column"))
                    not in actual_signatures)
    extra = tuple(item for item in actual
                  if tuple(item[key] for key in
                           ("left_table", "left_column", "right_table", "right_column"))
                  not in expected_signatures)

    # A pair connecting the expected left column to a different right column
    # is concrete evidence of a wrong-column join rather than a mere omission.
    mismatched: list[dict[str, Any]] = []
    actual_by_left = {
        (item["left_table"], item["left_column"]): item for item in actual
    }
    for item in expected:
        observed = actual_by_left.get((item["left_table"], item["left_column"]))
        if observed is not None and observed != item:
            mismatched.append({"expected": item, "actual": observed})
    if mismatched:
        return _reject(
            REASON_WRONG_COLUMN,
            expected=expected, actual=actual, missing=missing,
            mismatched=tuple(mismatched), extra=extra,
            expected_type=contract.join_type, actual_type=join.join_type,
            unique_key=contract.right_unique_key,
        )
    if missing:
        return _reject(
            REASON_MISSING_KEY_PAIR,
            expected=expected, actual=actual, missing=missing, extra=extra,
            expected_type=contract.join_type, actual_type=join.join_type,
            unique_key=contract.right_unique_key,
        )

    # Every component of the declared candidate unique key must be covered by
    # an equality pair whose right endpoint is that right-table column.
    covered_right = {
        item["right_column"] for item in actual
        if item["right_table"] == contract.right_table
    }
    uncovered = tuple(sorted(set(contract.right_unique_key) - covered_right))
    if uncovered:
        return _reject(
            REASON_KEY_NOT_COVERED,
            expected=expected, actual=actual, extra=extra,
            expected_type=contract.join_type, actual_type=join.join_type,
            unique_key=contract.right_unique_key, uncovered=uncovered,
        )

    return JoinVerdict(
        status=PROVABLE,
        provable=True,
        expected_join_type=contract.join_type,
        actual_join_type=join.join_type,
        expected_pairs=expected,
        actual_pairs=actual,
        extra_pairs=extra,
        unique_key=contract.right_unique_key,
        detail="在显式契约下可证明为多对一连接（候选唯一键来自字典声明，"
               "不代表运行时数据已唯一）",
    )
