"""JiJi Home composes; it must not decide.

The page exists to put one decision in front of a director. These tests hold
the two properties that make that safe: it never writes, and it never invents
a number that a service already owns.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "api" / "director_home.py"
PAGE = Path(__file__).resolve().parents[1] / "kopos" / "page" / "jiji_home" / "jiji_home.js"


def _tree() -> ast.Module:
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def test_director_home_never_writes() -> None:
    forbidden = {"insert", "save", "submit", "delete", "set_value", "commit", "rename_doc", "new_doc"}
    used = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (used & forbidden), f"director home must stay read-only, found {sorted(used & forbidden)}"


def test_cash_variance_is_read_not_recomputed() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # FB Shift owns this number. Recomputing it here would create a second
    # answer to "how much cash was missing".
    assert "cash_variance" in source
    assert "counted_cash" not in source
    assert "expected_cash" not in source


def test_exactly_one_next_step_is_returned() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # Assigned once per branch, never accumulated into a list.
    assert source.count("next_step = {") == 4
    assert "next_step.append" not in source


def test_every_tile_links_to_the_record_it_came_from() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    for route in ("/app/sales-invoice", "/app/jiji-menu-recipes", "/app/kopos-staff-access"):
        assert route in source


def test_the_page_is_registered_for_directors() -> None:
    install = (Path(__file__).resolve().parents[1] / "install" / "install.py").read_text(encoding="utf-8")
    assert '"name": "jiji_home"' in install
    assert '"title": "JiJi Home"' in install


def test_the_page_degrades_instead_of_blanking() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # One area failing must not take the home page down with it.
    assert "renderUnavailable" in page
    assert "Nothing is wrong with your records" in page


def test_a_login_is_never_shown_as_a_person() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    money = (SOURCE.parent / "shift_money.py").read_text(encoding="utf-8")
    # FB Shift records staff_id, which is an email. Running the endpoint against
    # real data printed "Start with staff@smoke.kopos.local." The name now
    # comes from the shared reader, so the id never reaches the page.
    assert "staff_id" not in source
    assert "person_name(" in money
    assert "employee_name" in money


def test_a_single_company_site_does_not_ask_which_company() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # A director who never set a user default met an empty page on a site that
    # only has one company.
    assert 'frappe.get_all("Company"' in source
    assert "len(companies) == 1" in source
