# pyright: reportMissingImports=false

"""ERP units, said the way a café says them.

A Purchase Order stores 12 Bag with a conversion factor of 250 into Gram. A
stock entry stores 1000 Millilitre. Both are correct and neither is what a
person says out loud, and every desk page that shows an amount needs the same
translation, so it lives here once.

This owns wording only. The quantity and its unit of measure stay owned by the
ERP documents; nothing here changes what is ordered, counted, or consumed. The
tablet does the same job for the counter in ``unit-display.ts`` -- two
presentations of one stored quantity, which cannot be one file across a
language boundary.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from frappe.utils import cstr


# ERP unit names that are not words a café owner would say out loud. Anything
# not listed keeps the name the ERP already uses.
_UNIT_WORDS = {
    "bag": ("bag", "bags"),
    "bottle": ("bottle", "bottles"),
    "box": ("box", "boxes"),
    "canister": ("canister", "canisters"),
    "carton": ("carton", "cartons"),
    "dozen": ("dozen", "dozens"),
    "ea": ("item", "items"),
    "each": ("item", "items"),
    "gram": ("g", "g"),
    "millilitre": ("ml", "ml"),
    "milliliter": ("ml", "ml"),
    "litre": ("L", "L"),
    "liter": ("L", "L"),
    "kg": ("kg", "kg"),
    "kilogram": ("kg", "kg"),
    "no": ("item", "items"),
    "number": ("item", "items"),
    "nos": ("item", "items"),
    "packet": ("packet", "packets"),
    "pc": ("item", "items"),
    "pcs": ("item", "items"),
    "piece": ("piece", "pieces"),
    "pieces": ("piece", "pieces"),
    "sack": ("sack", "sacks"),
    "sleeve": ("sleeve", "sleeves"),
    "tin": ("tin", "tins"),
    "tub": ("tub", "tubs"),
    "unit": ("item", "items"),
}


def unit_words(unit: str) -> tuple[str, str]:
    """Singular and plural words for one ERP unit of measure."""
    name = cstr(unit).strip()
    if not name:
        return "", ""
    known = _UNIT_WORDS.get(name.lower())
    if known:
        return known
    singular = name.lower()
    if singular.endswith("s"):
        return singular, singular
    if singular.endswith(("x", "ch", "sh")):
        return singular, f"{singular}es"
    return singular, f"{singular}s"


# A thousand of the small unit is the big one, and people say the big one.
# Only ever exact: 1,500 ml stays 1,500 ml rather than becoming a rounded 2 L.
_SCALED_UNITS = {"millilitre": "litre", "milliliter": "litre", "gram": "kg"}


def _quantity(value: Any) -> Decimal:
    try:
        quantity = Decimal(cstr(value).strip())
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("quantity must be a finite decimal") from error
    if not quantity.is_finite():
        raise ValueError("quantity must be a finite decimal")
    return quantity


def _number_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, ",f") if normalized else "0"


def amount_phrase(qty: Any, unit: str) -> str:
    """"12 bags", "1 carton", "1 L", "250 g" -- an amount someone can picture."""
    quantity = _quantity(qty)
    name = cstr(unit).strip().lower()
    bigger = _SCALED_UNITS.get(name)
    scale = Decimal("1000")
    if bigger and quantity >= scale and quantity % scale == 0:
        quantity, unit = quantity / scale, bigger
    singular, plural = unit_words(unit)
    number = _number_text(quantity)
    word = singular if quantity == 1 else plural
    return f"{number} {word}".strip()


def _item_noun(name: str, *, plural: bool) -> str:
    """Use the named thing when ERP only tells us it is one ``Nos``."""
    phrase = cstr(name).strip().lower()
    if not phrase or not plural:
        return phrase
    words = phrase.split()
    last = words[-1]
    if last.endswith(("s", "x", "ch", "sh")):
        words[-1] = f"{last}es"
    elif len(last) > 1 and last.endswith("y") and last[-2] not in "aeiou":
        words[-1] = f"{last[:-1]}ies"
    else:
        words[-1] = f"{last}s"
    return " ".join(words)


def amount_of_phrase(qty: Any, unit: str, item_name: str) -> str:
    """Say ``3,060 ml of milk``, ``2 bags of beans``, or ``17 cups``."""
    quantity = _quantity(qty)
    singular, plural = unit_words(unit)
    name = cstr(item_name).strip()
    if singular == "item" and plural == "items" and name:
        return f"{_number_text(quantity)} {_item_noun(name, plural=quantity != 1)}"
    amount = amount_phrase(quantity, unit)
    return f"{amount} of {name.lower()}" if name else amount


def difference_phrase(qty: Any, unit: str) -> str:
    """Say an exact stock difference as the action-relevant amount and direction."""
    difference = _quantity(qty)
    if difference == 0:
        return "Matches the saved amount"
    amount = amount_phrase(abs(difference), unit)
    direction = "less than expected" if difference < 0 else "more than expected"
    return f"{amount} {direction}"
