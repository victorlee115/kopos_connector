# pyright: reportMissingImports=false

"""Contained restored-data proof for one real inventory projection.

This module is deliberately separate from :mod:`restored_catalog_preflight`.
The catalog preflight is read-only; this proof creates a small, deterministic
``INV-ACCEPT-`` fixture in the contained restored site and then exercises the
real recipe compiler, projection log, inventory worker, ERPNext Stock Entry,
Stock Ledger, and GL paths.  It never changes a pre-existing business record.

The fixture is intentionally small: one stock ingredient, one non-stock
sellable, one published FB Recipe, one opening Stock Reconciliation, one
cutover policy, and one submitted FB Order.  Re-running the command reuses the
same authorities and the same order identity.  Existing standard documents are
never deleted or cancelled by the producer.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from typing import Any

try:  # The pure contract validator is also used by mocked tests.
    import frappe
except ImportError:  # pragma: no cover - only outside a Frappe bench.
    frappe = None  # type: ignore[assignment]


PRODUCER = "kopos_connector.acceptance.restored_inventory_acceptance.run_v1"
READ_PRODUCER = "kopos_connector.acceptance.restored_inventory_acceptance.read_v1"
CONTRACT_ID = "kopos.restored-inventory-acceptance.v1"
AUTHORITY_PREFIX = "INV-ACCEPT-"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")

ITEM_INGREDIENT = f"{AUTHORITY_PREFIX}INGREDIENT"
ITEM_SELLABLE = f"{AUTHORITY_PREFIX}SELLABLE"
RECIPE_CODE = f"{AUTHORITY_PREFIX}RECIPE"
POLICY_NAME = f"{AUTHORITY_PREFIX}POLICY"
OPENING_NAME = f"{AUTHORITY_PREFIX}OPENING"
SHIFT_CODE = f"{AUTHORITY_PREFIX}SHIFT"
SHIFT_NAME = f"{AUTHORITY_PREFIX}SHIFT"
DEVICE_ID = f"{AUTHORITY_PREFIX}DEVICE"
ORDER_ID = f"{AUTHORITY_PREFIX}ORDER"
ORDER_IDEMPOTENCY = f"{AUTHORITY_PREFIX}IDEMPOTENCY"
LINE_ID = f"{AUTHORITY_PREFIX}LINE"
PAYMENT_ID = f"{AUTHORITY_PREFIX}PAYMENT"

REQUIRED_PROOF_FIELDS = (
    "contractId",
    "status",
    "resolvedSaleCount",
    "stockEntryCount",
    "stockLedgerEntryCount",
    "glEntryCount",
    "duplicateTargetCount",
)
POSITIVE_COUNT_FIELDS = (
    "resolvedSaleCount",
    "stockEntryCount",
    "stockLedgerEntryCount",
    "glEntryCount",
)


def validate_inventory_acceptance_proof(value: Any) -> dict[str, Any]:
    """Validate the small wire contract without requiring Frappe.

    Keeping this function pure gives release validators and mocked tests one
    authoritative shape check while the producer remains responsible for
    obtaining the counts from real ERPNext tables.
    """

    if not isinstance(value, Mapping):
        raise ValueError("inventory acceptance proof must be an object")
    if value.get("contractId") != CONTRACT_ID:
        raise ValueError("inventory acceptance proof has the wrong contractId")
    if value.get("status") != "passed":
        raise ValueError("inventory acceptance proof must be passed")
    for fieldname in POSITIVE_COUNT_FIELDS:
        field_value = value.get(fieldname)
        if (
            isinstance(field_value, bool)
            or not isinstance(field_value, int)
            or field_value < 1
        ):
            raise ValueError(f"{fieldname} must be a positive integer")
    if value.get("duplicateTargetCount") != 0:
        raise ValueError("duplicateTargetCount must be zero")
    return {fieldname: value.get(fieldname) for fieldname in REQUIRED_PROOF_FIELDS}


def _require_frappe() -> Any:
    if frappe is None:
        raise RuntimeError("restored inventory acceptance requires a Frappe bench")
    return frappe


def _value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if frappe is not None:
        try:
            return str(frappe.utils.cstr(value)).strip()
        except Exception:
            pass
    return str(value).strip()


def _fail(message: str) -> None:
    runtime_frappe = _require_frappe()
    runtime_frappe.throw(message, runtime_frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")


def _required_text(value: Any, fieldname: str) -> str:
    result = _text(value)
    if not result:
        _fail(f"{fieldname} is required")
    return result


def _required_sha256(value: Any, fieldname: str) -> str:
    result = _required_text(value, fieldname)
    if not SHA256_PATTERN.fullmatch(result):
        _fail(f"{fieldname} must be a lowercase SHA-256")
    return result


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _acceptance_datetime(value: Any) -> datetime | None:
    """Normalize Frappe timestamps before comparing sale and cutover time."""

    if value in (None, ""):
        return None
    candidate: Any = value
    if not isinstance(candidate, datetime):
        try:
            parser = getattr(getattr(frappe, "utils", None), "get_datetime", None)
            candidate = parser(value) if callable(parser) else datetime.fromisoformat(
                _text(value).replace("Z", "+00:00")
            )
        except (TypeError, ValueError, OverflowError):
            return None
    if not isinstance(candidate, datetime):
        return None
    if candidate.tzinfo is not None:
        return candidate.astimezone(ZoneInfo("Asia/Kuala_Lumpur")).replace(
            tzinfo=None
        )
    return candidate


def _meta_has(doctype: str, fieldname: str) -> bool:
    runtime_frappe = _require_frappe()
    return bool(runtime_frappe.get_meta(doctype).has_field(fieldname))


def _set_if_present(doc: Any, fieldname: str, value: Any) -> None:
    if value in (None, "") or not _meta_has(doc.doctype, fieldname):
        return
    setattr(doc, fieldname, value)


def _first_row(doctype: str, filters: dict[str, Any], fields: list[str]) -> Any:
    runtime_frappe = _require_frappe()
    rows = runtime_frappe.get_all(
        doctype,
        filters=filters,
        fields=fields,
        order_by="name asc",
        limit_page_length=1,
    )
    return rows[0] if rows else None


def _discover_company() -> dict[str, Any]:
    row = _first_row(
        "Company",
        {},
        ["name", "default_currency", "abbr"],
    )
    if not row:
        _fail("restored database has no Company for the inventory acceptance fixture")
    company = _required_text(_value(row, "name"), "acceptance company")
    currency = _required_text(
        _value(row, "default_currency"), f"Company {company} default currency"
    )
    return {
        "name": company,
        "currency": currency,
        "abbr": _required_text(_value(row, "abbr"), f"Company {company} abbreviation"),
    }


def _discover_warehouse(company: str) -> str:
    row = _first_row(
        "Warehouse",
        {"company": company, "is_group": 0},
        ["name"],
    )
    if not row:
        _fail(
            f"restored database has no leaf Warehouse for Company {company}; "
            "the acceptance fixture will not invent a production warehouse"
        )
    return _required_text(_value(row, "name"), "acceptance warehouse")


def _discover_uom() -> str:
    runtime_frappe = _require_frappe()
    if runtime_frappe.db.exists("UOM", "Nos"):
        return "Nos"
    row = _first_row("UOM", {}, ["name"])
    if not row:
        _fail("restored database has no UOM for the inventory acceptance fixture")
    return _required_text(_value(row, "name"), "acceptance UOM")


def _discover_item_group() -> str:
    row = _first_row("Item Group", {"is_group": 0}, ["name"])
    if not row:
        _fail("restored database has no leaf Item Group for the acceptance fixture")
    return _required_text(_value(row, "name"), "acceptance Item Group")


def _discover_expense_account(company: str) -> str:
    row = _first_row(
        "Account",
        {
            "company": company,
            "root_type": "Expense",
            "is_group": 0,
            "disabled": 0,
        },
        ["name"],
    )
    if not row:
        _fail(
            f"restored database has no enabled leaf Expense Account for {company}; "
            "configure one before running the acceptance proof"
        )
    return _required_text(_value(row, "name"), "acceptance expense account")


def _discover_opening_difference_account(company: str) -> str:
    """Resolve an existing balance-sheet account for an opening stock entry.

    ERPNext requires the Difference Account on an opening Stock Reconciliation
    to be an Asset or Liability account.  It is deliberately not interchangeable
    with the Expense account used by the inventory policy and later stock issue
    entries.  Only an unambiguous existing Temporary account is accepted first;
    a Stock account is the narrow fallback.  If neither authority is unique,
    stop rather than silently assigning an arbitrary production account.
    """

    runtime_frappe = _require_frappe()
    rows = runtime_frappe.get_all(
        "Account",
        filters={
            "company": company,
            "root_type": ["in", ["Asset", "Liability"]],
            "is_group": 0,
            "disabled": 0,
        },
        fields=["name", "account_type"],
        order_by="name asc",
        limit_page_length=0,
    )
    for account_type in ("Temporary", "Stock"):
        matches = [
            _required_text(_value(row, "name"), "opening difference account")
            for row in rows or []
            if _text(_value(row, "account_type")) == account_type
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            _fail(
                f"restored database has multiple enabled {account_type} accounts "
                f"for {company}; opening Difference Account is ambiguous"
            )
    _fail(
        f"restored database has no unique enabled Temporary or Stock account for "
        f"{company}; configure an opening Difference Account before running the "
        "acceptance proof"
    )
    raise AssertionError("frappe.throw must raise")


def _discover_mode_of_payment() -> str:
    runtime_frappe = _require_frappe()
    rows = runtime_frappe.get_all(
        "Mode of Payment",
        filters={"enabled": 1},
        fields=["name", "type"],
        order_by="name asc",
        limit_page_length=0,
    )
    for row in rows or []:
        name = _text(_value(row, "name"))
        if name and "qr" not in name.lower():
            return name
    if rows:
        name = _text(_value(rows[0], "name"))
        if name:
            return name
    _fail("restored database has no enabled Mode of Payment for the acceptance sale")


def _historical_fingerprint() -> dict[str, list[tuple[str, str]]]:
    """Capture existing business-row identities to prove we did not edit them."""

    runtime_frappe = _require_frappe()
    result: dict[str, list[tuple[str, str]]] = {}
    for doctype in (
        "FB Recipe",
        "FB Order",
        "FB Resolved Sale",
        "Stock Entry",
        "Stock Reconciliation",
    ):
        table = f"tab{doctype}"
        rows = runtime_frappe.db.sql(
            f"SELECT name, modified FROM `{table}` "
            "WHERE name NOT LIKE %s ORDER BY name ASC",
            (f"{AUTHORITY_PREFIX}%",),
            as_dict=True,
        )
        result[doctype] = [
            (_text(_value(row, "name")), _text(_value(row, "modified")))
            for row in rows or []
        ]
    return result


def _ensure_item(
    *,
    item_code: str,
    item_group: str,
    stock_uom: str,
    is_stock_item: int,
    role: str,
) -> Any:
    runtime_frappe = _require_frappe()
    existing = runtime_frappe.db.exists("Item", item_code)
    if existing:
        doc = runtime_frappe.get_doc("Item", existing)
        expected = {
            "item_code": item_code,
            "item_group": item_group,
            "stock_uom": stock_uom,
            "is_stock_item": int(is_stock_item),
        }
        mismatches = [
            fieldname
            for fieldname, expected_value in expected.items()
            if fieldname != "is_stock_item"
            and _text(getattr(doc, fieldname, None)) != _text(expected_value)
        ]
        if int(getattr(doc, "is_stock_item", 0) or 0) != int(is_stock_item):
            mismatches.append("is_stock_item")
        if mismatches:
            _fail(
                f"{item_code} exists with incompatible acceptance authority fields: "
                + ", ".join(mismatches)
            )
        return doc

    doc = runtime_frappe.get_doc(
        {
            "doctype": "Item",
            "name": item_code,
            "item_code": item_code,
            "item_name": item_code,
            "item_group": item_group,
            "stock_uom": stock_uom,
            "is_stock_item": int(is_stock_item),
            "is_sales_item": 0 if is_stock_item else 1,
            "is_purchase_item": 1 if is_stock_item else 0,
            "include_item_in_manufacturing": 1,
            "has_batch_no": 0,
            "has_serial_no": 0,
            "description": "Contained restored-data inventory acceptance authority",
        }
    )
    _set_if_present(doc, "custom_fb_item_role", role)
    _set_if_present(doc, "custom_fb_recipe_required", 1 if not is_stock_item else 0)
    _set_if_present(doc, "custom_fb_inventory_excluded", 0)
    doc.insert(ignore_permissions=True)
    return doc


def _ensure_recipe(
    *, company: str, sellable_item: str, ingredient_item: str, uom: str
) -> Any:
    runtime_frappe = _require_frappe()
    recipe_name = runtime_frappe.db.get_value(
        "FB Recipe", {"recipe_code": RECIPE_CODE}, "name"
    )
    if recipe_name:
        recipe = runtime_frappe.get_doc("FB Recipe", recipe_name)
        existing_identity = {
            "recipe_code": RECIPE_CODE,
            "company": company,
            "sellable_item": sellable_item,
            "version_no": 1,
        }
        mismatches = [
            fieldname
            for fieldname, expected_value in existing_identity.items()
            if _text(getattr(recipe, fieldname, None)) != _text(expected_value)
        ]
        if mismatches:
            _fail(
                f"{RECIPE_CODE} exists with incompatible recipe identity fields: "
                + ", ".join(mismatches)
            )
        if _text(getattr(recipe, "status", None)) != "Active":
            # A prior contained run may have stopped after inserting a draft.
            # Complete only this prefixed fixture; never touch historical recipes.
            if _text(getattr(recipe, "status", None)) not in {"", "Draft"}:
                _fail(f"Acceptance recipe {recipe.name} is not a reusable Draft")
            recipe.status = "Active"
            recipe.company = company
            recipe.sellable_item = sellable_item
            recipe.recipe_name = RECIPE_CODE
            recipe.recipe_type = "Finished Drink"
            recipe.version_no = 1
            recipe.yield_qty = 1
            recipe.yield_uom = uom
            recipe.default_serving_qty = 1
            recipe.default_serving_uom = uom
            recipe.effective_from = runtime_frappe.utils.now_datetime() - timedelta(minutes=10)
            recipe.set("components", [])
            recipe.append(
                "components",
                {
                    "item": ingredient_item,
                    "component_type": "Ingredient",
                    "qty": 1,
                    "uom": uom,
                    "affects_stock": 1,
                    "affects_cogs": 1,
                    "loss_factor_pct": 0,
                },
            )
            recipe.save(ignore_permissions=True)
        return recipe

    recipe = runtime_frappe.get_doc(
        {
            "doctype": "FB Recipe",
            "name": RECIPE_CODE,
            "recipe_code": RECIPE_CODE,
            "recipe_name": RECIPE_CODE,
            "sellable_item": sellable_item,
            "recipe_type": "Finished Drink",
            "status": "Active",
            "version_no": 1,
            "yield_qty": 1,
            "yield_uom": uom,
            "default_serving_qty": 1,
            "default_serving_uom": uom,
            "company": company,
            "effective_from": runtime_frappe.utils.now_datetime() - timedelta(minutes=10),
            "components": [
                {
                    "item": ingredient_item,
                    "component_type": "Ingredient",
                    "qty": 1,
                    "uom": uom,
                    "affects_stock": 1,
                    "affects_cogs": 1,
                    "loss_factor_pct": 0,
                }
            ],
        }
    )
    recipe.insert(ignore_permissions=True)
    return recipe


def _ensure_opening_reconciliation(
    *,
    company: str,
    warehouse: str,
    ingredient_item: str,
    uom: str,
    expense_account: str,
    difference_account: str,
) -> Any:
    runtime_frappe = _require_frappe()
    existing_name = runtime_frappe.db.exists("Stock Reconciliation", OPENING_NAME)
    if existing_name:
        reconciliation = runtime_frappe.get_doc("Stock Reconciliation", existing_name)
        if int(getattr(reconciliation, "docstatus", 0) or 0) == 0:
            reconciliation.submit()
        if int(getattr(reconciliation, "docstatus", 0) or 0) != 1:
            _fail(f"Acceptance opening Stock Reconciliation {existing_name} is not submitted")
        return reconciliation

    reconciliation = runtime_frappe.new_doc("Stock Reconciliation")
    reconciliation.name = OPENING_NAME
    reconciliation.company = company
    reconciliation.purpose = "Stock Reconciliation"
    reconciliation.posting_date = runtime_frappe.utils.nowdate()
    reconciliation.posting_time = runtime_frappe.utils.now_datetime().time()
    _set_if_present(reconciliation, "expense_account", expense_account)
    _set_if_present(reconciliation, "difference_account", difference_account)
    _set_if_present(reconciliation, "remarks", f"{AUTHORITY_PREFIX} opening stock")
    _set_if_present(reconciliation, "cost_center", _discover_cost_center(company))
    reconciliation.append(
        "items",
        {
            "item_code": ingredient_item,
            "warehouse": warehouse,
            "qty": 10,
            "uom": uom,
            "valuation_rate": 1,
        },
    )
    reconciliation.insert(ignore_permissions=True)
    reconciliation.submit()
    if int(getattr(reconciliation, "docstatus", 0) or 0) != 1:
        _fail("Acceptance opening Stock Reconciliation did not submit")
    return reconciliation


def _discover_cost_center(company: str) -> str | None:
    row = _first_row(
        "Cost Center",
        {"company": company, "is_group": 0, "disabled": 0},
        ["name"],
    )
    return _text(_value(row, "name")) if row else None


def _ensure_policy(
    *, company: str, warehouse: str, opening_name: str, expense_account: str
) -> Any:
    runtime_frappe = _require_frappe()
    cutover_at = runtime_frappe.utils.now_datetime() - timedelta(minutes=5)
    cutover_token = hashlib.sha256(
        f"{AUTHORITY_PREFIX}|{company}|{warehouse}|{opening_name}".encode("utf-8")
    ).hexdigest()
    existing_name = runtime_frappe.db.exists("FB Inventory Policy", POLICY_NAME)
    policy_rows = runtime_frappe.get_all(
        "FB Inventory Policy",
        filters={"company": company, "warehouse": warehouse},
        fields=["name", "cutover_token", "opening_stock_reconciliation"],
        order_by="name asc",
        limit_page_length=0,
    )
    if len(policy_rows) > 1:
        _fail(
            f"Company {company} and warehouse {warehouse} already have multiple "
            "FB Inventory Policies; the contained proof will not guess an authority"
        )
    if policy_rows:
        row = policy_rows[0]
        row_name = _text(_value(row, "name"))
        row_token = _text(_value(row, "cutover_token"))
        row_opening = _text(_value(row, "opening_stock_reconciliation"))
        if row_token == cutover_token or row_opening == opening_name:
            if existing_name and _text(existing_name) != row_name:
                _fail("Acceptance policy lookup returned conflicting names")
            existing_name = row_name
        else:
            _fail(
                f"Company {company} and warehouse {warehouse} already have "
                f"non-acceptance FB Inventory Policy {row_name}; the proof will "
                "not modify it"
            )
    if existing_name:
        policy = runtime_frappe.get_doc("FB Inventory Policy", existing_name)
        if (
            _text(getattr(policy, "company", None)) != company
            or _text(getattr(policy, "warehouse", None)) != warehouse
        ):
            _fail("Acceptance policy name belongs to another company or warehouse")
        previous_token = _text(getattr(policy, "cutover_token", None))
        if previous_token and previous_token != cutover_token:
            _fail("Acceptance policy cutover identity changed between reruns")
    else:
        policy = runtime_frappe.get_doc(
            {
                "doctype": "FB Inventory Policy",
                "name": POLICY_NAME,
            }
        )

    policy.company = company
    policy.warehouse = warehouse
    policy.automation_state = "Active"
    policy.inventory_contract_version = "inventory-autopilot-v1"
    policy.cutover_token = cutover_token
    policy.cutover_at = getattr(policy, "cutover_at", None) or cutover_at
    policy.opening_stock_reconciliation = opening_name
    policy.max_source_age_minutes = 30
    policy.expense_account = expense_account
    policy.permitted_actions = "stock_projection"
    if getattr(policy, "is_new", lambda: False)():
        policy.insert(ignore_permissions=True)
    else:
        policy.save(ignore_permissions=True)
    if _text(getattr(policy, "automation_state", None)) != "Active":
        _fail("Acceptance inventory policy did not become Active")
    return policy


def _ensure_shift(*, company: str, warehouse: str, staff_id: str) -> Any:
    runtime_frappe = _require_frappe()
    existing_name = runtime_frappe.db.get_value(
        "FB Shift", {"shift_code": SHIFT_CODE}, "name"
    )
    if existing_name:
        shift = runtime_frappe.get_doc("FB Shift", existing_name)
        if (
            _text(getattr(shift, "device_id", None)) != DEVICE_ID
            or _text(getattr(shift, "staff_id", None)) != staff_id
            or _text(getattr(shift, "warehouse", None)) != warehouse
            or _text(getattr(shift, "company", None)) != company
        ):
            _fail("Acceptance FB Shift fields changed between reruns")
        if _text(getattr(shift, "status", None)) != "Open":
            _fail("Acceptance FB Shift must remain Open for an idempotent replay")
        return shift

    shift = runtime_frappe.get_doc(
        {
            "doctype": "FB Shift",
            "name": SHIFT_NAME,
            "shift_code": SHIFT_CODE,
            "device_id": DEVICE_ID,
            "staff_id": staff_id,
            "warehouse": warehouse,
            "company": company,
            "status": "Open",
            "opened_at": runtime_frappe.utils.now_datetime() - timedelta(minutes=15),
            "opening_float": 0,
        }
    )
    shift.insert(ignore_permissions=True)
    return shift


def _ensure_order(
    *,
    company: str,
    warehouse: str,
    currency: str,
    shift: Any,
    sellable_item: Any,
    recipe: Any,
    mode_of_payment: str,
) -> Any:
    runtime_frappe = _require_frappe()
    from kopos_connector.kopos.api.fb_orders import submit_order_payload

    existing_name = runtime_frappe.db.get_value(
        "FB Order", {"external_idempotency_key": ORDER_IDEMPOTENCY}, "name"
    )
    if existing_name:
        return runtime_frappe.get_doc("FB Order", existing_name)

    now = runtime_frappe.utils.now_datetime()
    payload = {
        "money_contract_version": "sen_v1",
        "order_id": ORDER_ID,
        "idempotency_key": ORDER_IDEMPOTENCY,
        "source": "ERP",
        "device_id": DEVICE_ID,
        # The public sale contract accepts the stable shift code.  The API
        # resolves that code to the generated FB Shift name before submit.
        "shift_id": SHIFT_CODE,
        "staff_id": "Administrator",
        "warehouse": warehouse,
        "company": company,
        "currency": currency,
        "order": {
            "display_number": ORDER_ID,
            "order_type": "acceptance",
            "created_at": now.isoformat(),
            "subtotal_sen": 100,
            "tax_amount_sen": 0,
            "rounding_adjustment_sen": 0,
            "total_sen": 100,
            "items": [
                {
                    "line_id": LINE_ID,
                    "backend_line_uuid": LINE_ID,
                    "item_code": sellable_item.name,
                    "item_name": sellable_item.item_name,
                    "recipe": recipe.name,
                    "recipe_version": int(recipe.version_no),
                    "recipe_hash": _text(recipe.canonical_hash).lower(),
                    "qty": 1,
                    "uom": sellable_item.stock_uom,
                    "unit_price_sen": 100,
                    "modifier_total_sen": 0,
                    "discount_amount_sen": 0,
                    "line_total_sen": 100,
                    "modifiers": [],
                }
            ],
            "payments": [
                {
                    "payment_id": PAYMENT_ID,
                    "payment_method": mode_of_payment,
                    "amount_sen": 100,
                    "tendered_amount_sen": 100,
                    "change_amount_sen": 0,
                }
            ],
        },
    }
    result = submit_order_payload(payload)
    order_name = _text(result.get("fb_order")) if isinstance(result, Mapping) else ""
    if not order_name:
        _fail("Acceptance sale registration did not return an FB Order")
    return runtime_frappe.get_doc("FB Order", order_name)


def _existing_authorities() -> dict[str, Any]:
    runtime_frappe = _require_frappe()
    ingredient = runtime_frappe.db.exists("Item", ITEM_INGREDIENT)
    sellable = runtime_frappe.db.exists("Item", ITEM_SELLABLE)
    recipe_name = runtime_frappe.db.get_value(
        "FB Recipe", {"recipe_code": RECIPE_CODE}, "name"
    )
    policy_name = runtime_frappe.db.exists("FB Inventory Policy", POLICY_NAME)
    shift_name = runtime_frappe.db.get_value(
        "FB Shift", {"shift_code": SHIFT_CODE}, "name"
    )
    order_name = runtime_frappe.db.get_value(
        "FB Order", {"external_idempotency_key": ORDER_IDEMPOTENCY}, "name"
    )
    missing = [
        label
        for label, value in (
            ("ingredient Item", ingredient),
            ("sellable Item", sellable),
            ("FB Recipe", recipe_name),
            ("FB Inventory Policy", policy_name),
            ("FB Shift", shift_name),
            ("FB Order", order_name),
        )
        if not value
    ]
    if missing:
        _fail(
            "restored inventory acceptance authorities are incomplete; run "
            "restored-inventory-acceptance first (missing: "
            + ", ".join(missing)
            + ")"
        )

    policy = runtime_frappe.get_doc("FB Inventory Policy", policy_name)
    opening_name = _text(getattr(policy, "opening_stock_reconciliation", None))
    if not opening_name:
        _fail("Acceptance policy has no opening Stock Reconciliation")
    if not runtime_frappe.db.exists("Stock Reconciliation", opening_name):
        _fail(f"Acceptance opening Stock Reconciliation {opening_name} was not found")

    return {
        "ingredient": runtime_frappe.get_doc("Item", ingredient),
        "sellable": runtime_frappe.get_doc("Item", sellable),
        "recipe": runtime_frappe.get_doc("FB Recipe", recipe_name),
        "policy": policy,
        "opening": runtime_frappe.get_doc("Stock Reconciliation", opening_name),
        "shift": runtime_frappe.get_doc("FB Shift", shift_name),
        "order": runtime_frappe.get_doc("FB Order", order_name),
    }


def _proof_from_authorities(authorities: Mapping[str, Any]) -> tuple[dict[str, Any], int, dict[str, Any]]:
    runtime_frappe = _require_frappe()
    assertions = 0

    def check(condition: bool, message: str) -> None:
        nonlocal assertions
        if not condition:
            _fail(message)
        assertions += 1

    ingredient = authorities["ingredient"]
    sellable = authorities["sellable"]
    recipe = authorities["recipe"]
    policy = authorities["policy"]
    opening = authorities["opening"]
    shift = authorities["shift"]
    order = authorities["order"]

    check(_text(ingredient.name).startswith(AUTHORITY_PREFIX), "Acceptance ingredient Item is not prefixed")
    check(int(getattr(ingredient, "is_stock_item", 0) or 0) == 1, "Acceptance ingredient Item is not stock-enabled")
    check(_text(sellable.name).startswith(AUTHORITY_PREFIX), "Acceptance sellable Item is not prefixed")
    check(int(getattr(sellable, "is_stock_item", 0) or 0) == 0, "Acceptance sellable Item must be non-stock")
    check(_text(recipe.status) == "Active", "Acceptance FB Recipe is not Active")
    check(bool(_text(recipe.canonical_hash)), "Acceptance FB Recipe has no canonical hash")
    components = list(getattr(recipe, "components", None) or [])
    check(
        len(components) == 1
        and _text(getattr(components[0], "item", None)) == _text(ingredient.name)
        and _text(getattr(components[0], "stock_uom", None)) == _text(ingredient.stock_uom),
        "Acceptance FB Recipe does not freeze exactly one stock ingredient",
    )
    check(_text(policy.automation_state) == "Active", "Acceptance policy is not Active")
    check(bool(_text(policy.cutover_token)) and bool(policy.cutover_at), "Acceptance policy has no immutable cutover")
    check(_text(policy.opening_stock_reconciliation) == _text(opening.name), "Acceptance policy opening reconciliation link is wrong")
    check(int(getattr(opening, "docstatus", 0) or 0) == 1, "Acceptance opening reconciliation is not submitted")
    check(_text(shift.status) == "Open", "Acceptance FB Shift is not Open")
    check(_text(order.status) == "Submitted", "Acceptance FB Order is not Submitted")
    check(int(getattr(order, "docstatus", 0) or 0) == 1, "Acceptance FB Order has not completed real submit")
    sale_time = _acceptance_datetime(getattr(order, "sale_datetime", None))
    cutover_at = _acceptance_datetime(getattr(policy, "cutover_at", None))
    check(
        bool(sale_time and cutover_at and sale_time >= cutover_at),
        "Acceptance sale is not post-cutover",
    )

    resolved_sales = runtime_frappe.get_all(
        "FB Resolved Sale",
        filters={"fb_order": order.name},
        fields=["name", "stock_entry_issue", "status", "resolution_hash"],
        order_by="creation asc",
    )
    check(len(resolved_sales) == 1, "Acceptance order must have exactly one FB Resolved Sale")
    resolved_sale_name = _text(_value(resolved_sales[0], "name")) if resolved_sales else ""
    if resolved_sales:
        check(bool(_text(_value(resolved_sales[0], "resolution_hash"))), "Acceptance resolved sale has no resolution hash")

    required_stock_fields = ("custom_fb_order", "custom_fb_projection_id")
    missing_stock_fields = [
        fieldname
        for fieldname in required_stock_fields
        if not _meta_has("Stock Entry", fieldname)
    ]
    if missing_stock_fields:
        _fail(
            "Stock Entry is missing the projection identity fields required for "
            "idempotency proof: "
            + ", ".join(missing_stock_fields)
        )

    target_entries = runtime_frappe.get_all(
        "Stock Entry",
        filters={
            "custom_fb_order": order.name,
            "purpose": "Material Issue",
            "docstatus": 1,
        },
        fields=["name", "docstatus", "purpose", "stock_entry_type", "custom_fb_projection_id"],
        order_by="creation asc",
    )
    check(len(target_entries) == 1, "Acceptance order must have exactly one submitted Material Issue")
    target_name = _text(_value(target_entries[0], "name")) if target_entries else ""
    if target_entries:
        check(_text(_value(target_entries[0], "stock_entry_type")) == "Material Issue", "Acceptance Stock Entry type is not Material Issue")
        check(bool(_text(_value(target_entries[0], "custom_fb_projection_id"))), "Acceptance Stock Entry has no projection identity")
        check(_text(getattr(order, "ingredient_stock_entry", None)) == target_name, "FB Order does not point to its Material Issue")
        if resolved_sales:
            check(
                _text(_value(resolved_sales[0], "stock_entry_issue")) == target_name,
                "FB Resolved Sale does not point to its Material Issue",
            )

    stock_ledger_count = 0
    stock_ledger_has_issue = False
    if target_name:
        ledger_rows = runtime_frappe.get_all(
            "Stock Ledger Entry",
            filters={
                "voucher_type": "Stock Entry",
                "voucher_no": target_name,
                "is_cancelled": 0,
            },
            fields=["name", "actual_qty"],
            order_by="creation asc, name asc",
            limit_page_length=0,
        )
        stock_ledger_count = len(ledger_rows or [])
        for row in ledger_rows or []:
            try:
                actual_qty = Decimal(str(_value(row, "actual_qty") or "0"))
            except (InvalidOperation, ValueError):
                actual_qty = Decimal("0")
            if actual_qty < 0:
                stock_ledger_has_issue = True
    check(stock_ledger_count > 0, "Acceptance Material Issue created no Stock Ledger Entry")
    check(stock_ledger_has_issue, "Acceptance Stock Ledger has no negative ingredient issue row")

    gl_count = 0
    if target_name:
        gl_count = int(
            runtime_frappe.db.count(
                "GL Entry",
                {"voucher_type": "Stock Entry", "voucher_no": target_name, "is_cancelled": 0},
            )
            or 0
        )
    check(gl_count > 0, "Acceptance Material Issue created no GL Entry")

    projection_logs = runtime_frappe.get_all(
        "FB Projection Log",
        filters={
            "source_doctype": "FB Order",
            "source_name": order.name,
            "projection_type": "Stock Issue",
        },
        fields=["name", "state", "target_doctype", "target_name"],
        order_by="creation asc",
    )
    check(len(projection_logs) == 1, "Acceptance order must have exactly one inventory Projection Log")
    if projection_logs:
        check(_text(_value(projection_logs[0], "state")) == "Succeeded", "Acceptance inventory Projection Log is not Succeeded")
        check(_text(_value(projection_logs[0], "target_doctype")) == "Stock Entry", "Acceptance Projection Log target doctype is not Stock Entry")
        check(_text(_value(projection_logs[0], "target_name")) == target_name, "Acceptance Projection Log target is not the Material Issue")

    duplicate_target_count = max(0, len(target_entries) - 1)
    proof = {
        "contractId": CONTRACT_ID,
        "status": "passed",
        "resolvedSaleCount": len(resolved_sales),
        "stockEntryCount": len(target_entries),
        "stockLedgerEntryCount": stock_ledger_count,
        "glEntryCount": gl_count,
        "duplicateTargetCount": duplicate_target_count,
    }
    validate_inventory_acceptance_proof(proof)
    return proof, assertions, {
        "ingredientItem": ingredient.name,
        "sellableItem": sellable.name,
        "recipe": recipe.name,
        "policy": policy.name,
        "openingStockReconciliation": opening.name,
        "shift": shift.name,
        "order": order.name,
        "resolvedSale": resolved_sale_name,
        "stockEntry": target_name,
    }


def read_v1(
    restored_backup_sha256: str,
    erp_artifact_sha256: str,
    expected_connector_version: str,
) -> dict[str, Any]:
    """Read and validate authorities created by :func:`run_v1` without writes."""

    runtime_frappe = _require_frappe()
    backup_sha256 = _required_sha256(restored_backup_sha256, "restored_backup_sha256")
    artifact_sha256 = _required_sha256(erp_artifact_sha256, "erp_artifact_sha256")
    expected_version = _required_text(expected_connector_version, "expected_connector_version")
    installed_version = metadata.version("kopos_connector")
    if installed_version != expected_version:
        _fail("Installed connector version does not match the candidate binding")
    authorities = _existing_authorities()
    proof, assertions, authority_names = _proof_from_authorities(authorities)
    return {
        "schemaVersion": 1,
        "status": "passed",
        "evidenceLevel": "restored_production_data",
        "producer": READ_PRODUCER,
        "readOnly": True,
        "providerNetworkCalls": 0,
        "inventoryAssertions": assertions,
        "inventoryAcceptanceProof": proof,
        "connectorVersion": installed_version,
        "erpArtifactSha256": artifact_sha256,
        "restoredBackupSha256": backup_sha256,
        "authorityPrefix": AUTHORITY_PREFIX,
        "authorityNames": authority_names,
    }


def run_v1(
    restored_backup_sha256: str,
    erp_artifact_sha256: str,
    expected_connector_version: str,
) -> dict[str, Any]:
    """Create/reuse the contained fixture and prove a real idempotent projection."""

    runtime_frappe = _require_frappe()
    backup_sha256 = _required_sha256(restored_backup_sha256, "restored_backup_sha256")
    artifact_sha256 = _required_sha256(erp_artifact_sha256, "erp_artifact_sha256")
    expected_version = _required_text(expected_connector_version, "expected_connector_version")
    installed_version = metadata.version("kopos_connector")
    if installed_version != expected_version:
        _fail("Installed connector version does not match the candidate binding")

    historical_before = _historical_fingerprint()
    company = _discover_company()
    warehouse = _discover_warehouse(company["name"])
    uom = _discover_uom()
    item_group = _discover_item_group()
    expense_account = _discover_expense_account(company["name"])
    opening_difference_account = _discover_opening_difference_account(company["name"])
    ingredient = _ensure_item(
        item_code=ITEM_INGREDIENT,
        item_group=item_group,
        stock_uom=uom,
        is_stock_item=1,
        role="Ingredient",
    )
    sellable = _ensure_item(
        item_code=ITEM_SELLABLE,
        item_group=item_group,
        stock_uom=uom,
        is_stock_item=0,
        role="Sellable Drink",
    )
    recipe = _ensure_recipe(
        company=company["name"],
        sellable_item=sellable.name,
        ingredient_item=ingredient.name,
        uom=uom,
    )
    opening = _ensure_opening_reconciliation(
        company=company["name"],
        warehouse=warehouse,
        ingredient_item=ingredient.name,
        uom=uom,
        expense_account=expense_account,
        difference_account=opening_difference_account,
    )
    policy = _ensure_policy(
        company=company["name"],
        warehouse=warehouse,
        opening_name=opening.name,
        expense_account=expense_account,
    )
    shift = _ensure_shift(
        company=company["name"],
        warehouse=warehouse,
        staff_id="Administrator",
    )
    mode_of_payment = _discover_mode_of_payment()
    order = _ensure_order(
        company=company["name"],
        warehouse=warehouse,
        currency=company["currency"],
        shift=shift,
        sellable_item=sellable,
        recipe=recipe,
        mode_of_payment=mode_of_payment,
    )

    # This is the production worker, not a direct Stock Entry helper.  Calling
    # it twice proves projection-log and target-document idempotency.
    from kopos_connector.kopos.services.inventory_autopilot.projection_worker import (
        project_inventory_order,
    )

    first_result = project_inventory_order(order.name)
    if _text(first_result.get("state")) != "Succeeded":
        _fail(f"Acceptance inventory worker did not succeed on first delivery: {first_result}")
    second_result = project_inventory_order(order.name)
    if _text(second_result.get("state")) != "Succeeded":
        _fail(f"Acceptance inventory worker replay did not remain Succeeded: {second_result}")

    authorities = _existing_authorities()
    proof, assertions, authority_names = _proof_from_authorities(authorities)
    historical_after = _historical_fingerprint()
    if historical_after != historical_before:
        _fail("Restored inventory acceptance changed a pre-existing recipe, sale, or stock document")

    runtime_frappe.db.commit()
    return {
        "schemaVersion": 1,
        "status": "passed",
        "evidenceLevel": "restored_production_data",
        "producer": PRODUCER,
        "readOnly": False,
        "providerNetworkCalls": 0,
        "inventoryAssertions": assertions,
        "inventoryAcceptanceProof": proof,
        "connectorVersion": installed_version,
        "erpArtifactSha256": artifact_sha256,
        "restoredBackupSha256": backup_sha256,
        "authorityPrefix": AUTHORITY_PREFIX,
        "authorityNames": authority_names,
        "firstWorkerState": first_result.get("state"),
        "replayWorkerState": second_result.get("state"),
    }
