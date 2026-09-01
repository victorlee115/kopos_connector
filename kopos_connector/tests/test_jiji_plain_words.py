"""The desk is read by someone who mixes matcha and sells it.

Not an accountant, not an ERP administrator, not the person who wrote the
schema. Someone who knows their café. Every word on these pages has to be a
word that person already uses, and the way jargon returns is one string at a
time, each one defensible on its own.

This is a vocabulary guard, not a style opinion. Each banned word is here
because it appeared on screen and could not be explained to that person
without teaching them the system first.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "public" / "js" / "jiji_desk.js"
PAGES = (
    "jiji_home",
    "jiji_money",
    "jiji_supplies",
    "jiji_staff",
    "jiji_menu_recipes",
    "jiji_stock_autopilot",
)
APIS = (
    "director_home",
    "shift_money",
    "supplier_orders",
    "staff_desk",
    "stock_desk",
    "menu_authoring",
    "inventory",
    "cafe_units",
)

# Word -> what to say instead. Every JiJi page is covered. Words are banned
# only after they have been seen on screen and found to need the system
# explained first; a word that is fine in a query or a route is not on screen,
# and the scanner below only reads what a person is shown.
BANNED = {
    "reconciliation": "say what it is: every shift, one by one",
    "reconcile": "say whether the money matched",
    "purchase order": "supplier order, or just the order",
    "projection": "say what actually happened to the stock",
    "permlevel": "never appears on screen",
    "doctype": "never appears on screen",
    "idempotenc": "never appears on screen",
    "docstatus": "never appears on screen",
    "restock plan": "suggested order",
    "stock correction": "the corrected amount",
    "stock work": "say which work: the automatic updates",
    "needs review": "say who needs to do what",
    "need review": "say who needs to do what",
    "guided editor": "the recipe form",
    "commissioning": "setting up",
    "cutover": "starting stock tracking",
    "schema": "never appears on screen",
    "payload": "never appears on screen",
    "endpoint": "never appears on screen",
    "erpnext": "say JiJi or name the café task",
    "uom": "show the familiar unit word instead",
    "bom": "preparation recipe",
    "sku": "product",
    "item code": "product or ingredient name",
    "modifier": "customer choice",
    "api": "never appears on screen",
    "json": "never appears on screen",
    "hash": "never appears on screen",
    "canonical": "never appears on screen",
    "queue": "say waiting work or saved work",
    "worker": "say JiJi",
    "redis": "never appears on screen",
    "database": "never appears on screen",
    "java": "never appears on screen",
    "technical": "say what Support should check",
    "advanced": "keep support tools outside business pages",
    "warehouse": "outlet",
    "company": "café",
}

# The response fields the desk pages actually render. Scanning every literal
# instead would flag "Purchase Order Item" in a frappe.get_all call, which is
# addressed to the framework and never reaches a screen.
COPY_FIELDS = (
    "title", "detail", "label", "state", "reason", "arrival", "action",
    "moved_note", "moved_empty_note", "running_low_note", "days", "when",
    "reliability", "why", "person",
    "headline", "next_action", "message", "failure_reason", "state_label",
    "document_label", "open_label",
)


def _contains_banned_word(sentence: str, word: str) -> bool:
    if word == "idempotenc":
        return word in sentence.lower()
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", sentence, re.IGNORECASE))


def _user_strings(text: str) -> list[str]:
    """Every string the desk hands to a reader, and only those.

    Translated strings on the JavaScript side, and on the Python side the
    values of the response fields the pages render. A doctype name inside a
    query is addressed to Frappe, not to a person.
    """
    strings = re.findall(r'__\("([^"]{2,200})"', text)
    strings += re.findall(r'frappe\._\("([^"]{2,200})"', text)
    for field in COPY_FIELDS:
        strings += re.findall(rf'"{field}":\s*"([^"]{{2,200}})"', text)
        strings += re.findall(rf'"{field}":\s*f"([^"]{{2,200}})"', text)
    return strings


def _sources() -> dict[str, str]:
    sources = {"jiji_desk.js": SHARED.read_text(encoding="utf-8")}
    for name in PAGES:
        path = ROOT / "kopos" / "page" / name / f"{name}.js"
        if path.exists():
            sources[f"{name}.js"] = path.read_text(encoding="utf-8")
    for name in APIS:
        path = ROOT / "api" / f"{name}.py"
        if path.exists():
            sources[f"{name}.py"] = path.read_text(encoding="utf-8")
    return sources


def test_no_word_on_screen_needs_the_system_explained_first() -> None:
    problems = []
    for name, text in _sources().items():
        for sentence in _user_strings(text):
            for word, instead in BANNED.items():
                if _contains_banned_word(sentence, word):
                    problems.append(f"{name}: {sentence!r} uses {word!r} — {instead}")
    assert not problems, "words a café owner would have to be taught:\n" + "\n".join(problems)


def test_money_is_described_by_what_happened_to_it() -> None:
    money = (ROOT / "api" / "shift_money.py").read_text(encoding="utf-8")
    # "Cash matched / Need review / Never counted" made a reader work out that
    # "need review" meant the money was wrong.
    assert '"label": "Did not match"' in money
    assert '"label": "Not counted"' in money


def test_takings_are_sold_not_taken() -> None:
    home = (ROOT / "kopos" / "page" / "jiji_home" / "jiji_home.js").read_text(encoding="utf-8")
    # "Nothing was taken in the last seven days" can be read as a theft report.
    assert "taken" not in home
    assert '__("{0} collected", [week.total || ""])' in home


def test_a_place_is_called_what_the_owner_named_it() -> None:
    stock = (ROOT / "kopos" / "page" / "jiji_stock_autopilot" / "jiji_stock_autopilot.js").read_text("utf-8")
    # The screen said "Matcha & Bean Café - MBC". The café is called Matcha &
    # Bean Café; the suffix is a warehouse id the owner never chose.
    assert "placeName(" in stock


def test_count_reviews_show_cafe_amounts_not_erp_units_or_percentages() -> None:
    stock = (ROOT / "kopos" / "page" / "jiji_stock_autopilot" / "jiji_stock_autopilot.js").read_text("utf-8")
    review = stock.split("renderCountReviews(summary) {", 1)[1].split("\n\treviewCount(", 1)[0]
    assert "line.pack_breakdown_display" in review
    assert "line.counted_display" in review
    assert "line.difference_display" in review
    assert "line.purchase_uom" not in review
    assert "line.variance_percent" not in review


def test_a_control_and_its_note_do_not_contradict_each_other() -> None:
    supplies = (ROOT / "kopos" / "page" / "jiji_supplies" / "jiji_supplies.js").read_text("utf-8")
    assert "Approve order" in supplies
    assert "Approval marks it as ordered. Send it to the supplier outside JiJi." in supplies
    assert "will not send anything to the supplier" in supplies


def test_no_page_claims_a_result_it_drew_from_an_absence() -> None:
    """Three times now, a screen has reported a positive result from no data.

    "Running low: 0 · nothing short" on an outlet with no runout estimate.
    "The cash is accounted for" on a café where no shift had ever closed.
    Both read as reassurance and neither was earned. The fix is the same each
    time: an empty list and an unanswerable question are different facts and
    must not produce the same sentence.
    """
    money = (ROOT / "api" / "shift_money.py").read_text(encoding="utf-8")
    stock = (ROOT / "api" / "stock_desk.py").read_text(encoding="utf-8")

    # Money distinguishes "every shift matched" from "no shift has closed".
    assert '"No shift has been counted yet"' in money
    assert 'if not grouped["matched"]:' in money
    accounted = money.index('"The cash is accounted for"')
    guard = money.index('if not grouped["matched"]:')
    assert guard < accounted, "the reassurance must sit behind the check for any data at all"

    # Stock distinguishes "nothing is short" from "JiJi cannot say yet".
    assert '"JiJi cannot say yet"' in stock
    assert "if low or not no_estimate" in stock

    # And Home's Money tile, which read "0 · to check" beside a Money page
    # already saying no shift had ever been counted.
    home = (ROOT / "api" / "director_home.py").read_text(encoding="utf-8")
    assert '"no shift counted yet"' in home
    assert "any_shift = bool(" in home
    assert '"label": "Cash checks"' in home
