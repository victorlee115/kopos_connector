"""Commission a complete hypothetical ingredient master on restored data.

The restored backup carries no ingredient master, no BOMs, and no suppliers, so
the Phase 4 authoring path has never been exercised end to end.  This producer
commissions a deliberately hypothetical, clearly prefixed set of documents
inside the isolated rehearsal site only -- the sanctioned place for fixture
suppliers and quotations -- and proves the authoring path produces
inventory-ready data.

It commissions nothing in production and converts no existing Item.  Real
commissioning stays director-led and physical.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from kopos_connector.acceptance.restored_inventory_acceptance import (
    AUTHORITY_PREFIX,
    _discover_company,
    _discover_item_group,
    _discover_warehouse,
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
CONTRACT_ID = "kopos.restored-commissioning.v1"

SUPPLIER = f"{AUTHORITY_PREFIX}SUPPLIER"
PREPARED_ITEM = f"{AUTHORITY_PREFIX}COLD-FOAM"
PREPARED_BOM_QTY = Decimal("2")
PREPARED_MIN_READY_QTY = Decimal("1")
PREPARED_LEAD_MINUTES = 20

# Stock UOM, purchase UOM, and the exact whole-unit conversion between them.
# Every conversion is exact by construction: no ingredient may rely on an
# inferred or rounded factor.
INGREDIENTS: tuple[dict[str, Any], ...] = (
    {
        "code": f"{AUTHORITY_PREFIX}MILK",
        "stock_uom": "Litre",
        "purchase_uom": "Box",
        "conversion": Decimal("12"),
        "shelf_life_days": 7,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{AUTHORITY_PREFIX}MATCHA",
        "stock_uom": "Gram",
        "purchase_uom": "Box",
        "conversion": Decimal("1000"),
        "shelf_life_days": 180,
        "has_batch_no": 1,
        "role": "Ingredient",
    },
    {
        "code": f"{AUTHORITY_PREFIX}CUP",
        "stock_uom": "Nos",
        "purchase_uom": "Box",
        "conversion": Decimal("50"),
        "shelf_life_days": 0,
        "has_batch_no": 0,
        "role": "Packaging",
    },
)

# One serving of the commissioned drink, expressed in stock UOM.
RECIPE_CODE = f"{AUTHORITY_PREFIX}COMMISSIONED-RECIPE"
SELLABLE_ITEM = f"{AUTHORITY_PREFIX}COMMISSIONED-DRINK"
RECIPE_COMPONENTS: tuple[tuple[str, str], ...] = (
    (f"{AUTHORITY_PREFIX}MILK", "0.2"),
    (f"{AUTHORITY_PREFIX}MATCHA", "4"),
    (f"{AUTHORITY_PREFIX}CUP", "1"),
    (PREPARED_ITEM, "0.05"),
)


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


def _commission_ingredient(spec: dict[str, Any], *, item_group: str, warehouse: str) -> str:
    runtime_frappe = _require_frappe()
    _ensure_uom(spec["stock_uom"])
    _ensure_uom(spec["purchase_uom"])
    item = _ensure_item(
        item_code=spec["code"],
        item_group=item_group,
        stock_uom=spec["stock_uom"],
        is_stock_item=1,
        role=spec["role"],
    )
    _set_if_present(item, "purchase_uom", spec["purchase_uom"])
    _set_if_present(item, "has_batch_no", spec["has_batch_no"])
    if spec["shelf_life_days"]:
        _set_if_present(item, "shelf_life_in_days", spec["shelf_life_days"])
    _set_if_present(item, "is_purchase_item", 1)
    if getattr(item, "_dirty", True):
        item.save(ignore_permissions=True)
    _ensure_conversion(spec["code"], spec["purchase_uom"], spec["conversion"])
    _ensure_default_warehouse(spec["code"], warehouse)
    return item.name


def _ensure_default_warehouse(item_code: str, warehouse: str) -> None:
    runtime_frappe = _require_frappe()
    if not runtime_frappe.db.exists("DocType", "Item Default"):
        return
    item = runtime_frappe.get_doc("Item", item_code)
    defaults = list(getattr(item, "item_defaults", None) or [])
    if defaults:
        return
    company = _text(runtime_frappe.db.get_value("Warehouse", warehouse, "company"))
    if not company:
        return
    item.append(
        "item_defaults", {"company": company, "default_warehouse": warehouse}
    )
    item.save(ignore_permissions=True)


def _commission_prepared_component(*, item_group: str, warehouse: str, company: str) -> str:
    """The prepared component is stocked; its BOM consumes raw ingredients.

    A sale consumes the prepared stock Item.  The BOM is never recursively
    consumed again during the sale -- manufacturing is what creates the stock.
    """

    runtime_frappe = _require_frappe()
    _ensure_uom("Litre")
    item = _ensure_item(
        item_code=PREPARED_ITEM,
        item_group=item_group,
        stock_uom="Litre",
        is_stock_item=1,
        role="Prep Item",
    )
    _set_if_present(item, "has_batch_no", 1)
    _set_if_present(item, "shelf_life_in_days", 1)
    _set_if_present(item, "is_purchase_item", 0)
    item.save(ignore_permissions=True)
    _ensure_default_warehouse(PREPARED_ITEM, warehouse)

    existing = runtime_frappe.db.get_value(
        "BOM", {"item": PREPARED_ITEM, "docstatus": 1, "is_active": 1}, "name"
    )
    if existing:
        return _text(existing)

    bom = runtime_frappe.new_doc("BOM")
    bom.item = PREPARED_ITEM
    bom.company = company
    bom.quantity = PREPARED_BOM_QTY
    _set_if_present(bom, "uom", "Litre")
    _set_if_present(bom, "is_active", 1)
    _set_if_present(bom, "is_default", 1)
    _set_if_present(bom, "with_operations", 0)
    _set_if_present(bom, "custom_kopos_automatic_batch_preparation", 1)
    _set_if_present(bom, "custom_kopos_batch_qty", PREPARED_BOM_QTY)
    _set_if_present(bom, "custom_kopos_minimum_ready_qty", PREPARED_MIN_READY_QTY)
    _set_if_present(bom, "custom_kopos_preparation_lead_minutes", PREPARED_LEAD_MINUTES)
    bom.append(
        "items",
        {
            "item_code": f"{AUTHORITY_PREFIX}MILK",
            "qty": Decimal("2"),
            "uom": "Litre",
            "stock_uom": "Litre",
            "conversion_factor": 1,
        },
    )
    bom.insert(ignore_permissions=True)
    bom.submit()
    return bom.name


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
        return {"supplier": SUPPLIER, "quotation": _text(existing)}

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
                "rate": 10,
                "warehouse": warehouse,
                "schedule_date": runtime_frappe.utils.add_days(
                    runtime_frappe.utils.nowdate(), 3
                ),
            },
        )
    quotation.insert(ignore_permissions=True)
    quotation.submit()
    return {"supplier": SUPPLIER, "quotation": quotation.name}


def _discover_supplier_group() -> str | None:
    runtime_frappe = _require_frappe()
    rows = runtime_frappe.get_all(
        "Supplier Group", fields=["name"], order_by="name asc", limit_page_length=1
    )
    return _text(_value(rows[0], "name")) if rows else None


def _commission_recipe(*, company: str, item_group: str) -> str:
    """Author a real multi-component recipe, including a prepared component."""

    runtime_frappe = _require_frappe()
    _ensure_item(
        item_code=SELLABLE_ITEM,
        item_group=item_group,
        stock_uom="Nos",
        is_stock_item=0,
        role="Sellable Drink",
    )
    existing = runtime_frappe.db.get_value("FB Recipe", {"recipe_code": RECIPE_CODE}, "name")
    if existing:
        recipe = runtime_frappe.get_doc("FB Recipe", existing)
        if _text(getattr(recipe, "status", None)) != "Active":
            _fail(f"Commissioned recipe {recipe.name} is not Active")
        return recipe.name

    recipe = runtime_frappe.new_doc("FB Recipe")
    recipe.recipe_code = RECIPE_CODE
    recipe.recipe_name = RECIPE_CODE
    recipe.sellable_item = SELLABLE_ITEM
    recipe.company = company
    recipe.recipe_type = "Finished Drink"
    recipe.status = "Active"
    recipe.version_no = 1
    recipe.yield_qty = 1
    recipe.yield_uom = "Nos"
    recipe.default_serving_qty = 1
    recipe.default_serving_uom = "Nos"
    recipe.effective_from = runtime_frappe.utils.now_datetime() - timedelta(minutes=10)
    for item_code, qty in RECIPE_COMPONENTS:
        stock_uom = _text(runtime_frappe.db.get_value("Item", item_code, "stock_uom"))
        recipe.append(
            "components",
            {
                "item": item_code,
                "component_type": "Ingredient",
                "qty": Decimal(qty),
                "uom": stock_uom,
                "stock_qty": Decimal(qty),
                "stock_uom": stock_uom,
                "stock_conversion_factor": 1,
                "affects_stock": 1,
                "affects_cogs": 1,
                "loss_factor_pct": 0,
            },
        )
    recipe.insert(ignore_permissions=True)
    return recipe.name


def run_v1() -> dict[str, Any]:
    """Commission the hypothetical master and prove it is inventory-ready."""

    runtime_frappe = _require_frappe()
    company = _discover_company()
    warehouse = _discover_warehouse(company["name"])
    item_group = _discover_item_group()

    ingredient_names = [
        _commission_ingredient(spec, item_group=item_group, warehouse=warehouse)
        for spec in INGREDIENTS
    ]
    bom = _commission_prepared_component(
        item_group=item_group, warehouse=warehouse, company=company["name"]
    )
    purchasing = _commission_supplier_and_quotation(
        company=company["name"], warehouse=warehouse
    )
    recipe = _commission_recipe(company=company["name"], item_group=item_group)

    checks = _completion_checks(
        ingredient_names=ingredient_names, bom=bom, recipe=recipe, purchasing=purchasing
    )
    failed = sorted(name for name, ok in checks.items() if not ok)
    if failed:
        _fail("Commissioning completion checklist failed: " + ", ".join(failed))

    runtime_frappe.db.commit()
    return {
        "schemaVersion": 1,
        "status": "passed",
        "contractId": CONTRACT_ID,
        "producer": PRODUCER,
        "evidenceLevel": "restored_production_data",
        "authorityPrefix": AUTHORITY_PREFIX,
        "commissionedIngredients": sorted(ingredient_names),
        "preparedComponent": PREPARED_ITEM,
        "preparedComponentBom": bom,
        "commissionedRecipe": recipe,
        "supplier": purchasing["supplier"],
        "supplierQuotation": purchasing["quotation"],
        "completionChecks": dict(sorted(checks.items())),
        "commissioningAssertions": len(checks),
    }


def _completion_checks(
    *, ingredient_names: list[str], bom: str, recipe: str, purchasing: dict[str, str]
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

    checks["prepared_component_is_stocked"] = bool(
        runtime_frappe.db.get_value("Item", PREPARED_ITEM, "is_stock_item")
    )
    checks["prepared_component_has_submitted_bom"] = bool(bom) and (
        int(runtime_frappe.db.get_value("BOM", bom, "docstatus") or 0) == 1
    )
    checks["prepared_component_tracks_batches"] = bool(
        runtime_frappe.db.get_value("Item", PREPARED_ITEM, "has_batch_no")
    )

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

    recipe_doc = runtime_frappe.get_doc("FB Recipe", recipe)
    components = list(getattr(recipe_doc, "components", None) or [])
    checks["recipe_is_active"] = _text(getattr(recipe_doc, "status", None)) == "Active"
    checks["recipe_has_every_component"] = len(components) == len(RECIPE_COMPONENTS)
    checks["recipe_components_all_affect_stock"] = all(
        int(_value(row, "affects_stock") or 0) == 1 for row in components
    )
    checks["recipe_consumes_the_prepared_component"] = any(
        _text(_value(row, "item")) == PREPARED_ITEM for row in components
    )
    # The sale consumes prepared stock; it must never expand the BOM again.
    checks["recipe_does_not_expand_the_prepared_bom"] = not any(
        _text(_value(row, "item")) == f"{AUTHORITY_PREFIX}MILK"
        and _proof_decimal_text(_value(row, "qty"), "component qty") == "2"
        for row in components
    )
    checks["every_recipe_component_is_a_stock_item"] = all(
        bool(runtime_frappe.db.get_value("Item", _text(_value(row, "item")), "is_stock_item"))
        for row in components
    )
    return checks


def read_v1() -> dict[str, Any]:
    """Read-only view of what has been commissioned, for evidence bundling."""

    runtime_frappe = _require_frappe()
    return {
        "schemaVersion": 1,
        "status": "passed",
        "contractId": CONTRACT_ID,
        "producer": PRODUCER,
        "readOnly": True,
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
    }
