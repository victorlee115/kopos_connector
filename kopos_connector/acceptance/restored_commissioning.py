"""Commission the fictional Moonleaf coffee-and-matcha cafe on restored data.

The restored backup carries no ingredient master, BOMs, suppliers, or measured
recipes, so a one-drink placeholder cannot prove that the real commissioning
experience is usable.  This producer creates a deliberately fictional,
clearly-prefixed cafe inside the isolated rehearsal site only.  It covers
coffee and matcha ingredients, exact supplier packs, two prepared components,
six drinks, operational modifiers, selling prices, quotations, and promotions.

It commissions nothing in production and converts no existing Item.  Real
commissioning stays director-led and physical.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
from typing import Any
from zoneinfo import ZoneInfo

from importlib import metadata

from kopos_connector.acceptance.restored_inventory_acceptance import (
    AUTHORITY_PREFIX,
    _required_sha256,
    _discover_company,
    _discover_item_group,
    _discover_warehouse,
    _discover_opening_difference_account,
    _ensure_item,
    _fail,
    _meta_has,
    _proof_decimal_text,
    _require_frappe,
    _required_text,
    _set_if_present,
    _text,
    _value,
)

PRODUCER = "kopos_connector.acceptance.restored_commissioning.run_v1"
CONTRACT_ID = "kopos.restored-commissioning.v2"
CAFE_NAME = "Moonleaf Coffee & Matcha (Fictional)"
SELLING_PRICE_LIST = f"{AUTHORITY_PREFIX}MOONLEAF-MENU-MYR"
POS_PROFILE = f"{AUTHORITY_PREFIX}MOONLEAF-CAFE-PILOT"

SUPPLIER = f"{AUTHORITY_PREFIX}MOONLEAF-PROVISIONS"
PREPARED_ITEM = f"{AUTHORITY_PREFIX}MOONLEAF-VANILLA-COLD-FOAM"
MATCHA_CONCENTRATE_ITEM = f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-CONCENTRATE"
PREPARED_BOM_QTY = Decimal("1000")
PREPARED_MIN_READY_QTY = Decimal("300")
PREPARED_LEAD_MINUTES = 20

# Stock UOM, purchase UOM, and the exact whole-unit conversion between them.
# Every conversion is exact by construction: no ingredient may rely on an
# inferred or rounded factor.
INGREDIENTS: tuple[dict[str, Any], ...] = (
    {
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-ESPRESSO-BEANS",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-POWDER",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-FRESH-MILK",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-OAT-MILK",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-STRAWBERRY-PUREE",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-VANILLA-SYRUP",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-BROWN-SUGAR-SYRUP",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-WHIPPING-CREAM",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-FILTERED-WATER",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-16OZ-CUP",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-16OZ-LID",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-PAPER-STRAW",
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
            (f"{AUTHORITY_PREFIX}MOONLEAF-WHIPPING-CREAM", "700"),
            (f"{AUTHORITY_PREFIX}MOONLEAF-FRESH-MILK", "250"),
            (f"{AUTHORITY_PREFIX}MOONLEAF-VANILLA-SYRUP", "50"),
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
            (f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-POWDER", "50"),
            (f"{AUTHORITY_PREFIX}MOONLEAF-FILTERED-WATER", "950"),
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
    _component(f"{AUTHORITY_PREFIX}MOONLEAF-16OZ-CUP", "1", "Packaging"),
    _component(f"{AUTHORITY_PREFIX}MOONLEAF-16OZ-LID", "1", "Packaging"),
    _component(f"{AUTHORITY_PREFIX}MOONLEAF-PAPER-STRAW", "1", "Packaging"),
)

SELLABLES: tuple[dict[str, Any], ...] = (
    {
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-CLASSIC-MATCHA-LATTE",
        "name": "Moonleaf Classic Matcha Latte",
        "price": Decimal("12.90"),
        "components": (
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-POWDER", "4", loss="2"),
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-FRESH-MILK", "240", loss="1", substitution_key="milk"),
        ) + PACKAGING_COMPONENTS,
        "modifier_groups": ("milk", "matcha_extras"),
    },
    {
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-STRAWBERRY-MATCHA-LATTE",
        "name": "Moonleaf Strawberry Matcha Latte",
        "price": Decimal("15.90"),
        "components": (
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-POWDER", "4", loss="2"),
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-FRESH-MILK", "200", loss="1", substitution_key="milk"),
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-STRAWBERRY-PUREE", "40", loss="1"),
        ) + PACKAGING_COMPONENTS,
        "modifier_groups": ("milk", "matcha_extras"),
    },
    {
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-ICED-AMERICANO",
        "name": "Moonleaf Iced Americano",
        "price": Decimal("9.90"),
        "components": (
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-ESPRESSO-BEANS", "18", loss="2"),
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-FILTERED-WATER", "220"),
        ) + PACKAGING_COMPONENTS,
        "modifier_groups": ("coffee_extras",),
    },
    {
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-VANILLA-OAT-LATTE",
        "name": "Moonleaf Vanilla Oat Latte",
        "price": Decimal("14.90"),
        "components": (
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-ESPRESSO-BEANS", "18", loss="2"),
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-OAT-MILK", "220", loss="1"),
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-VANILLA-SYRUP", "15"),
        ) + PACKAGING_COMPONENTS,
        "modifier_groups": ("coffee_extras",),
    },
    {
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-BROWN-SUGAR-CLOUD-COFFEE",
        "name": "Moonleaf Brown Sugar Cloud Coffee",
        "price": Decimal("15.90"),
        "components": (
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-ESPRESSO-BEANS", "18", loss="2"),
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-OAT-MILK", "170", loss="1"),
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-BROWN-SUGAR-SYRUP", "20"),
            _component(PREPARED_ITEM, "50", "Prep Item"),
        ) + PACKAGING_COMPONENTS,
        "modifier_groups": ("coffee_extras",),
    },
    {
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-CLOUD",
        "name": "Moonleaf Matcha Cloud",
        "price": Decimal("16.90"),
        "components": (
            _component(MATCHA_CONCENTRATE_ITEM, "80", "Prep Item"),
            _component(f"{AUTHORITY_PREFIX}MOONLEAF-FRESH-MILK", "180", loss="1", substitution_key="milk"),
            _component(PREPARED_ITEM, "40", "Prep Item"),
        ) + PACKAGING_COMPONENTS,
        "modifier_groups": ("milk", "matcha_extras"),
    },
)

RECIPE_CODE = f"{AUTHORITY_PREFIX}MOONLEAF-BROWN-SUGAR-CLOUD-COFFEE-RECIPE"
SELLABLE_ITEM = f"{AUTHORITY_PREFIX}MOONLEAF-BROWN-SUGAR-CLOUD-COFFEE"
RECIPE_COMPONENTS: tuple[tuple[str, str], ...] = tuple(
    (row["item"], row["qty"])
    for row in next(spec for spec in SELLABLES if spec["code"] == SELLABLE_ITEM)["components"]
)

MODIFIER_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "key": "milk",
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-MILK-CHOICE",
        "name": "Milk choice",
        "selection_type": "Single",
        "required": 1,
        "min": 1,
        "max": 1,
        "options": (
            {
                "code": f"{AUTHORITY_PREFIX}MOONLEAF-DAIRY-MILK",
                "name": "Fresh milk",
                "kind": "Instruction Only",
                "default": 1,
                "price": Decimal("0"),
                "instruction": "Use the measured fresh-milk recipe.",
            },
            {
                "code": f"{AUTHORITY_PREFIX}MOONLEAF-OAT-MILK-SWAP",
                "name": "Oat milk",
                "kind": "Replace",
                "default": 0,
                "price": Decimal("2"),
                "target_substitution_key": "milk",
                "target_item": f"{AUTHORITY_PREFIX}MOONLEAF-FRESH-MILK",
                "new_item": f"{AUTHORITY_PREFIX}MOONLEAF-OAT-MILK",
            },
        ),
    },
    {
        "key": "matcha_extras",
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-EXTRAS",
        "name": "Matcha extras",
        "selection_type": "Multiple",
        "required": 0,
        "min": 0,
        "max": 2,
        "options": (
            {
                "code": f"{AUTHORITY_PREFIX}MOONLEAF-EXTRA-MATCHA",
                "name": "Extra matcha",
                "kind": "Add",
                "default": 0,
                "price": Decimal("2.50"),
                "new_item": f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-POWDER",
                "qty_delta": Decimal("2"),
                "qty_uom": "Gram",
            },
            {
                "code": f"{AUTHORITY_PREFIX}MOONLEAF-EXTRA-COLD-FOAM",
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
        "code": f"{AUTHORITY_PREFIX}MOONLEAF-COFFEE-EXTRAS",
        "name": "Coffee extras",
        "selection_type": "Multiple",
        "required": 0,
        "min": 0,
        "max": 2,
        "options": (
            {
                "code": f"{AUTHORITY_PREFIX}MOONLEAF-EXTRA-ESPRESSO",
                "name": "Extra espresso shot",
                "kind": "Add",
                "default": 0,
                "price": Decimal("3"),
                "new_item": f"{AUTHORITY_PREFIX}MOONLEAF-ESPRESSO-BEANS",
                "qty_delta": Decimal("18"),
                "qty_uom": "Gram",
            },
            {
                "code": f"{AUTHORITY_PREFIX}MOONLEAF-COFFEE-COLD-FOAM",
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
        "name": f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-MONDAY",
        "label": "Matcha Monday 10%",
        "type": "item_discount",
        "discount": Decimal("10"),
        "items": tuple(spec["code"] for spec in SELLABLES if "MATCHA" in spec["code"]),
    },
    {
        "name": f"{AUTHORITY_PREFIX}MOONLEAF-CAFE-PAIR",
        "label": "Coffee + Matcha Pair 15%",
        "type": "order_discount",
        "discount": Decimal("15"),
        "items": tuple(spec["code"] for spec in SELLABLES),
    },
)

OPENING_REMARKS = f"{AUTHORITY_PREFIX}MOONLEAF fictional opening stock"
OPENING_PACKS: dict[str, Decimal] = {
    f"{AUTHORITY_PREFIX}MOONLEAF-ESPRESSO-BEANS": Decimal("8"),
    f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-POWDER": Decimal("6"),
    f"{AUTHORITY_PREFIX}MOONLEAF-FRESH-MILK": Decimal("24"),
    f"{AUTHORITY_PREFIX}MOONLEAF-OAT-MILK": Decimal("12"),
    f"{AUTHORITY_PREFIX}MOONLEAF-STRAWBERRY-PUREE": Decimal("6"),
    f"{AUTHORITY_PREFIX}MOONLEAF-VANILLA-SYRUP": Decimal("4"),
    f"{AUTHORITY_PREFIX}MOONLEAF-BROWN-SUGAR-SYRUP": Decimal("4"),
    f"{AUTHORITY_PREFIX}MOONLEAF-WHIPPING-CREAM": Decimal("8"),
    f"{AUTHORITY_PREFIX}MOONLEAF-FILTERED-WATER": Decimal("10"),
    f"{AUTHORITY_PREFIX}MOONLEAF-16OZ-CUP": Decimal("10"),
    f"{AUTHORITY_PREFIX}MOONLEAF-16OZ-LID": Decimal("10"),
    f"{AUTHORITY_PREFIX}MOONLEAF-PAPER-STRAW": Decimal("3"),
}


def _ensure_uom(name: str) -> str:
    runtime_frappe = _require_frappe()
    if not runtime_frappe.db.exists("UOM", name):
        runtime_frappe.get_doc({"doctype": "UOM", "uom_name": name}).insert(
            ignore_permissions=True
        )
    return name


def _ensure_conversion(item_code: str, purchase_uom: str, conversion: Decimal) -> None:
    """Bind an exact purchase-to-stock conversion onto the Item itself."""

    runtime_frappe = _require_frappe()
    item = runtime_frappe.get_doc("Item", item_code)
    rows = [
        row
        for row in (getattr(item, "uoms", None) or [])
        if _text(_value(row, "uom")) == purchase_uom
    ]
    if rows:
        existing_factor = _proof_decimal_text(_value(rows[0], "conversion_factor"), "conversion factor")
        if existing_factor != _proof_decimal_text(conversion, "conversion factor"):
            _fail(f"{item_code} already has a different {purchase_uom} conversion")
        return
    item.append("uoms", {"uom": purchase_uom, "conversion_factor": conversion})
    item.save(ignore_permissions=True)


def _discover_commissioning_warehouse(company: str) -> str:
    """Use the single enabled outlet warehouse; never guess between outlets."""

    runtime_frappe = _require_frappe()
    profile_rows = runtime_frappe.get_all(
        "POS Profile",
        filters={"company": company, "disabled": 0},
        fields=["name", "warehouse"],
        order_by="name asc",
        limit_page_length=0,
    )
    warehouses = sorted(
        {
            _text(_value(row, "warehouse"))
            for row in profile_rows
            if _text(_value(row, "warehouse"))
        }
    )
    if len(warehouses) == 1:
        return warehouses[0]
    if len(warehouses) > 1:
        _fail(
            "Enabled POS Profiles use more than one warehouse; choose one pilot outlet before commissioning"
        )
    return _discover_warehouse(company)


def _commission_ingredient(spec: dict[str, Any], *, item_group: str, warehouse: str) -> str:
    _ensure_uom(spec["stock_uom"])
    _ensure_uom(spec["purchase_uom"])
    item = _ensure_item(
        item_code=spec["code"],
        item_group=item_group,
        stock_uom=spec["stock_uom"],
        is_stock_item=1,
        role=spec["role"],
    )
    item.item_name = spec["name"]
    item.description = f"{CAFE_NAME} rehearsal stock — fictional, never production master data"
    _set_if_present(item, "purchase_uom", spec["purchase_uom"])
    _set_if_present(item, "has_batch_no", spec["has_batch_no"])
    _set_if_present(item, "shelf_life_in_days", spec["shelf_life_days"])
    _set_if_present(item, "is_purchase_item", 1)
    _set_if_present(item, "standard_rate", spec["valuation_rate"])
    _set_if_present(item, "custom_fb_item_role", spec["role"])
    _set_if_present(item, "custom_fb_inventory_excluded", 0)
    item.save(ignore_permissions=True)
    _ensure_conversion(spec["code"], spec["purchase_uom"], spec["conversion"])
    _ensure_default_warehouse(spec["code"], warehouse)
    return item.name


def _commission_count_cadence(item_group: str) -> None:
    """Give the fictional stock group an explicit, reviewable count cadence."""

    runtime_frappe = _require_frappe()
    group = runtime_frappe.get_doc("Item Group", item_group)
    _set_if_present(group, "custom_kopos_count_frequency", "Daily")
    _set_if_present(group, "custom_kopos_count_weekdays", "")
    group.save(ignore_permissions=True)


def _ensure_default_warehouse(item_code: str, warehouse: str) -> None:
    runtime_frappe = _require_frappe()
    if not runtime_frappe.db.exists("DocType", "Item Default"):
        return
    item = runtime_frappe.get_doc("Item", item_code)
    company = _text(runtime_frappe.db.get_value("Warehouse", warehouse, "company"))
    if not company:
        return
    defaults = list(getattr(item, "item_defaults", None) or [])
    company_defaults = [row for row in defaults if _text(_value(row, "company")) == company]
    if len(company_defaults) > 1:
        _fail(f"{item_code} has multiple Item Defaults for {company}")
    if company_defaults:
        if _text(_value(company_defaults[0], "default_warehouse")) == warehouse:
            return
        company_defaults[0].default_warehouse = warehouse
    else:
        item.append(
            "item_defaults", {"company": company, "default_warehouse": warehouse}
        )
    item.save(ignore_permissions=True)


def _commission_prepared_components(
    *, item_group: str, warehouse: str, company: str
) -> dict[str, str]:
    """The prepared component is stocked; its BOM consumes raw ingredients.

    A sale consumes the prepared stock Item.  The BOM is never recursively
    consumed again during the sale -- manufacturing is what creates the stock.
    """

    runtime_frappe = _require_frappe()
    _ensure_uom("Millilitre")
    result: dict[str, str] = {}
    for spec in PREPARED_COMPONENTS:
        item = _ensure_item(
            item_code=spec["code"],
            item_group=item_group,
            stock_uom="Millilitre",
            is_stock_item=1,
            role="Prep Item",
        )
        item.item_name = spec["name"]
        item.description = f"{CAFE_NAME} prepared component — fictional rehearsal data"
        _set_if_present(item, "purchase_uom", "Millilitre")
        _set_if_present(item, "has_batch_no", 1)
        _set_if_present(item, "shelf_life_in_days", spec["shelf_life_days"])
        _set_if_present(item, "is_purchase_item", 0)
        _set_if_present(item, "custom_fb_item_role", "Prep Item")
        item.save(ignore_permissions=True)
        _ensure_default_warehouse(spec["code"], warehouse)

        existing = runtime_frappe.db.get_value(
            "BOM", {"item": spec["code"], "docstatus": 1, "is_active": 1}, "name"
        )
        if existing:
            existing_bom = runtime_frappe.get_doc("BOM", existing)
            if (
                int(getattr(existing_bom, "custom_kopos_autoprep_enabled", 0) or 0) == 1
                and _proof_decimal_text(
                    getattr(existing_bom, "custom_kopos_min_ready_qty", 0) or 0,
                    "minimum ready quantity",
                )
                == _proof_decimal_text(spec["min_ready_qty"], "minimum ready quantity")
                and _proof_decimal_text(
                    getattr(existing_bom, "custom_kopos_preparation_variance_percent", 0) or 0,
                    "preparation variance",
                )
                == "10"
                and _text(getattr(existing_bom, "custom_kopos_preparation_instructions", None))
                == spec["instructions"]
            ):
                result[spec["code"]] = _text(existing)
                continue
            existing_bom.is_active = 0
            existing_bom.is_default = 0
            existing_bom.save(ignore_permissions=True)

        bom = runtime_frappe.new_doc("BOM")
        bom.item = spec["code"]
        bom.company = company
        bom.quantity = spec["batch_qty"]
        _set_if_present(bom, "uom", "Millilitre")
        _set_if_present(bom, "is_active", 1)
        _set_if_present(bom, "is_default", 1)
        _set_if_present(bom, "with_operations", 0)
        _set_if_present(bom, "custom_kopos_autoprep_enabled", 1)
        _set_if_present(bom, "custom_kopos_batch_qty", spec["batch_qty"])
        _set_if_present(bom, "custom_kopos_min_ready_qty", spec["min_ready_qty"])
        _set_if_present(bom, "custom_kopos_preparation_lead_minutes", spec["lead_minutes"])
        _set_if_present(bom, "custom_kopos_preparation_variance_percent", Decimal("10"))
        _set_if_present(bom, "custom_kopos_preparation_instructions", spec["instructions"])
        for input_item, input_qty in spec["inputs"]:
            input_uom = _text(runtime_frappe.db.get_value("Item", input_item, "stock_uom"))
            bom.append(
                "items",
                {
                    "item_code": input_item,
                    "qty": Decimal(input_qty),
                    "uom": input_uom,
                    "stock_uom": input_uom,
                    "conversion_factor": 1,
                },
            )
        bom.insert(ignore_permissions=True)
        bom.submit()
        result[spec["code"]] = bom.name
    return result


def _commission_modifiers() -> dict[str, dict[str, Any]]:
    runtime_frappe = _require_frappe()
    commissioned: dict[str, dict[str, Any]] = {}
    for order, spec in enumerate(MODIFIER_GROUPS, start=1):
        group_name = runtime_frappe.db.get_value(
            "FB Modifier Group", {"group_code": spec["code"]}, "name"
        )
        if group_name:
            group = runtime_frappe.get_doc("FB Modifier Group", group_name)
            expected_group = {
                "group_name": spec["name"],
                "selection_type": spec["selection_type"],
                "is_required": spec["required"],
                "min_selection": spec["min"],
                "max_selection": spec["max"],
                "active": 1,
            }
            mismatches = [
                fieldname
                for fieldname, expected in expected_group.items()
                if _text(_value(group, fieldname)) != _text(expected)
            ]
            if mismatches:
                _fail(
                    f"Published Moonleaf modifier group {group.name} changed: "
                    + ", ".join(mismatches)
                )
        else:
            group = runtime_frappe.new_doc("FB Modifier Group")
            group.group_code = spec["code"]
            group.group_name = spec["name"]
            group.selection_type = spec["selection_type"]
            group.is_required = spec["required"]
            group.min_selection = spec["min"]
            group.max_selection = spec["max"]
            group.display_order = order
            group.active = 1
            group.default_resolution_policy = "Auto Apply Default"
            group.insert(ignore_permissions=True)

        option_names: list[str] = []
        default_modifier = ""
        for display_order, option in enumerate(spec["options"], start=1):
            option_name = runtime_frappe.db.get_value(
                "FB Modifier", {"modifier_code": option["code"]}, "name"
            )
            if option_name:
                modifier = runtime_frappe.get_doc("FB Modifier", option_name)
                expected_modifier = {
                    "modifier_group": group.name,
                    "kind": option["kind"],
                    "target_substitution_key": option.get("target_substitution_key") or "",
                    "target_item": option.get("target_item") or "",
                    "new_item": option.get("new_item") or "",
                    "qty_uom": option.get("qty_uom") or "",
                    "active": 1,
                }
                mismatches = [
                    fieldname
                    for fieldname, expected in expected_modifier.items()
                    if _text(_value(modifier, fieldname)) != _text(expected)
                ]
                expected_qty = option.get("qty_delta") or Decimal("0")
                if _proof_decimal_text(_value(modifier, "qty_delta") or 0, "modifier qty") != _proof_decimal_text(expected_qty, "modifier qty"):
                    mismatches.append("qty_delta")
                if mismatches:
                    _fail(
                        f"Published Moonleaf modifier {modifier.name} changed: "
                        + ", ".join(sorted(set(mismatches)))
                    )
            else:
                modifier = runtime_frappe.new_doc("FB Modifier")
                modifier.modifier_code = option["code"]
                modifier.modifier_name = option["name"]
                modifier.modifier_group = group.name
                modifier.kind = option["kind"]
                modifier.price_adjustment = option["price"]
                modifier.is_default = option["default"]
                modifier.display_order = display_order
                modifier.active = 1
                modifier.target_substitution_key = option.get("target_substitution_key")
                modifier.target_item = option.get("target_item")
                modifier.new_item = option.get("new_item")
                modifier.qty_delta = option.get("qty_delta")
                modifier.qty_uom = option.get("qty_uom")
                modifier.instruction_text = option.get("instruction")
                modifier.affects_stock = int(option["kind"] in {"Add", "Replace", "Remove", "Scale"})
                modifier.affects_recipe = modifier.affects_stock
                modifier.insert(ignore_permissions=True)
            option_names.append(modifier.name)
            if option["default"]:
                default_modifier = modifier.name
        commissioned[spec["key"]] = {
            "group": group.name,
            "default_modifier": default_modifier,
            "options": tuple(option_names),
        }
    return commissioned


def _ensure_selling_price_list(currency: str) -> str:
    runtime_frappe = _require_frappe()
    existing = runtime_frappe.db.exists("Price List", SELLING_PRICE_LIST)
    if existing:
        price_list = runtime_frappe.get_doc("Price List", existing)
    else:
        price_list = runtime_frappe.new_doc("Price List")
        price_list.price_list_name = SELLING_PRICE_LIST
    price_list.enabled = 1
    price_list.selling = 1
    price_list.buying = 0
    price_list.currency = currency
    if existing:
        price_list.save(ignore_permissions=True)
    else:
        price_list.insert(ignore_permissions=True)
    return price_list.name


def _ensure_item_price(item_code: str, price_list: str, rate: Decimal, currency: str) -> str:
    runtime_frappe = _require_frappe()
    existing = runtime_frappe.db.get_value(
        "Item Price", {"item_code": item_code, "price_list": price_list}, "name"
    )
    if existing:
        price = runtime_frappe.get_doc("Item Price", existing)
    else:
        price = runtime_frappe.new_doc("Item Price")
        price.item_code = item_code
        price.price_list = price_list
    price.price_list_rate = rate
    price.currency = currency
    price.selling = 1
    if existing:
        price.save(ignore_permissions=True)
    else:
        price.insert(ignore_permissions=True)
    return price.name


def _commission_supplier_and_quotation(*, company: str, warehouse: str) -> dict[str, str]:
    """Draft PO creation requires exactly one current submitted quotation."""

    runtime_frappe = _require_frappe()
    if not runtime_frappe.db.exists("Supplier", SUPPLIER):
        supplier = runtime_frappe.new_doc("Supplier")
        supplier.supplier_name = SUPPLIER
        _set_if_present(supplier, "supplier_group", _discover_supplier_group())
        supplier.insert(ignore_permissions=True)

    existing = runtime_frappe.db.get_value(
        "Supplier Quotation",
        {"supplier": SUPPLIER, "docstatus": 1},
        "name",
    )
    if existing:
        existing_doc = runtime_frappe.get_doc("Supplier Quotation", existing)
        item_codes = {
            _text(_value(row, "item_code"))
            for row in (getattr(existing_doc, "items", None) or [])
        }
        warehouses = {
            _text(_value(row, "warehouse"))
            for row in (getattr(existing_doc, "items", None) or [])
        }
        if item_codes != {spec["code"] for spec in INGREDIENTS}:
            _fail("Existing Moonleaf quotation does not match the fictional ingredient manifest")
        if warehouses == {warehouse}:
            return {"supplier": SUPPLIER, "quotation": _text(existing)}
        existing_doc.cancel()

    quotation = runtime_frappe.new_doc("Supplier Quotation")
    quotation.supplier = SUPPLIER
    quotation.company = company
    quotation.transaction_date = runtime_frappe.utils.nowdate()
    _set_if_present(quotation, "currency", "MYR")
    _set_if_present(
        quotation, "valid_till", runtime_frappe.utils.add_days(runtime_frappe.utils.nowdate(), 30)
    )
    for spec in INGREDIENTS:
        quotation.append(
            "items",
            {
                "item_code": spec["code"],
                "qty": 1,
                "uom": spec["purchase_uom"],
                "stock_uom": spec["stock_uom"],
                "conversion_factor": spec["conversion"],
                "rate": spec["purchase_rate"],
                "warehouse": warehouse,
                "schedule_date": runtime_frappe.utils.add_days(
                    runtime_frappe.utils.nowdate(), 3
                ),
            },
        )
    quotation.insert(ignore_permissions=True)
    quotation.submit()
    return {"supplier": SUPPLIER, "quotation": quotation.name}


def _ensure_opening_batch(spec: dict[str, Any]) -> str | None:
    if not spec["has_batch_no"]:
        return None
    runtime_frappe = _require_frappe()
    batch_id = f"{spec['code']}-OPENING"
    existing = runtime_frappe.db.exists("Batch", batch_id)
    if existing:
        batch = runtime_frappe.get_doc("Batch", existing)
        if _text(getattr(batch, "item", None)) != spec["code"]:
            _fail(f"Moonleaf opening Batch {batch_id} belongs to another Item")
        return batch.name
    batch = runtime_frappe.new_doc("Batch")
    batch.batch_id = batch_id
    batch.item = spec["code"]
    batch.manufacturing_date = runtime_frappe.utils.nowdate()
    batch.expiry_date = runtime_frappe.utils.add_days(
        runtime_frappe.utils.nowdate(), spec["shelf_life_days"]
    )
    batch.insert(ignore_permissions=True)
    return batch.name


def _commission_opening_stock(*, company: str, warehouse: str) -> str:
    """Set one standard, batch-aware synthetic opening balance."""

    runtime_frappe = _require_frappe()
    if _meta_has("Stock Reconciliation", "remarks"):
        rows = runtime_frappe.get_all(
            "Stock Reconciliation",
            filters={"company": company, "remarks": OPENING_REMARKS},
            fields=["name", "docstatus"],
            order_by="creation asc, name asc",
            limit_page_length=0,
        )
    else:
        child_rows = runtime_frappe.get_all(
            "Stock Reconciliation Item",
            filters={"item_code": next(iter(OPENING_PACKS))},
            fields=["parent"],
            limit_page_length=0,
        )
        parent_names = [_text(_value(row, "parent")) for row in child_rows]
        rows = runtime_frappe.get_all(
            "Stock Reconciliation",
            filters={"company": company, "name": ["in", parent_names]},
            fields=["name", "docstatus"],
            limit_page_length=0,
        ) if parent_names else []
    if len(rows) > 1:
        _fail("Multiple Moonleaf opening Stock Reconciliations exist")
    if rows:
        name = _text(_value(rows[0], "name"))
        if int(_value(rows[0], "docstatus") or 0) != 1:
            _fail(f"Moonleaf opening Stock Reconciliation {name} is not submitted")
        return name

    reconciliation = runtime_frappe.new_doc("Stock Reconciliation")
    reconciliation.company = company
    reconciliation.purpose = "Opening Stock"
    opened_at = runtime_frappe.utils.now_datetime() - timedelta(minutes=5)
    _set_if_present(reconciliation, "set_posting_time", 1)
    reconciliation.posting_date = opened_at.date()
    _set_if_present(reconciliation, "posting_time", opened_at.time())
    _set_if_present(
        reconciliation,
        "expense_account",
        _discover_opening_difference_account(company),
    )
    _set_if_present(reconciliation, "remarks", OPENING_REMARKS)
    for spec in INGREDIENTS:
        quantity = OPENING_PACKS[spec["code"]] * spec["conversion"]
        row = {
            "item_code": spec["code"],
            "warehouse": warehouse,
            "qty": quantity,
            "stock_uom": spec["stock_uom"],
            "valuation_rate": spec["valuation_rate"],
        }
        batch_no = _ensure_opening_batch(spec)
        if batch_no:
            row["batch_no"] = batch_no
            row["use_serial_batch_fields"] = 1
        reconciliation.append("items", row)
    reconciliation.insert(ignore_permissions=True)
    reconciliation.submit()
    if int(getattr(reconciliation, "docstatus", 0) or 0) != 1:
        _fail("Moonleaf opening Stock Reconciliation did not submit")
    return reconciliation.name


def _ensure_prepared_batch(spec: dict[str, Any]) -> str:
    runtime_frappe = _require_frappe()
    batch_id = f"{spec['code']}-FIRST-BATCH"
    existing = runtime_frappe.db.exists("Batch", batch_id)
    if existing:
        batch = runtime_frappe.get_doc("Batch", existing)
        if _text(getattr(batch, "item", None)) != spec["code"]:
            _fail(f"Moonleaf prepared Batch {batch_id} belongs to another Item")
        return batch.name
    batch = runtime_frappe.new_doc("Batch")
    batch.batch_id = batch_id
    batch.item = spec["code"]
    batch.manufacturing_date = runtime_frappe.utils.nowdate()
    batch.expiry_date = runtime_frappe.utils.add_days(
        runtime_frappe.utils.nowdate(), spec["shelf_life_days"]
    )
    batch.insert(ignore_permissions=True)
    return batch.name


def _commission_prepared_stock(
    *,
    company: str,
    warehouse: str,
    boms: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Prepare the first real batches through Work Order and Manufacture."""

    runtime_frappe = _require_frappe()
    from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

    result: dict[str, dict[str, str]] = {}
    for spec in PREPARED_COMPONENTS:
        fingerprint = hashlib.sha256(
            f"moonleaf-first-batch|{company}|{warehouse}|{boms[spec['code']]}".encode("utf-8")
        ).hexdigest()
        work_order_name = runtime_frappe.db.get_value(
            "Work Order", {"custom_kopos_preparation_fingerprint": fingerprint}, "name"
        )
        if work_order_name:
            work_order = runtime_frappe.get_doc("Work Order", work_order_name)
        else:
            work_order = runtime_frappe.new_doc("Work Order")
            work_order.company = company
            work_order.production_item = spec["code"]
            work_order.bom_no = boms[spec["code"]]
            work_order.qty = spec["batch_qty"]
            work_order.fg_warehouse = warehouse
            _set_if_present(work_order, "skip_transfer", 1)
            _set_if_present(work_order, "custom_kopos_preparation_fingerprint", fingerprint)
            work_order.insert(ignore_permissions=True)
        stock_entry_name = runtime_frappe.db.get_value(
            "Stock Entry",
            {"work_order": work_order.name, "docstatus": 1, "purpose": "Manufacture"},
            "name",
        )
        if stock_entry_name:
            result[spec["code"]] = {
                "work_order": work_order.name,
                "stock_entry": _text(stock_entry_name),
                "batch": f"{spec['code']}-FIRST-BATCH",
            }
            continue
        for row in getattr(work_order, "required_items", None) or []:
            row.source_warehouse = warehouse
        _set_if_present(work_order, "skip_transfer", 1)
        if int(getattr(work_order, "docstatus", 0) or 0) == 0:
            work_order.save(ignore_permissions=True)
            work_order.submit()
        if int(getattr(work_order, "docstatus", 0) or 0) != 1:
            _fail(f"Moonleaf Work Order {work_order.name} is not submitted")

        values = make_stock_entry(
            work_order.name,
            "Manufacture",
            qty=spec["batch_qty"],
            target_warehouse=warehouse,
        )
        stock_entry = runtime_frappe.get_doc(values)
        stock_entry.company = company
        stock_entry.work_order = work_order.name
        stock_entry.fg_completed_qty = spec["batch_qty"]
        _set_if_present(stock_entry, "to_warehouse", warehouse)
        finished_batch = _ensure_prepared_batch(spec)
        for row in getattr(stock_entry, "items", None) or []:
            item_code = _text(getattr(row, "item_code", None))
            if int(getattr(row, "is_finished_item", 0) or 0):
                row.t_warehouse = warehouse
                _set_if_present(row, "batch_no", finished_batch)
                _set_if_present(row, "use_serial_batch_fields", 1)
                continue
            row.s_warehouse = warehouse
            ingredient = next(
                (candidate for candidate in INGREDIENTS if candidate["code"] == item_code),
                None,
            )
            if ingredient and ingredient["has_batch_no"]:
                _set_if_present(row, "batch_no", f"{item_code}-OPENING")
                _set_if_present(row, "use_serial_batch_fields", 1)
        stock_entry.insert(ignore_permissions=True)
        stock_entry.submit()
        if int(getattr(stock_entry, "docstatus", 0) or 0) != 1:
            _fail(f"Moonleaf Manufacture Stock Entry {stock_entry.name} is not submitted")
        result[spec["code"]] = {
            "work_order": work_order.name,
            "stock_entry": stock_entry.name,
            "batch": finished_batch,
        }
    return result


