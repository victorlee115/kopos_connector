# pyright: reportMissingImports=false

"""Read-only ERP business-state evidence for Maybank production UAT."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import frappe
from frappe.utils import cint, cstr

from kopos_connector.acceptance.maybank_uat_accounting import (
    BUSINESS_STATE_SOURCE,
    settlement_gl_state,
    stock_state,
)
from kopos_connector.acceptance.maybank_uat_common import (
    AcceptanceContext,
    build_export_bindings,
    canonical_json_sha256,
    decimal_money_to_sen,
    load_acceptance_context,
    producer_source_sha256,
    write_report_atomically,
)


PRODUCER = (
    "kopos_connector.acceptance.maybank_uat_business_state.export_v1"
)


def _value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _fail(message: str) -> None:
    frappe.throw(message, frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")


def _exact_text(value: Any, fieldname: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        _fail(f"{fieldname} must be a nonempty exact value")
    return value


def _get_value(
    doctype: str,
    name: str,
    fields: tuple[str, ...],
) -> dict[str, Any]:
    row = frappe.db.get_value(
        doctype,
        name,
        list(fields),
        as_dict=True,
    )
    if not row:
        _fail(f"{doctype} evidence is missing")
    return {
        fieldname: _value(row, fieldname)
        for fieldname in fields
    }


def _count(doctype: str, filters: dict[str, Any]) -> int:
    value = frappe.db.count(doctype, filters=filters)
    count = cint(value)
    if count < 0 or cstr(value).strip() != cstr(count):
        _fail(f"{doctype} count is invalid")
    return count


def _provider_transactions(
    context: AcceptanceContext,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    for index, transaction in enumerate(context.transactions):
        reference = _exact_text(
            transaction.get("transaction_refno"),
            f"Maybank transaction {index} reference",
        )
        status = cstr(transaction.get("status")).strip()
        raw_status = cint(transaction.get("maybank_status"))
        is_winner = status == "paid" and raw_status == 1
        if is_winner:
            if winner is not None:
                _fail("More than one Maybank transaction is durably paid")
            winner = transaction
        elif any(
            cstr(transaction.get(fieldname)).strip()
            for fieldname in (
                "sales_invoice",
                "consumption_key",
                "invoice_consumption_key",
                "consumed_at",
            )
        ):
            _fail(
                f"Retained Maybank transaction {index} is unexpectedly consumed"
            )

        output.append(
            {
                "name": _exact_text(
                    transaction.get("name"),
                    f"Maybank transaction {index} name",
                ),
                "provider": "maybank_qr",
                "status": status,
                "maybankStatus": raw_status,
                "providerPaidAuthoritative": is_winner,
                "providerReferenceDigest": hashlib.sha256(
                    reference.encode("utf-8")
                ).hexdigest(),
                "amountSen": int(transaction["sale_amount_sen"]),
                "currency": context.currency,
                "providerOrigin": context.provider_origin,
                "outletIdSha256": context.outlet_id_sha256,
                "deviceUdid": context.bindings.device_udid,
                "company": context.company,
                "idempotencyKey": _exact_text(
                    transaction.get("idempotency_key"),
                    f"Maybank transaction {index} idempotency_key",
                ),
                # This is ERP's canonical generation-attempt fingerprint. It
                # is deliberately not a status HTTP request-body hash.
                "requestFingerprintSha256": _exact_text(
                    transaction.get("request_fingerprint"),
                    f"Maybank transaction {index} request_fingerprint",
                ),
            }
        )
    if winner is None:
        _fail("The Maybank business state has no paid winner")
    return output, winner


def _load_sale_and_payment(
    context: AcceptanceContext,
    winner: dict[str, Any],
) -> tuple[dict[str, Any], Any, Any]:
    order_name = _exact_text(winner.get("fb_order"), "winning FB Order")
    payment_name = _exact_text(
        winner.get("fb_order_payment"),
        "winning FB Order Payment",
    )
    invoice_name = _exact_text(
        winner.get("sales_invoice"),
        "winning Sales Invoice",
    )
    if (
        cstr(winner.get("consumption_key")) != order_name
        or cstr(winner.get("invoice_consumption_key")) != invoice_name
        or not winner.get("consumed_at")
    ):
        _fail("The paid Maybank winner is not exactly consumed by its sale")

    order_fields = (
        "name",
        "status",
        "docstatus",
        "sales_invoice",
        "ingredient_stock_entry",
        "device_id",
        "company",
        "currency",
        "external_idempotency_key",
        "automatic_qr_state",
        "automatic_qr_payment",
        "automatic_qr_winner_channel",
        "invoice_status",
        "stock_status",
    )
    order = _get_value("FB Order", order_name, order_fields)
    if (
        cstr(order.get("status")) != "Submitted"
        or cint(order.get("docstatus")) != 1
        or cstr(order.get("sales_invoice")) != invoice_name
        or cstr(order.get("device_id")) != context.bindings.device_udid
        or cstr(order.get("company")) != context.company
        or cstr(order.get("currency")).upper() != context.currency
        or cstr(order.get("automatic_qr_state")) != "finalized"
        or cstr(order.get("automatic_qr_payment")) != payment_name
        or cstr(order.get("automatic_qr_winner_channel")) != "maybank_qr"
        or cstr(order.get("invoice_status")) != "Posted"
        or cstr(order.get("stock_status")) != "Posted"
    ):
        _fail("The paid Maybank FB Order is not fully and exactly submitted")
    order_idempotency_key = _exact_text(
        order.get("external_idempotency_key"),
        "FB Order external_idempotency_key",
    )
    if (
        _count(
            "FB Order",
            {"external_idempotency_key": order_idempotency_key},
        )
        != 1
    ):
        _fail("The paid sale idempotency key has duplicate FB Orders")

    order_doc = frappe.get_doc("FB Order", order_name)
    payments = [
        payment
        for payment in list(getattr(order_doc, "payments", None) or [])
        if cstr(_value(payment, "name")) == payment_name
    ]
    if len(payments) != 1:
        _fail("The winning FB Order Payment is missing or duplicated")
    payment = payments[0]
    reference = _exact_text(
        winner.get("transaction_refno"),
        "winning Maybank reference",
    )
    payment_method = cstr(_value(payment, "payment_method")).strip()
    payment_channel = cstr(
        _value(payment, "payment_channel_code")
    ).strip().lower()
    if (
        payment_method.strip().lower() != "duitnow qr"
        or payment_channel not in {"maybank", "maybank qr"}
        or cstr(_value(payment, "maybank_qr_transaction"))
        != cstr(winner.get("name"))
        or cstr(_value(payment, "external_transaction_id")) != reference
        or cstr(_value(payment, "reference_no")) != reference
        or cstr(_value(payment, "settlement_status")) != "verified"
        or cint(_value(payment, "is_manual_confirmation")) != 0
        or decimal_money_to_sen(
            _value(payment, "amount"),
            "FB Order Payment amount",
        )
        != context.amount_sen
    ):
        _fail("The winning FB Order Payment does not prove verified Maybank")
    _exact_text(
        _value(payment, "source_payment_id"),
        "FB Order Payment source_payment_id",
    )
    return order, order_doc, payment


def _load_invoice(
    context: AcceptanceContext,
    order: dict[str, Any],
    payment: Any,
) -> tuple[dict[str, Any], Any, Any, str]:
    invoice_name = _exact_text(
        order.get("sales_invoice"),
        "FB Order sales_invoice",
    )
    fields = (
        "name",
        "docstatus",
        "is_return",
        "currency",
        "company",
        "custom_fb_order",
        "custom_fb_device_id",
        "custom_fb_idempotency_key",
        "grand_total",
        "net_total",
        "total_taxes_and_charges",
        "write_off_amount",
        "outstanding_amount",
        "customer",
        "debit_to",
    )
    invoice = _get_value("Sales Invoice", invoice_name, fields)
    order_name = cstr(order.get("name"))
    order_idempotency_key = cstr(order.get("external_idempotency_key"))
    if (
        cint(invoice.get("docstatus")) != 1
        or cint(invoice.get("is_return")) != 0
        or cstr(invoice.get("currency")).upper() != context.currency
        or cstr(invoice.get("company")) != context.company
        or cstr(invoice.get("custom_fb_order")) != order_name
        or cstr(invoice.get("custom_fb_device_id"))
        != context.bindings.device_udid
        or cstr(invoice.get("custom_fb_idempotency_key"))
        != order_idempotency_key
        or decimal_money_to_sen(
            invoice.get("grand_total"),
            "Sales Invoice grand_total",
        )
        != context.amount_sen
        or decimal_money_to_sen(
            invoice.get("net_total"),
            "Sales Invoice net_total",
        )
        != context.amount_sen
        or decimal_money_to_sen(
            invoice.get("total_taxes_and_charges"),
            "Sales Invoice total_taxes_and_charges",
        )
        != 0
        or decimal_money_to_sen(
            invoice.get("write_off_amount"),
            "Sales Invoice write_off_amount",
        )
        != 0
        or decimal_money_to_sen(
            invoice.get("outstanding_amount"),
            "Sales Invoice outstanding_amount",
        )
        != 0
    ):
        _fail(
            "The Sales Invoice is not the exact settled zero-tax acceptance sale"
        )
    if (
        _count(
            "Sales Invoice",
            {"custom_fb_idempotency_key": order_idempotency_key},
        )
        != 1
        or _count(
            "Sales Invoice",
            {"custom_fb_order": order_name, "docstatus": 1},
        )
        != 1
    ):
        _fail("The paid sale has duplicate or ambiguous Sales Invoices")

    invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)
    items = list(getattr(invoice_doc, "items", None) or [])
    income_accounts = {
        cstr(_value(item, "income_account")).strip()
        for item in items
        if cstr(_value(item, "income_account")).strip()
    }
    if not items or len(income_accounts) != 1:
        _fail(
            "The acceptance Sales Invoice must use one exact income account"
        )
    income_account = next(iter(income_accounts))

    source_payment_id = cstr(_value(payment, "source_payment_id"))
    invoice_payments = [
        invoice_payment
        for invoice_payment in list(
            getattr(invoice_doc, "payments", None) or []
        )
        if cstr(
            _value(invoice_payment, "custom_fb_source_payment_id")
        )
        == source_payment_id
    ]
    if len(invoice_payments) != 1:
        _fail("The Sales Invoice payment row is missing or ambiguous")
    invoice_payment = invoice_payments[0]
    payment_account = _exact_text(
        _value(invoice_payment, "account"),
        "Sales Invoice payment account",
    )
    if (
        cstr(_value(invoice_payment, "mode_of_payment"))
        != cstr(_value(payment, "payment_method"))
        or decimal_money_to_sen(
            _value(invoice_payment, "amount"),
            "Sales Invoice payment amount",
        )
        != context.amount_sen
    ):
        _fail("The Sales Invoice payment does not match the FB Order payment")
    return invoice, invoice_doc, invoice_payment, income_account



def _collect_business_state(context: AcceptanceContext) -> dict[str, Any]:
    provider_transactions, winner = _provider_transactions(context)
    order, _order_doc, payment = _load_sale_and_payment(context, winner)
    invoice, _invoice_doc, invoice_payment, income_account = _load_invoice(
        context,
        order,
        payment,
    )
    payment_account = cstr(_value(invoice_payment, "account"))
    gl_query, gl_rows, accounts = settlement_gl_state(
        context,
        invoice,
        income_account,
        payment_account,
    )
    stock_entries, stock_ledger_entries = stock_state(context, order)

    winner_reference = cstr(winner.get("transaction_refno"))
    winner_digest = hashlib.sha256(
        winner_reference.encode("utf-8")
    ).hexdigest()
    payment_name = cstr(_value(payment, "name"))
    order_name = cstr(order.get("name"))
    invoice_name = cstr(invoice.get("name"))
    manual_reconciliation_count = _count(
        "Manual QR Reconciliation",
        {"fb_order": order_name},
    )
    if manual_reconciliation_count:
        _fail(
            "The provider-paid Maybank sale has an unexpected manual reconciliation"
        )

    fb_orders = [
        {
            "name": order_name,
            "status": "Submitted",
            "docstatus": 1,
            "salesInvoice": invoice_name,
            "ingredientStockEntry": cstr(
                order.get("ingredient_stock_entry")
            ),
            "providerReferenceDigest": winner_digest,
            "deviceUdid": context.bindings.device_udid,
            "company": context.company,
            "currency": context.currency,
            "idempotencyKey": cstr(
                order.get("external_idempotency_key")
            ),
        }
    ]
    sales_invoices = [
        {
            "name": invoice_name,
            "fbOrder": order_name,
            "docstatus": 1,
            "currency": context.currency,
            "company": context.company,
            "deviceUdid": context.bindings.device_udid,
            "idempotencyKey": cstr(
                invoice.get("custom_fb_idempotency_key")
            ),
            "grandTotalSen": decimal_money_to_sen(
                invoice.get("grand_total"),
                "Sales Invoice grand_total",
            ),
            "netTotalSen": decimal_money_to_sen(
                invoice.get("net_total"),
                "Sales Invoice net_total",
            ),
            "totalTaxesAndChargesSen": decimal_money_to_sen(
                invoice.get("total_taxes_and_charges"),
                "Sales Invoice total_taxes_and_charges",
            ),
            "writeOffAmountSen": decimal_money_to_sen(
                invoice.get("write_off_amount"),
                "Sales Invoice write_off_amount",
            ),
            "outstandingAmountSen": decimal_money_to_sen(
                invoice.get("outstanding_amount"),
                "Sales Invoice outstanding_amount",
            ),
            "customer": _exact_text(
                invoice.get("customer"),
                "Sales Invoice customer",
            ),
            "debitTo": _exact_text(
                invoice.get("debit_to"),
                "Sales Invoice debit_to",
            ),
            "incomeAccount": income_account,
        }
    ]
    payment_account_type = next(
        (
            account["accountType"]
            for account in accounts
            if account["name"] == payment_account
        ),
        "",
    )
    if payment_account_type != "Bank":
        _fail("The verified Maybank payment account is not a Bank account")
    payments = [
        {
            "name": payment_name,
            "fbOrder": order_name,
            "salesInvoice": invoice_name,
            "providerTransaction": cstr(winner.get("name")),
            "method": "qr",
            "paymentChannelCode": "maybank",
            "account": payment_account,
            "accountType": payment_account_type,
            "settlementStatus": "verified",
            "providerReferenceDigest": winner_digest,
            "amountSen": context.amount_sen,
            "company": context.company,
            "currency": context.currency,
        }
    ]
    reconciliation = {
        "status": "completed",
        "providerReferenceDigest": winner_digest,
        "providerTransaction": cstr(winner.get("name")),
        "fbOrder": order_name,
        "salesInvoice": invoice_name,
        "payment": payment_name,
        "settledPaymentCount": 1,
        "duplicateFbOrderCount": 0,
        "duplicateSalesInvoiceCount": 0,
        "pendingReconciliationCount": 0,
        "company": context.company,
        "currency": context.currency,
    }
    return {
        "providerTransactions": provider_transactions,
        "fbOrders": fb_orders,
        "salesInvoices": sales_invoices,
        "payments": payments,
        "settlementGlQuery": gl_query,
        "settlementGlRows": gl_rows,
        "accounts": accounts,
        "stockEntries": stock_entries,
        "stockLedgerEntries": stock_ledger_entries,
        "reconciliation": reconciliation,
    }


@frappe.whitelist(methods=["POST"])
def export_v1(
    transaction_references: Any,
    capacity_fence_transaction: str,
    candidate_apk_sha256: str,
    erp_artifact_sha256: str,
    mobile_manifest_sha256: str,
    device_udid: str,
    run_nonce: str,
    output_filename: str = "maybank-erp-business-state.json",
) -> dict[str, Any]:
    """Export exact ERP sale/accounting state without changing business data."""

    bindings = build_export_bindings(
        candidate_apk_sha256=candidate_apk_sha256,
        erp_artifact_sha256=erp_artifact_sha256,
        mobile_manifest_sha256=mobile_manifest_sha256,
        device_udid=device_udid,
        run_nonce=run_nonce,
    )
    context = load_acceptance_context(
        transaction_references=transaction_references,
        capacity_fence_transaction=capacity_fence_transaction,
        bindings=bindings,
    )
    collected_at = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    state = _collect_business_state(context)
    query_execution_id = canonical_json_sha256(
        {
            "collectedAt": collected_at,
            "erpArtifactSha256": bindings.erp_artifact_sha256,
            "providerTransactionNames": [
                transaction["name"]
                for transaction in state["providerTransactions"]
            ],
            "runNonce": bindings.run_nonce,
        }
    )
    counts = {
        fieldname: len(state[fieldname])
        for fieldname in (
            "providerTransactions",
            "fbOrders",
            "salesInvoices",
            "payments",
            "settlementGlRows",
            "accounts",
            "stockEntries",
            "stockLedgerEntries",
        )
    }
    counts.update(
        {
            "duplicateFbOrders": 0,
            "duplicateSalesInvoices": 0,
            "pendingReconciliations": 0,
        }
    )
    report = {
        "schemaVersion": "2",
        "status": "passed",
        "source": BUSINESS_STATE_SOURCE,
        "candidateApkSha256": bindings.candidate_apk_sha256,
        "erpArtifactSha256": bindings.erp_artifact_sha256,
        "mobileManifestSha256": bindings.mobile_manifest_sha256,
        "deviceUdid": bindings.device_udid,
        "runNonce": bindings.run_nonce,
        "providerOrigin": context.provider_origin,
        "outletIdSha256": context.outlet_id_sha256,
        "company": context.company,
        "currency": context.currency,
        "collectedAt": collected_at,
        "provenance": {
            "producer": PRODUCER,
            "producerSourceSha256": producer_source_sha256(__file__),
            "collectorMode": "read_only_business_dump_v1",
            "readOnly": True,
            "queryExecutionId": query_execution_id,
            "erpArtifactSha256": bindings.erp_artifact_sha256,
            "runNonce": bindings.run_nonce,
        },
        **state,
        "capacityRejection": context.capacity_rejection,
        "documentCounts": counts,
    }
    write_report_atomically(report, output_filename)
    return report
