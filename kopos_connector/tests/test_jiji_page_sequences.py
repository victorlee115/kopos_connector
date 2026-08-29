"""One sequence per page, and never an empty required filter.

Two pages predate the desk vocabulary and were restructured rather than
rewritten. Menu & Recipes offered three competing step models -- a numbered
prose list, four chips (two numbered, one greyed with no reason), and a
progress card with its own "Next" -- so a director had to work out which one
was the actual order. Stock opened with a required outlet filter and showed
nothing at all until it was filled.
"""

from __future__ import annotations

import re
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


def test_every_area_is_named_by_what_it_holds() -> None:
    """An area card's title has to survive being read on its own.

    "Today" held the automatic job's status, on a page where every other card
    is named by its subject. "Stock" sat on a page called Stock Overview and
    held how many tablets have the current instructions. "Finance" is a
    department, not a thing a café owner looks for.
    """
    page = STOCK.read_text(encoding="utf-8")
    # Only the six area cards. "Finance" is also a role in the who-does-what
    # fold, where a department name is exactly what is meant.
    sections = page[page.index("const sections = ["):page.index("this.page.body.find(\"[data-sections]\")")]
    for named in ("Automatic updates", "What the tablets know", "Cost and value"):
        assert f'title: __("{named}")' in sections, f"the {named!r} area is not named after its contents"
    for vague in ('title: __("Today")', 'title: __("Stock")', 'title: __("Finance")'):
        assert vague not in sections, f"{vague} names a time or a department, not what the card holds"


def test_an_area_button_says_where_it_goes() -> None:
    page = STOCK.read_text(encoding="utf-8")
    # "Open stock" on a stock page, and "Open finance view" on a card holding
    # stock value, both told a reader nothing they did not already see.
    assert '__("See the tablets")' in page
    assert '__("See cost and value")' in page
    assert '__("Open finance view")' not in page


def _template(page: str, root_class: str) -> str:
    """The page's own markup template, from its opening tag to its close."""
    start = page.index(f'<div class="{root_class}">')
    return page[start:page.index("</div>`);", start)]


def test_page_markup_closes_what_it_opens() -> None:
    """A page that builds its own HTML can nest wrongly and still run.

    Wrapping three sections in <details> left one </div> too many in each,
    and the browser's error recovery swallowed two whole sections into a
    third. Every test passed. Only opening the page showed it.
    """
    for path, root in (
        (MENU, "jiji-menu-page"),
        (STOCK, "jiji-stock-autopilot"),
    ):
        template = _template(path.read_text(encoding="utf-8"), root)
        opens = len(re.findall(r"<(?:div|details)\b", template))
        closes = len(re.findall(r"</(?:div|details)>", template))
        # The template stops one short of its own closing tag, by construction.
        assert opens - closes == 1, f"{path.name}: {opens} opened, {closes} closed"


def test_the_menu_leads_with_the_drink_not_the_setup() -> None:
    page = MENU.read_text(encoding="utf-8")
    template = _template(page, "jiji-menu-page")
    # Seven full cards competed on this page. The tools are folded, and the
    # block holding the drink is named for what a person came to do.
    assert '__("Look at one thing on your menu")' in page
    assert template.count("<details") == 4, "the tools and the section map must stay folded"
    # Setup progress is onboarding, not furniture.
    assert 'find("[data-completion-card]").toggleClass("d-none", complete)' in page
    # And the warning bar about who sees cost is a footnote now.
    assert 'alert alert-warning mt-2' not in page