def _discover_supplier_group() -> str | None:
    runtime_frappe = _require_frappe()
    rows = runtime_frappe.get_all(
        "Supplier Group", fields=["name"], order_by="name asc", limit_page_length=1
    )
    return _text(_value(rows[0], "name")) if rows else None


def _commission_sellable_and_recipe(
    spec: dict[str, Any],
    *,
    company: str,
    item_group: str,
    price_list: str,
    currency: str,
    modifiers: dict[str, dict[str, Any]],
) -> str:
    """Publish one exact recipe and bind the resulting immutable version."""

    runtime_frappe = _require_frappe()
    item = _ensure_item(
        item_code=spec["code"],
        item_group=item_group,
        stock_uom="Nos",
        is_stock_item=0,
        role="Sellable Drink",
    )
    item.item_name = spec["name"]
    item.description = f"{CAFE_NAME} menu drink — fictional rehearsal data"
    item.standard_rate = spec["price"]
    _set_if_present(item, "is_sales_item", 1)
    _set_if_present(item, "is_purchase_item", 0)
    _set_if_present(item, "custom_fb_item_role", "Sellable Drink")
    _set_if_present(item, "custom_fb_recipe_required", 1)
    _set_if_present(item, "custom_fb_inventory_excluded", 0)
    _set_if_present(item, "custom_fb_track_theoretical_stock", 1)
    _set_if_present(item, "custom_kopos_track_stock", 0)
    _set_if_present(item, "custom_kopos_availability_mode", "auto")
    item.save(ignore_permissions=True)
    _ensure_item_price(spec["code"], price_list, spec["price"], currency)

    recipe_code = f"{spec['code']}-RECIPE"
    existing = runtime_frappe.db.get_value("FB Recipe", {"recipe_code": recipe_code}, "name")
    if existing:
        recipe = runtime_frappe.get_doc("FB Recipe", existing)
        if _text(getattr(recipe, "status", None)) != "Active":
            _fail(f"Commissioned recipe {recipe.name} is not Active")
        _set_if_present(item, "custom_fb_default_recipe", recipe.name)
        item.save(ignore_permissions=True)
        return recipe.name

    recipe = runtime_frappe.new_doc("FB Recipe")
    recipe.recipe_code = recipe_code
    recipe.recipe_name = f"{spec['name']} — measured v1"
    recipe.sellable_item = spec["code"]
    recipe.company = company
    recipe.recipe_type = "Finished Drink"
    recipe.status = "Active"
    recipe.version_no = 1
    recipe.yield_qty = 1
    recipe.yield_uom = "Nos"
    recipe.default_serving_qty = 1
    recipe.default_serving_uom = "Nos"
    recipe.station = "Moonleaf Drinks Bar"
    recipe.effective_from = runtime_frappe.utils.now_datetime() - timedelta(minutes=10)
    recipe.notes = (
        "Fictional director-approved rehearsal measurement. "
        "Quantities include the declared normal loss and are not production authority."
    )
    for component in spec["components"]:
        stock_uom = _text(
            runtime_frappe.db.get_value("Item", component["item"], "stock_uom")
        )
        recipe.append(
            "components",
            {
                "item": component["item"],
                "component_type": component["component_type"],
                "qty": Decimal(component["qty"]),
                "uom": stock_uom,
                "stock_qty": Decimal(component["qty"]),
                "stock_uom": stock_uom,
                "stock_conversion_factor": 1,
                "is_substitutable": int(bool(component.get("substitution_key"))),
                "substitution_key": component.get("substitution_key"),
                "affects_stock": 1,
                "affects_cogs": 1,
                "loss_factor_pct": Decimal(component["loss"]),
            },
        )
    for display_order, group_key in enumerate(spec["modifier_groups"], start=1):
        group = modifiers[group_key]
        recipe.append(
            "allowed_modifier_groups",
            {
                "modifier_group": group["group"],
                "required": int(bool(group["default_modifier"])),
                "override_min_selection": 1 if group["default_modifier"] else 0,
                "override_max_selection": 1 if group["default_modifier"] else 2,
                "default_modifier": group["default_modifier"] or None,
                "display_order": display_order,
                "always_prompt": int(bool(group["default_modifier"])),
            },
        )
    recipe.insert(ignore_permissions=True)
    _set_if_present(item, "custom_fb_default_recipe", recipe.name)
    item.save(ignore_permissions=True)
    return recipe.name


