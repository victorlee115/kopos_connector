from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr

WASTE_COMMAND_PREFIX = "fb-waste"


def build_waste_command_id(waste_id: str) -> str:
    """One waste event may only ever produce one Material Issue.

    The FB Waste Event's own identity is the command identity, so a retried or
    replayed submit adopts the Stock Entry that already exists instead of issuing
    the same stock a second time.
    """

    normalized = cstr(waste_id).strip()
    if not normalized:
        frappe.throw(_("Waste event requires waste_id"), frappe.ValidationError)
    return f"{WASTE_COMMAND_PREFIX}:{normalized}"


def _decimal_quantity(line: dict[str, Any]) -> Decimal:
    """Prefer the exact decimal text; fall back to the legacy Float column.

    Historical rows were written before qty_decimal existed and may leave it
    blank, exactly as recipe components do.
    """

    raw = cstr(line.get("qty_decimal")).strip()
    if not raw:
        raw = cstr(line.get("qty")).strip()
    try:
        quantity = Decimal(raw or "0")
    except (InvalidOperation, ValueError):
        frappe.throw(_("Waste line quantity is not a valid number"), frappe.ValidationError)
    if quantity <= 0:
        frappe.throw(_("Waste line quantity must be greater than zero"), frappe.ValidationError)
    return quantity


def _existing_waste_stock_entry(command_id: str) -> str | None:
    meta = frappe.get_meta("Stock Entry")
    if not meta.has_field("custom_kopos_inventory_command_id"):
        frappe.throw(
            _("Stock Entry is missing the inventory command identity field; run bench migrate before recording waste"),
            frappe.ValidationError,
        )
    existing = frappe.db.get_value(
        "Stock Entry",
        {"custom_kopos_inventory_command_id": command_id},
        ["name", "docstatus"],
        as_dict=True,
    )
    if not existing:
        return None
    return cstr(existing.get("name")).strip() or None


def create_waste_stock_entry(
    company: str,
    warehouse: str,
    items: list[dict[str, Any]],
    *,
    waste_id: str,
) -> str:
    """Post one Material Issue for a waste event, exactly once.

    Previously this inserted and submitted a new Stock Entry on every call and
    only recorded the link afterwards, so a retry or a replayed submit issued the
    same stock again. Quantities were also taken straight from the Float column.
    """

    if not items:
        frappe.throw(_("Waste event requires at least one line"), frappe.ValidationError)

    command_id = build_waste_command_id(waste_id)
    already_posted = _existing_waste_stock_entry(command_id)
    if already_posted:
        return already_posted

    doc = frappe.new_doc("Stock Entry")
    doc.company = company
    doc.stock_entry_type = "Material Issue"
    doc.purpose = "Material Issue"
    doc.custom_kopos_inventory_command_id = command_id
    for item in items:
        row = dict(item)
        row.setdefault("s_warehouse", warehouse)
        # Keep the exact decimal as the quantity authority; Frappe stores its own
        # Float alongside, but the value written here is the one we computed.
        row["qty"] = str(_decimal_quantity(row))
        row.pop("qty_decimal", None)
        doc.append("items", row)
    doc.insert(ignore_permissions=True)
    doc.submit()
    return doc.name
