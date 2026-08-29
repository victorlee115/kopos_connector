"""One sequence per page, and never an empty required filter.

Two pages predate the desk vocabulary and were restructured rather than
rewritten. Menu & Recipes offered three competing step models -- a numbered
prose list, four chips (two numbered, one greyed with no reason), and a
progress card with its own "Next" -- so a director had to work out which one
was the actual order. Stock opened with a required outlet filter and showed
nothing at all until it was filled.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MENU = ROOT / "kopos" / "page" / "jiji_menu_recipes" / "jiji_menu_recipes.js"
STOCK = ROOT / "kopos" / "page" / "jiji_stock_autopilot" / "jiji_stock_autopilot.js"
SCOPE = ROOT / "api" / "director_scope.py"


def test_the_menu_has_one_numbered_sequence() -> None:
    page = MENU.read_text(encoding="utf-8")
    # The prose list said the same three things in a different order and a
    # different voice. The chips are the sequence.
    assert "<ol " not in page and "<ol>" not in page
    for step in ('1. Choose item type', '2. Enter recipe', '3. Check what the tablet will show'):
        assert step in page, f"the menu sequence is missing {step!r}"


def test_a_tool_is_not_numbered_as_a_step() -> None:
    page = MENU.read_text(encoding="utf-8")
    # "Copy or change a recipe" sat unnumbered among numbered chips, which is
    # how a sequence stops reading as one. FB Recipe still points people here
    # by this exact name, so the label is kept.
    assert "Copy or change a recipe" in page
    assert "3. Copy or change" not in page
    assert "for an item that already has one" in page


def test_a_greyed_step_says_what_it_is_waiting_for() -> None:
    page = MENU.read_text(encoding="utf-8")
    for waiting in ("Step 1 is waiting for", "Step 2 is waiting for", "Step 3 is waiting for"):
        assert waiting in page, f"a disabled step never explains itself: {waiting!r}"


def test_stock_opens_on_the_outlet_the_director_has() -> None:
    page = STOCK.read_text(encoding="utf-8")
    scope = SCOPE.read_text(encoding="utf-8")
    assert "kopos_connector.api.director_scope.get_director_outlet" in page
    assert "def get_director_outlet" in scope
    # A director who already chose is never overridden, and a slow or failing
    # lookup leaves the filter usable instead of blanking the page.
    assert "if (this.warehouseControl.get_value()) return;" in page
    assert "error: () => this.updateWarehouseHelp()" in page
    # set_value fills the input without firing the change the page listens for,
    # and resolves after its own validation. Reading the field back directly
    # left the outlet filled in and the page still asking for an outlet, so a
    # defaulted outlet takes the same path a chosen one does.
    assert "Promise.resolve(this.warehouseControl.set_value(warehouse))" in page
    assert 'this.warehouseControl.$input.trigger("change")' in page


def test_stock_no_longer_opens_with_an_instruction_to_fill_a_filter() -> None:
    page = STOCK.read_text(encoding="utf-8")
    assert "Choose an outlet. JiJi will show what needs your attention." not in page


def test_the_outlet_store_is_the_one_the_tablets_sell_from() -> None:
    scope = SCOPE.read_text(encoding="utf-8")
    # A POS Profile already names it. Counting warehouses cannot work: ERPNext
    # creates four for every company, so a single-outlet café never looks like
    # one -- verified on a real site, where the count rule never fired.
    assert '"POS Profile"' in scope
    assert 'pluck="warehouse"' in scope


def test_an_ambiguous_outlet_is_still_asked_for() -> None:
    scope = SCOPE.read_text(encoding="utf-8")
    # Two outlets selling from different stores is a real question. Choosing
    # one would show a director the wrong café without saying so.
    assert "len(warehouses) == 1" in scope


def test_stock_says_the_urgent_thing_once() -> None:
    page = STOCK.read_text(encoding="utf-8")
    # The first area card repeated the next step word for word, directly under
    # it: same value, same detail, same button, same route. Six areas remain,
    # and the one the next step came from points at it instead of raising a
    # second alarm.
    assert "Needs your attention" not in page
    assert "This is your next step" in page
    assert "section.route === needs.route" in page


def test_stock_keeps_all_six_areas() -> None:
    page = STOCK.read_text(encoding="utf-8")
    areas = [key for key in ("today", "stock", "counts", "plans", "finance", "settings")
             if f'key: "{key}"' in page]
    assert areas == ["today", "stock", "counts", "plans", "finance", "settings"]