def _commission_promotions(pos_profile: str) -> list[str]:
    """Create reviewable draft promotions; economics remains server-owned."""

    runtime_frappe = _require_frappe()
    result: list[str] = []
    for priority, spec in enumerate(PROMOTIONS, start=1):
        existing = runtime_frappe.db.exists("KoPOS Promotion", spec["name"])
        if existing:
            promotion = runtime_frappe.get_doc("KoPOS Promotion", existing)
            promotion.display_label = spec["label"]
            promotion.customer_message = f"{spec['label']} at {CAFE_NAME}."
            promotion.promotion_type = spec["type"]
            promotion.discount_type = "percentage"
            promotion.discount_value = spec["discount"]
            promotion.outlet_scope_mode = "selected_pos_profiles"
            expected_items = list(spec["items"])
            if [
                _text(_value(row, "item_code"))
                for row in (getattr(promotion, "eligible_items", None) or [])
            ] != expected_items:
                promotion.set("eligible_items", [])
                for item_code in expected_items:
                    promotion.append("eligible_items", {"item_code": item_code})
            if [
                _text(_value(row, "pos_profile"))
                for row in (getattr(promotion, "eligible_pos_profiles", None) or [])
            ] != [pos_profile]:
                promotion.set("eligible_pos_profiles", [])
                promotion.append("eligible_pos_profiles", {"pos_profile": pos_profile})
            promotion.save(ignore_permissions=True)
            result.append(promotion.name)
            continue
        promotion = runtime_frappe.new_doc("KoPOS Promotion")
        promotion.promotion_name = spec["name"]
        promotion.display_label = spec["label"]
        promotion.customer_message = f"{spec['label']} at {CAFE_NAME}."
        promotion.promotion_type = spec["type"]
        promotion.is_active = 0
        promotion.offline_allowed = 1
        promotion.activation_mode = "manual_selectable"
        promotion.priority = priority * 10
        promotion.stacking_policy = "best_price"
        promotion.outlet_scope_mode = "selected_pos_profiles"
        promotion.eligible_scope_mode = "eligible_pool"
        promotion.repeat_mode = "once"
        promotion.buy_qty = 1
        promotion.discount_qty = 1
        promotion.discount_type = "percentage"
        promotion.discount_value = spec["discount"]
        promotion.modifier_policy = "excluded_by_default"
        promotion.economics_status = "Not checked"
        for item_code in spec["items"]:
            promotion.append("eligible_items", {"item_code": item_code})
        promotion.append("eligible_pos_profiles", {"pos_profile": pos_profile})
        promotion.insert(ignore_permissions=True)
        result.append(promotion.name)
    return result


