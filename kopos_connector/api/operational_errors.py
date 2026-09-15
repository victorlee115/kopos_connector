# pyright: reportMissingImports=false

"""Stable tablet error reasons; messages remain private diagnostics."""

from __future__ import annotations

import frappe


class OperationalValidationError(frappe.ValidationError):
    error_code = "request_rejected"


class StoreSetupIncompleteError(OperationalValidationError):
    error_code = "store_setup_incomplete"


class TabletDisabledError(OperationalValidationError):
    error_code = "tablet_disabled"


class ShiftRequiredError(OperationalValidationError):
    error_code = "shift_required"


class ShiftOwnedByOtherError(OperationalValidationError):
    error_code = "shift_owned_by_other"


class ShiftAlreadyOpenedError(OperationalValidationError):
    error_code = "shift_already_opened"


class ShiftAlreadyClosedError(OperationalValidationError):
    error_code = "shift_already_closed"


class RefundChangedError(OperationalValidationError):
    """The refund the tablet showed is no longer what ERP would post."""

    error_code = "refund_changed"
