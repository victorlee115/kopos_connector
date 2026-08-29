# pyright: reportMissingImports=false

"""Read-only composition for the supplies page.

Approving a supplier order today means reading a Purchase Order form: five
tabs, a Submit button as the most prominent control, an item reading
``SMOKE-CU...``, ``100``, ``Nos``, and six of eight grid columns truncated. A
director approving that cannot see what the item is, why it is being ordered,
or whether this supplier delivers on time.

The decision needs five facts and a reason: what, how much, what it costs, when
it lands, whether they deliver on time -- and why JiJi suggested it. This module
gathers exactly those and writes nothing. **Sending stays in ERPNext.** The
Purchase Order is the accounting document and the only place it is submitted,
so this page ends at a link to it rather than growing a second submit path.

The "why" line reads the runout figure from the stock snapshot that already
computes it for the tablet, so the café is never told two different days.

One deliberate duplication: the tablet turns ERP units into café words in
``unit-display.ts`` and this module does the same for the desk. They are two
presentations of one stored quantity, not two authorities over it -- neither
changes what is ordered -- and they cannot be one file across a language
boundary.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cstr, flt, getdate

from kopos_connector.api.director_scope import director_company

# How many drafts get the full treatment. The design shows one decision at a
# time; the rest are one line each until the director opens them.
DETAILED_DRAFTS = 1
# Enough history to see a pattern, not enough to become a report.
HISTORY_LIMIT = 3
RELIABILITY_SAMPLE = 6
ON_THE_WAY_LIMIT = 3

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


def _unit_words(unit: str) -> tuple[str, str]:
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


def _amount_phrase(qty: float, unit: str) -> str:
    """"12 bags", "1 carton", "1 L", "250 g" -- an amount someone can picture."""
    name = cstr(unit).strip().lower()
    bigger = _SCALED_UNITS.get(name)
    if bigger and flt(qty) >= 1000 and flt(qty) % 1000 == 0:
        qty, unit = flt(qty) / 1000, bigger
    singular, plural = _unit_words(unit)
    rounded = round(flt(qty), 3)
    whole = int(rounded)
    number = str(whole) if rounded == whole else f"{rounded:g}"
    word = singular if rounded == 1 else plural
    return f"{number} {word}".strip()


def _line_amount(row: dict[str, Any]) -> str:
    """What is being bought, in packs and what is inside one.

    A Purchase Order stores 12 Bag with a conversion factor of 250 into Gram.
    "12 bags · 250 g each" is the same fact, said the way it is bought.
    """
    ordered = _amount_phrase(flt(row.get("qty")), cstr(row.get("uom")))
    factor = flt(row.get("conversion_factor"))
    stock_unit = cstr(row.get("stock_uom"))
    same_unit = cstr(row.get("uom")).strip().lower() == stock_unit.strip().lower()
    if same_unit or factor <= 0 or factor == 1:
        return ordered
    return f"{ordered} · {_amount_phrase(factor, stock_unit)} each"


def _money(value: Any, currency: str) -> str:
    symbol = "RM" if cstr(currency).strip().upper() in {"", "MYR"} else cstr(currency).strip()
    return f"{symbol} {flt(value):,.2f}"


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


def _draft_orders(company: str) -> list[dict[str, Any]]:
    return frappe.get_all(
        "Purchase Order",
        filters={"company": company, "docstatus": 0},
        fields=["name", "supplier", "supplier_name", "grand_total", "currency", "schedule_date"],
        order_by="schedule_date asc, creation asc",
        limit_page_length=0,
    )


def _order_lines(order: str) -> list[dict[str, Any]]:
    return frappe.get_all(
        "Purchase Order Item",
        filters={"parent": order, "parenttype": "Purchase Order"},
        fields=["item_code", "item_name", "qty", "uom", "conversion_factor", "stock_uom", "amount", "warehouse"],
        order_by="idx asc",
        limit_page_length=0,
    )


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

    receipts = frappe.get_all(
        "Purchase Receipt Item",
        filters={"purchase_order": ["in", [row["name"] for row in orders]], "docstatus": 1},
        fields=["purchase_order", "parent"],
        limit_page_length=0,
    )
    receipt_parents = {cstr(row.get("parent")) for row in receipts}
    posted: dict[str, Any] = {}
    if receipt_parents:
        for row in frappe.get_all(
            "Purchase Receipt",
            filters={"name": ["in", sorted(receipt_parents)]},
            fields=["name", "posting_date"],
            limit_page_length=0,
        ):
            posted[cstr(row.get("name"))] = row.get("posting_date")
    arrived: dict[str, Any] = {}
    for row in receipts:
        order = cstr(row.get("purchase_order"))
        when = posted.get(cstr(row.get("parent")))
        if when and (order not in arrived or getdate(when) > getdate(arrived[order])):
            arrived[order] = when

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

        snapshot = build_edge_inventory_snapshot(company=company, warehouse=warehouse, limit=len(items) + 20)
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


def _on_the_way(company: str) -> list[dict[str, Any]]:
    """Orders already sent. Nothing here is a decision; it is what not to reorder."""
    rows = frappe.get_all(
        "Purchase Order",
        filters={"company": company, "docstatus": 1, "per_received": ["<", 100]},
        fields=["name", "supplier_name", "grand_total", "currency", "schedule_date", "transaction_date"],
        order_by="schedule_date asc",
        limit_page_length=ON_THE_WAY_LIMIT,
    )
    return [
        {
            "order": cstr(row["name"]),
            "supplier": cstr(row.get("supplier_name")),
            "total": _money(row.get("grand_total"), cstr(row.get("currency"))),
            "when": (
                f"sent {_readable_date(row.get('transaction_date'))} · arrives {_readable_date(row.get('schedule_date'))}"
                if row.get("schedule_date")
                else f"sent {_readable_date(row.get('transaction_date'))}"
            ),
            "route": ["Form", "Purchase Order", cstr(row["name"])],
        }
        for row in rows
    ]


@frappe.whitelist(methods=["GET"])
def get_supplier_orders() -> dict[str, Any]:
    """What is waiting to be ordered, why, and from whom."""
    company = director_company()
    if not company:
        return {"status": "no_company"}

    drafts = _draft_orders(company)
    detailed: list[dict[str, Any]] = []
    for row in drafts[:DETAILED_DRAFTS]:
        supplier = cstr(row.get("supplier"))
        currency = cstr(row.get("currency"))
        lines = _order_lines(cstr(row["name"]))
        record = _delivery_record(supplier, company)
        warehouse = next((cstr(line.get("warehouse")) for line in lines if line.get("warehouse")), "")
        runout = _runout_days(company, warehouse, {cstr(line.get("item_code")) for line in lines})
        detailed.append({
            "order": cstr(row["name"]),
            "supplier": cstr(row.get("supplier_name") or supplier),
            "reliability": _reliability_line(record),
            "total": _money(row.get("grand_total"), currency),
            "arrives": _readable_date(row.get("schedule_date")),
            "why": _why_line(lines, runout, record),
            "lines": [
                {
                    "item": cstr(line.get("item_name") or line.get("item_code")),
                    "amount": _line_amount(line),
                    "cost": _money(line.get("amount"), currency),
                    "route": ["Form", "Item", cstr(line.get("item_code"))],
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
                    "total": _money(entry.get("grand_total"), cstr(entry.get("currency"))),
                    "route": ["Form", "Purchase Order", cstr(entry.get("name"))],
                }
                for entry in [row for row in record if row["late_days"] is not None][:HISTORY_LIMIT]
            ],
            "route": ["Form", "Purchase Order", cstr(row["name"])],
        })

    return {
        "status": "ok",
        "company": company,
        "draft_count": len(drafts),
        "detailed": detailed,
        "other_drafts": [
            {
                "order": cstr(row["name"]),
                "supplier": cstr(row.get("supplier_name") or row.get("supplier")),
                "total": _money(row.get("grand_total"), cstr(row.get("currency"))),
                "route": ["Form", "Purchase Order", cstr(row["name"])],
            }
            for row in drafts[DETAILED_DRAFTS:]
        ],
        "on_the_way": _on_the_way(company),
        "orders_route": ["List", "Purchase Order", {"docstatus": 0}],
    }