def _commission_pos_profile(*, company: str, warehouse: str, price_list: str) -> str:
    """Create a dedicated fictional outlet profile without guessing accounts."""

    runtime_frappe = _require_frappe()
    source_rows = runtime_frappe.get_all(
        "POS Profile",
        filters={"company": company, "warehouse": warehouse, "disabled": 0},
        fields=["name"],
        order_by="name asc",
        limit_page_length=10,
    )
    source_name = next(
        (
            _text(_value(row, "name"))
            for row in source_rows
            if _text(_value(row, "name")) != POS_PROFILE
        ),
        "",
    )
    if not source_name:
        _fail("Moonleaf POS Profile needs one configured outlet profile to supply accounting defaults")
    source = runtime_frappe.get_doc("POS Profile", source_name)
    existing = runtime_frappe.db.exists("POS Profile", POS_PROFILE)
    profile = (
        runtime_frappe.get_doc("POS Profile", existing)
        if existing
        else runtime_frappe.get_doc(
            {
                "doctype": "POS Profile",
                "name": POS_PROFILE,
                "payments": [
                    {
                        "mode_of_payment": _text(_value(row, "mode_of_payment")),
                        "default": int(_value(row, "default") or 0),
                    }
                    for row in (getattr(source, "payments", None) or [])
                    if _text(_value(row, "mode_of_payment"))
                ],
            }
        )
    )
    profile.company = company
    profile.warehouse = warehouse
    profile.currency = _text(getattr(source, "currency", None))
    profile.selling_price_list = price_list
    profile.customer = _text(getattr(source, "customer", None))
    profile.write_off_account = _text(getattr(source, "write_off_account", None))
    profile.write_off_cost_center = _text(getattr(source, "write_off_cost_center", None))
    profile.write_off_limit = getattr(source, "write_off_limit", 0) or 0
    profile.disabled = 0
    for fieldname in ("custom_kopos_enable_sst", "custom_kopos_sst_rate"):
        _set_if_present(profile, fieldname, getattr(source, fieldname, None))
    if existing:
        profile.save(ignore_permissions=True)
    else:
        profile.insert(ignore_permissions=True)
    return profile.name


