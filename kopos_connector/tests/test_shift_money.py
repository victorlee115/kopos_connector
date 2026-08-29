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
    namespace: dict = {"flt": float, "cstr": str}
    body = source.split("def describe_difference", 1)[1]
    exec("def describe_difference" + body.split("def group_shifts", 1)[0], namespace)
    return namespace


def test_the_difference_is_stated_in_words_with_a_direction() -> None:
    describe = _module()["describe_difference"]
    # A signed number is not an instruction. "-12.0" tells a director nothing.
    assert describe(-12) == "RM 12.00 short"
    assert describe(12) == "RM 12.00 over"
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


def test_home_and_money_share_one_reader() -> None:
    home = HOME.read_text(encoding="utf-8")
    # Two answers to "how many shifts need a director" is one too many.
    assert "from kopos_connector.api.shift_money import" in home
    assert 'frappe.get_all(\n        "FB Shift"' not in home


def test_the_raw_report_stays_reachable_as_advanced_detail() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "More detail" in page
    assert "report_route" in page


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
