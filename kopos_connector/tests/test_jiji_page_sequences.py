"""Beginner pages expose one obvious job and never an empty required filter."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MENU = ROOT / "kopos" / "page" / "jiji_menu_recipes" / "jiji_menu_recipes.js"
STOCK = ROOT / "kopos" / "page" / "jiji_stock_autopilot" / "jiji_stock_autopilot.js"
SCOPE = ROOT / "api" / "director_scope.py"


def test_the_menu_is_a_product_workspace_not_a_setup_course() -> None:
    page = MENU.read_text(encoding="utf-8")
    assert "<ol " not in page and "<ol>" not in page
    assert 'title: __("Menu")' in page
    assert 'data-add-product>${__("Add product")}' in page
    assert '${__("Edit ingredients and choices")}' in page
    assert '${__("Check the counter")}' in page
    assert "setup steps" not in page.lower()


def test_the_menu_does_not_mix_in_unrelated_admin_tools() -> None:
    page = MENU.read_text(encoding="utf-8")
    for obsolete in (
        "checkPromotionEconomics",
        "renderPromotionEconomics",
        "downloadTemplate",
        "validateCsv",
        "renderCommissioningStatus",
    ):
        assert obsolete not in page


def test_a_disabled_menu_action_says_what_it_is_waiting_for() -> None:
    page = MENU.read_text(encoding="utf-8")
    assert 'if (!company) waiting = __("Choose your café first.")' in page
    assert 'else if (!item) waiting = __("Choose a product, or add a new one.")' in page


def test_menu_choices_are_friendly_scoped_selects() -> None:
    page = MENU.read_text(encoding="utf-8")
    assert "kopos_connector.api.menu_authoring.get_menu_page_choices" in page
    assert 'data-company-select' in page
    assert 'data-menu-item-select' in page
    assert 'data-catalog-profile-select' in page
    assert 'options: "Item"' not in page
    assert 'options: "POS Profile"' not in page
    assert 'options: "Item Group"' not in page
    assert 'data-new-menu-group' in page
    assert "Use the name staff say at the counter, for example Matcha Latte." in page


def test_the_landscape_recipe_editor_uses_the_available_width() -> None:
    page = MENU.read_text(encoding="utf-8")
    assert 'size: "extra-large"' in page


def test_one_incomplete_product_is_written_in_the_singular() -> None:
    page = MENU.read_text(encoding="utf-8")
    assert 'missing.length === 1' in page
    assert '__("1 product needs ingredients")' in page


def test_stock_opens_on_the_outlet_the_director_has() -> None:
    page = STOCK.read_text(encoding="utf-8")
    scope = SCOPE.read_text(encoding="utf-8")
    assert "kopos_connector.api.director_scope.get_director_outlet" in page
    assert "def get_director_outlet" in scope
    # The server returns scoped friendly names, and a refresh keeps a choice
    # that still belongs to the director.
    assert 'Array.isArray(data.outlets)' in page
    assert 'outlets.some((row) => row.value === current)' in page
    assert '"outlets": [' in scope
    assert "error: () => this.updateWarehouseHelp()" in page
    # A defaulted outlet takes the same path as one chosen by the person.
    assert "Promise.resolve(this.warehouseControl.set_value(warehouse))" in page
    assert 'this.warehouseControl.$input.trigger("change")' in page
    assert 'options: "Warehouse"' not in page


def test_stock_no_longer_opens_with_an_instruction_to_fill_a_filter() -> None:
    page = STOCK.read_text(encoding="utf-8")
    assert "Choose an outlet. JiJi will show what needs your attention." not in page


def test_the_outlet_store_is_the_one_the_tablets_sell_from() -> None:
    scope = SCOPE.read_text(encoding="utf-8")
    # A POS Profile already names it. Counting warehouses cannot work: ERPNext
    # creates four for every company, so a single-outlet café never looks like
    # one -- verified on a real site, where the count rule never fired.
    assert '"POS Profile"' in scope
    assert 'fields=["name", "warehouse", "selling_price_list"]' in scope
    assert 'warehouses = _unique(row.get("warehouse") for row in profiles)' in scope


def test_an_ambiguous_outlet_is_still_asked_for() -> None:
    scope = SCOPE.read_text(encoding="utf-8")
    # Two outlets selling from different stores is a real question. Choosing
    # one would show a director the wrong café without saying so.
    assert 'return allowed[0] if len(allowed) == 1 else ""' in scope


def test_stock_says_the_urgent_thing_once() -> None:
    page = STOCK.read_text(encoding="utf-8")
    # The first area card repeated the next step word for word, directly under
    # it: same value, same detail, same button, same route. Six areas remain,
    # and the one the next step came from points at it instead of raising a
    # second alarm.
    assert "Needs your attention" not in page
    assert "This is your next step" in page
    assert "section.route === needs.route" in page


def test_stock_keeps_only_the_three_director_decisions() -> None:
    page = STOCK.read_text(encoding="utf-8")
    areas = [key for key in ("today", "stock", "counts", "plans", "finance", "settings")
             if f'key: "{key}"' in page]
    assert areas == ["counts", "plans", "finance"]


def test_support_machinery_is_not_a_business_area() -> None:
    """An area card's title has to survive being read on its own.

    "Today" held the automatic job's status, on a page where every other card
    is named by its subject. "Stock" sat on a page called Stock Overview and
    held how many tablets have the current instructions. "Finance" is a
    department, not a thing a café owner looks for.
    """
    page = STOCK.read_text(encoding="utf-8")
    sections = page[page.index("const sections = ["):page.index("this.page.body.find(\"[data-sections]\")")]
    for named in ("Stock counts", "Ordering", "Cost and value"):
        assert f'title: __("{named}")' in sections, f"the {named!r} area is not named after its contents"
    for support_only in ('title: __("Automatic updates")', 'title: __("What the tablets know")', 'title: __("Settings")'):
        assert support_only not in sections, f"{support_only} belongs in the support workspace"


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
    assert '__("Products")' in template
    assert 'data-drink' in template
    assert template.count('<div class="card mb-4">') == 2
    assert "renderDrink(data)" in page
    assert '__("What goes into a {0}?"' in page
    assert '__("Available to staff")' in page
    assert '__("Not published yet")' in page
    assert "highlightSequenceAction" not in page


def test_a_stock_problem_says_what_it_is() -> None:
    """Four rows read "JiJi found a stock problem that needs you." and nothing else.

    Three of them shared one cause, and none of the four named it. The reason
    codes the demo outlet raises -- cutover_identity_missing,
    inventory_count_before_cutover, inventory_plan_gate_failed -- matched no
    branch, so every one fell through to a sentence that says nothing.
    """
    page = STOCK.read_text(encoding="utf-8")
    assert 'reason.includes("cutover_identity")' in page
    assert 'reason.includes("plan_gate")' in page
    assert 'reason === "operating_day_evidence_missing"' in page
    assert 'reason === "inventory_count_schedule_unconfigured"' in page
    # The support record keeps its code; the beginner page never asks the
    # director to decode it.
    assert "has no plain description for: {0}" not in page
    assert "Open it to see what needs fixing" in page


def test_stock_setup_action_and_status_sentences_match_real_state() -> None:
    page = STOCK.read_text(encoding="utf-8")
    assert 'value: __("Start stock tracking")' in page
    assert 'route: "start"' in page
    assert 'if (key === "start")' in page
    assert "add_inner_button" not in page
    assert 'title: __("Start stock tracking?")' in page
    assert 'primary_action_label: __("Start stock tracking")' in page
    assert 'secondary_action_label: __("Not now")' in page
    assert "frappe.confirm(" not in page
    assert 'reason.includes("before_cutover")' in page
    assert 'JiJi last updated your stock {0} ago' not in page
    assert "needs.consequence" in page
    assert "JiJi has stopped updating your stock until this is fixed" not in page


def test_one_cause_is_one_alarm() -> None:
    page = STOCK.read_text(encoding="utf-8")
    # The same reason code raised four times was four identical rows.
    assert "const grouped = new Map();" in page
    assert '__("{0} times", [count])' in page
    # And grouped by the sentence, not the code: two codes mapped to one
    # sentence were still two identical rows.
    assert "const key = this.plainExceptionMessage(row.reason_code);" in page
