# pyright: reportMissingImports=false

"""System-manager recovery for ambiguous Maybank QR generation."""

from __future__ import annotations

import html
import json
from typing import Any

import frappe
from frappe.utils import cint, cstr, now_datetime

from kopos_connector.services.maybank.client import MaybankClient
from kopos_connector.utils.diagnostics import redacted_json

from ._maybank_qr_contract import (
    CREATION_ABANDON_AFTER_SECONDS,
    CREATION_ABANDON_CONFIRMATION,
    PAID_TRANSACTION_MESSAGE,
    PENDING_RECONCILIATION_CONFIRMATION,
    PENDING_RECONCILIATION_RESOLUTION_AFTER_SECONDS,
    POLLABLE_STATUSES,
    PROVIDER_STATUS_TRANSITIONS,
    REUSABLE_STATUSES,
    STATUS_MAP,
    UNKNOWN_STATUS,
    _coerce_site_datetime,
    _existing_value,
    _extract_status_entry,
    _reservation_reference,
    _serialize_site_datetime,
    _validate_status_entry_identity,
    _validate_status_response,
)
from ._maybank_qr_persistence import (
    _durable_generation_release,
    _load_generation_snapshot,
    _load_txn_for_update,
)
from ._maybank_qr_status import _enqueue_paid_automatic_qr_finalization

def _validate_support_text(value: Any, fieldname: str, minimum: int, maximum: int) -> str:
    text = " ".join(cstr(value).strip().split())
    if len(text) < minimum or len(text) > maximum:
        frappe.throw(
            f"{fieldname} must be {minimum}-{maximum} characters",
            frappe.ValidationError,
        )
    return text


def _audit_generation_resolution(
    transaction_name: str,
    *,
    resolution: str,
    reason: str,
    evidence_reference: str,
    provider_transaction_refno: str | None,
) -> None:
    audit_payload = {
        "event": "maybank_qr_generation_resolution",
        "resolution": resolution,
        "reason": reason,
        "evidence_reference": evidence_reference,
        "provider_transaction_refno": provider_transaction_refno,
        "resolved_by": cstr(getattr(frappe.session, "user", None)).strip(),
        "resolved_at": _serialize_site_datetime(now_datetime()),
    }
    comment = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Info",
            "reference_doctype": "Maybank QR Transaction",
            "reference_name": transaction_name,
            "content": "<pre>"
            + html.escape(
                json.dumps(
                    audit_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            + "</pre>",
        }
    )
    comment.insert(ignore_permissions=True)


def _resolve_generation_with_provider_reference(
    transaction_name: str,
    provider_transaction_refno: str,
    *,
    reason: str,
    evidence_reference: str,
) -> dict[str, Any]:
    snapshot = _load_generation_snapshot(transaction_name)
    snapshot_status = cstr(_existing_value(snapshot, "status")).strip()
    if snapshot_status not in {"creating", UNKNOWN_STATUS, *REUSABLE_STATUSES, "paid"}:
        frappe.throw(
            "Maybank QR generation is already terminal",
            frappe.ValidationError,
        )
    snapshot_reference = cstr(
        _existing_value(snapshot, "transaction_refno")
    ).strip()
    if snapshot_status == "paid":
        if snapshot_reference != provider_transaction_refno:
            frappe.throw(
                "Maybank transaction is already paid under another provider reference",
                frappe.ValidationError,
            )
        return {
            "status": "paid",
            "transaction_name": transaction_name,
            "transaction_refno": snapshot_reference,
            "resolution": "already_paid",
        }

    # The first provider status check intentionally happens before taking the
    # mutable transaction row lock.  The immutable generation snapshot already
    # contains the provider account/outlet binding, so use it as the client
    # source instead of consulting the current POS Profile.
    client = MaybankClient.from_transaction(snapshot)
    result = client.check_status(provider_transaction_refno)
    _validate_status_response(result)
    entry = _extract_status_entry(result)
    if entry is None:
        frappe.throw(
            "Maybank returned no transaction for the supplied provider reference",
            frappe.ValidationError,
        )
    identity = dict(snapshot) if isinstance(snapshot, dict) else {
        field: _existing_value(snapshot, field)
        for field in (
            "provider",
            "device_id",
            "sale_amount_sen",
            "outlet_id",
            "currency",
        )
    }
    identity["transaction_refno"] = provider_transaction_refno
    raw_status = _validate_status_entry_identity(identity, entry)
    provider_status = STATUS_MAP.get(str(raw_status), UNKNOWN_STATUS)

    locked = _load_txn_for_update(transaction_name)
    current_status = cstr(_existing_value(locked, "status")).strip()
    current_reference = cstr(_existing_value(locked, "transaction_refno")).strip()
    if current_status == "paid":
        if current_reference != provider_transaction_refno:
            frappe.throw(
                "Maybank transaction is already paid under another provider reference",
                frappe.ValidationError,
            )
        return {
            "status": "paid",
            "transaction_name": transaction_name,
            "transaction_refno": current_reference,
            "resolution": "already_paid",
        }
    placeholder = _reservation_reference(
        cstr(_existing_value(locked, "request_fingerprint"))
    )
    if current_reference not in {placeholder, provider_transaction_refno}:
        frappe.throw(
            "Maybank QR generation was finalized with another provider reference",
            frappe.ValidationError,
        )
    if provider_status not in PROVIDER_STATUS_TRANSITIONS.get(
        current_status, frozenset()
    ) and provider_status != current_status:
        frappe.throw(
            "Provider evidence would regress the current Maybank transaction state",
            frappe.ValidationError,
        )

    poll_now = now_datetime()
    updates: dict[str, Any] = {
        "transaction_refno": provider_transaction_refno,
        "status": provider_status,
        "maybank_status": raw_status,
        "expires_at": _existing_value(locked, "expires_at") or poll_now,
        "last_polled_at": poll_now,
        "poll_count": cint(_existing_value(locked, "poll_count")) + 1,
        "raw_response": redacted_json(result),
    }
    if provider_status == "scanned" and not _existing_value(locked, "scanned_at"):
        updates["scanned_at"] = poll_now
    if provider_status == "paid" and not _existing_value(locked, "paid_at"):
        updates["paid_at"] = poll_now
    frappe.db.set_value(
        "Maybank QR Transaction",
        transaction_name,
        updates,
        update_modified=False,
    )
    _audit_generation_resolution(
        transaction_name,
        resolution="provider_transaction_found",
        reason=reason,
        evidence_reference=evidence_reference,
        provider_transaction_refno=provider_transaction_refno,
    )
    if provider_status == "paid":
        _enqueue_paid_automatic_qr_finalization(transaction_name, locked)
    return {
        "status": provider_status,
        "transaction_name": transaction_name,
        "transaction_refno": provider_transaction_refno,
        "resolution": "provider_transaction_found",
        "new_generation_authorized": False,
    }


