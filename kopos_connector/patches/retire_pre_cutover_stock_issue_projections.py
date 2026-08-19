# pyright: reportMissingImports=false

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import frappe
from frappe.utils import cstr

from kopos_connector.kopos.services.inventory_autopilot.projection_worker import (
    order_is_pre_cutover,
)


PROJECTION_TYPE = "Stock Issue"
LEGACY_STATES = ("Failed", "Dead Letter")
RETIRED_STATE = "Not Evaluated"
ORDER_FIELDS = ("name", "company", "booth_warehouse", "sale_datetime")
CHUNK_SIZE = 500


def execute() -> None:
    """Retire pre-cutover Stock Issue projections left by the deployed connector.

    The redesigned worker returns ``Not Evaluated`` and writes no log row for a
    pre-cutover order, so a ``Failed`` ingredient projection against pre-cutover
    history can only have been written by the previously deployed connector.
    Leaving those rows alone makes ``get_autopilot_health`` report a projection
    failure forever, and the retry lease keeps them nominally claimable.

    Reclassifying removes the false alarm while preserving the evidence:
    ``last_error``, ``dead_lettered_at``, and ``retry_count`` are untouched, no
    ``Succeeded`` or post-cutover row is considered, and no stock, GL, or
    commercial document is read or written.  Replaying the patch is a no-op.
    """

    if not frappe.db.exists("DocType", "FB Projection Log"):
        return

    frappe.reload_doc("kopos", "doctype", "fb_projection_log")

    candidates = frappe.get_all(
        "FB Projection Log",
        filters={
            "projection_type": PROJECTION_TYPE,
            "source_doctype": "FB Order",
            "state": ["in", list(LEGACY_STATES)],
        },
        fields=["name", "source_name"],
        limit_page_length=0,
    )
    if not candidates:
        return

    order_names = sorted({cstr(row.get("source_name")).strip() for row in candidates})
    order_names = [name for name in order_names if name]
    if not order_names:
        return

    pre_cutover = _pre_cutover_order_names(order_names)
    if not pre_cutover:
        return

    retire = [
        cstr(row.get("name"))
        for row in candidates
        if cstr(row.get("source_name")).strip() in pre_cutover
    ]
    for index in range(0, len(retire), CHUNK_SIZE):
        _retire_chunk(retire[index : index + CHUNK_SIZE])
    frappe.db.commit()


def _pre_cutover_order_names(order_names: list[str]) -> set[str]:
    """Classify each order once using the worker's own cutover rule.

    An order that no longer exists is deliberately left alone: the patch
    reclassifies only history it can positively identify as pre-cutover.
    """

    pre_cutover: set[str] = set()
    for index in range(0, len(order_names), CHUNK_SIZE):
        chunk = order_names[index : index + CHUNK_SIZE]
        rows = frappe.get_all(
            "FB Order",
            filters={"name": ["in", chunk]},
            fields=list(ORDER_FIELDS),
            limit_page_length=0,
        )
        for row in rows:
            order = _as_order(row)
            if order_is_pre_cutover(order):
                pre_cutover.add(cstr(row.get("name")).strip())
    return pre_cutover


def _as_order(row: Any) -> SimpleNamespace:
    return SimpleNamespace(**{field: row.get(field) for field in ORDER_FIELDS})


def _retire_chunk(names: list[str]) -> None:
    """Re-assert the legacy state in the WHERE clause so replay cannot drift."""

    placeholders = ", ".join(["%s"] * len(names))
    state_placeholders = ", ".join(["%s"] * len(LEGACY_STATES))
    frappe.db.sql(
        f"""
        UPDATE `tabFB Projection Log`
        SET state = %s,
            next_retry_at = NULL,
            lease_token = NULL,
            lease_expires_at = NULL
        WHERE name IN ({placeholders})
          AND projection_type = %s
          AND state IN ({state_placeholders})
        """,
        tuple([RETIRED_STATE, *names, PROJECTION_TYPE, *LEGACY_STATES]),
    )
