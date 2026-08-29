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

from frappe.utils import cstr, flt


# ERP unit names that are not words a café owner would say out loud. Anything
# not listed keeps the name the ERP already uses.
_UNIT_WORDS = {
    "gram": ("g", "g"),
    "millilitre": ("ml", "ml"),
    "milliliter": ("ml", "ml"),
    "litre": ("L", "L"),
    "liter": ("L", "L"),
    "kg": ("kg", "kg"),
    "kilogram": ("kg", "kg"),
    "nos": ("piece", "pieces"),
    "unit": ("unit", "units"),
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
# Only ever exact: 1500 ml stays 1500 ml rather than becoming a rounded 2 L.
_SCALED_UNITS = {"millilitre": "litre", "milliliter": "litre", "gram": "kg"}


def amount_phrase(qty: float, unit: str) -> str:
    """"12 bags", "1 carton", "1 L", "250 g" -- an amount someone can picture."""
    name = cstr(unit).strip().lower()
    bigger = _SCALED_UNITS.get(name)
    if bigger and flt(qty) >= 1000 and flt(qty) % 1000 == 0:
        qty, unit = flt(qty) / 1000, bigger
    singular, plural = unit_words(unit)
    rounded = round(flt(qty), 3)
    whole = int(rounded)
    number = str(whole) if rounded == whole else f"{rounded:g}"
    word = singular if rounded == 1 else plural
    return f"{number} {word}".strip()