def _commission_cafe() -> dict[str, Any]:
    runtime_frappe = _require_frappe()
    company = _discover_company()
    warehouse = _discover_commissioning_warehouse(company["name"])
    item_group = _discover_item_group()
    _commission_count_cadence(item_group)

    ingredient_names = [
        _commission_ingredient(spec, item_group=item_group, warehouse=warehouse)
        for spec in INGREDIENTS
    ]
    boms = _commission_prepared_components(
        item_group=item_group, warehouse=warehouse, company=company["name"]
    )
    modifiers = _commission_modifiers()
    price_list = _ensure_selling_price_list(company["currency"])
    pos_profile = _commission_pos_profile(
        company=company["name"], warehouse=warehouse, price_list=price_list
    )
    purchasing = _commission_supplier_and_quotation(
        company=company["name"], warehouse=warehouse
    )
    opening_reconciliation = _commission_opening_stock(
        company=company["name"], warehouse=warehouse
    )
    prepared_stock = _commission_prepared_stock(
        company=company["name"], warehouse=warehouse, boms=boms
    )
    recipes = [
        _commission_sellable_and_recipe(
            spec,
            company=company["name"],
            item_group=item_group,
            price_list=price_list,
            currency=company["currency"],
            modifiers=modifiers,
        )
        for spec in SELLABLES
    ]
    promotions = _commission_promotions(pos_profile)
    checks = _completion_checks(
        ingredient_names=ingredient_names,
        boms=boms,
        recipes=recipes,
        modifiers=modifiers,
        promotions=promotions,
        purchasing=purchasing,
        price_list=price_list,
        pos_profile=pos_profile,
        opening_reconciliation=opening_reconciliation,
        warehouse=warehouse,
        prepared_stock=prepared_stock,
    )
    failed = sorted(name for name, ok in checks.items() if not ok)
    if failed:
        _fail("Commissioning completion checklist failed: " + ", ".join(failed))
    runtime_frappe.db.commit()
    return {
        "fictionalCafe": CAFE_NAME,
        "company": company["name"],
        "warehouse": warehouse,
        "commissionedIngredients": sorted(ingredient_names),
        "preparedComponents": sorted(boms),
        "preparedComponentBoms": dict(sorted(boms.items())),
        "commissionedSellables": sorted(spec["code"] for spec in SELLABLES),
        "commissionedRecipes": sorted(recipes),
        "modifierGroups": sorted(row["group"] for row in modifiers.values()),
        "promotions": sorted(promotions),
        "sellingPriceList": price_list,
        "posProfile": pos_profile,
        "supplier": purchasing["supplier"],
        "supplierQuotation": purchasing["quotation"],
        "openingStockReconciliation": opening_reconciliation,
        "preparedStockDocuments": dict(sorted(prepared_stock.items())),
        "completionChecks": dict(sorted(checks.items())),
        "commissioningAssertions": len(checks),
    }


def run_v1(
    restored_backup_sha256: str,
    erp_artifact_sha256: str,
    expected_connector_version: str,
) -> dict[str, Any]:
    """Commission the hypothetical master and prove it is inventory-ready.

    Bound to the exact candidate the way every other restored-data producer is:
    an unbound commissioning result could otherwise float free of the artifact
    that produced it.
    """

    runtime_frappe = _require_frappe()
    backup_sha256 = _required_sha256(restored_backup_sha256, "restored_backup_sha256")
    artifact_sha256 = _required_sha256(erp_artifact_sha256, "erp_artifact_sha256")
    expected_version = _required_text(expected_connector_version, "expected_connector_version")
    installed_version = metadata.version("kopos_connector")
    if installed_version != expected_version:
        _fail("Installed connector version does not match the candidate binding")
    commissioned = _commission_cafe()
    return {
        "schemaVersion": 2,
        "status": "passed",
        "contractId": CONTRACT_ID,
        "producer": PRODUCER,
        "evidenceLevel": "restored_production_data",
        "connectorVersion": installed_version,
        "erpArtifactSha256": artifact_sha256,
        "restoredBackupSha256": backup_sha256,
        "authorityPrefix": AUTHORITY_PREFIX,
        **commissioned,
    }


