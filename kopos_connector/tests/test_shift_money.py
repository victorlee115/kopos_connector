"""Money reads the shift; it never recounts it."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "api" / "shift_money.py"
HOME = Path(__file__).resolve().parents[1] / "api" / "director_home.py"
PAGE = Path(__file__).resolve().parents[1] / "kopos" / "page" / "jiji_money" / "jiji_money.js"


def _module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("shift_money_under_test", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Only the pure helpers are exercised; frappe is not importable here.
    source = SOURCE.read_text(encoding="utf-8")
    namespace: dict = {
        "flt": float,
        "cstr": str,
        "money_text": lambda value, currency="MYR": f"{'RM' if currency == 'MYR' else currency} {value / 100:,.2f}",
    }
    body = source.split("def describe_difference", 1)[1]
    exec("def describe_difference" + body.split("def group_shifts", 1)[0], namespace)
    return namespace


def test_the_difference_is_stated_in_words_with_a_direction() -> None:
    describe = _module()["describe_difference"]
    # A signed number is not an instruction. "-12.0" tells a director nothing.
    assert describe(-1_200) == "RM 12.00 short"
    assert describe(1_200) == "RM 12.00 over"
    assert describe(0) == "Matched"


def test_money_never_writes() -> None:
    forbidden = {"insert", "save", "submit", "delete", "set_value", "commit", "new_doc"}
    used = {
        node.func.attr
        for node in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (used & forbidden)


def test_the_stored_variance_is_read_not_recomputed() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # FB Shift's close path owns this number.
    assert "cash_variance" in source
    assert "counted_cash - expected_cash" not in source
    assert "expected_cash -" not in source


def test_a_missing_cash_count_is_not_called_a_match() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert 'else "Not compared"' in source
    assert 'row.get("cash_variance") or 0' not in source


def test_home_and_money_share_one_reader() -> None:
    home = HOME.read_text(encoding="utf-8")
    # Two answers to "how many shifts need a director" is one too many.
    assert "from kopos_connector.api.shift_money import" in home
    assert 'frappe.get_all(\n        "FB Shift"' not in home


def test_the_beginner_page_does_not_open_raw_shift_records_or_reports() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "More detail" not in page
    assert "report_route" not in page
    assert '["Form", "FB Shift"' not in page


def test_a_person_has_the_same_name_everywhere() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    staff_access = (
        SOURCE.parent.parent / "kopos" / "services" / "inventory_autopilot" / "staff_access.py"
    ).read_text(encoding="utf-8")
    # The tablet builds its staff list from User.full_name. A director's
    # screens must not read a different record and show a different name.
    assert 'get_value("User", row["user"], "full_name")' in staff_access
    user_index = source.index('get_value("User", identifier, "full_name")')
    employee_index = source.index('get_value("Employee", employee, "employee_name")')
    assert user_index < employee_index, "User.full_name must be preferred, as the tablet does"


def _overview(rows, *, offset=0):
    """Execute the production SELECTs against SQLite, not a canned row mock.

    This is supplemental query/unit evidence, not MariaDB/Frappe acceptance.
    """
    import sqlite3
    from decimal import Decimal
    from types import SimpleNamespace
    from typing import Any

    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute("CREATE TABLE `tabFB Shift` (name TEXT, company TEXT, status TEXT, staff_id TEXT, closed_at TEXT, expected_cash NUMERIC, counted_cash NUMERIC, cash_variance NUMERIC)")
    database.executemany("INSERT INTO `tabFB Shift` VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def sql(query, parameters, *, as_dict):
        assert as_dict
        return [dict(row) for row in database.execute(query.replace("%s", "?"), parameters)]

    names = {"closed_shifts", "shift_money_overview", "group_shifts", "describe_difference", "_money_next_step"}
    module = ast.parse(SOURCE.read_text(encoding="utf-8"))
    selected = ast.Module(body=[node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
    namespace = {
        "Any": Any, "frappe": SimpleNamespace(db=SimpleNamespace(sql=sql)),
        "cstr": str, "company_currency": lambda company: "MYR", "person_name": lambda staff: staff,
        "money_sen": lambda value, field: int(Decimal(str(value)) * 100),
        "money_text": lambda sen, currency: f"{currency} {Decimal(sen) / 100:.2f}",
    }
    try:
        exec(compile(selected, str(SOURCE), "exec"), namespace)
        result = namespace["shift_money_overview"]("Review company", offset=offset)
        return result, namespace["_money_next_step"](result)
    finally:
        database.close()


def _shift(name, variance=0, *, company="Review company", counted=100):
    return (name, company, "Closed", "Cashier", name, 100, counted, variance)


def test_older_cash_difference_survives_fifty_newer_matches():
    rows = [_shift(f"2026-09-{index:03}") for index in range(60)]
    rows.append(_shift("2026-07-001", -20, counted=80))
    result, statement = _overview(rows)
    assert result["counts"] == {"matched": 60, "needs_review": 1, "missing_count": 0}
    assert result["needs_review"][0]["variance_sen"] == -2000
    assert "different amount" in statement["title"]


def test_exception_pages_are_bounded_without_changing_complete_counts():
    rows = [_shift(f"2026-09-{index:03}", -1, counted=99) for index in range(120)]
    first, _ = _overview(rows)
    second, _ = _overview(rows, offset=50)
    last, _ = _overview(rows, offset=100)
    assert [len(page["needs_review"]) for page in (first, second, last)] == [50, 50, 20]
    assert [page["next_offset"] for page in (first, second, last)] == [50, 100, None]
    assert all(page["counts"]["needs_review"] == 120 for page in (first, second, last))
    dates = [row["closed_at"] for page in (first, second, last) for row in page["needs_review"]]
    assert len(set(dates)) == 120


def test_company_scope_and_uncounted_shifts_are_preserved():
    result, statement = _overview([
        _shift("newer", -20, company="Another company", counted=80),
        _shift("older", None, counted=None),
    ])
    assert result["counts"] == {"matched": 0, "needs_review": 0, "missing_count": 1}
    assert len(result["missing_count"]) == 1
    assert "without counting" in statement["title"]


def test_no_closed_shifts_is_not_an_all_clear():
    _, statement = _overview([])
    assert statement["title"] == "No shift has been counted yet"


def test_missing_stored_comparison_stays_an_exception():
    result, statement = _overview([_shift("uncompared", None)])
    assert result["counts"]["needs_review"] == 1
    assert result["needs_review"][0]["variance_sen"] is None
    assert result["needs_review"][0]["difference"] == "Not compared"
    assert statement["title"] != "The cash is accounted for"


def test_matching_shifts_use_total_count_not_an_empty_detail_page():
    result, statement = _overview([_shift("matched")])
    assert result["matched"] == []
    assert result["counts"]["matched"] == 1
    assert statement["title"] == "The cash is accounted for"
