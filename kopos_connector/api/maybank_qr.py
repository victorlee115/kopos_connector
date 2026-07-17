# pyright: reportMissingImports=false

"""Compatibility facade for the production Maybank QR workflow.

Desk-only test simulation is intentionally supplied by the subsequent test
simulation slice and is not part of this production provider module.
"""

from __future__ import annotations

import frappe

from kopos_connector.services.maybank.client import MaybankClient

from ._maybank_qr_contract import (
    AMBIGUOUS_IDEMPOTENCY_MESSAGE,
    CREATION_ABANDON_AFTER_SECONDS,
    CREATION_ABANDON_CONFIRMATION,
    CREATION_LEASE_SECONDS,
    DEFAULT_QR_PER_OUTLET_PER_MINUTE,
    DEFAULT_QR_TTL_SECONDS,
    GRACE_SECONDS,
    MAX_AMOUNT_SEN,
    MAX_QR_PER_MINUTE,
    MAYBANK_CURRENCY,
    MAYBANK_MOCK_REFERENCE_PATTERN,
    MAYBANK_PROVIDER,
    MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT,
    PAID_TRANSACTION_MESSAGE,
    PAYMENT_STATUS_RESPONSE_STATUSES,
    PENDING_RECONCILIATION_CONFIRMATION,
    PENDING_RECONCILIATION_RESOLUTION_AFTER_SECONDS,
    POLLABLE_STATUSES,
    PREFLIGHT_REASON_CODES,
    PREFLIGHT_REASON_PROVIDER_CONFIGURATION,
    PREFLIGHT_REASON_RATE_LIMIT,
    PREFLIGHT_REASON_RATE_LIMITER_UNAVAILABLE,
    PROVIDER_STATUS_TRANSITIONS,
    QR_RATE_LIMIT_SCRIPT,
    QR_RATE_LIMIT_WINDOW_SECONDS,
    REUSABLE_STATUSES,
    STATUS_MAP,
    UNKNOWN_STATUS,
    USED_IDEMPOTENCY_MESSAGE,
    MaybankQrPreflightRejection,
    _coerce_site_datetime,
    _existing_value,
    _extract_expiry_seconds,
    _extract_status_entry,
    _format_sale_amount,
    _has_explicit_timezone,
    _parse_decimal_amount_sen,
    _parse_integer_sen,
    _parse_positive_amount_sen,
    _parse_provider_amount_sen,
    _persisted_sale_amount_sen,
    _request_fingerprint,
    _require_exact_persisted_text,
    _require_provider_transaction_reference,
    _reservation_reference,
    _serialize_site_datetime,
    _validate_status_entry_identity,
    _validate_status_response,
)
from ._maybank_qr_generation import (
    _configuration_preflight_rejection,
    _finalize_reserved_generation,
    _generate_qr_payload,
    _late_provider_result_response,
    _load_prepared_automatic_qr_sale,
    _mark_generation_ambiguous,
    _record_late_provider_result_after_release,
    _register_preflight_rejection_fence,
    _validate_new_generation_attempt,
    generate_maybank_qr_payload,
)
from ._maybank_qr_persistence import (
    _build_creation_recovery_response,
    _build_existing_txn_response,
    _build_paid_existing_txn_response,
    _build_persisted_preflight_rejection_response,
    _build_preflight_rejection_response,
    _durable_generation_release,
    _load_existing_txn,
    _load_generation_snapshot,
    _load_linked_generation_attempts_for_update,
    _load_reserved_txn_for_update,
    _load_reserved_txn_with_order_lock,
    _load_txn_for_update,
    _parse_preflight_rejection_evidence,
    _raw_response_object,
    _record_poll_attempt,
)
from ._maybank_qr_rate_limit import (
    _check_rate_limit,
    _config_positive_int,
    _qr_rate_limit_key,
)
from ._maybank_qr_resolution import (
    _abandon_ambiguous_generation,
    _audit_generation_resolution,
    _close_expired_reconciliation,
    _resolve_generation_with_provider_reference,
    _validate_support_text,
    resolve_maybank_qr_generation_payload,
)
from ._maybank_qr_status import (
    _apply_provider_poll_result,
    _build_payment_status_response,
    _enqueue_paid_automatic_qr_finalization,
    _poll_txn_status,
    _resolve_existing_txn,
    _transition_txn_status_locked,
    _update_txn_status,
    check_maybank_payment_payload,
)
from .maybank_qr_simulation import (
    MAYBANK_SIMULATION_FINGERPRINT_PATTERN,
    MAYBANK_TEST_SIMULATION_CONFIG,
    MAYBANK_TEST_SIMULATION_CONFIRMATION,
    MAYBANK_TEST_SIMULATION_VERSION,
    _audit_maybank_test_payment_simulation,
    _build_maybank_test_simulation_identity,
    _load_maybank_simulation_sale_for_update,
    _maybank_desk_simulation_context_enabled,
    _require_maybank_desk_simulation_context,
    _validate_maybank_simulation_prepared_sale,
    get_maybank_qr_simulation_capability,
    simulate_maybank_qr_payment_payload,
)

__all__ = (
    "check_maybank_payment_payload",
    "generate_maybank_qr_payload",
    "get_maybank_qr_simulation_capability",
    "resolve_maybank_qr_generation_payload",
    "simulate_maybank_qr_payment_payload",
)