def run_local_v1(
    erp_artifact_sha256: str,
    expected_connector_version: str,
) -> dict[str, Any]:
    """Commission the same cafe on an isolated local real-service site.

    This entry point is intentionally unable to claim restored-production-data
    evidence.  It exists for developer and stakeholder rehearsal where Frappe,
    MariaDB, and Redis are real but the database is synthetic.
    """

    artifact_sha256 = _required_sha256(erp_artifact_sha256, "erp_artifact_sha256")
    expected_version = _required_text(expected_connector_version, "expected_connector_version")
    installed_version = metadata.version("kopos_connector")
    if installed_version != expected_version:
        _fail("Installed connector version does not match the local image binding")
    normalized_prerequisites = _normalize_local_fixture_items()
    return {
        "schemaVersion": 2,
        "status": "passed",
        "contractId": CONTRACT_ID,
        "producer": "kopos_connector.acceptance.restored_commissioning.run_local_v1",
        "evidenceLevel": "real_integration",
        "syntheticDatabase": True,
        "connectorVersion": installed_version,
        "erpArtifactSha256": artifact_sha256,
        "authorityPrefix": AUTHORITY_PREFIX,
        "normalizedSyntheticPrerequisites": normalized_prerequisites,
        **_commission_cafe(),
    }


def complete_local_commercial_v1(
    erp_artifact_sha256: str,
    expected_connector_version: str,
) -> dict[str, Any]:
    """Review, activate, and publish Moonleaf's fictional promotions.

    This is a real server-side economics and immutable-snapshot workflow, but
    its evidence level remains local integration because the cafe and database
    are synthetic.  No equivalent restored-data shortcut exists.
    """

    artifact_sha256 = _required_sha256(erp_artifact_sha256, "erp_artifact_sha256")
    expected_version = _required_text(expected_connector_version, "expected_connector_version")
    installed_version = metadata.version("kopos_connector")
    if installed_version != expected_version:
        _fail("Installed connector version does not match the local image binding")
    runtime_frappe = _require_frappe()
    from kopos_connector.api.inventory import get_promotion_economics
    from kopos_connector.api.promotions import publish_promotion_snapshot

    economics: dict[str, Any] = {}
    for spec in PROMOTIONS:
        promotion = runtime_frappe.get_doc("KoPOS Promotion", spec["name"])
        promotion.is_active = 1
        promotion.save(ignore_permissions=True)
        result = get_promotion_economics(
            payload={"promotion": promotion.name, "pos_profile": POS_PROFILE}
        )
        if result.get("status") != "ok" or result.get("publication_status") != "Ready":
            _fail(
                f"Moonleaf promotion {promotion.name} failed economics review: "
                f"{_text(result.get('reason')) or 'not ready'}"
            )
        reviewed = result.get("economics") or {}
        economics[promotion.name] = {
            "publicationStatus": result.get("publication_status"),
            "economicsHash": result.get("economics_hash"),
            "netRevenueSen": reviewed.get("net_revenue_sen"),
            "cogsSen": reviewed.get("cogs_sen"),
            "grossProfitSen": reviewed.get("gross_profit_sen"),
            "marginPercent": reviewed.get("margin_percent"),
            "baselineMarginPercent": reviewed.get("baseline_margin_percent"),
        }
    snapshot = publish_promotion_snapshot(pos_profile=POS_PROFILE)
    return {
        "schemaVersion": 1,
        "status": "passed",
        "producer": "kopos_connector.acceptance.restored_commissioning.complete_local_commercial_v1",
        "evidenceLevel": "real_integration",
        "syntheticDatabase": True,
        "fictionalCafe": CAFE_NAME,
        "connectorVersion": installed_version,
        "erpArtifactSha256": artifact_sha256,
        "posProfile": POS_PROFILE,
        "promotionEconomics": dict(sorted(economics.items())),
        "publishedSnapshot": snapshot,
    }


