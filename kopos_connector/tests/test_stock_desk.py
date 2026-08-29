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
    # No arithmetic of its own: the module reads the estimate and its reason,
    # and never derives either.
    assert "median" not in source and "timedelta" not in source
    assert 'runout.get("days")' in source


def test_the_item_nobody_ordered_is_the_one_that_needs_a_person() -> None:
    source = _source()
    page = PAGE.read_text(encoding="utf-8")
    assert '"Not ordered yet"' in source and '"Already on an order"' in source
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


def test_an_empty_list_says_which_kind_of_empty_it_is() -> None:
    source = _source()
    page = PAGE.read_text(encoding="utf-8")
    # "Nothing is due to run out" and "JiJi cannot say yet" are different
    # facts, and a blank space says neither. The snapshot already carries its
    # own plain-language reason, so it is passed through rather than reworded.
    assert '"running_low_note"' in source
    assert "runout_reason" in source and "reliability_reason" in source
    assert "data.running_low_note" in page
    assert "data.moved_empty_note" in page


def test_a_new_cafe_is_told_why_rather_than_shown_nothing() -> None:
    forecast = (ROOT / "kopos" / "services" / "inventory_autopilot" / "forecast.py").read_text("utf-8")
    # The forecast refuses to guess from a short history: 14 training days plus
    # 14 test days. So a café under four weeks past cutover has no runout at
    # all, by design, and the running-low list is empty for a good reason.
    assert "MIN_RELIABLE_DAYS = MIN_TRAINING_DAYS + MIN_TEST_DAYS" in forecast
    assert "MIN_TRAINING_DAYS = 14" in forecast and "MIN_TEST_DAYS = 14" in forecast


def test_the_page_never_shows_two_numbers_for_one_question() -> None:
    source = _source()
    # The health cards below the tiles count open count tasks with
    # OPEN_COUNT_TASK_STATUSES. A tile with its own idea of "waiting" showed 0
    # beside a card showing 1, on the same screen.
    assert "OPEN_COUNT_TASK_STATUSES" in source
    assert '"Submitted", "Review"' not in source


def test_the_counted_tiles_are_quantities_not_statuses() -> None:
    source = _source()
    # Six health paragraphs answer support's question. These six answer the
    # café's: what is short, waiting, coming, made, wasted, in transit.
    for label in ("Running low", "Stock checks", "Deliveries", "Made in store", "Waste", "Moved between shops"):
        assert f'"label": "{label}"' in source, f"the counted tiles are missing {label}"


def test_an_unanswerable_count_is_not_reported_as_zero() -> None:
    source = _source()
    # "Running low: 0 · nothing short" sat directly above a block saying JiJi
    # could not say yet. An empty list and an unanswerable question look the
    # same in a number and must not read the same.
    assert '"JiJi cannot say yet"' in source
    assert "if low or not no_estimate" in source
