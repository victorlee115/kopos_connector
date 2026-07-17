# pyright: reportMissingImports=false
"""Compatibility facade for duplicate Automatic QR incident accounting."""

from __future__ import annotations

import frappe

from kopos_connector.kopos.services.accounting._duplicate_qr_contract import (
    ACCOUNTING_PENDING_STATUS,
    COMPANY_CLEARING_ACCOUNT_FIELD,
    COMPANY_LIABILITY_ACCOUNT_FIELD,
    DUPLICATE_STATUSES,
    JOURNAL_ENTRY_DOCTYPE,
    LIABILITY_RECOGNITION_STAGE,
    MAX_PROVIDER_EVIDENCE_BYTES,
    MAYBANK_CURRENCY,
    MAYBANK_PROVIDER,
    MAYBANK_TRANSACTION_DOCTYPE,
    REFUNDED_STATUS,
    REFUND_REQUIRED_STATUS,
    REFUND_STAGE,
    REQUIRED_JOURNAL_FIELDS,
    REQUIRED_SOURCE_FIELDS,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_incident import (
    ensure_duplicate_liability_accounting,
    register_duplicate_paid_incident,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_refund import (
    resolve_duplicate_paid_refund,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_terminal_evidence import (
    assert_duplicate_refund_terminal_evidence,
    lock_and_assert_duplicate_refund_terminal_evidence,
)

__all__ = [
    "ACCOUNTING_PENDING_STATUS",
    "COMPANY_CLEARING_ACCOUNT_FIELD",
    "COMPANY_LIABILITY_ACCOUNT_FIELD",
    "DUPLICATE_STATUSES",
    "JOURNAL_ENTRY_DOCTYPE",
    "LIABILITY_RECOGNITION_STAGE",
    "MAX_PROVIDER_EVIDENCE_BYTES",
    "MAYBANK_CURRENCY",
    "MAYBANK_PROVIDER",
    "MAYBANK_TRANSACTION_DOCTYPE",
    "REFUNDED_STATUS",
    "REFUND_REQUIRED_STATUS",
    "REFUND_STAGE",
    "REQUIRED_JOURNAL_FIELDS",
    "REQUIRED_SOURCE_FIELDS",
    "assert_duplicate_refund_terminal_evidence",
    "ensure_duplicate_liability_accounting",
    "lock_and_assert_duplicate_refund_terminal_evidence",
    "register_duplicate_paid_incident",
    "resolve_duplicate_paid_refund",
]
