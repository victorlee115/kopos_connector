"""JiJi Supplies shows a decision; ERPNext still makes it.

A director approving a supplier order needs five facts and a reason. This page
gathers them and stops. The two properties that keep it safe are that it never
writes, and that it never becomes a second place to send an order -- the
Purchase Order is the accounting document and the only thing that can.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "api" / "supplier_orders.py"
PAGE = ROOT / "kopos" / "page" / "jiji_supplies" / "jiji_supplies.js"


def _tree() -> ast.Module:
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def test_supplies_never_writes() -> None:
    forbidden = {"insert", "save", "submit", "cancel", "delete", "set_value", "commit", "new_doc", "rename_doc"}
    used = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (used & forbidden), f"supplies must stay read-only, found {sorted(used & forbidden)}"


def test_sending_stays_in_erpnext() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # A second submit path would put the accounting document's authority on a
    # page that cannot frame the consequence. The button says where it goes.
    assert "Open the order to send it" in page
    assert "Nothing is sent from this page." in page
    # Every server call the page makes is a read, and says so.
    methods = re.findall(r'method:\s*"([^"]+)"', page)
    assert methods == ["kopos_connector.api.supplier_orders.get_supplier_orders"]
    assert page.count('type: "GET"') == len(methods)


def test_the_runout_figure_is_read_not_recomputed() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # The tablet and the desk must never tell the café two different days.
    assert "build_edge_inventory_snapshot" in source
    assert "forecast" not in source.replace("forecast_through_lead_time", "")


def test_a_failing_stock_area_does_not_take_the_page_down() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # Supplies is still useful without the "why" line; it is not useful blank.
    assert "except Exception:" in source
    assert "frappe.log_error" in source


def test_the_decision_carries_five_facts_and_a_reason() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    for fact in ('"lines"', '"total"', '"arrives"', '"reliability"', '"why"'):
        assert fact in source, f"the supplies decision is missing {fact}"


def test_amounts_are_said_the_way_they_are_bought() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # "12 Bag, conversion factor 250, stock UOM Gram" is the stored fact.
    # "12 bags · 250 g each" is the same fact a café owner can picture.
    assert "conversion_factor" in source
    assert '" each"' in source or "each" in source


def test_history_is_bounded() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # Enough to see a pattern, never enough to become a report the director
    # has to read sideways again.
    assert "HISTORY_LIMIT = 3" in source
    assert "RELIABILITY_SAMPLE = 6" in source
    assert "limit_page_length=RELIABILITY_SAMPLE" in source


def test_the_page_is_registered_for_directors() -> None:
    install = (ROOT / "install" / "install.py").read_text(encoding="utf-8")
    assert '"name": "jiji_supplies"' in install
    assert '"title": "JiJi Supplies"' in install


def test_the_page_degrades_instead_of_blanking() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "could not read your supplier orders just now" in page
    assert "Nothing has been sent or changed" in page


def test_amounts_are_worded_by_one_owner() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    units = (ROOT / "api" / "cafe_units.py").read_text(encoding="utf-8")
    # A real draft printed "6 cartons · 1000 ml each". Nobody says that about a
    # litre of milk. The scale only applies when it stays exact, so 1500 ml is
    # never rounded into 2 L. Two desk pages now show amounts, so the wording
    # has one home rather than a copy in each.
    assert "_SCALED_UNITS" in units
    assert "% 1000 == 0" in units
    assert "from kopos_connector.api.cafe_units import amount_phrase" in source
    assert "_UNIT_WORDS" not in source


def test_an_order_still_in_the_air_is_not_history() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # A real site showed the same sent order under both "Last orders from" and
    # "Already on the way". Two names for one order on one screen is exactly
    # the duplication this page exists to undo.
    assert 'for row in record if row["late_days"] is not None' in source


def test_the_supplier_record_stands_on_its_own() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # Folded into the decision card it read as a footnote to this order rather
    # than the evidence the order is weighed against.
    assert "renderHistory(draft)" in page
    # And the reliability line is drawn in exactly one place, beside the
    # decision. It appeared in both card headers, word for word, on one screen.
    assert page.count(".text(draft.reliability)") == 1


def test_how_many_are_waiting_is_said_where_the_page_is_named() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "set_indicator" in page
    assert '__("1 draft")' in page
