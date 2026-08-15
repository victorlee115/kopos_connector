"""Reviewed target schema and scheduler metadata required for acceptance."""

from __future__ import annotations


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
    "FB Order Line": {
        "commercial_modifier_snapshot_json": ("Code", False, False, False),
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
    "FB Return Event Line": {
        "original_sales_invoice_item": ("Data", False, False, True),
        "original_fb_order_line_ref": ("Data", False, False, False),
        "commercial_modifier_snapshot_json": ("Code", False, False, False),
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
        "static_qr_merchant_id": ("Data", False, False, False),
        "static_qr_acquirer_id": ("Data", False, False, False),
        "static_qr_merchant_name": ("Data", False, False, False),
        "static_qr_version": ("Data", False, False, False),
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

# Link targets, Dynamic Link controllers, and the exact Select state vocabulary
# are part of the deployed database contract. Field type alone is insufficient:
# a Link can otherwise point at the wrong DocType and a Select can silently lose
# a state that existing tablets or reconciliation jobs still use.
REQUIRED_FIELD_OPTIONS = {
    "Company": {
        "custom_kopos_qr_failure_variance_account": "Account",
        "custom_kopos_qr_duplicate_payment_clearing_account": "Account",
        "custom_kopos_qr_customer_liability_account": "Account",
    },
    "FB Order": {
        "automatic_qr_state": (
            "\nprepared\nprovider_pending\nprovider_ambiguous\nprovider_rejected"
            "\nprovider_paid\nmanual_pending_reconciliation\nfinalized"
        ),
        "shift": "FB Shift",
        "company": "Company",
        "currency": "Currency",
        "status": "Draft\nSubmitted\nCancelled",
        "sales_invoice": "Sales Invoice",
    },
    "FB Order Line": {
        "commercial_modifier_snapshot_json": "JSON",
    },
    "FB Order Payment": {
        "payment_method": "Mode of Payment",
        "maybank_qr_transaction": "Maybank QR Transaction",
        "settlement_status": (
            "awaiting_provider\nverified\npending_reconciliation\nreconciled"
            "\nreconciliation_failed"
        ),
        "manual_qr_reconciliation": "Manual QR Reconciliation",
    },
    "FB Return Event Line": {
        "commercial_modifier_snapshot_json": "JSON",
    },
    "FB Shift": {
        "status": "Open\nClosing\nClosed\nException\nCancelled",
        "company": "Company",
    },
    "Journal Entry": {
        "custom_kopos_qr_source_doctype": "DocType",
        "custom_kopos_qr_source_name": "custom_kopos_qr_source_doctype",
        "custom_kopos_qr_fb_order": "FB Order",
        "custom_kopos_qr_sales_invoice": "Sales Invoice",
        "custom_kopos_qr_currency": "Currency",
        "custom_kopos_qr_disposition": "\nreconciliation_failed",
    },
    "KoPOS Device": {
        "pos_profile": "POS Profile",
        "static_qr_company": "Company",
    },
    "Maybank QR Transaction": {
        "status": "creating\npending\nscanned\npaid\nfailed\ntimeout\nunknown",
        "fb_order": "FB Order",
        "sales_invoice": "Sales Invoice",
        "company": "Company",
        "currency": "Currency",
        "manual_reconciliation_status": (
            "\npending_reconciliation\nreconciled\nreconciliation_failed"
        ),
        "duplicate_payment_status": (
            "\npossible_duplicate\naccounting_pending\nrefund_required\nrefunded"
            "\nsettled_existing_sale"
        ),
    },
    "Sales Invoice": {
        "custom_fb_order": "FB Order",
        "custom_fb_shift": "FB Shift",
    },
}

# Defaults below change newly-authored business state. Empty means that the
# field must remain intentionally unset; values are compared as Frappe strings.
REQUIRED_FIELD_DEFAULTS = {
    "FB Order": {
        "automatic_qr_state": "",
        "status": "Draft",
    },
    "FB Order Line": {
        "commercial_modifier_snapshot_json": "",
    },
    "FB Order Payment": {
        "is_manual_confirmation": "0",
        "settlement_status": "verified",
    },
    "FB Return Event Line": {
        "original_sales_invoice_item": "",
        "original_fb_order_line_ref": "",
        "commercial_modifier_snapshot_json": "",
    },
    "FB Shift": {"status": "Open"},
    "KoPOS Device": {"enabled": "1"},
    "Maybank QR Transaction": {
        "status": "pending",
        "manual_reconciliation_status": "",
        "duplicate_payment_status": "",
        "is_test_simulation": "0",
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

# Inventory recovery is deliberately isolated from the commercial retry
# worker.  It is a five-minute cron job, not an ``all`` frequency hook; the
# target preflight checks both groups independently so adding it cannot weaken
# the commercial scheduler contract.
REQUIRED_CRON_SCHEDULER_FREQUENCIES = {
    (
        "kopos_connector.kopos.services.inventory_autopilot."
        "projection_worker.recover_inventory_projections"
    ): "*/5 * * * *",
    (
        "kopos_connector.kopos.services.inventory_autopilot."
        "preparation.schedule_preparation_tasks"
    ): "*/5 * * * *",
    (
        "kopos_connector.kopos.services.inventory_autopilot."
        "planning.generate_inventory_plans"
    ): "0 * * * *",
}

OBSOLETE_SCHEDULER_JOBS = (
    "kopos_connector.api.modifiers.aggregate_modifier_stats",
)