def complete_local_count_v1(
    task_id: str,
    erp_artifact_sha256: str,
    expected_connector_version: str,
) -> dict[str, Any]:
    """Exercise one exact synthetic blind count through the device boundary."""

    artifact_sha256 = _required_sha256(erp_artifact_sha256, "erp_artifact_sha256")
    expected_version = _required_text(expected_connector_version, "expected_connector_version")
    installed_version = metadata.version("kopos_connector")
    if installed_version != expected_version:
        _fail("Installed connector version does not match the local image binding")
    runtime_frappe = _require_frappe()
    device_id = "SMOKE-TAB-A001"
    staff_user = "staff@smoke.kopos.local"
    task_name = _required_text(task_id, "task_id")
    task_status = _text(runtime_frappe.db.get_value("FB Inventory Count Task", task_name, "status"))
    if task_status == "Reviewed":
        observation = runtime_frappe.db.get_value(
            "FB Inventory Count Observation",
            {"task_id": task_name},
            ["observation_id", "status"],
            as_dict=True,
        ) or {}
        return {
            "schemaVersion": 1,
            "status": "already_completed",
            "producer": "kopos_connector.acceptance.restored_commissioning.complete_local_count_v1",
            "evidenceLevel": "real_integration",
            "syntheticDatabase": True,
            "fictionalCafe": CAFE_NAME,
            "connectorVersion": installed_version,
            "erpArtifactSha256": artifact_sha256,
            "task": task_name,
            "taskStatus": task_status,
            "observation": _text(observation.get("observation_id")),
            "observationStatus": _text(observation.get("status")),
        }

    from kopos_connector.api import _resolve_manager_approval_scope
    from kopos_connector.api.inventory import (
        _get_count_task,
        _payload_hash,
        claim_count_task,
        confirm_count_reconciliation,
        submit_count_observation,
    )
    from kopos_connector.utils.manager_approval import (
        authorize_manager_for_device,
        generate_manager_approval_token,
    )

    device = runtime_frappe.get_doc("KoPOS Device", device_id)
    profile = runtime_frappe.get_doc("POS Profile", device.pos_profile)
    employee = _text(
        runtime_frappe.db.get_value("KoPOS Staff Access", staff_user, "employee")
    )
    if not employee:
        _fail("Synthetic count staff has no Employee authority")
    task_result = _get_count_task(device_id=device_id, task_id=task_name, include_barcodes=True)
    task = task_result.get("task") or {}
    if not task:
        _fail("Synthetic Moonleaf count task is not visible to the outlet device")
    observed_at = datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).isoformat()
    claim_command = f"{AUTHORITY_PREFIX}MOONLEAF-CLAIM-{task_name}"
    claim_payload: dict[str, Any] = {
        "schema_version": "inventory-command-v1",
        "command_id": claim_command,
        "task_type": "claim_count_task",
        "device_id": device_id,
        "device_config_version": int(device.config_version or 0),
        "staff_user": staff_user,
        "employee": employee,
        "company": profile.company,
        "outlet": profile.name,
        "warehouse": profile.warehouse,
        "source_document": task_name,
        "source_revision": int(task["revision"]),
        "observed_at": observed_at,
        "task_id": task_name,
        "task_revision": int(task["revision"]),
    }
    claim_payload["payload_hash"] = _payload_hash(claim_payload)
    claim = claim_count_task(device_id=device_id, payload=claim_payload)
    claimed_task = claim.get("task") or {}
    claimed_revision = int(claimed_task.get("revision") or 0)
    if claimed_revision < 1:
        _fail("Synthetic Moonleaf count claim returned no task revision")

    observation_lines: list[dict[str, str]] = []
    for row in task.get("lines") or []:
        item_code = _text(row.get("item_id"))
        factor = Decimal(str(row.get("conversion_factor")))
        total = Decimal(
            str(
                runtime_frappe.db.get_value(
                    "Bin",
                    {"item_code": item_code, "warehouse": profile.warehouse},
                    "actual_qty",
                )
                or 0
            )
        )
        full_packs = total // factor
        loose = total - (full_packs * factor)
        observation_lines.append(
            {
                "item_id": item_code,
                "stock_uom": _text(row.get("stock_uom")),
                "purchase_uom": _text(row.get("purchase_uom")),
                "conversion_factor": _decimal_text(factor),
                "full_packs": _decimal_text(full_packs),
                "loose_quantity": _decimal_text(loose),
                "total_quantity": _decimal_text(total),
            }
        )
    observation_id = f"{AUTHORITY_PREFIX}MOONLEAF-OBS-{task_name}"
    observation_payload: dict[str, Any] = {
        "schema_version": "inventory-command-v1",
        "command_id": f"{AUTHORITY_PREFIX}MOONLEAF-COUNT-{task_name}",
        "task_type": "submit_count_observation",
        "device_id": device_id,
        "device_config_version": int(device.config_version or 0),
        "staff_user": staff_user,
        "employee": employee,
        "company": profile.company,
        "outlet": profile.name,
        "warehouse": profile.warehouse,
        "source_document": task_name,
        "source_revision": claimed_revision,
        "observation_id": observation_id,
        "task_id": task_name,
        "task_revision": claimed_revision,
        "actor_id": staff_user,
        "stock_watermark": _text(claimed_task.get("stock_watermark")),
        "observed_at": observed_at,
        "lines": observation_lines,
    }
    observation_payload["payload_hash"] = _payload_hash(observation_payload)
    observation = submit_count_observation(
        device_id=device_id, payload=observation_payload
    )

    confirmation_command = f"{AUTHORITY_PREFIX}MOONLEAF-CONFIRM-{task_name}"
    approval_context = {
        "task_id": task_name,
        "task_revision": claimed_revision,
        "observation_id": observation_id,
        "warehouse": profile.warehouse,
    }
    scope = _resolve_manager_approval_scope(
        {
            "action": "inventory_count_reconciliation",
            "device_id": device_id,
            "staff_id": staff_user,
            "shift_id": task_name,
            "resource_id": observation_id,
            "idempotency_key": confirmation_command,
            "context": approval_context,
            "amount_sen": 0,
        }
    )
    manager_id = authorize_manager_for_device(
        device,
        manager_id=None,
        manager_pin=None,
        admin_approval=True,
        action="inventory_count_reconciliation",
    )
    approval = generate_manager_approval_token(
        device_id=scope["device_id"],
        staff_id=scope["staff_id"],
        action=scope["action"],
        manager_id=manager_id,
        shift_id=scope["shift_id"],
        resource_id=scope["resource_id"],
        amount_sen=scope["amount_sen"],
        context_hash=scope["context_hash"],
        authorization_mode="system_manager",
    )
    confirmation_payload: dict[str, Any] = {
        "schema_version": "inventory-command-v1",
        "command_id": confirmation_command,
        "task_type": "confirm_count_reconciliation",
        "observation_id": observation_id,
        "task_id": task_name,
        "task_revision": claimed_revision,
        "warehouse": profile.warehouse,
        "staff_user": staff_user,
        "employee": employee,
        "company": profile.company,
        "outlet": profile.name,
        "device_id": device_id,
        "device_config_version": int(device.config_version or 0),
        "source_document": task_name,
        "source_revision": claimed_revision,
        "observed_at": observed_at,
        "manager_approval_token": approval["token"],
    }
    confirmation_payload["payload_hash"] = _payload_hash(confirmation_payload)
    confirmation = confirm_count_reconciliation(
        device_id=device_id, payload=confirmation_payload
    )
    return {
        "schemaVersion": 1,
        "status": "passed",
        "producer": "kopos_connector.acceptance.restored_commissioning.complete_local_count_v1",
        "evidenceLevel": "real_integration",
        "syntheticDatabase": True,
        "fictionalCafe": CAFE_NAME,
        "connectorVersion": installed_version,
        "erpArtifactSha256": artifact_sha256,
        "task": task_name,
        "lineCount": len(observation_lines),
        "claim": claim,
        "observation": observation,
        "confirmation": confirmation,
    }


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def complete_local_buying_v1(
    erp_artifact_sha256: str,
    expected_connector_version: str,
) -> dict[str, Any]:
    """Create a submitted manual request and a review-only Draft Moonleaf PO."""

    artifact_sha256 = _required_sha256(erp_artifact_sha256, "erp_artifact_sha256")
    expected_version = _required_text(expected_connector_version, "expected_connector_version")
    installed_version = metadata.version("kopos_connector")
    if installed_version != expected_version:
        _fail("Installed connector version does not match the local image binding")
    runtime_frappe = _require_frappe()
    company = _discover_company()["name"]
    warehouse = _discover_commissioning_warehouse(company)
    required_date = datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date() + timedelta(days=2)
    request_remark = f"{AUTHORITY_PREFIX}MOONLEAF fictional manual replenishment rehearsal"
    order_remark = f"{AUTHORITY_PREFIX}MOONLEAF fictional Draft PO — review only, never sent"
    request_fingerprint = hashlib.sha256(request_remark.encode("utf-8")).hexdigest()
    order_fingerprint = hashlib.sha256(order_remark.encode("utf-8")).hexdigest()
    request_name = runtime_frappe.db.get_value(
        "Material Request",
        {"custom_kopos_inventory_fingerprint": request_fingerprint, "docstatus": 1},
        "name",
    )
    purchase_specs = (
        (f"{AUTHORITY_PREFIX}MOONLEAF-MATCHA-POWDER", Decimal("2")),
        (f"{AUTHORITY_PREFIX}MOONLEAF-FRESH-MILK", Decimal("12")),
        (f"{AUTHORITY_PREFIX}MOONLEAF-16OZ-CUP", Decimal("4")),
    )
    ingredients = {spec["code"]: spec for spec in INGREDIENTS}
    if not request_name:
        request = runtime_frappe.new_doc("Material Request")
        request.material_request_type = "Purchase"
        request.company = company
        request.schedule_date = required_date
        _set_if_present(request, "remarks", request_remark)
        _set_if_present(request, "custom_kopos_inventory_fingerprint", request_fingerprint)
        for item_code, packs in purchase_specs:
            spec = ingredients[item_code]
            request.append(
                "items",
                {
                    "item_code": item_code,
                    "qty": packs,
                    "uom": spec["purchase_uom"],
                    "stock_uom": spec["stock_uom"],
                    "conversion_factor": spec["conversion"],
                    "warehouse": warehouse,
                    "schedule_date": required_date,
                },
            )
        request.insert(ignore_permissions=True)
        request.submit()
        request_name = request.name
    request = runtime_frappe.get_doc("Material Request", request_name)

    order_name = runtime_frappe.db.get_value(
        "Purchase Order",
        {"custom_kopos_inventory_fingerprint": order_fingerprint, "docstatus": 0},
        "name",
    )
    quotation_name = runtime_frappe.db.get_value(
        "Supplier Quotation",
        {"supplier": SUPPLIER, "docstatus": 1},
        "name",
    )
    if not quotation_name:
        _fail("Moonleaf Draft PO needs the submitted fictional Supplier Quotation")
    quotation = runtime_frappe.get_doc("Supplier Quotation", quotation_name)
    quotation_items = {
        _text(_value(row, "item_code")): row
        for row in (getattr(quotation, "items", None) or [])
    }
    if not order_name:
        order = runtime_frappe.new_doc("Purchase Order")
        order.company = company
        order.supplier = SUPPLIER
        order.currency = quotation.currency
        order.schedule_date = required_date
        _set_if_present(order, "remarks", order_remark)
        _set_if_present(order, "custom_kopos_inventory_fingerprint", order_fingerprint)
        _set_if_present(order, "supplier_quotation", quotation_name)
        for request_row in request.items:
            item_code = _text(_value(request_row, "item_code"))
            spec = ingredients[item_code]
            quote_row = quotation_items.get(item_code)
            if quote_row is None:
                _fail(f"Moonleaf quotation has no authority for {item_code}")
            order.append(
                "items",
                {
                    "item_code": item_code,
                    "qty": _value(request_row, "qty"),
                    "uom": spec["purchase_uom"],
                    "stock_uom": spec["stock_uom"],
                    "conversion_factor": spec["conversion"],
                    "rate": spec["purchase_rate"],
                    "warehouse": warehouse,
                    "schedule_date": required_date,
                    "material_request": request.name,
                    "material_request_item": request_row.name,
                    "supplier_quotation": quotation_name,
                    "supplier_quotation_item": quote_row.name,
                },
            )
        order.insert(ignore_permissions=True)
        order_name = order.name
    runtime_frappe.db.commit()
    return {
        "schemaVersion": 1,
        "status": "passed",
        "producer": "kopos_connector.acceptance.restored_commissioning.complete_local_buying_v1",
        "evidenceLevel": "real_integration",
        "syntheticDatabase": True,
        "fictionalCafe": CAFE_NAME,
        "connectorVersion": installed_version,
        "erpArtifactSha256": artifact_sha256,
        "materialRequest": request_name,
        "materialRequestStatus": "Submitted",
        "purchaseOrder": order_name,
        "purchaseOrderStatus": "Draft — review only",
        "supplierQuotation": quotation_name,
        "lineCount": len(purchase_specs),
        "outboundAction": "none",
    }


def _normalize_local_fixture_items() -> list[str]:
    """Keep the disposable smoke prerequisites from blocking menu readiness.

    The synthetic real-service site contains two intentionally generic records
    created by the reliability smoke suite.  They predate the item-role
    checklist, so leaving them unclassified makes every later fictional cafe
    look incomplete.  This local-only normalization is deliberately absent
    from ``run_v1``: restored business data must always be classified by its
    owner instead of being inferred by an acceptance producer.
    """

    runtime_frappe = _require_frappe()
    normalized: list[str] = []
    fixture_roles = {
        "QA Prepared Batch": ("Prep Item", False),
        "Strawberry Matcha Latte": ("Sellable Drink", True),
    }
    for display_name, (role, saleable) in fixture_roles.items():
        fixture_items = runtime_frappe.get_all(
            "Item",
            filters={"item_name": display_name},
            fields=["name"],
            limit_page_length=10,
        )
        for row in fixture_items:
            item_name = _text(row.get("name"))
            if not item_name:
                continue
            item = runtime_frappe.get_doc("Item", item_name)
            _set_if_present(item, "custom_fb_item_role", role)
            item.is_sales_item = 1 if saleable else 0
            if saleable:
                recipe_name = runtime_frappe.db.get_value(
                    "FB Recipe",
                    {"sellable_item": item_name, "status": "Published"},
                    "name",
                )
                if recipe_name:
                    _set_if_present(item, "custom_fb_recipe_required", 1)
                    _set_if_present(item, "custom_fb_default_recipe", recipe_name)
            item.save(ignore_permissions=True)
            normalized.append(item_name)
    if runtime_frappe.db.exists("Item Group", "KoPOS Beverages"):
        smoke_group = runtime_frappe.get_doc("Item Group", "KoPOS Beverages")
        _set_if_present(smoke_group, "custom_kopos_count_frequency", "Off")
        _set_if_present(smoke_group, "custom_kopos_count_weekdays", "")
        smoke_group.save(ignore_permissions=True)
        normalized.append("Item Group:KoPOS Beverages")
    return normalized