def _abandon_ambiguous_generation(
    transaction_name: str,
    *,
    confirmation: str,
    reason: str,
    evidence_reference: str,
) -> dict[str, Any]:
    if cstr(confirmation).strip() != CREATION_ABANDON_CONFIRMATION:
        frappe.throw(
            f"confirmation must be exactly {CREATION_ABANDON_CONFIRMATION}",
            frappe.ValidationError,
        )
    locked = _load_txn_for_update(transaction_name)
    current_status = cstr(_existing_value(locked, "status")).strip()
    if current_status == UNKNOWN_STATUS:
        if (
            _durable_generation_release(locked)
            != "provider_transaction_absent"
        ):
            frappe.throw(
                "Maybank QR generation does not have a durable provider-absence fence",
                frappe.ValidationError,
            )
        return {
            "status": "generation_abandoned",
            "transaction_name": transaction_name,
            "resolution": "provider_transaction_absent",
            "new_generation_authorized": True,
        }
    if current_status != "creating":
        frappe.throw(
            "Only an ambiguous creating reservation can be abandoned",
            frappe.ValidationError,
        )
    request_fingerprint = cstr(
        _existing_value(locked, "request_fingerprint")
    ).strip()
    current_reference = cstr(
        _existing_value(locked, "transaction_refno")
    ).strip()
    if (
        not request_fingerprint
        or current_reference != _reservation_reference(request_fingerprint)
    ):
        frappe.throw(
            "A Maybank QR generation with a known provider reference cannot be marked absent",
            frappe.ValidationError,
        )
    created_at = _coerce_site_datetime(_existing_value(locked, "created_at"))
    age_seconds = int(
        (_coerce_site_datetime(now_datetime()) - created_at).total_seconds()
    )
    if age_seconds < CREATION_ABANDON_AFTER_SECONDS:
        frappe.throw(
            "Maybank QR generation lease is still active; abandonment is not yet safe",
            frappe.ValidationError,
        )

    resolution_evidence = {
        "status": "generation_abandoned",
        "resolution": "provider_transaction_absent",
        "reason": reason,
        "evidence_reference": evidence_reference,
        "resolved_by": cstr(getattr(frappe.session, "user", None)).strip(),
        "resolved_at": _serialize_site_datetime(now_datetime()),
        "provider_replay_blocked": True,
    }
    frappe.db.set_value(
        "Maybank QR Transaction",
        transaction_name,
        {
            "status": UNKNOWN_STATUS,
            "raw_response": redacted_json(resolution_evidence),
        },
        update_modified=False,
    )
    _audit_generation_resolution(
        transaction_name,
        resolution="provider_transaction_absent",
        reason=reason,
        evidence_reference=evidence_reference,
        provider_transaction_refno=None,
    )
    return {
        "status": "generation_abandoned",
        "transaction_name": transaction_name,
        "resolution": "provider_transaction_absent",
        "new_generation_authorized": True,
        "old_idempotency_key_replay_blocked": True,
    }


