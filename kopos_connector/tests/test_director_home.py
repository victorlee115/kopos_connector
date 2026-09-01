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


def test_a_week_is_aggregated_in_the_database_not_loaded_sale_by_sale() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    body = source.split("def _recent_takings", 1)[1].split("def _shift_money", 1)[0]
    # A busy week may contain thousands of invoices. The result remains a
    # bounded set of seven days, outlets and hours.
    assert 'frappe.get_all(\n        "Sales Invoice"' not in body
    assert body.count("frappe.db.sql(") == 3
    assert "SUM(invoice.grand_total)" in body and "GROUP BY invoice.posting_date" in body
    assert "fb_order.completion_kind = 'complimentary'" in body
    assert "fb_order.complimentary_value" in body
    assert "ORDER BY order_count DESC, total DESC" in body
    assert "TAKINGS_DAYS = 7" in source
    assert '"week_total"' in source
    assert '"outlets"' in source


def test_complimentary_value_is_separate_from_money_collected() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")

    assert '"week_complimentary_value_sen"' in source
    assert '"complimentary_orders"' in source
    assert '"label": "Money in today"' in source
    assert '__("{0} collected"' in page
    assert '__("{0} given free"' in page


def test_home_uses_singular_menu_copy_for_one_item() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert '"thing on sale" if items_ready == 1 else "things on sale"' in source


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


def test_missing_cash_count_is_never_presented_as_zero_work() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert 'uncounted_shifts = shift_money["missing_count"]' in source
    assert "money_issues = [*uncounted_shifts, *unmatched_shifts]" in source
    assert '"value": str(len(money_issues))' in source
    assert '"attention": bool(money_issues)' in source
    assert "still need a cash count" in source


def test_clear_home_does_not_claim_it_checked_stock() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert "Your sales and your stock both look normal" not in source
    assert "no money, menu, supplies, or staff action waiting" in source


def test_exactly_one_next_step_is_returned() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # Assigned per priority branch, returned as one object, never accumulated.
    assert '"next_step": next_step' in source
    assert "next_step.append" not in source


def test_every_tile_opens_the_page_that_owns_the_number() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # Home composes; it never becomes the owner. Each tile hands the director
    # to the simple page that can act on that number. Today is a fact rather
    # than a job, so it is deliberately not a door into the raw invoice list.
    assert '["List", "Sales Invoice"]' not in source
    assert '"key": "today"' in source and '"route": []' in source
    for route in ('["jiji_menu_recipes"]', '["jiji_supplies"]',
                  '["jiji_staff"]', '["jiji_money"]'):
        assert route in source, f"a tile is not routed to {route}"


def test_home_never_opens_raw_business_records() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    assert '["List"' not in source
    assert '["Form"' not in source
    assert "routeLink(outlet.route)" not in page


def test_the_page_is_registered_for_directors() -> None:
    install = (Path(__file__).resolve().parents[1] / "install" / "install.py").read_text(encoding="utf-8")
    assert '"name": "jiji_home"' in install
    assert '"title": "JiJi Home"' in install


def test_the_page_degrades_instead_of_blanking() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # One area failing must not take the home page down with it.
    assert "renderUnavailable" in page
    assert "This page has not confirmed anything yet" in page
    assert "Nothing is wrong with your café" not in page


def test_a_login_is_never_shown_as_a_person() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    money = (SOURCE.parent / "shift_money.py").read_text(encoding="utf-8")
    # FB Shift records staff_id, which is an email. Running the endpoint against
    # real data printed "Start with staff@smoke.kopos.local." The name now
    # comes from the shared reader, so the id never reaches the page.
    assert "staff_id" not in source
    assert "person_name(" in money
    assert "employee_name" in money


def test_a_single_permitted_company_is_selected_without_granting_site_wide_access() -> None:
    scope = (SOURCE.parent / "director_scope.py").read_text(encoding="utf-8")
    source = SOURCE.read_text(encoding="utf-8")
    # Site configuration is not authorization. Only a single explicitly
    # permitted company may be chosen automatically.
    assert '"User Permission"' in scope
    assert 'return allowed[0] if len(allowed) == 1 else ""' in scope
    assert "resolve_director_context" in source


def test_routes_are_segments_not_urls() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    # The desk lives at /app on some deployments and /desk on others, and its
    # router intercepts clicks rather than following an href. Hard-coded URLs
    # 404'd on one and did nothing on the other.
    assert "/app/" not in source
    # The click itself is routed by the one desk owner; see test_jiji_desk.
    assert "jiji.desk.routeLink" in page
