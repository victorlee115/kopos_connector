# pyright: reportMissingImports=false

"""What is running low, and what moved today.

The stock page answers six areas well and one question badly. It says three
things are running low and never says which three, so a director reading
"Running low: 3" has to open a report to learn whether it matters. It also
shows a waste figure with no cause, which is a number nobody can act on.

This module names them. Every figure is read from the service that already
owns it -- how long something lasts comes from the same snapshot the tablet
reads, and what moved comes from the Stock Entries that recorded it -- so the
desk and the counter can never describe the same day differently.

It writes nothing.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import add_days, cint, cstr, flt, format_datetime, getdate, now_datetime, nowdate

from kopos_connector.api.cafe_money import company_currency, money_sen, money_text
from kopos_connector.api.cafe_units import amount_of_phrase
from kopos_connector.api.director_scope import resolve_director_context

# Past this, an item is simply in stock. Inside it, a director can still act
# before it runs out, because a delivery takes days.
LOW_STOCK_DAYS = 21
# The bar is drawn against this, so "12 days left" reads as most of a month.
METER_FULL_DAYS = 25
LOW_STOCK_LIMIT = 8
MOVEMENT_LIMIT = 6
# The snapshot contract caps a read at 100 items. Asking for more is refused,
# not truncated, which cost the whole block on a real site.
SNAPSHOT_LIMIT = 100

# Which Stock Entry purposes tell which part of the day's story.
MADE_PURPOSES = ("Manufacture", "Repack")


def _runouts(company: str, warehouse: str) -> tuple[list[dict[str, Any]], str]:
    """Every item with a runout estimate, soonest first, and why there is none.

    Delegated to the snapshot the tablet reads. Recomputing it here would give
    the café two answers to "when does the matcha run out".

    A café that has not traded for four weeks since it started tracking stock
    has no reliable estimate, on purpose -- the forecast refuses to guess from
    a short history. That is worth saying rather than showing a director an
    empty space, so the snapshot's own reason comes back with the rows.
    """
    from kopos_connector.kopos.services.inventory_autopilot.edge_snapshot import (
        build_edge_inventory_snapshot,
    )

    snapshot = build_edge_inventory_snapshot(company=company, warehouse=warehouse, limit=SNAPSHOT_LIMIT)
    rows = []
    reason = ""
    for target in snapshot.get("items") or []:
        runout = target.get("runout") or {}
        if runout.get("days") is None:
            reason = reason or cstr(target.get("runout_reason")).strip()
            continue
        rows.append({
            "item": cstr(target.get("stock_item")).strip(),
            "name": cstr(target.get("name") or target.get("stock_item")).strip(),
            "days": flt(runout.get("days")),
        })
    rows.sort(key=lambda row: row["days"])
    if not rows:
        reason = reason or cstr(snapshot.get("reliability_reason")).strip()
    return rows, reason


def _items_on_draft_orders(company: str, warehouse: str, items: set[str]) -> set[str]:
    """Items already on a supplier order a director has not sent yet.

    An item running low that nobody has ordered is the one that needs a
    person; one already on a draft is waiting on the same decision the
    supplies page is asking for.
    """
    selected = sorted(item for item in items if item)
    if not selected:
        return set()
    placeholders = ", ".join(["%s"] * len(selected))
    return {
        cstr(row.get("item_code")).strip()
        for row in frappe.db.sql(
            f"""
            SELECT DISTINCT line.item_code
            FROM `tabPurchase Order Item` line
            INNER JOIN `tabPurchase Order` purchase_order
              ON purchase_order.name = line.parent
            WHERE line.parenttype = 'Purchase Order'
              AND purchase_order.company = %s
              AND purchase_order.docstatus = 0
              AND COALESCE(NULLIF(line.warehouse, ''), purchase_order.set_warehouse) = %s
              AND line.item_code IN ({placeholders})
            """,
            (company, warehouse, *selected),
            as_dict=True,
        )
        if cstr(row.get("item_code")).strip()
    }


def _days_phrase(days: float) -> str:
    whole = int(days)
    if whole <= 0:
        return "out today"
    if whole == 1:
        return "about 1 day left"
    return f"about {whole} days left"


def _entry_lines_today(
    company: str,
    warehouse: str,
    purposes: tuple[str, ...],
    *,
    source: bool,
) -> list[dict[str, Any]]:
    """The item rows of some Stock Entries, on the side of the outlet's store.

    ``source`` picks the warehouse the stock left; otherwise the one it
    arrived in. A transfer between two of a café's own stores would otherwise
    be counted twice.
    """
    field = "s_warehouse" if source else "t_warehouse"
    purpose_placeholders = ", ".join(["%s"] * len(purposes))
    return frappe.db.sql(
        f"""
        SELECT line.item_code, MAX(line.item_name) AS item_name,
               SUM(line.qty) AS qty, line.uom
        FROM `tabStock Entry Detail` line
        INNER JOIN `tabStock Entry` entry ON entry.name = line.parent
        WHERE line.parenttype = 'Stock Entry'
          AND entry.company = %s AND entry.docstatus = 1
          AND entry.posting_date = %s
          AND entry.purpose IN ({purpose_placeholders})
          AND line.`{field}` = %s
        GROUP BY line.item_code, line.uom
        ORDER BY SUM(line.qty) DESC, line.item_code ASC
        LIMIT %s
        """,
        (company, nowdate(), *purposes, warehouse, MOVEMENT_LIMIT),
        as_dict=True,
    )


def _sale_issue_lines_today(company: str, warehouse: str) -> list[dict[str, Any]]:
    """Ingredients consumed by completed sales, excluding remakes and ad-hoc issues."""

    return frappe.db.sql(
        """
        SELECT line.item_code, MAX(line.item_name) AS item_name,
               SUM(line.qty) AS qty, line.uom
        FROM `tabStock Entry Detail` line
        INNER JOIN `tabStock Entry` entry ON entry.name = line.parent
        INNER JOIN `tabFB Order` fb_order
          ON fb_order.name = entry.custom_fb_order
         AND fb_order.ingredient_stock_entry = entry.name
        WHERE line.parenttype = 'Stock Entry'
          AND entry.company = %s AND entry.docstatus = 1
          AND entry.posting_date = %s AND entry.purpose = 'Material Issue'
          AND line.s_warehouse = %s
        GROUP BY line.item_code, line.uom
        ORDER BY SUM(line.qty) DESC, line.item_code ASC
        LIMIT %s
        """,
        (company, nowdate(), warehouse, MOVEMENT_LIMIT),
        as_dict=True,
    )


def _delivery_summary(company: str, warehouse: str) -> dict[str, Any]:
    rows = frappe.db.sql(
        """
        SELECT COUNT(DISTINCT purchase_order.name) AS delivery_count,
               MIN(line.schedule_date) AS first_arrival
        FROM `tabPurchase Order` purchase_order
        INNER JOIN `tabPurchase Order Item` line ON line.parent = purchase_order.name
        WHERE line.parenttype = 'Purchase Order'
          AND purchase_order.company = %s
          AND purchase_order.docstatus = 1
          AND purchase_order.per_received < 100
          AND COALESCE(NULLIF(line.warehouse, ''), purchase_order.set_warehouse) = %s
        """,
        (company, warehouse),
        as_dict=True,
    )
    return rows[0] if rows else {"delivery_count": 0, "first_arrival": None}


def _stock_entry_count_today(
    company: str,
    warehouse: str,
    purposes: tuple[str, ...],
) -> int:
    placeholders = ", ".join(["%s"] * len(purposes))
    rows = frappe.db.sql(
        f"""
        SELECT COUNT(DISTINCT entry.name) AS entry_count
        FROM `tabStock Entry` entry
        INNER JOIN `tabStock Entry Detail` line ON line.parent = entry.name
        WHERE line.parenttype = 'Stock Entry'
          AND entry.company = %s AND entry.docstatus = 1
          AND entry.posting_date = %s
          AND entry.purpose IN ({placeholders})
          AND (line.s_warehouse = %s OR line.t_warehouse = %s)
        """,
        (company, nowdate(), *purposes, warehouse, warehouse),
        as_dict=True,
    )
    return cint(rows[0].get("entry_count") if rows else 0)


def _summarize(lines: list[dict[str, Any]]) -> str:
    """"152 g matcha, 4.1 L milk, 38 cups" -- the day in things, not rows."""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for line in lines:
        key = (cstr(line.get("item_code")), cstr(line.get("uom")))
        entry = totals.setdefault(
            key,
            {
                "name": cstr(line.get("item_name")).strip()
                or "Ingredient or supply",
                "qty": 0.0,
            },
        )
        entry["qty"] += flt(line.get("qty"))
    ordered = sorted(totals.items(), key=lambda pair: -pair[1]["qty"])[:MOVEMENT_LIMIT]
    return ", ".join(
        amount_of_phrase(row["qty"], unit, row["name"])
        for (_item, unit), row in ordered
    )


def _waste_today(company: str, warehouse: str) -> dict[str, Any] | None:
    """What was thrown away today, and who threw it.

    A waste figure with no cause is a number nobody can act on. FB Waste Event
    already records both, so the desk reads it rather than inferring a reason
    from a Stock Entry.
    """
    events = frappe.get_all(
        "FB Waste Event",
        filters={"company": company, "warehouse": warehouse, "docstatus": 1},
        fields=["name", "reason_text", "reason_code", "stock_entry", "modified"],
        order_by="modified desc",
        limit_page_length=MOVEMENT_LIMIT,
    )
    today = [row for row in events if cstr(row.get("modified"))[:10] == nowdate()]
    if not today:
        return None
    lines = frappe.get_all(
        "FB Waste Event Line",
        filters={"parent": ["in", [row["name"] for row in today]], "parenttype": "FB Waste Event"},
        fields=["item", "qty", "uom"],
        limit_page_length=(MOVEMENT_LIMIT * 10) + 1,
    )
    if not lines:
        return None
    item_codes = sorted(
        {cstr(line.get("item")).strip() for line in lines if cstr(line.get("item")).strip()}
    )
    item_names = {
        cstr(row.get("name")).strip(): cstr(row.get("item_name")).strip()
        for row in frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes]},
            fields=["name", "item_name"],
            limit_page_length=len(item_codes),
        )
    } if item_codes else {}
    described = _summarize([
        {
            "item_code": cstr(line.get("item")).strip(),
            "item_name": item_names.get(cstr(line.get("item")).strip()),
            "qty": line.get("qty"),
            "uom": line.get("uom"),
        }
        for line in lines[:MOVEMENT_LIMIT]
    ])
    if len(lines) > MOVEMENT_LIMIT:
        # The query is deliberately bounded, so do not turn the rows returned
        # into a false claim about the total number still hidden.
        described += ", and more"
    reason = cstr(today[0].get("reason_text")).strip() or cstr(today[0].get("reason_code")).strip()
    return {
        "title": f"Thrown away today{f' · {reason}' if reason else ''}",
        "detail": described,
    }


def _counted_tiles(
    company: str,
    warehouse: str,
    low: list[dict[str, Any]],
    no_estimate: str,
) -> list[dict[str, Any]]:
    """The six things a director counts, not the six areas support watches.

    The page already reports how the automatic work is behaving, which is the
    right question for support and the wrong one for a café. These are the
    quantities: what is short, what is waiting, what is coming, what was made,
    what was thrown away, what is in transit.
    """
    # The health cards on the same page count open tasks with this set. A
    # second definition here would put two different numbers for one question
    # on one screen, which is the failure this page exists to undo.
    from kopos_connector.api.inventory import OPEN_COUNT_TASK_STATUSES

    counts_waiting = cint(frappe.db.count(
        "FB Inventory Count Task",
        {"company": company, "warehouse": warehouse, "status": ["in", sorted(OPEN_COUNT_TASK_STATUSES)]},
    ))
    deliveries = _delivery_summary(company, warehouse)
    arriving_count = cint(deliveries.get("delivery_count"))
    batches = _stock_entry_count_today(company, warehouse, MADE_PURPOSES)
    week_start = add_days(nowdate(), -6)
    waste_total = frappe.db.sql(
        """
        SELECT COALESCE(SUM(entry.total_outgoing_value), 0) AS total
        FROM `tabFB Waste Event` waste
        INNER JOIN `tabStock Entry` entry ON entry.name = waste.stock_entry
        WHERE waste.company = %s AND waste.warehouse = %s
          AND waste.docstatus = 1 AND entry.docstatus = 1
          AND entry.posting_date >= %s
        """,
        (company, warehouse, week_start),
        as_dict=True,
    )
    waste_sen = money_sen(
        waste_total[0].get("total") if waste_total else 0,
        "Stock Entry weekly waste total",
    )
    in_transit = _stock_entry_count_today(company, warehouse, ("Material Transfer",))

    first_arrival = deliveries.get("first_arrival")
    return [
        # "0 · nothing short" is a claim, and it is false when there is no
        # estimate to count. An empty list and an unanswerable question look
        # identical in a number; they must not read identically.
        {"label": "Running low",
         "value": str(len(low)) if low or not no_estimate else "\u2014",
         "detail": (
             ", ".join(row["name"] for row in low[:3])
             if low
             else ("nothing short" if not no_estimate else "JiJi cannot say yet")
         ),
         "attention": bool(low)},
        {"label": "Stock checks", "value": str(counts_waiting),
         "detail": "waiting for your check" if counts_waiting else "nothing waiting",
         "attention": counts_waiting > 0},
        {"label": "Deliveries", "value": str(arriving_count),
         "detail": (f"arriving {_weekday(first_arrival)}" if first_arrival else "nothing on the way"),
         "attention": False},
        {"label": "Made in store", "value": str(batches),
         "detail": "batches today" if batches else "nothing made today",
         "attention": False},
        {"label": "Waste", "value": money_text(waste_sen, company_currency(company)), "value_sen": waste_sen,
         "detail": "this week", "attention": False},
        {"label": "Moved between shops", "value": str(in_transit),
         "detail": "moving today" if in_transit else "nothing moving",
         "attention": False},
    ]


def _weekday(value: Any) -> str:
    try:
        return getdate(value).strftime("%A")
    except (ValueError, TypeError):
        return cstr(value)


def _runout_note(reason: str) -> str:
    """Never put an internal forecast reason on the beginner stock page."""
    value = cstr(reason).strip()
    lowered = value.lower()
    if not value:
        return "Nothing is due to run out in the next three weeks."
    if "could not read" in lowered:
        return value
    if "sales" in lowered or "operating_day" in lowered or "evidence" in lowered:
        return "JiJi needs a few more recent sales before it can estimate what may run out."
    return "JiJi needs more sales and stock information before it can estimate what may run out."


@frappe.whitelist(methods=["GET"])
def get_stock_desk(*, warehouse: str | None = None) -> dict[str, Any]:
    """The two questions the six area cards do not answer."""
    context = resolve_director_context(warehouse=cstr(warehouse).strip() or None)
    if not context.company:
        return {
            "status": context.status,
            "companies": list(context.allowed_companies),
        }
    store = context.warehouse
    if not store:
        return {
            "status": context.status,
            "company": context.company,
            "warehouses": list(context.allowed_warehouses),
        }

    try:
        runouts, no_estimate = _runouts(context.company, store)
    except Exception:
        # Six area cards are still worth reading without this one.
        frappe.log_error(title="JiJi stock desk could not read the runout snapshot")
        runouts, no_estimate = [], "JiJi could not read the stock estimate just now."

    ordered = _items_on_draft_orders(
        context.company, store, {cstr(row.get("item")) for row in runouts}
    )
    low = [
        {
            "item": row["item"],
            "name": row["name"],
            "days": _days_phrase(row["days"]),
            "fill": max(2, min(100, round(row["days"] / METER_FULL_DAYS * 100))),
            "on_order": row["item"] in ordered,
            "state": "Already on an order" if row["item"] in ordered else "Not ordered yet",
        }
        for row in runouts
        if row["days"] <= LOW_STOCK_DAYS
    ][:LOW_STOCK_LIMIT]

    moved = []
    sold = _sale_issue_lines_today(context.company, store)
    if sold:
        moved.append({"title": "Selling used", "detail": _summarize(sold)})
    made = _entry_lines_today(context.company, store, MADE_PURPOSES, source=False)
    if made:
        moved.append({"title": "Made in store", "detail": _summarize(made)})
    waste = _waste_today(context.company, store)
    if waste:
        moved.append(waste)

    return {
        "status": "ok",
        "company": context.company,
        "warehouse": store,
        "tiles": _counted_tiles(context.company, store, low, no_estimate),
        "checked_at": format_datetime(now_datetime(), "HH:mm"),
        "running_low": low,
        # Empty because nothing is low, or empty because JiJi cannot say yet.
        # A director cannot tell those apart from a blank space.
        "running_low_note": (
            ""
            if low
            else _runout_note(no_estimate)
        ),
        "moved": moved,
        "moved_empty_note": "Nothing has moved in or out of this outlet today.",
        "moved_note": "Recorded from today’s sales and stock changes.",
    }
