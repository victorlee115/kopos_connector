# pyright: reportMissingImports=false
"""Compatibility facade for Automatic QR finalization and recovery.

The stable hook and RQ callable paths remain in this module. Accounting and
sale mutation live in the core module; bounded scheduler scanning and retry
backoff live in the recovery module.
"""

from __future__ import annotations

import frappe

from kopos_connector.kopos.services.accounting.automatic_qr_finalization_core import (
    FINALIZER_JOB_TIMEOUT_SECONDS,
    MAYBANK_CURRENCY,
    MAYBANK_MODE_OF_PAYMENT,
    MAYBANK_PAYMENT_CHANNELS,
    MAYBANK_PROVIDER,
    _apply_provider_paid_payment,
    _ensure_provider_paid_reconciliation_context,
    _get_exact_payment,
    _load_paid_attempts_for_update,
    _mark_late_paid_incident,
    _mark_order_finalized,
    _normalized,
    _payment_matches_attempt,
    _reconcile_provider_paid_manual_settlement,
    _register_late_paid_incidents_after_sale_commit,
    _reload_late_paid_incident_for_update,
    _result,
    _strict_integer_sen,
    _submitted_payment_matches_attempt,
    _validate_paid_attempt,
    _value,
    enqueue_automatic_qr_finalization,
    finalize_paid_automatic_qr_sale,
)
from kopos_connector.kopos.services.accounting.automatic_qr_finalization_recovery import (
    DEFAULT_RECOVERY_BATCH_SIZE,
    MAX_RECOVERY_BATCH_SIZE,
    MAX_RECOVERY_SCAN_SIZE,
    RECOVERY_BACKOFF_BASE_SECONDS,
    RECOVERY_BACKOFF_MAX_SECONDS,
    RECOVERY_FAILURE_COUNT_TTL_SECONDS,
    RECOVERY_SCAN_MULTIPLIER,
    _bounded_batch_size,
    _clear_recovery_backoff,
    _query_recovery_lane,
    _record_recovery_backoff,
    _recovery_backoff_active,
    _recovery_backoff_keys,
    _recovery_candidates,
    _select_recovery_rows,
    recover_paid_automatic_qr_sales,
)
from kopos_connector.kopos.services.accounting.qr_reconciliation_service import (
    ensure_qr_suspense_reclassification,
)
from kopos_connector.utils.diagnostics import log_sanitized_error


__all__ = [
    "enqueue_automatic_qr_finalization",
    "finalize_paid_automatic_qr_sale",
    "recover_paid_automatic_qr_sales",
]
