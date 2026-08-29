"""Naming the three things that are running low, and what moved today.

The stock page said "Running low: 3" and never said which three, and showed a
waste figure with no cause. Both are numbers nobody can act on. These tests
hold the two properties that keep the answers trustworthy: the page never
becomes a second opinion about when something runs out, and it never writes.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "api" / "stock_desk.py"
PAGE = ROOT / "kopos" / "page" / "jiji_stock_autopilot" / "jiji_stock_autopilot.js"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_the_stock_desk_never_writes() -> None:
    forbidden = {"insert", "save", "submit", "cancel", "delete", "set_value", "commit", "new_doc"}
    used = {
        node.func.attr
        for node in ast.walk(ast.parse(_source()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (used & forbidden), f"the stock desk must stay read-only, found {sorted(used & forbidden)}"


def test_how_long_something_lasts_has_one_owner() -> None:
    source = _source()
    # The tablet reads the same snapshot. Recomputing here would give the café
    # two answers to "when does the matcha run out".
    assert "build_edge_inventory_snapshot" in source
    assert "forecast" not in source


def test_the_item_nobody_ordered_is_the_one_that_needs_a_person() -> None:
    source = _source()
    page = PAGE.read_text(encoding="utf-8")
    assert '"Not ordered yet"' in source and '"On the draft order"' in source
    # Attention is reserved for the one that is not covered; a draft order is
    # already waiting on the decision the supplies page is asking for.
    assert 'row.on_order ? "" : " jiji-pill--attention"' in page


def test_waste_is_shown_with_its_cause() -> None:
    source = _source()
    # A waste figure with no cause is a number nobody can act on. FB Waste
    # Event already records the reason, so it is read rather than inferred.
    assert '"FB Waste Event"' in source
    assert "reason_text" in source


def test_a_failing_snapshot_costs_two_blocks_not_the_page() -> None:
    source = _source()
    page = PAGE.read_text(encoding="utf-8")
    assert "except Exception:" in source and "frappe.log_error" in source
    assert "error: () => this.renderStockDesk({})" in page


def test_a_transfer_between_two_of_your_own_stores_is_not_counted_twice() -> None:
    source = _source()
    assert 's_warehouse" if source else "t_warehouse' in source


def test_the_meter_never_overflows_or_vanishes() -> None:
    source = _source()
    # A bar at 0% reads as a missing bar, and one past 100% escapes its track.
    assert "max(2, min(100," in source


def test_the_snapshot_is_asked_for_a_size_it_accepts() -> None:
    source = _source()
    supplies = (ROOT / "api" / "supplier_orders.py").read_text(encoding="utf-8")
    # The snapshot refuses a limit above 100 rather than truncating it. Asking
    # for 200 cost the whole running-low block on a real site, silently,
    # because the degrade path caught the refusal.
    assert "SNAPSHOT_LIMIT = 100" in source
    assert "limit=SNAPSHOT_LIMIT" in source
    assert "min(100, max(1," in supplies