def _close_expired_reconciliation(
    transaction_name: str,
    provider_transaction_refno: str,
    *,
    confirmation: str,
    reason: str,
    evidence_reference: str,
) -> dict[str, Any]:
    if cstr(confirmation).strip() != PENDING_RECONCILIATION_CONFIRMATION:
        frappe.throw(
            f"confirmation must be exactly {PENDING_RECONCILIATION_CONFIRMATION}",
            frappe.ValidationError,
        )
    locked = _load_txn_for_update(transaction_name)
    current_status = cstr(_existing_value(locked, "status")).strip()
    current_reference = cstr(
        _existing_value(locked, "transaction_refno")
    ).strip()
    if current_reference != provider_transaction_refno:
        frappe.throw(
            "provider_transaction_refno does not match the Maybank transaction",
            frappe.ValidationError,
        )
    if current_status == "paid":
        frappe.throw(
            PAID_TRANSACTION_MESSAGE,
            frappe.ValidationError,
        )
    if current_status == "timeout":
        return {
            "status": "timeout",
            "transaction_name": transaction_name,
            "transaction_refno": current_reference,
            "resolution": "provider_transaction_cancelled",
            "new_generation_authorized": True,
        }
    if current_status not in POLLABLE_STATUSES:
        frappe.throw(
            "Only a pending or scanned Maybank transaction can be closed by reconciliation",
            frappe.ValidationError,
        )
    expires_at = _existing_value(locked, "expires_at")
    if not expires_at:
        frappe.throw(
            "Maybank transaction expiry evidence is missing",
            frappe.ValidationError,
        )
    expired_age_seconds = int(
        (
            _coerce_site_datetime(now_datetime())
            - _coerce_site_datetime(expires_at)
        ).total_seconds()
    )
    if expired_age_seconds < PENDING_RECONCILIATION_RESOLUTION_AFTER_SECONDS:
        frappe.throw(
            "Maybank transaction is still within the mandatory late-settlement "
            "reconciliation window",
            frappe.ValidationError,
        )

    resolution_evidence = {
        "status": "timeout",
        "resolution": "provider_transaction_cancelled",
        "reason": reason,
        "evidence_reference": evidence_reference,
        "resolved_by": cstr(getattr(frappe.session, "user", None)).strip(),
        "resolved_at": _serialize_site_datetime(now_datetime()),
        "provider_reference": provider_transaction_refno,
    }
    frappe.db.set_value(
        "Maybank QR Transaction",
        transaction_name,
        {
            "status": "timeout",
            "maybank_status": None,
            "last_polled_at": now_datetime(),
            "raw_response": redacted_json(resolution_evidence),
        },
        update_modified=False,
    )
    _audit_generation_resolution(
        transaction_name,
        resolution="provider_transaction_cancelled",
        reason=reason,
        evidence_reference=evidence_reference,
        provider_transaction_refno=provider_transaction_refno,
    )
    return {
        "status": "timeout",
        "transaction_name": transaction_name,
        "transaction_refno": provider_transaction_refno,
        "resolution": "provider_transaction_cancelled",
        "new_generation_authorized": True,
        "old_idempotency_key_replay_blocked": True,
    }


def resolve_maybank_qr_generation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    transaction_name = cstr(payload.get("transaction_name")).strip()
    if not transaction_name:
        frappe.throw("transaction_name is required", frappe.ValidationError)
    resolution = cstr(payload.get("resolution")).strip()
    if resolution not in {
        "provider_transaction_found",
        "provider_transaction_absent",
        "provider_transaction_cancelled",
    }:
        frappe.throw(
            "resolution must be provider_transaction_found, "
            "provider_transaction_absent, or provider_transaction_cancelled",
            frappe.ValidationError,
        )
    reason = _validate_support_text(payload.get("reason"), "reason", 12, 500)
    evidence_reference = _validate_support_text(
        payload.get("evidence_reference"),
        "evidence_reference",
        4,
        200,
    )
    if resolution in {
        "provider_transaction_found",
        "provider_transaction_cancelled",
    }:
        provider_reference = _validate_support_text(
            payload.get("provider_transaction_refno"),
            "provider_transaction_refno",
            1,
            120,
        )
        if provider_reference.lower().startswith("static-") or provider_reference.startswith(
            "REQUEST-"
        ):
            frappe.throw(
                "provider_transaction_refno is not a provider dynamic QR reference",
                frappe.ValidationError,
            )
        if resolution == "provider_transaction_cancelled":
            return _close_expired_reconciliation(
                transaction_name,
                provider_reference,
                confirmation=cstr(payload.get("confirmation")),
                reason=reason,
                evidence_reference=evidence_reference,
            )
        return _resolve_generation_with_provider_reference(
            transaction_name,
            provider_reference,
            reason=reason,
            evidence_reference=evidence_reference,
        )
    return _abandon_ambiguous_generation(
        transaction_name,
        confirmation=cstr(payload.get("confirmation")),
        reason=reason,
        evidence_reference=evidence_reference,
    )
