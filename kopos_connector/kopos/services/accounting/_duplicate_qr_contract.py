# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cint, cstr, getdate, now_datetime

from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    persisted_money_to_sen,
)


MAYBANK_TRANSACTION_DOCTYPE = "Maybank QR Transaction"
MANUAL_QR_RECONCILIATION_DOCTYPE = "Manual QR Reconciliation"
JOURNAL_ENTRY_DOCTYPE = "Journal Entry"
MAYBANK_PROVIDER = "maybank_qr"
STATIC_QR_PROVIDER = "static_qr"
MAYBANK_CURRENCY = "MYR"

ACCOUNTING_PENDING_STATUS = "accounting_pending"
POSSIBLE_DUPLICATE_STATUS = "possible_duplicate"
REFUND_REQUIRED_STATUS = "refund_required"
REFUNDED_STATUS = "refunded"
SETTLED_EXISTING_SALE_STATUS = "settled_existing_sale"
DUPLICATE_STATUSES = {
    POSSIBLE_DUPLICATE_STATUS,
    ACCOUNTING_PENDING_STATUS,
    REFUND_REQUIRED_STATUS,
    REFUNDED_STATUS,
    SETTLED_EXISTING_SALE_STATUS,
}

LIABILITY_RECOGNITION_STAGE = "liability_recognition"
REFUND_STAGE = "refund"
COMPANY_CLEARING_ACCOUNT_FIELD = (
    "custom_kopos_qr_duplicate_payment_clearing_account"
)
COMPANY_LIABILITY_ACCOUNT_FIELD = (
    "custom_kopos_qr_customer_liability_account"
)
ACCOUNTING_KEY_NAMESPACE = "kopos:duplicate-qr-liability:v1:"
REFUND_KEY_NAMESPACE = "kopos:duplicate-qr-refund:v1:"
SEN_PER_UNIT = Decimal("100")
MAX_PROVIDER_EVIDENCE_BYTES = 10 * 1024 * 1024

REQUIRED_SOURCE_FIELDS = (
    "duplicate_payment_status",
    "duplicate_winning_channel",
    "duplicate_winning_transaction",
    "duplicate_winning_static_reconciliation",
    "duplicate_accounting_key",
    "duplicate_clearing_account",
    "duplicate_liability_account",
    "duplicate_liability_journal_entry",
    "duplicate_refund_key",
    "duplicate_refund_journal_entry",
    "duplicate_refund_reference",
    "duplicate_refund_evidence_reference",
    "duplicate_refund_evidence_file",
    "duplicate_refund_evidence_sha256",
    "duplicate_refund_amount_sen",
    "duplicate_refund_currency",
    "duplicate_refund_date",
    "duplicate_refund_note",
    "duplicate_refunded_by",
    "duplicate_refunded_at",
)
REQUIRED_JOURNAL_FIELDS = (
    "custom_kopos_qr_duplicate_key",
    "custom_kopos_qr_duplicate_stage",
    "custom_kopos_qr_provider_transaction",
    "custom_kopos_qr_winning_transaction",
    "custom_kopos_qr_winning_channel",
    "custom_kopos_qr_winning_static_reconciliation",
    "custom_kopos_qr_provider_evidence_reference",
    "custom_kopos_qr_provider_evidence_file",
    "custom_kopos_qr_provider_evidence_sha256",
    "custom_kopos_qr_source_doctype",
    "custom_kopos_qr_source_name",
    "custom_kopos_qr_fb_order",
    "custom_kopos_qr_sales_invoice",
    "custom_kopos_qr_amount_sen",
    "custom_kopos_qr_currency",
)

