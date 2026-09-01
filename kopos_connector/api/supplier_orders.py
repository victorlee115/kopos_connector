# pyright: reportMissingImports=false

"""Human supplier-order decisions backed by the existing Purchase Order.

Approving a supplier order today means reading a Purchase Order form: five
tabs, a Submit button as the most prominent control, an item reading
``SMOKE-CU...``, ``100``, ``Nos``, and six of eight grid columns truncated. A
director approving that cannot see what the item is, why it is being ordered,
or whether this supplier delivers on time.

The decision needs five facts and a reason: what, how much, what it costs, when
it lands, whether they deliver on time -- and why JiJi suggested it. Approval
submits that same Purchase Order through one scoped action; JiJi never creates
a second order and never sends anything to the supplier.

The "why" line reads the runout figure from the stock snapshot that already
computes it for the tablet, so the café is never told two different days.

One deliberate duplication: the tablet turns ERP units into café words in
``unit-display.ts`` and this module does the same for the desk. They are two
presentations of one stored quantity, not two authorities over it -- neither
changes what is ordered -- and they cannot be one file across a language
boundary.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate

from kopos_connector.api.cafe_money import money_sen, money_text
from kopos_connector.api.cafe_units import amount_phrase
from kopos_connector.api.director_scope import resolve_director_context
from kopos_connector.utils.diagnostics import log_sanitized_error

# How many drafts get the full treatment. The design shows one decision at a
# time; the rest are one line each until the director opens them.
DETAILED_DRAFTS = 1
# Enough history to see a pattern, not enough to become a report.
HISTORY_LIMIT = 3
RELIABILITY_SAMPLE = 6
ON_THE_WAY_LIMIT = 3
DRAFT_PAGE_SIZE = 100
ORDER_LINE_LIMIT = 200


@frappe.whitelist(methods=["GET"])
def get_supplier_page_choices(*, company: str | None = None) -> dict[str, Any]:
    """Return only cafés and outlets this director may use, with friendly labels."""

    context = resolve_director_context(company=cstr(company).strip() or None)
    company_rows = (
        frappe.get_all(
            "Company",
            filters={"name": ["in", list(context.allowed_companies)]},
            fields=["name", "company_name"],
            order_by="company_name asc, name asc",
            limit_page_length=len(context.allowed_companies),
        )
        if context.allowed_companies
        else []
    )
    outlet_rows = (
        frappe.get_all(
            "Warehouse",
            filters={"name": ["in", list(context.allowed_warehouses)]},
            fields=["name", "warehouse_name"],
            order_by="warehouse_name asc, name asc",
            limit_page_length=len(context.allowed_warehouses),
        )
        if context.allowed_warehouses
        else []
    )
    return {
        "status": context.status,
        "selected_cafe": context.company or None,
        "selected_outlet": context.warehouse or None,
        "cafes": [
            {
                "value": cstr(row.get("name")).strip(),
                "label": cstr(row.get("company_name")).strip()
                or cstr(row.get("name")).strip(),
            }
            for row in company_rows
            if cstr(row.get("name")).strip()
        ],
        "outlets": [
            {
                "value": cstr(row.get("name")).strip(),
                "label": cstr(row.get("warehouse_name")).strip()
                or cstr(row.get("name")).strip(),
            }
            for row in outlet_rows
            if cstr(row.get("name")).strip()
        ],
    }

def _line_amount(row: dict[str, Any]) -> str:
    """What is being bought, in packs and what is inside one.

    A Purchase Order stores 12 Bag with a conversion factor of 250 into Gram.
    "12 bags · 250 g each" is the same fact, said the way it is bought.
    """
    ordered = amount_phrase(flt(row.get("qty")), cstr(row.get("uom")))
    factor = flt(row.get("conversion_factor"))
    stock_unit = cstr(row.get("stock_uom"))
    same_unit = cstr(row.get("uom")).strip().lower() == stock_unit.strip().lower()
    if same_unit or factor <= 0 or factor == 1:
        return ordered
    return f"{ordered} · {amount_phrase(factor, stock_unit)} each"


def _money(value: Any, currency: str, fieldname: str) -> str:
    # A missing persisted amount is not the same fact as a zero-value order.
    # Let the surrounding page area become unavailable instead of reassuring
    # a director with a number the database did not contain.
    return money_text(money_sen(value, fieldname), currency or "MYR")


def _readable_date(value: Any) -> str:
    """"Friday 4 September" -- the way a delivery day is spoken, not stored."""
    if not value:
        return ""
    try:
        day = getdate(value)
    except (ValueError, TypeError):
        return cstr(value)
    # Built from parts rather than a %-d format, which is not portable.
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def _short_date(value: Any) -> str:
    """"14 Aug" -- a date in a list, where the weekday is noise."""
    if not value:
        return ""
    try:
        day = getdate(value)
    except (ValueError, TypeError):
        return cstr(value)
    return f"{day.day} {day.strftime('%b')}"


def _draft_orders(company: str, warehouse: str) -> list[dict[str, Any]]:
    return frappe.db.sql(
        """
        SELECT purchase_order.name, purchase_order.supplier,
               purchase_order.supplier_name, purchase_order.currency,
               purchase_order.schedule_date, purchase_order.creation,
               purchase_order.modified,
               SUM(line.amount) AS outlet_total
        FROM `tabPurchase Order` purchase_order
        INNER JOIN `tabPurchase Order Item` line ON line.parent = purchase_order.name
        WHERE line.parenttype = 'Purchase Order'
          AND purchase_order.company = %s AND purchase_order.docstatus = 0
          AND COALESCE(NULLIF(line.warehouse, ''), purchase_order.set_warehouse) = %s
        GROUP BY purchase_order.name, purchase_order.supplier,
                 purchase_order.supplier_name, purchase_order.currency,
                 purchase_order.schedule_date, purchase_order.creation,
                 purchase_order.modified
        ORDER BY purchase_order.schedule_date ASC, purchase_order.creation ASC
        LIMIT %s
        """,
        (company, warehouse, DRAFT_PAGE_SIZE + 1),
        as_dict=True,
    )


def _order_lines(order: str, warehouse: str) -> list[dict[str, Any]]:
    rows = frappe.db.sql(
        """
        SELECT line.item_code, line.item_name, line.qty, line.uom,
               line.conversion_factor, line.stock_uom, line.amount, line.warehouse
        FROM `tabPurchase Order Item` line
        INNER JOIN `tabPurchase Order` purchase_order ON purchase_order.name = line.parent
        WHERE line.parenttype = 'Purchase Order' AND line.parent = %s
          AND COALESCE(NULLIF(line.warehouse, ''), purchase_order.set_warehouse) = %s
        ORDER BY line.idx ASC
        LIMIT %s
        """,
        (order, warehouse, ORDER_LINE_LIMIT + 1),
        as_dict=True,
    )
    if len(rows) > ORDER_LINE_LIMIT:
        raise RuntimeError("Supplier order has more lines than this page can safely show")
    return rows


def _delivery_record(supplier: str, company: str) -> list[dict[str, Any]]:
    """Recent finished orders from one supplier, with whether they landed on time.

    An order counts as late when the receipt was posted after the date the
    order asked for. Nothing here is recomputed from stock movements; both
    dates are the ones the documents already carry.
    """
    orders = frappe.get_all(
        "Purchase Order",
        filters={"company": company, "supplier": supplier, "docstatus": 1},
        fields=["name", "supplier_name", "grand_total", "currency", "schedule_date", "transaction_date", "per_received"],
        order_by="transaction_date desc, creation desc",
        limit_page_length=RELIABILITY_SAMPLE,
    )
    if not orders:
        return []

    order_names = [cstr(row.get("name")) for row in orders]
    placeholders = ", ".join(["%s"] * len(order_names))
    receipts = frappe.db.sql(
        f"""
        SELECT line.purchase_order, MAX(receipt.posting_date) AS arrived_on
        FROM `tabPurchase Receipt Item` line
        INNER JOIN `tabPurchase Receipt` receipt ON receipt.name = line.parent
        WHERE line.purchase_order IN ({placeholders}) AND receipt.docstatus = 1
        GROUP BY line.purchase_order
        """,
        tuple(order_names),
        as_dict=True,
    )
    arrived = {
        cstr(row.get("purchase_order")): row.get("arrived_on")
        for row in receipts
        if cstr(row.get("purchase_order")).strip()
    }

    record = []
    for row in orders:
        landed = arrived.get(cstr(row["name"]))
        promised = row.get("schedule_date")
        if landed and promised:
            late_days = (getdate(landed) - getdate(promised)).days
        else:
            late_days = None
        record.append({**row, "arrived_on": landed, "late_days": late_days})
    return record


def _reliability_line(record: list[dict[str, Any]]) -> str:
    """"Usually delivers in 3 days · 5 of the last 6 arrived on time"."""
    judged = [row for row in record if row["late_days"] is not None]
    if not judged:
        return ""
    on_time = sum(1 for row in judged if row["late_days"] <= 0)
    turnarounds = [
        (getdate(row["arrived_on"]) - getdate(row["transaction_date"])).days
        for row in judged
        if row.get("transaction_date")
    ]
    parts = []
    if turnarounds:
        typical = round(sum(turnarounds) / len(turnarounds))
        parts.append(f"Usually delivers in {typical} day" + ("" if typical == 1 else "s"))
    if on_time == len(judged):
        parts.append(f"the last {len(judged)} arrived on time")
    else:
        parts.append(f"{on_time} of the last {len(judged)} arrived on time")
    return " · ".join(parts)


def _arrival_words(row: dict[str, Any]) -> str:
    if row["late_days"] is None:
        return "Not received yet"
    if row["late_days"] <= 0:
        return "Arrived on time"
    if row["late_days"] == 1:
        return "One day late"
    return f"{row['late_days']} days late"


def _runout_days(company: str, warehouse: str, items: set[str]) -> dict[str, float]:
    """How long each item lasts, read from the owner the tablet already reads.

    Recomputing it here would give the café two answers to "when does the
    matcha run out". A stock area that cannot report must not take the supplies
    page down with it.
    """
    if not warehouse or not items:
        return {}
    try:
        from kopos_connector.kopos.services.inventory_autopilot.edge_snapshot import (
            build_edge_inventory_snapshot,
        )

        # The snapshot contract refuses a limit above 100 rather than
        # truncating it, so ask for a size it will accept.
        snapshot = build_edge_inventory_snapshot(
            company=company, warehouse=warehouse, limit=min(100, max(1, len(items) + 20))
        )
    except Exception:
        frappe.log_error(title="JiJi supplies could not read the stock snapshot")
        return {}

    days: dict[str, float] = {}
    for target in snapshot.get("items") or []:
        item = cstr(target.get("stock_item")).strip()
        runout = target.get("runout") or {}
        if item in items and runout.get("days") is not None:
            days[item] = flt(runout.get("days"))
    return days


def _why_line(lines: list[dict[str, Any]], runout: dict[str, float], reliability: list[dict[str, Any]]) -> str:
    """The one sentence a director actually weighs the order against."""
    soonest_item = ""
    soonest_days = None
    for row in lines:
        days = runout.get(cstr(row.get("item_code")))
        if days is None:
            continue
        if soonest_days is None or days < soonest_days:
            soonest_days, soonest_item = days, cstr(row.get("item_name") or row.get("item_code"))
    if soonest_days is None:
        return ""
    whole = max(0, int(soonest_days))
    sentence = f"{soonest_item} runs out in about {whole} day" + ("" if whole == 1 else "s") + " at this week's sales"
    turnarounds = [
        (getdate(row["arrived_on"]) - getdate(row["transaction_date"])).days
        for row in reliability
        if row.get("arrived_on") and row.get("transaction_date")
    ]
    if turnarounds:
        typical = round(sum(turnarounds) / len(turnarounds))
        sentence += f", and delivery takes {typical}"
    return sentence + "."


def _on_the_way(company: str, warehouse: str) -> list[dict[str, Any]]:
    """Orders already sent. Nothing here is a decision; it is what not to reorder."""
    rows = frappe.db.sql(
        """
        SELECT purchase_order.name, purchase_order.supplier_name,
               purchase_order.currency, purchase_order.schedule_date,
               purchase_order.transaction_date, SUM(line.amount) AS outlet_total
        FROM `tabPurchase Order` purchase_order
        INNER JOIN `tabPurchase Order Item` line ON line.parent = purchase_order.name
        WHERE line.parenttype = 'Purchase Order'
          AND purchase_order.company = %s AND purchase_order.docstatus = 1
          AND purchase_order.per_received < 100
          AND purchase_order.status NOT IN ('Closed', 'Cancelled')
          AND COALESCE(NULLIF(line.warehouse, ''), purchase_order.set_warehouse) = %s
        GROUP BY purchase_order.name, purchase_order.supplier_name,
                 purchase_order.currency, purchase_order.schedule_date,
                 purchase_order.transaction_date
        ORDER BY purchase_order.schedule_date ASC, purchase_order.name ASC
        LIMIT %s
        """,
        (company, warehouse, ON_THE_WAY_LIMIT),
        as_dict=True,
    )
    return [
        {
            "supplier": cstr(row.get("supplier_name")),
            "total": _money(row.get("outlet_total"), cstr(row.get("currency")), "Purchase Order Item.amount total"),
            "when": (
                f"sent {_readable_date(row.get('transaction_date'))} · arrives {_readable_date(row.get('schedule_date'))}"
                if row.get("schedule_date")
                else f"sent {_readable_date(row.get('transaction_date'))}"
            ),
        }
        for row in rows
    ]


def _outlet_draft_count(company: str, warehouse: str) -> int:
    rows = frappe.db.sql(
        """
        SELECT COUNT(DISTINCT purchase_order.name) AS draft_count
        FROM `tabPurchase Order` purchase_order
        INNER JOIN `tabPurchase Order Item` line ON line.parent = purchase_order.name
        WHERE line.parenttype = 'Purchase Order'
          AND purchase_order.company = %s AND purchase_order.docstatus = 0
          AND COALESCE(NULLIF(line.warehouse, ''), purchase_order.set_warehouse) = %s
        """,
        (company, warehouse),
        as_dict=True,
    )
    return cint(rows[0].get("draft_count") if rows else 0)


@frappe.whitelist(methods=["POST"])
def approve_supplier_order(
    *,
    order: str,
    outlet: str,
    expected_modified: str,
) -> dict[str, Any]:
    """Approve the exact draft shown on Supplies; never send it externally."""

    order_name = cstr(order).strip()
    outlet_name = cstr(outlet).strip()
    shown_revision = cstr(expected_modified).strip()
    if not order_name or not outlet_name or not shown_revision:
        frappe.throw(_("Refresh Supplies and choose the order again."), frappe.ValidationError)
    context = resolve_director_context(warehouse=outlet_name)
    if not context.company or context.warehouse != outlet_name:
        frappe.throw(_("You do not have access to this outlet."), frappe.PermissionError)

    frappe.db.sql(
        "SELECT name FROM `tabPurchase Order` WHERE name = %s FOR UPDATE",
        (order_name,),
    )
    document = frappe.get_doc("Purchase Order", order_name)
    if cstr(getattr(document, "company", None)).strip() != context.company:
        frappe.throw(_("This order belongs to another café."), frappe.PermissionError)
    lines = list(getattr(document, "items", None) or [])
    line_outlets = {
        cstr(getattr(row, "warehouse", None) or getattr(document, "set_warehouse", None)).strip()
        for row in lines
    }
    if not lines or line_outlets != {outlet_name}:
        frappe.throw(
            _("Ask Support to check this order. It must belong to one outlet."),
            frappe.ValidationError,
        )
    if cint(getattr(document, "docstatus", 0)) == 1:
        return {
            "status": "already_approved",
            "message": _("This order was already approved. Nothing changed."),
        }
    if cint(getattr(document, "docstatus", 0)) != 0:
        frappe.throw(_("This order can no longer be approved."), frappe.ValidationError)
    if cstr(getattr(document, "modified", None)).strip() != shown_revision:
        frappe.throw(
            _("This order changed. Refresh Supplies and check it again."),
            frappe.ValidationError,
        )
    if not cstr(getattr(document, "supplier", None)).strip():
        frappe.throw(_("Ask Support to add the supplier before approving this order."), frappe.ValidationError)
    for row in lines:
        try:
            quantity = Decimal(cstr(getattr(row, "qty", None)).strip())
            rate = Decimal(cstr(getattr(row, "rate", None) or "0").strip())
        except (InvalidOperation, ValueError):
            frappe.throw(_("Ask Support to check an amount in this order."), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise")
        if not quantity.is_finite() or quantity <= 0 or not rate.is_finite() or rate < 0:
            frappe.throw(_("Ask Support to check an amount in this order."), frappe.ValidationError)

    try:
        document.flags.ignore_permissions = True
        document.submit()
    except Exception as error:
        log_sanitized_error("JiJi supplier order approval failed", error)
        frappe.throw(
            _("JiJi could not approve this order. Nothing was sent. Refresh and try again."),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error
    return {
        "status": "approved",
        "message": _("Order approved. JiJi did not send it to the supplier."),
    }


@frappe.whitelist(methods=["GET"])
def get_supplier_orders(
    *, company: str | None = None, warehouse: str | None = None
) -> dict[str, Any]:
    """What is waiting to be ordered, why, and from whom."""
    context = resolve_director_context(company=company, warehouse=warehouse)
    if not context.company:
        return {
            "status": context.status,
            "companies": list(context.allowed_companies),
        }

    if not context.warehouse:
        return {
            "status": context.status,
            "company": context.company,
            "warehouses": list(context.allowed_warehouses),
        }

    draft_count = _outlet_draft_count(context.company, context.warehouse)
    drafts = _draft_orders(context.company, context.warehouse)[:DRAFT_PAGE_SIZE]
    detailed: list[dict[str, Any]] = []
    for row in drafts[:DETAILED_DRAFTS]:
        supplier = cstr(row.get("supplier"))
        currency = cstr(row.get("currency"))
        lines = _order_lines(cstr(row["name"]), context.warehouse)
        record = _delivery_record(supplier, context.company)
        runout = _runout_days(
            context.company,
            context.warehouse,
            {cstr(line.get("item_code")) for line in lines},
        )
        detailed.append({
            "order": cstr(row["name"]),
            "revision": cstr(row.get("modified")),
            "supplier": cstr(row.get("supplier_name") or supplier),
            "reliability": _reliability_line(record),
            "total": _money(row.get("outlet_total"), currency, "Purchase Order Item.amount total"),
            "arrives": _readable_date(row.get("schedule_date")),
            "why": _why_line(lines, runout, record),
            "lines": [
                {
                    "item": cstr(line.get("item_name") or line.get("item_code")),
                    "amount": _line_amount(line),
                    "cost": _money(line.get("amount"), currency, "Purchase Order Item.amount"),
                }
                for line in lines
            ],
            # Only orders that finished. One still in the air is not history,
            # and it is already named under "Already on the way" -- saying it
            # twice on one screen is the duplication this page exists to undo.
            "history": [
                {
                    "when": _short_date(entry.get("transaction_date")),
                    "arrival": _arrival_words(entry),
                    "on_time": entry["late_days"] <= 0,
                    "total": _money(entry.get("grand_total"), cstr(entry.get("currency")), "Purchase Order.grand_total"),
                }
                for entry in [row for row in record if row["late_days"] is not None][:HISTORY_LIMIT]
            ],
        })

    return {
        "status": "ok",
        "draft_count": draft_count,
        "drafts_have_more": draft_count > len(drafts),
        "detailed": detailed,
        "other_drafts": [
            {
                "supplier": cstr(row.get("supplier_name") or row.get("supplier")),
                "total": _money(row.get("outlet_total"), cstr(row.get("currency")), "Purchase Order Item.amount total"),
            }
            for row in drafts[DETAILED_DRAFTS:]
        ],
        "selected_outlet": context.warehouse,
        "on_the_way": _on_the_way(context.company, context.warehouse),
    }
