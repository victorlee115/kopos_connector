"""A recipe preview should answer the question a director actually has.

The preview said "Ingredient cost for the full recipe: RM 14.04 · for one
serving: RM 3.51", which answers "is the recipe valid". Nobody asks that. The
question is whether the drink still makes money, and that needs the price
beside the cost -- both of which were already computed, in two different
places, and never put together.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api" / "menu_authoring.py"
SCOPE = ROOT / "api" / "director_scope.py"
PAGE = ROOT / "kopos" / "page" / "jiji_menu_recipes" / "jiji_menu_recipes.js"


def test_the_price_comes_from_what_the_tablet_charges() -> None:
    api = API.read_text(encoding="utf-8")
    scope = SCOPE.read_text(encoding="utf-8")
    # Quoting a second price would let the desk and the counter disagree about
    # what a drink costs a customer.
    assert "get_item_prices_map" in api
    assert "director_price_list" in api and "def director_price_list" in scope
    assert '"POS Profile"' in scope and 'pluck="selling_price_list"' in scope


def test_a_cafe_without_a_counter_yet_still_sees_a_price() -> None:
    scope = SCOPE.read_text(encoding="utf-8")
    # The menu is built before the counter is set up, so a company with no POS
    # Profile falls back to the site default. Two profiles on two price lists
    # stays ambiguous rather than becoming a coin toss.
    assert 'get_single_value("Selling Settings", "selling_price_list")' in scope
    assert "if price_lists:\n        return \"\"" in scope


def test_the_margin_is_integer_sen_all_the_way() -> None:
    api = API.read_text(encoding="utf-8")
    # Subtracting two rounded strings is how a margin drifts from the numbers
    # it is drawn from.
    assert '"price_sen"' in api and '"keeps_sen"' in api
    assert "price_sen - cost_sen" in api


def test_a_missing_price_is_said_rather_than_guessed() -> None:
    api = API.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    assert "if not price_list:" in api
    assert "Set a selling price for this item to see what it keeps." in page
    assert "JiJi cannot cost this yet" in page


def test_selling_below_cost_is_stated_as_a_loss() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # A negative margin printed as "Keeps -RM 0.40" is a rounding detail. It is
    # the answer.
    assert "Loses {0} on every one sold" in page
    assert "keeps > 0" in page


def test_the_preview_leads_with_the_comparison() -> None:
    page = PAGE.read_text(encoding="utf-8")
    for part in ("Sells for", "Ingredients cost", "Keeps {0}"):
        assert part in page, f"the money line is missing {part!r}"
    # The old sentence answered a question nobody asks.
    assert "Ingredient cost for the full recipe" not in page


def test_a_broken_recipe_does_not_get_a_confident_margin() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # A real check with the yield fields cleared reported "Keeps RM 16.90" over
    # a recipe the same panel was refusing to save. Numbers over a broken
    # recipe are worse than no numbers.
    assert "moneyLineHtml(preview, response.status === \"ok\")" in page
    assert "Fix the details above before these figures mean anything." in page


def test_a_drink_that_costs_nothing_is_an_absence_not_a_margin() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # cost_status is "complete" when no component counts towards cost at all,
    # so a zero total would otherwise read as keeping the entire price.
    assert "if (!cost) {" in page
    assert "counts towards cost yet" in page


def test_the_drink_is_on_the_page_not_behind_a_dialog() -> None:
    page = PAGE.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")
    # Choosing a menu item used to change nothing visible until a dialog was
    # opened. What goes into it, and whether it still makes money, is now
    # answerable without opening anything.
    assert "renderDrink(" in page and "loadDrink()" in page
    assert "What goes into a {0}?" in page
    # Opening on a drink shows that drink's recipe, not an empty form.
    assert "active_recipe_name" in api


def test_which_recipe_is_active_has_one_owner() -> None:
    api = API.read_text(encoding="utf-8")
    capacity = (ROOT / "kopos" / "services" / "inventory_autopilot" / "availability_capacity.py").read_text("utf-8")
    # A second reading of "active" would let a director cost one recipe while
    # the counter pours another.
    assert "def active_recipe_name" in capacity
    assert '"status": "Active"' not in api


def test_a_director_never_reads_a_code() -> None:
    service = (ROOT / "kopos" / "services" / "inventory_autopilot" / "menu_authoring.py").read_text("utf-8")
    page = PAGE.read_text(encoding="utf-8")
    # The rows printed DEMO-CAFE-WHOLE-MILK. The item id stays for the link and
    # for support; the name is what is read.
    assert '"item_name":' in service
    assert "line.item_name" in page


def test_the_price_is_said_once_on_the_drink() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # It appeared in the card header and again in the money line below it.
    assert page.count('__("Sells for {0}"') == 0
    assert '__("Sells for")' in page


def test_what_stock_can_make_is_read_from_the_capacity_owner() -> None:
    api = API.read_text(encoding="utf-8")
    # The same number the tablet is refused past. A second calculation here
    # would let the desk promise drinks the counter will not pour.
    assert "target_capacity" in api
    assert "result.reliable" in api
    assert "frappe.log_error" in api