def _validate_duplicate_identity(
    transaction: Any,
    *,
    order_doc: Any,
    winning_transaction_name: str = "",
    require_submitted_sale: bool,
) -> dict[str, Any]:
    source_name = _text(_value(transaction, "name"))
    order_name = _text(_value(order_doc, "name"))
    payment_row_name = _text(_value(transaction, "fb_order_payment"))
    if not source_name or not order_name or not payment_row_name:
        frappe.throw(
            "Duplicate Automatic QR incident identity is incomplete",
            frappe.ValidationError,
        )
    if require_submitted_sale and cint(_value(order_doc, "docstatus")) != 1:
        frappe.throw(
            "Duplicate Automatic QR incident requires a submitted winning FB Order",
            frappe.ValidationError,
        )
    if (
        _text(_value(transaction, "status")).lower() != "paid"
        or cstr(_value(transaction, "maybank_status")).strip() != "1"
        or _text(_value(transaction, "provider")).lower() != MAYBANK_PROVIDER
    ):
        frappe.throw(
            "Duplicate Automatic QR incident lacks authoritative provider-paid evidence",
            frappe.ValidationError,
        )
    transaction_refno = _text(_value(transaction, "transaction_refno"))
    if not transaction_refno or transaction_refno.lower().startswith("static-"):
        frappe.throw(
            "Duplicate Automatic QR provider reference is invalid",
            frappe.ValidationError,
        )
    if _text(_value(transaction, "fb_order")) != order_name:
        frappe.throw(
            "Duplicate Automatic QR transaction does not match the winning FB Order",
            frappe.ValidationError,
        )
    company = _text(_value(order_doc, "company"))
    if not company or _text(_value(transaction, "company")) != company:
        frappe.throw(
            "Duplicate Automatic QR transaction does not match the winning sale company",
            frappe.ValidationError,
        )
    if _text(_value(order_doc, "automatic_qr_payment")) != payment_row_name:
        frappe.throw(
            "Duplicate Automatic QR payment row does not match the winning sale",
            frappe.ValidationError,
        )
    device_id = _text(_value(order_doc, "device_id"))
    if not device_id or _text(_value(transaction, "device_id")) != device_id:
        frappe.throw(
            "Duplicate Automatic QR transaction does not match the winning device",
            frappe.ValidationError,
        )
    if _text(_value(transaction, "consumption_key")) or _text(
        _value(transaction, "invoice_consumption_key")
    ) or _text(_value(transaction, "sales_invoice")):
        frappe.throw(
            "Duplicate Automatic QR attempt must not be consumed by an invoice",
            frappe.ValidationError,
        )
    currency = _text(_value(transaction, "currency")).upper()
    if currency != MAYBANK_CURRENCY or _text(
        _value(order_doc, "currency")
    ).upper() != MAYBANK_CURRENCY:
        frappe.throw(
            "Duplicate Automatic QR accounting requires MYR",
            frappe.ValidationError,
        )

    matching_payments = [
        payment
        for payment in list(_value(order_doc, "payments") or [])
        if _text(_value(payment, "name")) == payment_row_name
    ]
    if len(matching_payments) != 1:
        frappe.throw(
            "Duplicate Automatic QR winning payment row was not found exactly once",
            frappe.ValidationError,
        )
    payment = matching_payments[0]
    order_winner_channel = _text(
        _value(order_doc, "automatic_qr_winner_channel")
    )
    if order_winner_channel == STATIC_QR_PROVIDER:
        winning_channel = STATIC_QR_PROVIDER
        winning_name = ""
        if _text(winning_transaction_name):
            frappe.throw(
                "Static QR winning sale cannot name a Maybank winning transaction",
                frappe.ValidationError,
            )
        winning_static_reconciliation = _text(
            _value(order_doc, "automatic_qr_static_reconciliation")
            or _value(payment, "manual_qr_reconciliation")
        )
        if (
            not winning_static_reconciliation
            or winning_static_reconciliation
            != _text(_value(payment, "manual_qr_reconciliation"))
            or _normalize_channel(_value(payment, "payment_channel_code"))
            != "static qr"
            or not cint(_value(payment, "is_manual_confirmation"))
            or _text(_value(payment, "maybank_qr_transaction"))
        ):
            frappe.throw(
                "Duplicate Automatic QR incident lacks exact static QR winning evidence",
                frappe.ValidationError,
            )
    else:
        winning_channel = MAYBANK_PROVIDER
        winning_name = _text(
            winning_transaction_name
            or _value(transaction, "duplicate_winning_transaction")
        )
        winning_static_reconciliation = ""
        if not winning_name:
            frappe.throw(
                "Duplicate Automatic QR winning provider transaction is required",
                frappe.ValidationError,
            )
        if source_name == winning_name:
            frappe.throw(
                "Winning Automatic QR payment cannot be registered as a duplicate",
                frappe.ValidationError,
            )
        linked_winner = _text(_value(payment, "maybank_qr_transaction"))
        if (
            (require_submitted_sale and linked_winner != winning_name)
            or (linked_winner and linked_winner != winning_name)
        ):
            frappe.throw(
                "Duplicate Automatic QR incident does not match the winning provider transaction",
                frappe.ValidationError,
            )
    try:
        payment_amount_sen = persisted_money_to_sen(
            _value(payment, "amount"),
            "Winning Automatic QR payment amount",
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    amount_sen = _strict_positive_sen(
        _value(transaction, "sale_amount_sen"),
        "Duplicate Automatic QR sale_amount_sen",
    )
    if payment_amount_sen != amount_sen:
        frappe.throw(
            "Duplicate Automatic QR amount does not match the winning payment",
            frappe.ValidationError,
        )

    invoice_name = _text(_value(order_doc, "sales_invoice"))
    if not company or (require_submitted_sale and not invoice_name):
        frappe.throw(
            "Duplicate Automatic QR winning sale accounting context is incomplete",
            frappe.ValidationError,
        )
    identity = {
        "source_name": source_name,
        "transaction_refno": transaction_refno,
        "order_name": order_name,
        "payment_row_name": payment_row_name,
        "winning_channel": winning_channel,
        "winning_transaction": winning_name,
        "winning_static_reconciliation": winning_static_reconciliation,
        "winning_identity": winning_name or winning_static_reconciliation,
        "legacy_dynamic_winner_metadata": bool(
            winning_channel == MAYBANK_PROVIDER
            and _text(_value(transaction, "duplicate_payment_status"))
            and not _text(_value(transaction, "duplicate_winning_channel"))
            and _text(_value(transaction, "duplicate_winning_transaction"))
        ),
        "invoice_name": invoice_name,
        "company": company,
        "currency": currency,
        "amount_sen": amount_sen,
        "device_id": device_id,
        "paid_at": _value(transaction, "paid_at"),
    }
    if winning_channel == STATIC_QR_PROVIDER:
        # Static settlement is cashier evidence rather than provider-paid
        # authority.  Prove its exact durable reconciliation record before the
        # Maybank row is even labelled as a duplicate-payment incident.
        _validate_winning_static_reconciliation(identity)
    return identity


def _normalize_channel(value: Any) -> str:
    return " ".join(
        cstr(value).strip().lower().replace("_", " ").replace("-", " ").split()
    )


def _build_accounting_context(
    transaction: Any,
    *,
    order_doc: Any,
    identity: dict[str, Any],
    stage: str,
    refund: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if identity["winning_channel"] == STATIC_QR_PROVIDER:
        _validate_winning_static_reconciliation(identity)
    else:
        _validate_winning_provider_transaction(identity)
    invoice = frappe.db.get_value(
        "Sales Invoice",
        identity["invoice_name"],
        [
            "name",
            "docstatus",
            "is_return",
            "custom_fb_order",
            "custom_fb_idempotency_key",
            "custom_fb_device_id",
            "custom_fb_void_idempotency_key",
            "custom_fb_void_request_fingerprint",
            "custom_fb_void_manager",
            "custom_fb_void_approval_token_id",
            "company",
            "currency",
        ],
        as_dict=True,
    )
    _validate_winning_sales_invoice(
        invoice,
        order_doc=order_doc,
        identity=identity,
    )

    company_currency = _text(
        frappe.db.get_value(
            "Company", identity["company"], "default_currency"
        )
    ).upper()
    if company_currency != identity["currency"]:
        frappe.throw(
            "Duplicate Automatic QR company currency does not match the provider payment",
            frappe.ValidationError,
        )
    return _build_accounting_posting_context(
        transaction,
        identity=identity,
        stage=stage,
        refund=refund,
    )


def _validate_winning_sales_invoice(
    invoice: Any,
    *,
    order_doc: Any,
    identity: Mapping[str, Any],
) -> None:
    docstatus = cint(_value(invoice, "docstatus"))
    if (
        not invoice
        or cint(_value(invoice, "is_return"))
        or _text(_value(invoice, "name")) != identity["invoice_name"]
        or _text(_value(invoice, "custom_fb_order")) != identity["order_name"]
        or _text(_value(invoice, "company")) != identity["company"]
        or _text(_value(invoice, "currency")).upper() != identity["currency"]
    ):
        frappe.throw(
            "Duplicate Automatic QR winning Sales Invoice does not match the exact sale identity",
            frappe.ValidationError,
        )
    sale_idempotency_key = _text(
        _value(order_doc, "external_idempotency_key")
    )
    if (
        not sale_idempotency_key
        or _text(_value(invoice, "custom_fb_idempotency_key"))
        != sale_idempotency_key
        or _text(_value(invoice, "custom_fb_device_id"))
        != identity["device_id"]
    ):
        frappe.throw(
            "Duplicate Automatic QR winning Sales Invoice lacks exact KoPOS sale proof",
            frappe.ValidationError,
        )
    if docstatus == 1:
        if (
            _text(_value(order_doc, "status")) != "Submitted"
            or _text(_value(order_doc, "invoice_status")) != "Posted"
            or any(
                _text(_value(invoice, fieldname))
                for fieldname in (
                    "custom_fb_void_idempotency_key",
                    "custom_fb_void_request_fingerprint",
                    "custom_fb_void_manager",
                    "custom_fb_void_approval_token_id",
                )
            )
        ):
            frappe.throw(
                "Duplicate Automatic QR submitted winning sale lifecycle is invalid",
                frappe.ValidationError,
            )
        return
    if docstatus != 2:
        frappe.throw(
            "Duplicate Automatic QR winning Sales Invoice is neither submitted nor durably voided",
            frappe.ValidationError,
        )

    void_idempotency_key = _text(
        _value(invoice, "custom_fb_void_idempotency_key")
    )
    void_fingerprint = _text(
        _value(invoice, "custom_fb_void_request_fingerprint")
    ).lower()
    void_manager = _text(_value(invoice, "custom_fb_void_manager"))
    void_token_id = _text(
        _value(invoice, "custom_fb_void_approval_token_id")
    )
    if (
        _text(_value(order_doc, "status")) != "Cancelled"
        or _text(_value(order_doc, "invoice_status")) != "Reversed"
        or not void_idempotency_key
        or len(void_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in void_fingerprint)
        or not void_manager
        or not void_token_id
    ):
        frappe.throw(
            "Duplicate Automatic QR winning Sales Invoice has incomplete KoPOS void proof",
            frappe.ValidationError,
        )
    _load_consumed_void_approval_proof(
        approval_token_id=void_token_id,
        approval_manager_id=void_manager,
        idempotency_key=void_idempotency_key,
        resource_id=identity["invoice_name"],
    )


def _load_consumed_void_approval_proof(
    *,
    approval_token_id: str,
    approval_manager_id: str,
    idempotency_key: str,
    resource_id: str,
) -> dict[str, str]:
    from kopos_connector.utils.manager_approval import (
        load_consumed_manager_approval_proof,
    )

    return load_consumed_manager_approval_proof(
        approval_token_id=approval_token_id,
        approval_manager_id=approval_manager_id,
        action="void_order",
        idempotency_key=idempotency_key,
        resource_id=resource_id,
    )


def _build_accounting_posting_context(
    transaction: Any,
    *,
    identity: dict[str, Any],
    stage: str,
    refund: dict[str, Any] | None,
) -> dict[str, Any]:
    clearing_account = _text(_value(transaction, "duplicate_clearing_account"))
    liability_account = _text(_value(transaction, "duplicate_liability_account"))
    if not clearing_account:
        clearing_account = _text(
            frappe.db.get_value(
                "Company", identity["company"], COMPANY_CLEARING_ACCOUNT_FIELD
            )
        )
    if not liability_account:
        liability_account = _text(
            frappe.db.get_value(
                "Company", identity["company"], COMPANY_LIABILITY_ACCOUNT_FIELD
            )
        )
    if not clearing_account or not liability_account:
        frappe.throw(
            "Company duplicate QR clearing and customer liability accounts must be configured",
            frappe.ValidationError,
        )
    if clearing_account == liability_account:
        frappe.throw(
            "Duplicate QR clearing and customer liability accounts must differ",
            frappe.ValidationError,
        )
    _validate_account(
        clearing_account,
        company=identity["company"],
        currency=identity["currency"],
        expected_root_type="Asset",
        role="duplicate QR bank/clearing",
        forbidden_account_types={"Receivable"},
    )
    _validate_account(
        liability_account,
        company=identity["company"],
        currency=identity["currency"],
        expected_root_type="Liability",
        role="duplicate QR customer liability",
        forbidden_account_types={"Payable"},
    )

    recognition_key = _recognition_key(identity)
    if stage == LIABILITY_RECOGNITION_STAGE:
        journal_key = recognition_key
        debit_account = clearing_account
        credit_account = liability_account
        evidence_reference = identity["transaction_refno"]
        evidence_file = ""
        evidence_sha256 = ""
        posting_date = _provider_paid_posting_date(identity)
    elif stage == REFUND_STAGE and refund:
        journal_key = _refund_key(identity, refund)
        debit_account = liability_account
        credit_account = clearing_account
        evidence_reference = refund["provider_evidence_reference"]
        evidence_file = refund["provider_evidence_file"]
        evidence_sha256 = refund["provider_evidence_sha256"]
        posting_date = refund["refund_date"]
    else:
        raise ValueError("Duplicate QR accounting stage is invalid")

    return {
        "source": transaction,
        "source_name": identity["source_name"],
        "stage": stage,
        "journal_key": journal_key,
        "recognition_key": recognition_key,
        "winning_channel": identity["winning_channel"],
        "winning_transaction": identity["winning_transaction"],
        "winning_static_reconciliation": identity[
            "winning_static_reconciliation"
        ],
        "legacy_dynamic_winner_metadata": identity[
            "legacy_dynamic_winner_metadata"
        ],
        "order_name": identity["order_name"],
        "invoice_name": identity["invoice_name"],
        "company": identity["company"],
        "currency": identity["currency"],
        "amount_sen": identity["amount_sen"],
        "posting_date": posting_date,
        "clearing_account": clearing_account,
        "liability_account": liability_account,
        "debit_account": debit_account,
        "credit_account": credit_account,
        "evidence_reference": evidence_reference,
        "evidence_file": evidence_file,
        "evidence_sha256": evidence_sha256,
    }


def _validate_winning_provider_transaction(identity: Mapping[str, Any]) -> None:
    winner = frappe.db.get_value(
        MAYBANK_TRANSACTION_DOCTYPE,
        identity["winning_transaction"],
        [
            "name",
            "transaction_refno",
            "status",
            "maybank_status",
            "provider",
            "company",
            "currency",
            "sale_amount_sen",
            "device_id",
            "fb_order",
            "fb_order_payment",
            "consumption_key",
            "sales_invoice",
            "invoice_consumption_key",
        ],
        as_dict=True,
    )
    if not winner:
        frappe.throw(
            "Duplicate Automatic QR winning provider transaction was not found",
            frappe.ValidationError,
        )
    expected = {
        "status": "paid",
        "provider": MAYBANK_PROVIDER,
        "company": identity["company"],
        "currency": identity["currency"],
        "device_id": identity["device_id"],
        "fb_order": identity["order_name"],
        "fb_order_payment": identity["payment_row_name"],
        "consumption_key": identity["order_name"],
        "sales_invoice": identity["invoice_name"],
        "invoice_consumption_key": identity["invoice_name"],
    }
    for fieldname, expected_value in expected.items():
        actual = _text(_value(winner, fieldname))
        if fieldname in {"status", "provider"}:
            actual = actual.lower()
        if fieldname == "currency":
            actual = actual.upper()
        if actual != _text(expected_value):
            frappe.throw(
                f"Winning provider transaction {fieldname} does not match the submitted sale",
                frappe.ValidationError,
            )
    if cstr(_value(winner, "maybank_status")).strip() != "1":
        frappe.throw(
            "Winning provider transaction lacks authoritative paid status",
            frappe.ValidationError,
        )
    if not _text(_value(winner, "transaction_refno")):
        frappe.throw(
            "Winning provider transaction reference is missing",
            frappe.ValidationError,
        )
    if _strict_positive_sen(
        _value(winner, "sale_amount_sen"),
        "Winning provider transaction sale_amount_sen",
    ) != identity["amount_sen"]:
        frappe.throw(
            "Winning provider transaction amount does not match the duplicate incident",
            frappe.ValidationError,
        )


def _validate_winning_static_reconciliation(identity: Mapping[str, Any]) -> None:
    reconciliation = frappe.db.get_value(
        MANUAL_QR_RECONCILIATION_DOCTYPE,
        identity["winning_static_reconciliation"],
        [
            "name",
            "status",
            "claim_role",
            "winning_maybank_qr_transaction",
            "fb_order",
            "fb_order_payment",
            "sales_invoice",
            "device_id",
            "company",
            "currency",
            "amount_sen",
            "provider_session_id",
            "reconciliation_idempotency_key",
        ],
        as_dict=True,
    )
    if not reconciliation:
        frappe.throw(
            "Duplicate Automatic QR winning static reconciliation was not found",
            frappe.ValidationError,
        )
    if _text(_value(reconciliation, "claim_role")) not in {
        "",
        "winning_settlement",
    } or _text(_value(reconciliation, "winning_maybank_qr_transaction")):
        frappe.throw(
            "Duplicate Automatic QR winning static reconciliation is a secondary claim",
            frappe.ValidationError,
        )
    expected = {
        "name": identity["winning_static_reconciliation"],
        "fb_order": identity["order_name"],
        "fb_order_payment": identity["payment_row_name"],
        "sales_invoice": identity["invoice_name"],
        "device_id": identity["device_id"],
        "company": identity["company"],
        "currency": identity["currency"],
    }
    for fieldname, expected_value in expected.items():
        actual = _text(_value(reconciliation, fieldname))
        if fieldname == "currency":
            actual = actual.upper()
        if actual != _text(expected_value):
            frappe.throw(
                f"Winning static QR reconciliation {fieldname} does not match the submitted sale",
                frappe.ValidationError,
            )
    if _text(_value(reconciliation, "status")) not in {
        "pending_reconciliation",
        "reconciled",
        "reconciliation_failed",
    }:
        frappe.throw(
            "Winning static QR reconciliation state is invalid",
            frappe.ValidationError,
        )
    if not _text(_value(reconciliation, "provider_session_id")).startswith(
        "static-"
    ) or not _text(_value(reconciliation, "reconciliation_idempotency_key")):
        frappe.throw(
            "Winning static QR reconciliation evidence is incomplete",
            frappe.ValidationError,
        )
    if _strict_positive_sen(
        _value(reconciliation, "amount_sen"),
        "Winning static QR reconciliation amount_sen",
    ) != identity["amount_sen"]:
        frappe.throw(
            "Winning static QR reconciliation amount does not match the duplicate incident",
            frappe.ValidationError,
        )


def _validate_account(
    account_name: str,
    *,
    company: str,
    currency: str,
    expected_root_type: str,
    role: str,
    forbidden_account_types: set[str],
) -> None:
    account = frappe.db.get_value(
        "Account",
        account_name,
        [
            "name",
            "company",
            "is_group",
            "disabled",
            "root_type",
            "account_type",
            "account_currency",
        ],
        as_dict=True,
    )
    if not account:
        frappe.throw(f"Configured {role} account was not found", frappe.ValidationError)
    if (
        _text(_value(account, "company")) != company
        or cint(_value(account, "is_group"))
        or cint(_value(account, "disabled"))
        or _text(_value(account, "root_type")) != expected_root_type
        or _text(_value(account, "account_type")) in forbidden_account_types
        or _text(_value(account, "account_currency")).upper() != currency
    ):
        frappe.throw(
            f"Configured {role} account is not an enabled {expected_root_type} "
            f"ledger for {company} in {currency}",
            frappe.ValidationError,
        )

def _validated_refund_date(value: Any, *, identity: Mapping[str, Any]) -> str:
    text = _text(value)
    if not text:
        frappe.throw("provider_refund_date is required", frappe.ValidationError)
    try:
        resolved = getdate(text)
    except Exception as error:
        frappe.throw(
            "provider_refund_date must be a valid ISO date",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error
    if text != resolved.isoformat():
        frappe.throw(
            "provider_refund_date must use exact YYYY-MM-DD format",
            frappe.ValidationError,
        )
    paid_date = getdate(_provider_paid_posting_date(identity))
    today = getdate(str(now_datetime())[:10])
    if resolved < paid_date or resolved > today:
        frappe.throw(
            "provider_refund_date must be on or after provider payment and not in the future",
            frappe.ValidationError,
        )
    return resolved.isoformat()


def _provider_paid_posting_date(identity: Mapping[str, Any]) -> str:
    paid_at = identity.get("paid_at")
    if not paid_at:
        frappe.throw(
            "Duplicate Automatic QR provider paid_at is required for liability accounting",
            frappe.ValidationError,
        )
    try:
        paid_date = getdate(paid_at)
    except Exception as error:
        frappe.throw(
            "Duplicate Automatic QR provider paid_at is invalid",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error
    if paid_date > getdate(str(now_datetime())[:10]):
        frappe.throw(
            "Duplicate Automatic QR provider paid_at cannot be in the future",
            frappe.ValidationError,
        )
    return paid_date.isoformat()

def _recognition_key(identity: Mapping[str, Any]) -> str:
    if _text(identity.get("winning_channel")) == STATIC_QR_PROVIDER:
        winner_fields = [
            STATIC_QR_PROVIDER,
            _text(identity.get("winning_static_reconciliation")),
        ]
    else:
        # This exact input order is the released Maybank duplicate-accounting
        # contract. Never version it in place: existing immutable Journal
        # Entries and refund keys must replay after an additive migration.
        winner_fields = [_text(identity.get("winning_transaction"))]
    raw = "|".join(
        [
            _text(identity.get("source_name")),
            _text(identity.get("transaction_refno")),
            _text(identity.get("order_name")),
            *winner_fields,
            _text(identity.get("amount_sen")),
            _text(identity.get("currency")),
            _provider_paid_posting_date(identity),
        ]
    )
    return ACCOUNTING_KEY_NAMESPACE + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _refund_key(identity: Mapping[str, Any], refund: Mapping[str, Any]) -> str:
    raw = "|".join(
        [
            _recognition_key(identity),
            _text(refund.get("provider_refund_reference")),
            _text(refund.get("provider_evidence_reference")),
            _text(refund.get("provider_evidence_file")),
            _text(refund.get("provider_evidence_sha256")),
            _text(refund.get("amount_sen")),
            _text(refund.get("currency")),
            _text(refund.get("refund_date")),
        ]
    )
    return REFUND_KEY_NAMESPACE + hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _require_schema_fields() -> None:
    source_meta = frappe.get_meta(MAYBANK_TRANSACTION_DOCTYPE)
    missing_source = [
        fieldname
        for fieldname in REQUIRED_SOURCE_FIELDS
        if not source_meta.has_field(fieldname)
    ]
    journal_meta = frappe.get_meta(JOURNAL_ENTRY_DOCTYPE)
    missing_journal = [
        fieldname
        for fieldname in REQUIRED_JOURNAL_FIELDS
        if not journal_meta.has_field(fieldname)
    ]
    missing = [*missing_source, *missing_journal]
    if missing:
        frappe.throw(
            "Duplicate QR accounting fields are missing; run bench migrate: "
            + ", ".join(sorted(missing)),
            frappe.ValidationError,
        )

def _strict_positive_sen(value: Any, fieldname: str) -> int:
    if isinstance(value, bool):
        frappe.throw(f"{fieldname} must be integer sen", frappe.ValidationError)
    text = cstr(value).strip()
    if not text or not text.isdigit():
        frappe.throw(f"{fieldname} must be integer sen", frappe.ValidationError)
    resolved = int(text)
    if resolved <= 0:
        frappe.throw(f"{fieldname} must be positive", frappe.ValidationError)
    return resolved


def _money_to_sen(value: Any, fieldname: str) -> int:
    try:
        amount = Decimal(cstr(value or 0).strip())
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"{fieldname} is invalid", frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    scaled = amount * SEN_PER_UNIT
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        frappe.throw(f"{fieldname} must have at most two decimals", frappe.ValidationError)
    return int(scaled)


def _sen_to_amount(value: int) -> Decimal:
    return (Decimal(value) / SEN_PER_UNIT).quantize(Decimal("0.01"))


def _required_text(payload: Mapping[str, Any], fieldname: str) -> str:
    value = _text(payload.get(fieldname))
    if not value:
        frappe.throw(f"{fieldname} is required", frappe.ValidationError)
    return value


def _bounded_text(
    value: Any,
    fieldname: str,
    *,
    minimum: int,
    maximum: int,
) -> str:
    text = " ".join(cstr(value).strip().split())
    if len(text) < minimum or len(text) > maximum:
        frappe.throw(
            f"{fieldname} must be {minimum}-{maximum} characters",
            frappe.ValidationError,
        )
    return text


def _text(value: Any) -> str:
    return cstr(value).strip()


def _value(document: Any, fieldname: str) -> Any:
    if document is None:
        return None
    if isinstance(document, Mapping):
        return document.get(fieldname)
    getter = getattr(document, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None:
            return value
    return getattr(document, fieldname, None)
