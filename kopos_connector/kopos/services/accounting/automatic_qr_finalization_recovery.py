# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cint, cstr

from kopos_connector.kopos.services.accounting.automatic_qr_finalization_core import (
    finalize_paid_automatic_qr_sale,
)
from kopos_connector.utils.diagnostics import log_sanitized_error


DEFAULT_RECOVERY_BATCH_SIZE = 50
MAX_RECOVERY_BATCH_SIZE = 200
RECOVERY_SCAN_MULTIPLIER = 8
MAX_RECOVERY_SCAN_SIZE = 1_000
RECOVERY_BACKOFF_BASE_SECONDS = 60
RECOVERY_BACKOFF_MAX_SECONDS = 15 * 60
RECOVERY_FAILURE_COUNT_TTL_SECONDS = 24 * 60 * 60


def recover_paid_automatic_qr_sales(
    *,
    batch_size: int | None = None,
) -> list[dict[str, Any]]:
    """Recover paid prepared sales and unrecorded duplicate-payment incidents.

    Each candidate is committed independently. Row locks make overlapping
    scheduler runs idempotent, while a failed candidate cannot prevent the rest
    of a busy outlet's batch from being registered.
    """

    resolved_batch_size = _bounded_batch_size(batch_size)
    candidates = _recovery_candidates(resolved_batch_size)

    results: list[dict[str, Any]] = []
    for candidate in candidates or []:
        transaction_name = cstr(_value(candidate, "name")).strip()
        if not transaction_name:
            continue
        try:
            result = finalize_paid_automatic_qr_sale(transaction_name)
            frappe.db.commit()
            if result.get("settlement_status") in {
                "pending_reconciliation",
                "possible_duplicate",
                "accounting_pending",
            }:
                _record_recovery_backoff(transaction_name)
            else:
                _clear_recovery_backoff(transaction_name)
        except Exception as error:
            frappe.db.rollback()
            _record_recovery_backoff(transaction_name)
            log_sanitized_error(
                "Automatic QR paid-sale recovery failed",
                error,
            )
            result = {
                "status": "failed",
                "transaction": transaction_name,
                "error": "Automatic QR paid-sale recovery failed",
            }
        results.append(result)
    return results


