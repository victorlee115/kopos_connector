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


def test_a_week_is_one_query_and_today_comes_out_of_it() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # "Did we make money today" almost always means "compared with what", and
    # a second query for the same invoices could disagree with the first.
    # One read of Sales Invoice answers today, the week behind it, and the
    # split between counters. A second query for the same invoices could
    # disagree with the first.
    assert source.count('frappe.get_all(\n        "Sales Invoice"') == 1
    assert "TAKINGS_DAYS = 7" in source
    assert '"week_total"' in source
    assert '"outlets"' in source


def test_someone_who_cannot_sign_in_reaches_the_queue() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # An active person with no verifier cannot open a shift. That is the one
    # thing the staff area ever needs a director for, so it is never silent.
    assert '"needs_pin"' in source
    assert "cannot sign in until you set a PIN" in source


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


def test_every_tile_opens_the_page_that_owns_the_number() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # Home composes; it never becomes the owner. Each tile hands the director
    # to the page that can act on that number, and only Today -- which no JiJi
    # page owns -- still opens the ERP list.
    for route in ('["List", "Sales Invoice"]', '["jiji_menu_recipes"]', '["jiji_supplies"]',
                  '["jiji_staff"]', '["jiji_money"]'):
        assert route in source, f"a tile is not routed to {route}"


def test_the_page_is_registered_for_directors() -> None:
    install = (Path(__file__).resolve().parents[1] / "install" / "install.py").read_text(encoding="utf-8")
    assert '"name": "jiji_home"' in install
    assert '"title": "JiJi Home"' in install


def test_the_page_degrades_instead_of_blanking() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # One area failing must not take the home page down with it.
    assert "renderUnavailable" in page
    assert "Nothing is wrong with your café" in page


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
    scope = (SOURCE.parent / "director_scope.py").read_text(encoding="utf-8")
    source = SOURCE.read_text(encoding="utf-8")
    # A director who never set a user default met an empty page on a site that
    # only has one company. One owner answers this for every director page, so
    # two pages cannot disagree about which company is on screen.
    assert 'frappe.get_all("Company"' in scope
    assert "len(companies) == 1" in scope
    assert "director_company()" in source


def test_routes_are_segments_not_urls() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    # The desk lives at /app on some deployments and /desk on others, and its
    # router intercepts clicks rather than following an href. Hard-coded URLs
    # 404'd on one and did nothing on the other.
    assert "/app/" not in source
    # The click itself is routed by the one desk owner; see test_jiji_desk.
    assert "jiji.desk.routeLink" in page
