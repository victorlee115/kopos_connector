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

from kopos_connector.api.cafe_units import amount_phrase

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
SOLD_PURPOSES = ("Material Issue",)
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


def _items_on_draft_orders(company: str) -> set[str]:
    """Items already on a supplier order a director has not sent yet.

    An item running low that nobody has ordered is the one that needs a
    person; one already on a draft is waiting on the same decision the
    supplies page is asking for.
    """
    orders = frappe.get_all(
        "Purchase Order",
        filters={"company": company, "docstatus": 0},
        pluck="name",
        limit_page_length=0,
    )
    if not orders:
        return set()
    return {
        cstr(row.get("item_code")).strip()
        for row in frappe.get_all(
            "Purchase Order Item",
            filters={"parent": ["in", orders], "parenttype": "Purchase Order"},
            fields=["item_code"],
            limit_page_length=0,
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


def _stock_entries_today(company: str, purposes: tuple[str, ...]) -> list[str]:
    filters: dict[str, Any] = {
        "company": company,
        "docstatus": 1,
        "posting_date": nowdate(),
        "purpose": ["in", list(purposes)],
    }
    return frappe.get_all("Stock Entry", filters=filters, pluck="name", limit_page_length=0)


def _entry_lines(entries: list[str], warehouse: str, *, source: bool) -> list[dict[str, Any]]:
    """The item rows of some Stock Entries, on the side of the outlet's store.

    ``source`` picks the warehouse the stock left; otherwise the one it
    arrived in. A transfer between two of a café's own stores would otherwise
    be counted twice.
    """
    if not entries:
        return []
    field = "s_warehouse" if source else "t_warehouse"
    return frappe.get_all(
        "Stock Entry Detail",
        filters={"parent": ["in", entries], "parenttype": "Stock Entry", field: warehouse},
        fields=["item_code", "item_name", "qty", "uom", "amount"],
        limit_page_length=0,
    )


def _summarize(lines: list[dict[str, Any]]) -> str:
    """"152 g matcha, 4.1 L milk, 38 cups" -- the day in things, not rows."""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for line in lines:
        key = (cstr(line.get("item_code")), cstr(line.get("uom")))
        entry = totals.setdefault(key, {"name": cstr(line.get("item_name") or line.get("item_code")), "qty": 0.0})
        entry["qty"] += flt(line.get("qty"))
    ordered = sorted(totals.items(), key=lambda pair: -pair[1]["qty"])[:MOVEMENT_LIMIT]
    return ", ".join(
        f"{amount_phrase(row['qty'], unit)} {row['name'].lower()}" for (_item, unit), row in ordered
    )


def _waste_today(company: str, warehouse: str) -> dict[str, Any] | None:
    """What was thrown away today, and who threw it.

    A waste figure with no cause is a number nobody can act on. FB Waste Event
    already records both, so the desk reads it rather than inferring a reason
    from a Stock Entry.
    """
    events = frappe.get_all(
        "FB Waste Event",
        filters={"company": company, "warehouse": warehouse, "docstatus": ["<", 2]},
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
        limit_page_length=0,
    )
    if not lines:
        return None
    described = ", ".join(
        amount_phrase(flt(line.get("qty")), cstr(line.get("uom"))) + " " + cstr(line.get("item"))
        for line in lines[:MOVEMENT_LIMIT]
    )
    reason = cstr(today[0].get("reason_text")).strip() or cstr(today[0].get("reason_code")).strip()
    return {
        "title": f"Thrown away today{f' · {reason}' if reason else ''}",
        "detail": described,
        "route": ["Form", "FB Waste Event", cstr(today[0]["name"])],
    }


def _money(value: Any) -> str:
    return f"RM {flt(value):,.2f}"


def _counted_tiles(
    company: str,
    warehouse: str,
    low: list[dict[str, Any]],
    moved: list[dict[str, Any]],
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
    arriving = frappe.get_all(
        "Purchase Order",
        filters={"company": company, "docstatus": 1, "per_received": ["<", 100]},
        fields=["name", "schedule_date"],
        order_by="schedule_date asc",
        limit_page_length=0,
    )
    batches = sum(1 for row in moved if row.get("title") == "Made in store")
    week_start = add_days(nowdate(), -6)
    waste_rows = frappe.get_all(
        "Stock Entry",
        filters={
            "company": company, "docstatus": 1,
            "posting_date": [">=", week_start],
            "name": ["in", [
                cstr(name) for name in frappe.get_all(
                    "FB Waste Event",
                    filters={"company": company, "warehouse": warehouse, "docstatus": ["<", 2]},
                    pluck="stock_entry", limit_page_length=0,
                ) if cstr(name).strip()
            ] or [""]],
        },
        fields=["total_outgoing_value"],
        limit_page_length=0,
    )
    in_transit = cint(frappe.db.count(
        "Stock Entry",
        {"company": company, "docstatus": 1, "purpose": "Material Transfer", "posting_date": nowdate()},
    ))

    first_arrival = next((row.get("schedule_date") for row in arriving if row.get("schedule_date")), None)
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
         "attention": bool(low), "route": ["List", "Item"]},
        {"label": "Stock checks", "value": str(counts_waiting),
         "detail": "waiting for your check" if counts_waiting else "nothing waiting",
         "attention": counts_waiting > 0, "route": ["List", "FB Inventory Count Task"]},
        {"label": "Deliveries", "value": str(len(arriving)),
         "detail": (f"arriving {_weekday(first_arrival)}" if first_arrival else "nothing on the way"),
         "attention": False, "route": ["List", "Purchase Order", {"docstatus": 1}]},
        {"label": "Made in store", "value": str(batches),
         "detail": "batches today" if batches else "nothing made today",
         "attention": False, "route": ["List", "Stock Entry"]},
        {"label": "Waste", "value": _money(sum(flt(row.get("total_outgoing_value")) for row in waste_rows)),
         "detail": "this week", "attention": False, "route": ["List", "FB Waste Event"]},
        {"label": "Moved between shops", "value": str(in_transit),
         "detail": "moving today" if in_transit else "nothing moving",
         "attention": False, "route": ["List", "Stock Entry", {"purpose": "Material Transfer"}]},
    ]


def _weekday(value: Any) -> str:
    try:
        return getdate(value).strftime("%A")
    except (ValueError, TypeError):
        return cstr(value)


@frappe.whitelist(methods=["GET"])
def get_stock_desk(*, warehouse: str | None = None) -> dict[str, Any]:
    """The two questions the six area cards do not answer."""
    from kopos_connector.api.director_scope import director_company, director_warehouse

    company = director_company()
    if not company:
        return {"status": "no_company"}
    store = cstr(warehouse).strip() or director_warehouse(company)
    if not store:
        return {"status": "no_outlet", "company": company}

    try:
        runouts, no_estimate = _runouts(company, store)
    except Exception:
        # Six area cards are still worth reading without this one.
        frappe.log_error(title="JiJi stock desk could not read the runout snapshot")
        runouts, no_estimate = [], "JiJi could not read the stock estimate just now."

    ordered = _items_on_draft_orders(company)
    low = [
        {
            "item": row["item"],
            "name": row["name"],
            "days": _days_phrase(row["days"]),
            "fill": max(2, min(100, round(row["days"] / METER_FULL_DAYS * 100))),
            "on_order": row["item"] in ordered,
            "state": "Already on an order" if row["item"] in ordered else "Not ordered yet",
            "route": ["Form", "Item", row["item"]],
        }
        for row in runouts
        if row["days"] <= LOW_STOCK_DAYS
    ][:LOW_STOCK_LIMIT]

    moved = []
    sold = _entry_lines(_stock_entries_today(company, SOLD_PURPOSES), store, source=True)
    if sold:
        moved.append({"title": "Selling used", "detail": _summarize(sold), "route": ["List", "Stock Entry"]})
    made = _entry_lines(_stock_entries_today(company, MADE_PURPOSES), store, source=False)
    if made:
        moved.append({"title": "Made in store", "detail": _summarize(made), "route": ["List", "Stock Entry"]})
    waste = _waste_today(company, store)
    if waste:
        moved.append(waste)

    return {
        "status": "ok",
        "company": company,
        "warehouse": store,
        "tiles": _counted_tiles(company, store, low, moved, no_estimate),
        "checked_at": format_datetime(now_datetime(), "HH:mm"),
        "running_low": low,
        # Empty because nothing is low, or empty because JiJi cannot say yet.
        # A director cannot tell those apart from a blank space.
        "running_low_note": (
            ""
            if low
            else (no_estimate or "Nothing is due to run out in the next three weeks.")
        ),
        "moved": moved,
        "moved_empty_note": "Nothing has moved in or out of this outlet today.",
        "moved_note": "The tablets wrote this, nobody typed it",
    }