def _recovery_candidates(batch_size: int) -> list[Any]:
    """Keep paid Draft registration ahead of slower reconciliation retries."""

    newest_capacity = max(1, (batch_size * 3 + 3) // 4)
    oldest_capacity = max(0, batch_size - newest_capacity)
    seen: set[str] = set()
    candidates: list[Any] = []

    newest = _query_recovery_lane(
        "sale.docstatus = 0",
        descending=True,
        scan_capacity=newest_capacity,
    )
    candidates.extend(
        _select_recovery_rows(newest, newest_capacity, seen)
    )
    if oldest_capacity:
        oldest = _query_recovery_lane(
            "sale.docstatus = 0",
            descending=False,
            scan_capacity=oldest_capacity,
        )
        candidates.extend(
            _select_recovery_rows(oldest, oldest_capacity, seen)
        )

    auxiliary_capacity = max(1, batch_size // 4)
    incident_rows = _query_recovery_lane(
        """
        sale.docstatus = 1
        AND COALESCE(txn.consumption_key, '') = ''
        AND COALESCE(txn.duplicate_payment_status, '') IN (
            '', 'possible_duplicate', 'accounting_pending'
        )
        """,
        descending=False,
        scan_capacity=auxiliary_capacity,
    )
    candidates.extend(
        _select_recovery_rows(incident_rows, auxiliary_capacity, seen)
    )

    reconciliation_rows = _query_recovery_lane(
        """
        sale.docstatus = 1
        AND txn.consumption_key = sale.name
        AND txn.manual_reconciliation_status IN (
            'pending_reconciliation',
            'reconciliation_failed'
        )
        """,
        descending=False,
        scan_capacity=auxiliary_capacity,
    )
    candidates.extend(
        _select_recovery_rows(
            reconciliation_rows,
            auxiliary_capacity,
            seen,
        )
    )
    return candidates


def _query_recovery_lane(
    condition: str,
    *,
    descending: bool,
    scan_capacity: int,
) -> list[Any]:
    direction = "DESC" if descending else "ASC"
    scan_limit = min(
        max(scan_capacity * RECOVERY_SCAN_MULTIPLIER, scan_capacity),
        MAX_RECOVERY_SCAN_SIZE,
    )
    return list(
        frappe.db.sql(
            f"""
            SELECT txn.name
            FROM `tabMaybank QR Transaction` txn
            INNER JOIN `tabFB Order` sale ON sale.name = txn.fb_order
            WHERE txn.status = 'paid'
              AND txn.maybank_status = 1
              AND COALESCE(txn.fb_order_payment, '') != ''
              AND COALESCE(sale.accepted_sale_fingerprint, '') != ''
              AND ({condition})
            ORDER BY COALESCE(txn.paid_at, txn.creation) {direction},
                     txn.creation {direction}, txn.name {direction}
            LIMIT %s
            """,
            (scan_limit,),
            as_dict=True,
        )
        or []
    )


def _select_recovery_rows(
    rows: list[Any],
    capacity: int,
    seen: set[str],
) -> list[Any]:
    selected: list[Any] = []
    for row in rows:
        if len(selected) >= capacity:
            break
        transaction_name = cstr(_value(row, "name")).strip()
        if (
            not transaction_name
            or transaction_name in seen
            or _recovery_backoff_active(transaction_name)
        ):
            continue
        seen.add(transaction_name)
        selected.append(row)
    return selected


def _recovery_backoff_keys(transaction_name: str) -> tuple[str, str]:
    digest = hashlib.sha256(transaction_name.encode("utf-8")).hexdigest()
    prefix = f"kopos:auto-qr-finalization:{digest}"
    return f"{prefix}:cooldown", f"{prefix}:failures"


def _recovery_backoff_active(transaction_name: str) -> bool:
    cooldown_key, _ = _recovery_backoff_keys(transaction_name)
    try:
        return bool(frappe.cache().get_value(cooldown_key))
    except Exception as error:
        log_sanitized_error("Automatic QR recovery backoff read failed", error)
        return False


def _record_recovery_backoff(transaction_name: str) -> None:
    cooldown_key, failures_key = _recovery_backoff_keys(transaction_name)
    try:
        cache = frappe.cache()
        failures = max(0, cint(cache.get_value(failures_key))) + 1
        delay_seconds = min(
            RECOVERY_BACKOFF_BASE_SECONDS * (2 ** min(failures - 1, 8)),
            RECOVERY_BACKOFF_MAX_SECONDS,
        )
        cache.set_value(
            failures_key,
            failures,
            expires_in_sec=RECOVERY_FAILURE_COUNT_TTL_SECONDS,
        )
        cache.set_value(
            cooldown_key,
            1,
            expires_in_sec=delay_seconds,
        )
    except Exception as error:
        log_sanitized_error("Automatic QR recovery backoff write failed", error)


def _clear_recovery_backoff(transaction_name: str) -> None:
    cooldown_key, failures_key = _recovery_backoff_keys(transaction_name)
    try:
        cache = frappe.cache()
        cache.delete_value(cooldown_key)
        cache.delete_value(failures_key)
    except Exception as error:
        log_sanitized_error("Automatic QR recovery backoff clear failed", error)


def _bounded_batch_size(value: int | None) -> int:
    if value is None:
        config = getattr(frappe, "conf", None)
        getter = getattr(config, "get", None)
        configured = (
            getter("kopos_automatic_qr_finalization_batch_size", DEFAULT_RECOVERY_BATCH_SIZE)
            if callable(getter)
            else getattr(
                config,
                "kopos_automatic_qr_finalization_batch_size",
                DEFAULT_RECOVERY_BATCH_SIZE,
            )
        )
        resolved = cint(configured)
    else:
        resolved = cint(value)
    if resolved < 1:
        return DEFAULT_RECOVERY_BATCH_SIZE
    return min(resolved, MAX_RECOVERY_BATCH_SIZE)


def _value(document: Any, fieldname: str) -> Any:
    if isinstance(document, Mapping):
        return document.get(fieldname)
    getter = getattr(document, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None:
            return value
    return getattr(document, fieldname, None)
