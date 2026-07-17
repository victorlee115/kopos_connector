# pyright: reportMissingImports=false

"""Compatibility facade for manual QR reconciliation accounting."""

from __future__ import annotations

from typing import Any

import frappe

from ._qr_reconciliation_context import (
    COMPANY_FAILURE_ACCOUNT_FIELD,
    REQUIRED_FAILURE_JOURNAL_FIELDS,
    REQUIRED_FAILURE_RECOVERY_JOURNAL_FIELDS,
    REQUIRED_FAILURE_SOURCE_FIELDS,
    REQUIRED_JOURNAL_FIELDS,
    _build_context,
    _configured_mode_of_payment_account,
)
from ._qr_reconciliation_failure import (
    assert_qr_suspense_failure_reclassification,
    ensure_qr_suspense_failure_reclassification,
)
from ._qr_reconciliation_success import (
    assert_qr_suspense_reclassification,
    ensure_qr_suspense_reclassification,
)

__all__ = (
    "assert_qr_suspense_failure_reclassification",
    "assert_qr_suspense_reclassification",
    "ensure_qr_suspense_failure_reclassification",
    "ensure_qr_suspense_reclassification",
)
