# pyright: reportMissingImports=false

from __future__ import annotations

import frappe
from frappe.utils import cstr


COMMERCIAL_PROJECTION_TYPES = ("Sales Invoice", "FB Shift")
REQUEUE_MESSAGE = (
    "Returned to automatic retry during upgrade: this sale's projection was "
    "left Pending, where no retry could reach it."
)
CHUNK_SIZE = 500


def execute() -> None:
    """Hand commercial sale projections stranded in Pending back to the retry worker.

    FB Order submission resolves its Sales Invoice and FB Shift projection logs
    to Succeeded or Failed inside the submit transaction, and every commercial
    retry path (the scheduler worker, the cashier retry and support retries)
    selects Failed rows only. A committed commercial row that is still Pending
    was left by retired code, and nothing can ever run it: the August 11
    production backup holds one submitted sale in that state with no Sales
    Invoice.

    Marking the row Failed gives it exactly the handling of an ordinary failed
    sale. The idempotent handler reuses an existing invoice or creates one,
    retry counts and dead-lettering still apply, and the stored error, retry
    count and attempt time are kept. Cancelled orders are left alone. Replaying
    the patch is a no-op.
    """

    if not frappe.db.exists("DocType", "FB Projection Log"):
        return

    candidates = frappe.get_all(
        "FB Projection Log",
        filters={
            "source_doctype": "FB Order",
            "projection_type": ["in", list(COMMERCIAL_PROJECTION_TYPES)],
            "state": "Pending",
        },
        fields=["name", "source_name", "last_error"],
        limit_page_length=0,
    )
    order_names = sorted({cstr(row.get("source_name")).strip() for row in candidates} - {""})
    submitted: set[str] = set()
    for index in range(0, len(order_names), CHUNK_SIZE):
        submitted.update(
            frappe.get_all(
                "FB Order",
                filters={"name": ["in", order_names[index : index + CHUNK_SIZE]], "docstatus": 1},
                pluck="name",
                limit_page_length=0,
            )
        )

    for row in candidates:
        if cstr(row.get("source_name")).strip() not in submitted:
            continue
        previous = cstr(row.get("last_error")).strip()
        message = f"{REQUEUE_MESSAGE} Previous error: {previous}" if previous else REQUEUE_MESSAGE
        frappe.db.set_value(
            "FB Projection Log",
            row.get("name"),
            {
                "state": "Failed",
                "last_error": message[:1000],
                "next_retry_at": None,
                "lease_token": None,
                "lease_expires_at": None,
                "dead_lettered_at": None,
            },
            update_modified=False,
        )
