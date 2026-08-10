"""Reviewed target schema and scheduler metadata required for acceptance."""

from __future__ import annotations


# Narrow release-review exception, approved 2026-08-10 by the ERP release
# reviewer: target_preflight_machine.py may remain at exactly 929 lines for this
# release because it is one acceptance-only coordinator and its current bytes
# passed the real-stack gate. Owner: ERP release owner. Re-review and split it
# before the next connector release, or immediately if its line count grows.
# This does not waive the normal 900-line limit for any other module.

# Each value is: field type, required, unique, search index.
REQUIRED_FIELD_SPECS = {
    "Company": {
        "custom_kopos_qr_failure_variance_account": ("Link", False, False, False),
        "custom_kopos_qr_duplicate_payment_clearing_account": (
            "Link",
            False,
            False,
            False,
        ),
        "custom_kopos_qr_customer_liability_account": (
            "Link",
            False,
            False,
            False,
        ),
    },
    "FB Allowed Modifier Group": {
        "required": ("Check", False, False, False),
        "override_min_selection": ("Int", False, False, False),
        "override_max_selection": ("Int", False, False, False),
    },
    "FB Order": {
        "order_id": ("Data", True, True, False),
        "external_idempotency_key": ("Data", False, True, False),
        "request_fingerprint": ("Data", False, False, False),
        "automatic_qr_state": ("Select", False, False, True),
        "automatic_qr_payment": ("Data", False, False, True),
        "device_id": ("Data", True, False, True),
        "shift": ("Link", True, False, True),
        "company": ("Link", True, False, False),
        "currency": ("Link", True, False, False),
        "status": ("Select", True, False, True),
        "sales_invoice": ("Link", False, False, False),
    },
    "FB Order Payment": {
        "source_payment_id": ("Data", False, False, True),
        "payment_method": ("Link", True, False, False),
        "payment_channel_code": ("Data", False, False, False),
        "reference_no": ("Data", False, False, False),
        "external_transaction_id": ("Data", False, False, False),
        "is_manual_confirmation": ("Check", False, False, False),
        "maybank_qr_transaction": ("Link", False, False, False),
        "settlement_status": ("Select", False, False, False),
        "manual_qr_reconciliation": ("Link", False, False, False),
        "reconciliation_idempotency_key": ("Data", False, False, False),
    },
    "FB Shift": {
        "shift_code": ("Data", True, True, False),
        "open_idempotency_key": ("Data", False, True, True),
        "open_request_fingerprint": ("Data", False, False, False),
        "close_idempotency_key": ("Data", False, True, True),
        "close_request_fingerprint": ("Data", False, False, False),
        "device_id": ("Data", True, False, True),
        "status": ("Select", True, False, True),
        "company": ("Link", True, False, False),
    },
    "Journal Entry": {
        "custom_kopos_qr_reconciliation_key": ("Data", False, True, True),
        "custom_kopos_qr_source_doctype": ("Link", False, False, False),
        "custom_kopos_qr_source_name": ("Dynamic Link", False, False, False),
        "custom_kopos_qr_fb_order": ("Link", False, False, False),
        "custom_kopos_qr_sales_invoice": ("Link", False, False, False),
        "custom_kopos_qr_amount_sen": ("Int", False, False, False),
        "custom_kopos_qr_currency": ("Link", False, False, False),
        "custom_kopos_qr_disposition": ("Select", False, False, False),
        "custom_kopos_qr_fb_order_payment": ("Data", False, False, False),
    },
    "KoPOS Device": {
        "device_id": ("Data", True, True, False),
        "enabled": ("Check", False, False, False),
        "pos_profile": ("Link", True, False, False),
        "static_qr_payload": ("Small Text", False, False, False),
        "static_qr_payload_sha256": ("Data", False, False, False),
        "static_qr_company": ("Link", False, False, False),
        "static_qr_commissioned_at": ("Datetime", False, False, False),
    },
    "Maybank QR Transaction": {
        "transaction_refno": ("Data", True, True, True),
        "status": ("Select", True, False, True),
        "maybank_status": ("Int", False, False, False),
        "sale_amount_sen": ("Int", False, False, False),
        "fb_order": ("Link", False, False, True),
        "fb_order_payment": ("Data", False, False, True),
        "sales_invoice": ("Link", False, False, True),
        "outlet_id": ("Data", False, False, False),
        "device_id": ("Data", False, False, True),
        "provider": ("Data", False, False, False),
        "company": ("Link", False, False, False),
        "currency": ("Link", False, False, False),
        "idempotency_key": ("Data", False, False, True),
        "request_fingerprint": ("Data", False, True, False),
        "receipt_file_hash": ("Data", False, False, False),
        "manual_reconciliation_status": ("Select", False, False, True),
        "duplicate_payment_status": ("Select", False, False, True),
        "created_at": ("Datetime", False, False, False),
        "expires_at": ("Datetime", False, False, False),
        "last_polled_at": ("Datetime", False, False, False),
        "poll_count": ("Int", False, False, False),
        "is_test_simulation": ("Check", False, False, False),
    },
    "Sales Invoice": {
        "custom_fb_order": ("Link", False, False, False),
        "custom_fb_shift": ("Link", False, False, False),
        "custom_fb_device_id": ("Data", False, False, False),
        "custom_fb_idempotency_key": ("Data", False, True, False),
    },
    "Sales Invoice Payment": {
        "custom_fb_source_payment_id": ("Data", False, False, True),
    },
}

REQUIRED_SCHEDULER_FREQUENCIES = {
    "kopos_connector.tasks.poll_maybank.poll_pending_maybank_transactions": "All",
    (
        "kopos_connector.kopos.services.accounting."
        "automatic_qr_finalization_service.recover_paid_automatic_qr_sales"
    ): "All",
    (
        "kopos_connector.kopos.services.projection.retry_service."
        "retry_projection_failures"
    ): "All",
}

OBSOLETE_SCHEDULER_JOBS = (
    "kopos_connector.api.modifiers.aggregate_modifier_stats",
)
