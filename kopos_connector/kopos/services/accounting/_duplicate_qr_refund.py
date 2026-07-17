# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import now_datetime

from kopos_connector.api.devices import lock_device_for_operational_mutation
from kopos_connector.kopos.services.accounting._duplicate_qr_contract import (
    MAYBANK_TRANSACTION_DOCTYPE,
    REFUNDED_STATUS,
    REFUND_REQUIRED_STATUS,
    REFUND_STAGE,
    _bounded_text,
    _build_accounting_context,
    _required_text,
    _strict_positive_sen,
    _text,
    _validate_duplicate_identity,
    _validated_refund_date,
    _value,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_incident import (
    ensure_duplicate_liability_accounting,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_journal import (
    _assert_exact_refund_snapshot,
    _create_or_recover_journal,
    _find_existing_journal,
    _set_source_values,
    _snapshot_refund_context,
    _validate_journal,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_terminal_evidence import (
    _validate_private_provider_evidence_file,
    assert_duplicate_refund_terminal_evidence,
)


def resolve_duplicate_paid_refund(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Record one exact provider refund and its liability-to-clearing JE.

    Authorization belongs to the API boundary. This service enforces lock order,
    evidence identity, immutable snapshots, idempotency, and submitted GL proof.
    """

    source_name = _required_text(payload, "transaction")
    source_link = frappe.db.get_value(
        MAYBANK_TRANSACTION_DOCTYPE,
        source_name,
        ["name", "fb_order", "device_id"],
        as_dict=True,
    )
    order_name = _text(_value(source_link, "fb_order"))
    device_id = _text(_value(source_link, "device_id"))
    if not order_name or not device_id:
        frappe.throw(
            "Duplicate Automatic QR transaction is not bound to an FB Order and device",
            frappe.ValidationError,
        )

    # Serialize support mutations against safe reset before taking any sale or
    # provider locks. The source binding is re-read under lock below, so this
    # non-locking lookup is scope discovery only and never mutation authority.
    locked_device = lock_device_for_operational_mutation(device_id=device_id)
    if _text(_value(locked_device, "device_id")) != device_id:
        frappe.throw(
            "Duplicate Automatic QR device binding changed while its lock was acquired",
            frappe.ValidationError,
        )

    # Preserve the global operational lock order: Device, Safe Reset, FB Order,
    # provider row, accounting rows, then retained evidence File.
    locked_order = frappe.db.sql(
        "SELECT name FROM `tabFB Order` WHERE name = %s LIMIT 1 FOR UPDATE",
        (order_name,),
    )
    if len(locked_order or []) != 1:
        frappe.throw(
            "Duplicate Automatic QR FB Order was not found",
            frappe.ValidationError,
        )
    locked_source = frappe.db.sql(
        "SELECT name FROM `tabMaybank QR Transaction` WHERE name = %s LIMIT 1 FOR UPDATE",
        (source_name,),
    )
    if len(locked_source or []) != 1:
        frappe.throw(
            "Duplicate Automatic QR transaction was not found",
            frappe.ValidationError,
        )

    order_doc = frappe.get_doc("FB Order", order_name)
    transaction = frappe.get_doc(MAYBANK_TRANSACTION_DOCTYPE, source_name)
    if (
        _text(_value(transaction, "device_id")) != device_id
        or _text(_value(transaction, "fb_order")) != order_name
        or _text(_value(order_doc, "device_id")) != device_id
    ):
        frappe.throw(
            "Duplicate Automatic QR device or order binding changed while locks were acquired",
            frappe.ValidationError,
        )
    winning_transaction = _text(
        _value(transaction, "duplicate_winning_transaction")
    )
    identity = _validate_duplicate_identity(
        transaction,
        order_doc=order_doc,
        winning_transaction_name=winning_transaction,
        require_submitted_sale=True,
    )
    status = _text(_value(transaction, "duplicate_payment_status"))
    if status not in {REFUND_REQUIRED_STATUS, REFUNDED_STATUS}:
        frappe.throw(
            "Duplicate Automatic QR liability accounting must complete before refund resolution",
            frappe.ValidationError,
        )

    liability_evidence = ensure_duplicate_liability_accounting(
        transaction,
        order_doc=order_doc,
        identity=identity,
    )
    liability_journal = _text(liability_evidence.get("journal_entry"))
    if not liability_journal:
        frappe.throw(
            "Duplicate Automatic QR liability has no submitted accounting evidence",
            frappe.ValidationError,
        )

    refund = _validate_refund_evidence(payload, transaction, identity)
    if status == REFUNDED_STATUS:
        _assert_exact_refund_snapshot(transaction, refund)
        terminal_evidence = assert_duplicate_refund_terminal_evidence(
            transaction,
            order_doc=order_doc,
            identity=identity,
        )
        return _refund_result(
            "already_refunded",
            transaction=transaction,
            identity=identity,
            liability_journal=liability_journal,
            refund_journal=_text(
                terminal_evidence.get("refund_journal_entry")
            ),
        )

    refund_context = _build_accounting_context(
        transaction,
        order_doc=order_doc,
        identity=identity,
        stage=REFUND_STAGE,
        refund=refund,
    )
    _snapshot_refund_context(transaction, refund_context, refund)
    journal_name = _find_existing_journal(
        transaction,
        refund_context["journal_key"],
        link_field="duplicate_refund_journal_entry",
    )
    if not journal_name:
        journal_name = _create_or_recover_journal(refund_context)
    evidence = _validate_journal(refund_context, journal_name)
    verified_journal = _text(evidence.get("journal_entry"))
    if not verified_journal:
        frappe.throw(
            "Duplicate Automatic QR refund has no submitted accounting evidence",
            frappe.ValidationError,
        )

    _set_source_values(
        transaction,
        {
            "duplicate_payment_status": REFUNDED_STATUS,
            "duplicate_refund_journal_entry": verified_journal,
            "duplicate_refunded_by": _text(
                getattr(frappe.session, "user", None)
            ),
            "duplicate_refunded_at": now_datetime(),
            "reconciliation_note": (
                "Exact provider refund evidence was recorded and the customer "
                "liability was cleared without reopening or mutating the winning sale."
            ),
        },
    )
    terminal_evidence = assert_duplicate_refund_terminal_evidence(
        transaction,
        order_doc=order_doc,
        identity=identity,
    )
    return _refund_result(
        "refunded",
        transaction=transaction,
        identity=identity,
        liability_journal=liability_journal,
        refund_journal=_text(terminal_evidence.get("refund_journal_entry")),
    )

def _validate_refund_evidence(
    payload: Mapping[str, Any],
    transaction: Any,
    identity: dict[str, Any],
) -> dict[str, Any]:
    provider_transaction_refno = _required_text(
        payload, "provider_transaction_refno"
    )
    if provider_transaction_refno != identity["transaction_refno"]:
        frappe.throw(
            "Provider refund evidence does not match the duplicate payment reference",
            frappe.ValidationError,
        )
    provider_refund_status = _required_text(
        payload, "provider_refund_status"
    ).lower()
    if provider_refund_status != "refunded":
        frappe.throw(
            "Provider refund status must be exactly refunded",
            frappe.ValidationError,
        )
    currency = _required_text(payload, "provider_refund_currency").upper()
    if currency != identity["currency"]:
        frappe.throw(
            "Provider refund currency does not match the duplicate payment",
            frappe.ValidationError,
        )
    amount_sen = _strict_positive_sen(
        payload.get("provider_refund_amount_sen"),
        "provider_refund_amount_sen",
    )
    if amount_sen != identity["amount_sen"]:
        frappe.throw(
            "Provider refund amount does not match the duplicate payment",
            frappe.ValidationError,
        )
    refund_reference = _bounded_text(
        payload.get("provider_refund_reference"),
        "provider_refund_reference",
        minimum=6,
        maximum=140,
    )
    if refund_reference == provider_transaction_refno:
        frappe.throw(
            "Provider refund reference must differ from the payment reference",
            frappe.ValidationError,
        )
    evidence_reference = _bounded_text(
        payload.get("provider_evidence_reference"),
        "provider_evidence_reference",
        minimum=6,
        maximum=140,
    )
    evidence_file = _required_text(payload, "provider_evidence_file")
    evidence_sha256 = _required_text(payload, "provider_evidence_sha256")
    if len(evidence_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in evidence_sha256
    ):
        frappe.throw(
            "provider_evidence_sha256 must be exactly 64 lowercase hexadecimal characters",
            frappe.ValidationError,
        )
    _validate_private_provider_evidence_file(
        evidence_file,
        expected_sha256=evidence_sha256,
        source_name=identity["source_name"],
    )
    refund_date = _validated_refund_date(
        payload.get("provider_refund_date"),
        identity=identity,
    )
    note = _bounded_text(
        payload.get("note"),
        "note",
        minimum=20,
        maximum=1000,
    )
    return {
        "provider_transaction_refno": provider_transaction_refno,
        "provider_refund_reference": refund_reference,
        "provider_evidence_reference": evidence_reference,
        "provider_evidence_file": evidence_file,
        "provider_evidence_sha256": evidence_sha256,
        "amount_sen": amount_sen,
        "currency": currency,
        "refund_date": refund_date,
        "note": note,
    }

def _refund_result(
    status: str,
    *,
    transaction: Any,
    identity: dict[str, Any],
    liability_journal: str,
    refund_journal: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "duplicate_payment_status": REFUNDED_STATUS,
        "transaction": identity["source_name"],
        "provider_transaction_refno": identity["transaction_refno"],
        "fb_order": identity["order_name"],
        "sales_invoice": identity["invoice_name"],
        "winning_transaction": identity["winning_transaction"],
        "amount_sen": identity["amount_sen"],
        "currency": identity["currency"],
        "provider_refund_reference": _text(
            _value(transaction, "duplicate_refund_reference")
        ),
        "liability_journal_entry": liability_journal,
        "refund_journal_entry": refund_journal,
        "sales_invoice_created": False,
    }