def _completion_checks(
    *,
    ingredient_names: list[str],
    boms: dict[str, str],
    recipes: list[str],
    modifiers: dict[str, dict[str, Any]],
    promotions: list[str],
    purchasing: dict[str, str],
    price_list: str,
    pos_profile: str,
    opening_reconciliation: str,
    warehouse: str,
    prepared_stock: dict[str, dict[str, str]],
) -> dict[str, bool]:
    runtime_frappe = _require_frappe()
    checks: dict[str, bool] = {}

    for spec in INGREDIENTS:
        code = spec["code"]
        row = runtime_frappe.db.get_value(
            "Item",
            code,
            ["is_stock_item", "stock_uom", "purchase_uom", "has_batch_no"],
            as_dict=True,
        ) or {}
        checks[f"{code}:is_stock_item"] = bool(row.get("is_stock_item"))
        checks[f"{code}:stock_uom"] = _text(row.get("stock_uom")) == spec["stock_uom"]
        checks[f"{code}:purchase_uom"] = _text(row.get("purchase_uom")) == spec["purchase_uom"]
        conversion = runtime_frappe.db.get_value(
            "UOM Conversion Detail",
            {"parent": code, "uom": spec["purchase_uom"]},
            "conversion_factor",
        )
        checks[f"{code}:exact_conversion"] = (
            conversion is not None
            and _proof_decimal_text(conversion, "conversion factor")
            == _proof_decimal_text(spec["conversion"], "conversion factor")
        )

    checks["ingredient_manifest_is_complete"] = len(ingredient_names) == len(INGREDIENTS)
    commissioned_group = _text(
        runtime_frappe.db.get_value("Item", INGREDIENTS[0]["code"], "item_group")
    )
    checks["stock_group_has_explicit_daily_count_cadence"] = bool(commissioned_group) and (
        _text(
            runtime_frappe.db.get_value(
                "Item Group", commissioned_group, "custom_kopos_count_frequency"
            )
        )
        == "Daily"
    )
    for spec in PREPARED_COMPONENTS:
        code = spec["code"]
        bom = boms.get(code)
        checks[f"{code}:is_stocked"] = bool(
            runtime_frappe.db.get_value("Item", code, "is_stock_item")
        )
        checks[f"{code}:has_submitted_bom"] = bool(bom) and (
            int(runtime_frappe.db.get_value("BOM", bom, "docstatus") or 0) == 1
        )
        checks[f"{code}:tracks_batches"] = bool(
            runtime_frappe.db.get_value("Item", code, "has_batch_no")
        )
        checks[f"{code}:count_uom_is_explicit"] = (
            _text(runtime_frappe.db.get_value("Item", code, "purchase_uom"))
            == "Millilitre"
        )
        checks[f"{code}:bom_inputs_match"] = bool(bom) and len(
            runtime_frappe.get_all(
                "BOM Item", filters={"parent": bom}, fields=["name"], limit_page_length=0
            )
        ) == len(spec["inputs"])

    checks["supplier_quotation_is_submitted"] = (
        int(
            runtime_frappe.db.get_value(
                "Supplier Quotation", purchasing["quotation"], "docstatus"
            )
            or 0
        )
        == 1
    )
    checks["supplier_quotation_is_the_single_current_authority"] = (
        len(
            runtime_frappe.get_all(
                "Supplier Quotation",
                filters={"supplier": SUPPLIER, "docstatus": 1},
                fields=["name"],
                limit_page_length=0,
            )
        )
        == 1
    )
    checks["opening_stock_reconciliation_is_submitted"] = (
        int(
            runtime_frappe.db.get_value(
                "Stock Reconciliation", opening_reconciliation, "docstatus"
            )
            or 0
        )
        == 1
    )
    opening_rows = runtime_frappe.get_all(
        "Stock Reconciliation Item",
        filters={"parent": opening_reconciliation},
        fields=["item_code", "warehouse", "qty", "valuation_rate", "batch_no"],
        limit_page_length=0,
    )
    checks["opening_stock_contains_every_ingredient"] = {
        _text(_value(row, "item_code")) for row in opening_rows
    } == {spec["code"] for spec in INGREDIENTS}
    checks["opening_stock_uses_the_outlet_warehouse"] = all(
        _text(_value(row, "warehouse")) == warehouse
        for row in opening_rows
    )
    checks["opening_stock_batch_items_have_batches"] = all(
        not next(spec for spec in INGREDIENTS if spec["code"] == _text(_value(row, "item_code")))["has_batch_no"]
        or bool(_text(_value(row, "batch_no")))
        for row in opening_rows
    )
    checks["prepared_stock_uses_standard_documents"] = set(prepared_stock) == {
        spec["code"] for spec in PREPARED_COMPONENTS
    } and all(
        int(runtime_frappe.db.get_value("Work Order", docs["work_order"], "docstatus") or 0) == 1
        and int(runtime_frappe.db.get_value("Stock Entry", docs["stock_entry"], "docstatus") or 0) == 1
        and _text(runtime_frappe.db.get_value("Stock Entry", docs["stock_entry"], "purpose"))
        == "Manufacture"
        for docs in prepared_stock.values()
    )
    checks["prepared_stock_is_available_at_the_outlet"] = all(
        Decimal(
            str(
                runtime_frappe.db.get_value(
                    "Bin", {"item_code": spec["code"], "warehouse": warehouse}, "actual_qty"
                )
                or 0
            )
        )
        >= spec["batch_qty"]
        for spec in PREPARED_COMPONENTS
    )

    checks["selling_price_list_is_enabled"] = bool(
        runtime_frappe.db.get_value("Price List", price_list, "enabled")
    )
    profile_row = runtime_frappe.db.get_value(
        "POS Profile",
        pos_profile,
        ["warehouse", "selling_price_list", "disabled"],
        as_dict=True,
    ) or {}
    checks["moonleaf_pos_profile_uses_exact_outlet_and_price_list"] = (
        _text(profile_row.get("warehouse")) == warehouse
        and _text(profile_row.get("selling_price_list")) == price_list
        and not bool(profile_row.get("disabled"))
    )
    checks["every_sellable_has_a_price"] = all(
        bool(
            runtime_frappe.db.get_value(
                "Item Price", {"item_code": spec["code"], "price_list": price_list}, "name"
            )
        )
        for spec in SELLABLES
    )
    checks["every_sellable_has_one_active_recipe"] = len(recipes) == len(SELLABLES) and all(
        len(
            runtime_frappe.get_all(
                "FB Recipe",
                filters={"sellable_item": spec["code"], "status": "Active"},
                fields=["name"],
                limit_page_length=0,
            )
        )
        == 1
        for spec in SELLABLES
    )
    for spec, recipe_name in zip(SELLABLES, recipes):
        recipe_doc = runtime_frappe.get_doc("FB Recipe", recipe_name)
        components = list(getattr(recipe_doc, "components", None) or [])
        checks[f"{spec['code']}:component_count"] = len(components) == len(spec["components"])
        checks[f"{spec['code']}:canonical_hash"] = len(
            _text(getattr(recipe_doc, "canonical_hash", None))
        ) == 64
        checks[f"{spec['code']}:all_components_affect_stock"] = all(
            int(_value(row, "affects_stock") or 0) == 1 for row in components
        )
        checks[f"{spec['code']}:every_component_is_stocked"] = all(
            bool(
                runtime_frappe.db.get_value(
                    "Item", _text(_value(row, "item")), "is_stock_item"
                )
            )
            for row in components
        )
    prepared_recipe_items = {
        component["item"]
        for spec in SELLABLES
        for component in spec["components"]
        if component["component_type"] == "Prep Item"
    }
    checks["recipes_consume_prepared_stock"] = prepared_recipe_items == {
        PREPARED_ITEM,
        MATCHA_CONCENTRATE_ITEM,
    }
    checks["recipes_do_not_expand_prepared_boms"] = all(
        _text(_value(row, "item")) not in {
            input_item
            for prep in PREPARED_COMPONENTS
            for input_item, _qty in prep["inputs"]
        }
        or _text(_value(row, "item")) in {
            component["item"]
            for spec in SELLABLES
            for component in spec["components"]
        }
        for recipe_name in recipes
        for row in runtime_frappe.get_doc("FB Recipe", recipe_name).components
    )
    checks["modifier_groups_are_complete"] = len(modifiers) == len(MODIFIER_GROUPS)
    checks["modifier_effects_are_frozen"] = all(
        len(getattr(runtime_frappe.get_doc("FB Recipe", recipe_name), "recipe_modifier_effects", None) or [])
        > 0
        for recipe_name in recipes
    )
    promotion_states = [
        runtime_frappe.db.get_value(
            "KoPOS Promotion", name, ["is_active", "economics_status"], as_dict=True
        )
        or {}
        for name in promotions
    ]
    checks["promotions_are_economics_ready_or_reviewable"] = (
        len(promotions) == len(PROMOTIONS)
        and all(
            (
                not bool(state.get("is_active"))
                and _text(state.get("economics_status")) == "Not checked"
            )
            or (
                bool(state.get("is_active"))
                and _text(state.get("economics_status")) in {"Ready", "Override approved"}
            )
            for state in promotion_states
        )
    )
    return checks


def read_v1() -> dict[str, Any]:
    """Read-only view of what has been commissioned, for evidence bundling."""

    runtime_frappe = _require_frappe()
    return {
        "schemaVersion": 2,
        "status": "passed",
        "contractId": CONTRACT_ID,
        "producer": PRODUCER,
        "readOnly": True,
        "fictionalCafe": CAFE_NAME,
        "commissionedIngredientCount": len(
            runtime_frappe.get_all(
                "Item",
                filters={"name": ["like", f"{AUTHORITY_PREFIX}%"], "is_stock_item": 1},
                fields=["name"],
                limit_page_length=0,
            )
        ),
        "commissionedBomCount": len(
            runtime_frappe.get_all(
                "BOM",
                filters={"item": ["like", f"{AUTHORITY_PREFIX}%"], "docstatus": 1},
                fields=["name"],
                limit_page_length=0,
            )
        ),
        "commissionedRecipeCount": len(
            runtime_frappe.get_all(
                "FB Recipe",
                filters={"recipe_code": ["like", f"{AUTHORITY_PREFIX}MOONLEAF-%"]},
                fields=["name"],
                limit_page_length=0,
            )
        ),
        "commissionedPromotionCount": len(
            runtime_frappe.get_all(
                "KoPOS Promotion",
                filters={"name": ["like", f"{AUTHORITY_PREFIX}MOONLEAF-%"]},
                fields=["name"],
                limit_page_length=0,
            )
        ),
    }
