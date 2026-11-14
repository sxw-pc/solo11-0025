from __future__ import annotations

import pytest

from koujing.sql_parse.model import (
    canonical_expr,
    canonical_expr_surface,
    normalise_conjunction,
)


def test_and_terms_are_order_independent():
    assert canonical_expr("event_type = 'login' and status = 'ok'") == canonical_expr(
        "status = 'ok' AND event_type = 'login'"
    )


def test_duplicate_conjuncts_are_removed():
    assert canonical_expr("a = 1 and b = 2 and a = 1") == canonical_expr("a = 1 and b = 2")


def test_redundant_outer_parentheses_are_removed():
    assert canonical_expr("(a = 1 and b = 2)") == "a = 1 and b = 2"
    assert canonical_expr("((a = 1))") == "a = 1"


def test_nested_and_groups_are_flattened():
    assert canonical_expr("(a = 1 and b = 2) and c = 3") == canonical_expr(
        "c = 3 and a = 1 and b = 2"
    )


def test_or_chain_order_is_never_rewritten():
    first = canonical_expr("a = 1 or b = 2")
    second = canonical_expr("b = 2 or a = 1")
    assert first != second
    assert first == "a = 1 or b = 2"
    assert second == "b = 2 or a = 1"


def test_comparison_chain_order_is_never_rewritten():
    assert canonical_expr("a = b") != canonical_expr("b = a")


def test_or_group_inside_conjunction_keeps_order():
    assert canonical_expr("(a = 1 or b = 2) and c = 3") != canonical_expr(
        "(b = 2 or a = 1) and c = 3"
    )


def test_between_and_is_not_treated_as_a_conjunction():
    text = "a between 1 and 2 and b = 3"
    assert canonical_expr(text) == text
    assert canonical_expr("(a between 1 and 2 and b = 3)") == f"({text})"


def test_case_expression_is_left_untouched():
    text = "case when a = 1 and b = 2 then 1 else 0 end"
    assert canonical_expr(text) == text


def test_and_inside_string_literal_is_not_a_keyword():
    assert canonical_expr("note = 'a and b' and z = 1") == canonical_expr(
        "z = 1 and note = 'a and b'"
    )
    assert canonical_expr("note = 'a and b'") == "note = 'a and b'"


def test_existing_handling_of_literals_qualifiers_case_and_spacing_is_unchanged():
    assert canonical_expr("  T.Status   =   'OK'  ") == "status = 'OK'"
    assert canonical_expr_surface("  T.Status   =   'OK'  ") == "status = 'OK'"
    assert canonical_expr("note = '''And''' and Z = 2") == (
        "note = '''And''' and z = 2"
    )


def test_surface_form_keeps_written_order():
    assert canonical_expr_surface("status = 'ok' AND event_type = 'login'") == (
        "status = 'ok' and event_type = 'login'"
    )


def test_normalise_conjunction_is_idempotent():
    samples = [
        "b = 2 and a = 1",
        "(a = 1 and b = 2) and a = 1",
        "a = 1 or b = 2",
        "a between 1 and 2 and b = 3",
        "a = 1 and (b = 2 or c = 3)",
    ]
    for sample in samples:
        once = normalise_conjunction(sample)
        assert normalise_conjunction(once) == once


@pytest.mark.parametrize("text", ["", None, "   "])
def test_empty_inputs_are_empty(text):
    assert canonical_expr(text) == ""
