"""One fictional Moonleaf catalogue shared by every test-site runner.

This module owns the business facts only: ingredients, purchasing packs,
prepared batches, drinks, customer choices, prices, and opening quantities.
It deliberately has no Frappe or acceptance-runner dependencies so disposable
demo sites and restored-data rehearsals cannot drift into different cafés.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


CATALOG_PREFIX = "INV-ACCEPT-"
CAFE_NAME = "Moonleaf Coffee & Matcha (Fictional)"
SELLING_PRICE_LIST = f"{CATALOG_PREFIX}MOONLEAF-MENU-MYR"
POS_PROFILE = f"{CATALOG_PREFIX}MOONLEAF-CAFE-PILOT"
SUPPLIER = f"{CATALOG_PREFIX}MOONLEAF-PROVISIONS"
SUPPLIER_NAME = "Moonleaf Provisions"

PREPARED_ITEM = f"{CATALOG_PREFIX}MOONLEAF-VANILLA-COLD-FOAM"
MATCHA_CONCENTRATE_ITEM = f"{CATALOG_PREFIX}MOONLEAF-MATCHA-CONCENTRATE"
MATCHA_LATTE_ITEM = f"{CATALOG_PREFIX}MOONLEAF-CLASSIC-MATCHA-LATTE"
PREPARED_BOM_QTY = Decimal("1000")
PREPARED_MIN_READY_QTY = Decimal("300")
PREPARED_LEAD_MINUTES = 20

# Stock measure, supplier pack, and exact number of stock units in one pack.
INGREDIENTS: tuple[dict[str, Any], ...] = (
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-ESPRESSO-BEANS",
        "name": "Moonleaf Nightjar Espresso Beans",
        "stock_uom": "Gram",
        "purchase_uom": "Bag",
        "conversion": Decimal("1000"),
        "purchase_rate": Decimal("68"),
        "valuation_rate": Decimal("0.068"),
        "shelf_life_days": 60,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-MATCHA-POWDER",
        "name": "Moonleaf Jade Mist Matcha Powder",
        "stock_uom": "Gram",
        "purchase_uom": "Tin",
        "conversion": Decimal("500"),
        "purchase_rate": Decimal("95"),
        "valuation_rate": Decimal("0.19"),
        "shelf_life_days": 180,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-FRESH-MILK",
        "name": "Moonleaf Meadow Fresh Milk",
        "stock_uom": "Millilitre",
        "purchase_uom": "Carton",
        "conversion": Decimal("1000"),
        "purchase_rate": Decimal("7.40"),
        "valuation_rate": Decimal("0.0074"),
        "shelf_life_days": 7,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-OAT-MILK",
        "name": "Moonleaf Cloud Oat Milk",
        "stock_uom": "Millilitre",
        "purchase_uom": "Carton",
        "conversion": Decimal("1000"),
        "purchase_rate": Decimal("11.80"),
        "valuation_rate": Decimal("0.0118"),
        "shelf_life_days": 90,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-STRAWBERRY-PUREE",
        "name": "Moonleaf Ruby Strawberry Puree",
        "stock_uom": "Millilitre",
        "purchase_uom": "Bottle",
        "conversion": Decimal("1000"),
        "purchase_rate": Decimal("28"),
        "valuation_rate": Decimal("0.028"),
        "shelf_life_days": 30,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-VANILLA-SYRUP",
        "name": "Moonleaf Vanilla Syrup",
        "stock_uom": "Millilitre",
        "purchase_uom": "Bottle",
        "conversion": Decimal("750"),
        "purchase_rate": Decimal("24"),
        "valuation_rate": Decimal("0.032"),
        "shelf_life_days": 365,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-BROWN-SUGAR-SYRUP",
        "name": "Moonleaf Ember Brown Sugar Syrup",
        "stock_uom": "Millilitre",
        "purchase_uom": "Bottle",
        "conversion": Decimal("750"),
        "purchase_rate": Decimal("21.75"),
        "valuation_rate": Decimal("0.029"),
        "shelf_life_days": 180,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-WHIPPING-CREAM",
        "name": "Moonleaf Whipping Cream",
        "stock_uom": "Millilitre",
        "purchase_uom": "Carton",
        "conversion": Decimal("1000"),
        "purchase_rate": Decimal("18"),
        "valuation_rate": Decimal("0.018"),
        "shelf_life_days": 14,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-FILTERED-WATER",
        "name": "Moonleaf Filtered Water",
        "stock_uom": "Millilitre",
        "purchase_uom": "Canister",
        "conversion": Decimal("5000"),
        "purchase_rate": Decimal("4"),
        "valuation_rate": Decimal("0.0008"),
        "shelf_life_days": 30,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-16OZ-CUP",
        "name": "Moonleaf 16 oz Clear Cup",
        "stock_uom": "Nos",
        "purchase_uom": "Sleeve",
        "conversion": Decimal("50"),
        "purchase_rate": Decimal("14"),
        "valuation_rate": Decimal("0.28"),
        "shelf_life_days": 0,
        "has_batch_no": 0,
        "role": "Packaging",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-16OZ-LID",
        "name": "Moonleaf 16 oz Flat Lid",
        "stock_uom": "Nos",
        "purchase_uom": "Sleeve",
        "conversion": Decimal("50"),
        "purchase_rate": Decimal("9"),
        "valuation_rate": Decimal("0.18"),
        "shelf_life_days": 0,
        "has_batch_no": 0,
        "role": "Packaging",
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-PAPER-STRAW",
        "name": "Moonleaf Paper Straw",
        "stock_uom": "Nos",
        "purchase_uom": "Box",
        "conversion": Decimal("200"),
        "purchase_rate": Decimal("12"),
        "valuation_rate": Decimal("0.06"),
        "shelf_life_days": 0,
        "has_batch_no": 0,
        "role": "Packaging",
    },
)

PREPARED_COMPONENTS: tuple[dict[str, Any], ...] = (
    {
        "code": PREPARED_ITEM,
        "name": "Moonleaf Vanilla Cold Foam",
        "batch_qty": PREPARED_BOM_QTY,
        "min_ready_qty": PREPARED_MIN_READY_QTY,
        "lead_minutes": PREPARED_LEAD_MINUTES,
        "shelf_life_days": 1,
        "instructions": (
            "Chill the clean pitcher. Add whipping cream, fresh milk, and vanilla syrup. "
            "Blend for 30 seconds until pourable, then label the batch and refrigerate immediately."
        ),
        "inputs": (
            (f"{CATALOG_PREFIX}MOONLEAF-WHIPPING-CREAM", "700"),
            (f"{CATALOG_PREFIX}MOONLEAF-FRESH-MILK", "250"),
            (f"{CATALOG_PREFIX}MOONLEAF-VANILLA-SYRUP", "50"),
        ),
    },
    {
        "code": MATCHA_CONCENTRATE_ITEM,
        "name": "Moonleaf Matcha Concentrate",
        "batch_qty": Decimal("1000"),
        "min_ready_qty": Decimal("300"),
        "lead_minutes": 10,
        "shelf_life_days": 1,
        "instructions": (
            "Sift matcha into the sanitized jug. Add filtered water gradually while whisking "
            "until smooth, then label the batch and refrigerate immediately."
        ),
        "inputs": (
            (f"{CATALOG_PREFIX}MOONLEAF-MATCHA-POWDER", "50"),
            (f"{CATALOG_PREFIX}MOONLEAF-FILTERED-WATER", "950"),
        ),
    },
)


def _component(
    item: str,
    qty: str,
    component_type: str = "Ingredient",
    *,
    loss: str = "0",
    substitution_key: str | None = None,
) -> dict[str, Any]:
    return {
        "item": item,
        "qty": qty,
        "component_type": component_type,
        "loss": loss,
        "substitution_key": substitution_key,
    }


PACKAGING_COMPONENTS: tuple[dict[str, Any], ...] = (
    _component(f"{CATALOG_PREFIX}MOONLEAF-16OZ-CUP", "1", "Packaging"),
    _component(f"{CATALOG_PREFIX}MOONLEAF-16OZ-LID", "1", "Packaging"),
    _component(f"{CATALOG_PREFIX}MOONLEAF-PAPER-STRAW", "1", "Packaging"),
)

SELLABLES: tuple[dict[str, Any], ...] = (
    {
        "code": MATCHA_LATTE_ITEM,
        "name": "Matcha Latte",
        "menu_group": "Matcha",
        "price": Decimal("12.90"),
        "components": (
            _component(f"{CATALOG_PREFIX}MOONLEAF-MATCHA-POWDER", "4", loss="2"),
            _component(
                f"{CATALOG_PREFIX}MOONLEAF-FRESH-MILK",
                "240",
                loss="1",
                substitution_key="milk",
            ),
        )
        + PACKAGING_COMPONENTS,
        "modifier_groups": ("milk", "matcha_extras"),
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-STRAWBERRY-MATCHA-LATTE",
        "name": "Strawberry Matcha Latte",
        "menu_group": "Matcha",
        "price": Decimal("15.90"),
        "components": (
            _component(f"{CATALOG_PREFIX}MOONLEAF-MATCHA-POWDER", "4", loss="2"),
            _component(
                f"{CATALOG_PREFIX}MOONLEAF-FRESH-MILK",
                "200",
                loss="1",
                substitution_key="milk",
            ),
            _component(f"{CATALOG_PREFIX}MOONLEAF-STRAWBERRY-PUREE", "40", loss="1"),
        )
        + PACKAGING_COMPONENTS,
        "modifier_groups": ("milk", "matcha_extras"),
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-ICED-AMERICANO",
        "name": "Iced Americano",
        "menu_group": "Coffee",
        "price": Decimal("9.90"),
        "components": (
            _component(f"{CATALOG_PREFIX}MOONLEAF-ESPRESSO-BEANS", "18", loss="2"),
            _component(f"{CATALOG_PREFIX}MOONLEAF-FILTERED-WATER", "220"),
        )
        + PACKAGING_COMPONENTS,
        "modifier_groups": ("coffee_extras",),
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-VANILLA-OAT-LATTE",
        "name": "Vanilla Oat Latte",
        "menu_group": "Coffee",
        "price": Decimal("14.90"),
        "components": (
            _component(f"{CATALOG_PREFIX}MOONLEAF-ESPRESSO-BEANS", "18", loss="2"),
            _component(f"{CATALOG_PREFIX}MOONLEAF-OAT-MILK", "220", loss="1"),
            _component(f"{CATALOG_PREFIX}MOONLEAF-VANILLA-SYRUP", "15"),
        )
        + PACKAGING_COMPONENTS,
        "modifier_groups": ("coffee_extras",),
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-BROWN-SUGAR-CLOUD-COFFEE",
        "name": "Brown Sugar Cloud Coffee",
        "menu_group": "Coffee",
        "price": Decimal("15.90"),
        "components": (
            _component(f"{CATALOG_PREFIX}MOONLEAF-ESPRESSO-BEANS", "18", loss="2"),
            _component(f"{CATALOG_PREFIX}MOONLEAF-OAT-MILK", "170", loss="1"),
            _component(f"{CATALOG_PREFIX}MOONLEAF-BROWN-SUGAR-SYRUP", "20"),
            _component(PREPARED_ITEM, "50", "Prep Item"),
        )
        + PACKAGING_COMPONENTS,
        "modifier_groups": ("coffee_extras",),
    },
    {
        "code": f"{CATALOG_PREFIX}MOONLEAF-MATCHA-CLOUD",
        "name": "Matcha Cloud",
        "menu_group": "Matcha",
        "price": Decimal("16.90"),
        "components": (
            _component(MATCHA_CONCENTRATE_ITEM, "80", "Prep Item"),
            _component(
                f"{CATALOG_PREFIX}MOONLEAF-FRESH-MILK",
                "180",
                loss="1",
                substitution_key="milk",
            ),
            _component(PREPARED_ITEM, "40", "Prep Item"),
        )
        + PACKAGING_COMPONENTS,
        "modifier_groups": ("milk", "matcha_extras"),
    },
)

RECIPE_CODE = f"{CATALOG_PREFIX}MOONLEAF-BROWN-SUGAR-CLOUD-COFFEE-RECIPE"
SELLABLE_ITEM = f"{CATALOG_PREFIX}MOONLEAF-BROWN-SUGAR-CLOUD-COFFEE"
RECIPE_COMPONENTS: tuple[tuple[str, str], ...] = tuple(
    (row["item"], row["qty"])
    for row in next(spec for spec in SELLABLES if spec["code"] == SELLABLE_ITEM)["components"]
)

# These are customer-facing choices. Their stock effect is kept here so the
# recipe compiler, availability checks, waste, and accounting use one answer.
MODIFIER_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "key": "milk",
        "code": f"{CATALOG_PREFIX}MOONLEAF-MILK-CHOICE",
        "name": "Milk choice",
        "selection_type": "Single",
        "required": 1,
        "min": 1,
        "max": 1,
        "options": (
            {
                "code": f"{CATALOG_PREFIX}MOONLEAF-DAIRY-MILK",
                "name": "Fresh milk",
                "kind": "Instruction Only",
                "default": 1,
                "price": Decimal("0"),
                "instruction": "Use the measured fresh-milk recipe.",
            },
            {
                "code": f"{CATALOG_PREFIX}MOONLEAF-OAT-MILK-SWAP",
                "name": "Oat milk",
                "kind": "Replace",
                "default": 0,
                "price": Decimal("2"),
                "target_substitution_key": "milk",
                "target_item": f"{CATALOG_PREFIX}MOONLEAF-FRESH-MILK",
                "new_item": f"{CATALOG_PREFIX}MOONLEAF-OAT-MILK",
            },
        ),
    },
    {
        "key": "matcha_extras",
        "code": f"{CATALOG_PREFIX}MOONLEAF-MATCHA-EXTRAS",
        "name": "Matcha extras",
        "selection_type": "Multiple",
        "required": 0,
        "min": 0,
        "max": 2,
        "options": (
            {
                "code": f"{CATALOG_PREFIX}MOONLEAF-EXTRA-MATCHA",
                "name": "Extra matcha",
                "kind": "Add",
                "default": 0,
                "price": Decimal("2.50"),
                "new_item": f"{CATALOG_PREFIX}MOONLEAF-MATCHA-POWDER",
                "qty_delta": Decimal("2"),
                "qty_uom": "Gram",
            },
            {
                "code": f"{CATALOG_PREFIX}MOONLEAF-EXTRA-COLD-FOAM",
                "name": "Extra vanilla cold foam",
                "kind": "Add",
                "default": 0,
                "price": Decimal("3"),
                "new_item": PREPARED_ITEM,
                "qty_delta": Decimal("30"),
                "qty_uom": "Millilitre",
            },
        ),
    },
    {
        "key": "coffee_extras",
        "code": f"{CATALOG_PREFIX}MOONLEAF-COFFEE-EXTRAS",
        "name": "Coffee extras",
        "selection_type": "Multiple",
        "required": 0,
        "min": 0,
        "max": 2,
        "options": (
            {
                "code": f"{CATALOG_PREFIX}MOONLEAF-EXTRA-ESPRESSO",
                "name": "Extra espresso shot",
                "kind": "Add",
                "default": 0,
                "price": Decimal("3"),
                "new_item": f"{CATALOG_PREFIX}MOONLEAF-ESPRESSO-BEANS",
                "qty_delta": Decimal("18"),
                "qty_uom": "Gram",
            },
            {
                "code": f"{CATALOG_PREFIX}MOONLEAF-COFFEE-COLD-FOAM",
                "name": "Vanilla cold foam",
                "kind": "Add",
                "default": 0,
                "price": Decimal("3"),
                "new_item": PREPARED_ITEM,
                "qty_delta": Decimal("30"),
                "qty_uom": "Millilitre",
            },
        ),
    },
)

PROMOTIONS: tuple[dict[str, Any], ...] = (
    {
        "name": f"{CATALOG_PREFIX}MOONLEAF-MATCHA-MONDAY",
        "label": "Matcha Monday 10%",
        "type": "item_discount",
        "discount": Decimal("10"),
        "items": tuple(spec["code"] for spec in SELLABLES if "MATCHA" in spec["code"]),
    },
    {
        "name": f"{CATALOG_PREFIX}MOONLEAF-CAFE-PAIR",
        "label": "Coffee + Matcha Pair 15%",
        "type": "order_discount",
        "discount": Decimal("15"),
        "items": tuple(spec["code"] for spec in SELLABLES),
    },
)

OPENING_REMARKS = f"{CATALOG_PREFIX}MOONLEAF fictional opening stock"
OPENING_PACKS: dict[str, Decimal] = {
    f"{CATALOG_PREFIX}MOONLEAF-ESPRESSO-BEANS": Decimal("8"),
    f"{CATALOG_PREFIX}MOONLEAF-MATCHA-POWDER": Decimal("6"),
    f"{CATALOG_PREFIX}MOONLEAF-FRESH-MILK": Decimal("24"),
    f"{CATALOG_PREFIX}MOONLEAF-OAT-MILK": Decimal("12"),
    f"{CATALOG_PREFIX}MOONLEAF-STRAWBERRY-PUREE": Decimal("6"),
    f"{CATALOG_PREFIX}MOONLEAF-VANILLA-SYRUP": Decimal("4"),
    f"{CATALOG_PREFIX}MOONLEAF-BROWN-SUGAR-SYRUP": Decimal("4"),
    f"{CATALOG_PREFIX}MOONLEAF-WHIPPING-CREAM": Decimal("8"),
    f"{CATALOG_PREFIX}MOONLEAF-FILTERED-WATER": Decimal("10"),
    f"{CATALOG_PREFIX}MOONLEAF-16OZ-CUP": Decimal("10"),
    f"{CATALOG_PREFIX}MOONLEAF-16OZ-LID": Decimal("10"),
    f"{CATALOG_PREFIX}MOONLEAF-PAPER-STRAW": Decimal("3"),
}
