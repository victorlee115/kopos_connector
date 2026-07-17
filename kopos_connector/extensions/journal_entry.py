from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr


class KoPOSJournalEntryIntegrityMixin:
    """Prevent cancellation of immutable KoPOS duplicate-QR accounting proof."""

    def before_cancel(self) -> None:
        duplicate_key = cstr(
            getattr(self, "custom_kopos_qr_duplicate_key", None)
        ).strip()
        if duplicate_key:
            frappe.throw(
                _(
                    "KoPOS duplicate Automatic QR Journal Entries cannot be cancelled; "
                    "use a dedicated evidence-bound corrective posting"
                ),
                frappe.ValidationError,
            )
        parent_before_cancel: Any = getattr(super(), "before_cancel", None)
        if callable(parent_before_cancel):
            parent_before_cancel()
