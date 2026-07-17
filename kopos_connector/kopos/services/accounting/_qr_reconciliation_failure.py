# pyright: reportMissingImports=false

"""Terminal failure disposition for manual QR reconciliation."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cstr

from ._qr_reconciliation_context import (
    RECONCILIATION_FAILED_STATUS,
    _build_context,
    _find_existing_journal,
    _lock_and_reload_source,
    _require_failure_source_fields,
    _require_journal_fields,
    _validate_invoice_suspense_receipt,
    _value,
)
from ._qr_reconciliation_success import (
    _create_or_recover_journal,
    _validate_journal,
)


def ensure_qr_suspense_failure_reclassification(
    reconciliation_source: Any,
    failure_reason: str,
) -> dict[str, Any]:
    """Post one exact suspense-to-variance disposition before terminal failure.

    A missing account, source link, submitted invoice, or GL receipt raises before
    the reconciliation state changes. The caller must keep the source and FB Order
    Payment pending unless this returns submitted Journal Entry evidence.
    """
    source = _lock_and_reload_source(reconciliation_source)
    context = _build_context(
        source,
        disposition=RECONCILIATION_FAILED_STATUS,
        failure_reason=failure_reason,
    )
    _require_failure_source_fields(context["source_doctype"])
    _require_journal_fields(require_failure_fields=True)
    _validate_invoice_suspense_receipt(context)
    _snapshot_failure_context(context)

    journal_name = _find_existing_journal(
        source,
        context["reconciliation_key"],
        link_field="failure_journal_entry",
        key_field="custom_kopos_qr_failure_key",
    )
    if not journal_name:
        journal_name = _create_or_recover_journal(context)
    evidence = _validate_journal(context, journal_name)
    _link_failure_journal(context, journal_name)
    return evidence


def assert_qr_suspense_failure_reclassification(
    reconciliation_source: Any,
    failure_reason: str,
) -> dict[str, Any]:
    """Re-prove an exact submitted suspense-to-variance disposition."""
    source = _lock_and_reload_source(reconciliation_source)
    context = _build_context(
        source,
        disposition=RECONCILIATION_FAILED_STATUS,
        failure_reason=failure_reason,
    )
    _require_failure_source_fields(context["source_doctype"])
    _require_journal_fields(require_failure_fields=True)
    _validate_invoice_suspense_receipt(context)
    journal_name = _find_existing_journal(
        source,
        context["reconciliation_key"],
        link_field="failure_journal_entry",
        key_field="custom_kopos_qr_failure_key",
    )
    if not journal_name:
        frappe.throw(
            f"QR failure disposition {context['reconciliation_key']} has no Journal Entry",
            frappe.ValidationError,
        )
    evidence = _validate_journal(context, journal_name)
    _link_failure_journal(context, journal_name)
    return evidence


def _snapshot_failure_context(context: dict[str, Any]) -> None:
    source = context["source"]
    updates = {
        "failure_accounting_key": context["reconciliation_key"],
        "failure_variance_account": context["target_account"],
        "failure_cost_center": context["failure_cost_center"],
        "failure_accounting_reason": context["failure_reason"],
    }
    current = {
        fieldname: cstr(_value(source, fieldname)).strip()
        for fieldname in updates
    }
    if all(current[fieldname] == value for fieldname, value in updates.items()):
        return
    frappe.db.set_value(
        context["source_doctype"],
        context["source_name"],
        updates,
        update_modified=False,
    )
    for fieldname, value in updates.items():
        setattr(source, fieldname, value)


def _link_failure_journal(context: dict[str, Any], journal_name: str) -> None:
    source = context["source"]
    linked = cstr(_value(source, "failure_journal_entry")).strip()
    if linked and linked != journal_name:
        frappe.throw(
            f"QR failure disposition {context['reconciliation_key']} references "
            "conflicting Journal Entries",
            frappe.ValidationError,
        )
    if linked == journal_name:
        return
    frappe.db.set_value(
        context["source_doctype"],
        context["source_name"],
        "failure_journal_entry",
        journal_name,
        update_modified=False,
    )
    setattr(source, "failure_journal_entry", journal_name)
