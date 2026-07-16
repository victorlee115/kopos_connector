from __future__ import annotations

from typing import Any

import frappe

from kopos_connector.smoke import (
    DEMO_DRINK_ITEM,
    DEMO_DRINK_NAME,
    SMOKE_DEVICE_ID,
    _ensure_smoke_base_data,
    _ensure_kopos_device,
    _get_demo_currency,
    _get_demo_recipe_reference,
    set_demo_ingredient_quantities,
)


TEST_PREFIX = "KOPOS-BROAD-TEST"


def ensure_canonical_test_base(*, replenish_stock: bool = False) -> dict[str, Any]:
    """Return an idempotent, production-shaped ERP fixture for real-Frappe tests."""

    frappe.set_user("Administrator")
    base = _ensure_smoke_base_data()
    _ensure_kopos_device(
        device_id=SMOKE_DEVICE_ID,
        pos_profile=base["pos_profile"],
        company=base["company"],
    )
    if replenish_stock:
        set_demo_ingredient_quantities()
    recipe = _get_demo_recipe_reference(base["company"])
    return {
        **base,
        "currency": _get_demo_currency(base["company"]),
        "device_id": SMOKE_DEVICE_ID,
        "item_code": DEMO_DRINK_ITEM,
        "item_name": DEMO_DRINK_NAME,
        "recipe": recipe["name"],
        "recipe_version": recipe["version_no"],
    }


def create_open_test_shift(
    *,
    prefix: str = TEST_PREFIX,
    device_id: str = SMOKE_DEVICE_ID,
    opening_float: float = 3.0,
    replenish_stock: bool = False,
) -> Any:
    base = ensure_canonical_test_base(replenish_stock=replenish_stock)
    shift = frappe.new_doc("FB Shift")
    shift.shift_code = f"{prefix}-SHIFT-{frappe.generate_hash(length=10)}"
    shift.device_id = device_id
    shift.staff_id = frappe.session.user
    shift.warehouse = base["warehouse"]
    shift.company = base["company"]
    shift.opening_float = opening_float
    shift.status = "Open"
    shift.insert(ignore_permissions=True)
    return shift


def build_sen_v1_sale_payload(
    shift: Any,
    *,
    prefix: str = TEST_PREFIX,
    order_id: str | None = None,
    idempotency_key: str | None = None,
    display_number: str = "T001",
    quantity: int = 1,
    unit_price_sen: int = 1200,
    tendered_amount_sen: int | None = None,
) -> dict[str, Any]:
    recipe = _get_demo_recipe_reference(shift.company)
    total_sen = quantity * unit_price_sen
    tendered_sen = (
        total_sen if tendered_amount_sen is None else tendered_amount_sen
    )
    return {
        "money_contract_version": "sen_v1",
        "order_id": order_id
        or f"{prefix}-ORDER-{frappe.generate_hash(length=10)}",
        "idempotency_key": idempotency_key
        or f"{prefix}-IDEMP-{frappe.generate_hash(length=16)}",
        "device_id": shift.device_id,
        "shift_id": shift.name,
        "staff_id": shift.staff_id,
        "warehouse": shift.warehouse,
        "company": shift.company,
        "currency": _get_demo_currency(shift.company),
        "order": {
            "display_number": display_number,
            "order_type": "takeaway",
            "created_at": frappe.utils.now_datetime().isoformat(),
            "subtotal_sen": total_sen,
            "tax_amount_sen": 0,
            "rounding_adjustment_sen": 0,
            "total_sen": total_sen,
            "items": [
                {
                    "line_id": f"{prefix}-LINE-{frappe.generate_hash(length=10)}",
                    "item_code": DEMO_DRINK_ITEM,
                    "item_name": DEMO_DRINK_NAME,
                    "recipe": recipe["name"],
                    "recipe_version": recipe["version_no"],
                    "qty": quantity,
                    "unit_price_sen": unit_price_sen,
                    "modifier_total_sen": 0,
                    "discount_amount_sen": 0,
                    "line_total_sen": total_sen,
                    "modifiers": [],
                }
            ],
            "payments": [
                {
                    "payment_id": f"{prefix}-PAY-{frappe.generate_hash(length=10)}",
                    "payment_method": "Cash",
                    "amount_sen": total_sen,
                    "tendered_amount_sen": tendered_sen,
                    "change_amount_sen": tendered_sen - total_sen,
                }
            ],
        },
    }


def modifier_doc(values: dict[str, Any] | None = None, **overrides: Any) -> Any:
    """Build a typed transient modifier for pure resolver unit tests only."""

    defaults = {
        "doctype": "FB Modifier",
        "name": f"{TEST_PREFIX}-MOD-{frappe.generate_hash(length=8)}",
        "active": 1,
        "affects_recipe": 1,
        "affects_stock": 1,
        "price_adjustment": 0,
        "display_order": 1,
    }
    defaults.update(values or {})
    defaults.update(overrides)
    return frappe.get_doc(defaults)


def ensure_persisted_test_modifier(
    code: str,
    *,
    kind: str,
    target_item: str | None = None,
    new_item: str | None = None,
    qty_delta: float | None = None,
    qty_uom: str | None = None,
    scale_percent: float | None = None,
    affects_recipe: int = 1,
    affects_stock: int = 1,
) -> str:
    """Create an immutable persisted modifier used by integration-level tests."""

    base = ensure_canonical_test_base()
    recipe = frappe.get_doc("FB Recipe", base["recipe"])
    modifier_group = recipe.allowed_modifier_groups[0].modifier_group
    expected = {
        "modifier_code": code,
        "modifier_name": code.replace("-", " ").title(),
        "modifier_group": modifier_group,
        "kind": kind,
        "target_item": target_item,
        "new_item": new_item,
        "qty_delta": qty_delta,
        "qty_uom": qty_uom,
        "scale_percent": scale_percent,
        "affects_recipe": affects_recipe,
        "affects_stock": affects_stock,
        "active": 1,
        "is_default": 0,
    }
    existing = frappe.db.get_value("FB Modifier", {"modifier_code": code}, "name")
    if existing:
        doc = frappe.get_doc("FB Modifier", existing)
        mismatches = [
            fieldname
            for fieldname, value in expected.items()
            if getattr(doc, fieldname, None) != value
            and not (
                value is None and getattr(doc, fieldname, None) in (None, "", 0, 0.0)
            )
        ]
        if mismatches:
            raise AssertionError(
                f"Persisted test modifier {code} has unexpected immutable fields: "
                + ", ".join(mismatches)
            )
        return doc.name

    doc = frappe.get_doc({"doctype": "FB Modifier", **expected})
    doc.insert(ignore_permissions=True)
    return doc.name
