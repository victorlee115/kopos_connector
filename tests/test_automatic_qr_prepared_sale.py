from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.api import fb_orders


class PreparedOrder(SimpleNamespace):
    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        self.inserted = False
        self.saved = False
        self.submitted = False
        self.created_resolutions: list[dict[str, Any]] | None = None

    def insert(self, ignore_permissions: bool = False) -> "PreparedOrder":
        assert ignore_permissions is True
        self.inserted = True
        return self

    def save(self, ignore_permissions: bool = False) -> "PreparedOrder":
        assert ignore_permissions is True
        self.saved = True
        return self

    def submit(self) -> "PreparedOrder":
        self.submitted = True
        self.docstatus = 1
        return self

    def reload(self) -> "PreparedOrder":
        return self

    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)

    def build_line_resolutions(self) -> list[dict[str, Any]]:
        return [{"resolved_components": [{"item": "MILK"}]}]

    def validate_stock_availability(self, resolutions: list[dict[str, Any]]) -> None:
        assert resolutions == self.build_line_resolutions()

    def create_resolved_sales(self, resolutions: list[dict[str, Any]]) -> None:
        self.created_resolutions = resolutions


def _normalized_payment(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "payment_id": "LOCAL-PAYMENT-1",
        "payment_method": "DuitNow QR",
        "payment_channel_code": "maybank",
        "amount_sen": 1250,
        "tendered_amount_sen": 1250,
        "change_amount_sen": 0,
        "reference_no": None,
        "external_transaction_id": None,
        "manual_confirmation_evidence_json": None,
        "reconciliation_idempotency_key": None,
        "is_manual_confirmation": 0,
        "settlement_status": "verified",
    }
    values.update(overrides)
    return values


def _normalized(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "external_idempotency_key": "SALE-IDEMPOTENCY-1",
        "accepted_sale_fingerprint": "a" * 64,
        "device_id": "TAB-A-001",
        "shift": "FB-SHIFT-1",
        "staff_id": "cashier@example.test",
        "payments": [_normalized_payment()],
    }
    values.update(overrides)
    return values


def _prepared_order(*, docstatus: int = 0) -> PreparedOrder:
    payment = SimpleNamespace(
        name="FB-ORDER-PAYMENT-1",
        source_payment_id="LOCAL-PAYMENT-1",
        amount=Decimal("12.50"),
        reference_no=None,
        external_transaction_id=None,
        maybank_qr_transaction=None,
        manual_confirmation_evidence_json=None,
        reconciliation_idempotency_key=None,
        is_manual_confirmation=0,
        settlement_status="verified",
    )
    return PreparedOrder(
        name="FB-ORDER-1",
        order_id="LOCAL-ORDER-1",
        external_idempotency_key="SALE-IDEMPOTENCY-1",
        accepted_sale_fingerprint="a" * 64,
        automatic_qr_state="prepared",
        automatic_qr_payment="FB-ORDER-PAYMENT-1",
        device_id="TAB-A-001",
        currency="MYR",
        payments=[payment],
        docstatus=docstatus,
    )


def _exact_provider_transaction(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "MBQR-1",
        "transaction_refno": "MB-REF-1",
        "status": "paid",
        "maybank_status": 1,
        "paid_at": "2026-07-17 12:01:00",
        "expires_at": "2026-07-17 12:05:00",
        "provider": "maybank_qr",
        "qr_data": "issued-provider-qr",
        "outlet_id": "OUTLET-1",
        "device_id": "TAB-A-001",
        "currency": "MYR",
        "sale_amount_sen": 1250,
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FB-ORDER-PAYMENT-1",
    }
    values.update(overrides)
    return values


def _patch_finalization_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    transaction: dict[str, Any],
) -> None:
    monkeypatch.setattr(fb_orders.frappe.db, "sql", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        fb_orders.frappe.db,
        "get_value",
        lambda *args, **kwargs: transaction,
    )
    monkeypatch.setattr(fb_orders.frappe.db, "set_value", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        fb_orders,
        "_resolve_order_payment",
        lambda payment, index: {
            "reference_no": payment["reference_no"],
            "external_transaction_id": payment["external_transaction_id"],
            "manual_confirmation_evidence_json": payment[
                "manual_confirmation_evidence_json"
            ],
            "reconciliation_idempotency_key": payment[
                "reconciliation_idempotency_key"
            ],
        },
    )
    monkeypatch.setattr(
        fb_orders,
        "_build_submit_response",
        lambda status, document: {"status": status, "fb_order": document.name},
    )


def test_prepare_persists_sale_snapshot_and_payment_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = _normalized()
    order = _prepared_order()
    monkeypatch.setattr(
        fb_orders,
        "_normalize_submit_order_payload",
        lambda payload: normalized,
    )
    monkeypatch.setattr(
        fb_orders,
        "_validate_new_submit_order_state",
        lambda value: value,
    )
    monkeypatch.setattr(fb_orders, "_get_existing_fb_order_name", lambda key: None)
    monkeypatch.setattr(fb_orders, "_validate_submit_shift", lambda **kwargs: None)
    monkeypatch.setattr(fb_orders, "_build_fb_order", lambda value: order)
    monkeypatch.setattr(
        fb_orders,
        "now_datetime",
        lambda: datetime(2026, 7, 17, 12, 0, 0),
    )

    result = fb_orders.prepare_automatic_qr_sale_payload({"ignored": True})

    assert result["status"] == "ok"
    assert result["fb_order"] == "FB-ORDER-1"
    assert result["fb_order_payment"] == "FB-ORDER-PAYMENT-1"
    assert result["accepted_sale_fingerprint"] == "a" * 64
    assert order.inserted is True
    assert order.saved is True
    assert order.created_resolutions == order.build_line_resolutions()
    assert order.payments[0].settlement_status == "awaiting_provider"
    assert order.automatic_qr_state == "prepared"


def test_prepare_rejects_any_provider_or_manual_evidence() -> None:
    for payment in (
        _normalized_payment(
            reference_no="MB-REF-1",
            external_transaction_id="MB-REF-1",
        ),
        _normalized_payment(
            manual_confirmation_evidence_json="{}",
            is_manual_confirmation=1,
        ),
    ):
        with pytest.raises(
            fb_orders.frappe.ValidationError,
            match="before provider or manual settlement evidence exists",
        ):
            fb_orders._validate_automatic_qr_prepare_payment(
                _normalized(payments=[payment])
            )


def test_pending_provider_without_manual_evidence_cannot_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _prepared_order()
    incoming = _normalized_payment(
        reference_no="MB-REF-1",
        external_transaction_id="MB-REF-1",
    )
    monkeypatch.setattr(fb_orders.frappe.db, "sql", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        fb_orders.frappe.db,
        "get_value",
        lambda *args, **kwargs: {
            "name": "MBQR-1",
            "status": "pending",
            "maybank_status": 0,
            "fb_order": "FB-ORDER-1",
            "fb_order_payment": "FB-ORDER-PAYMENT-1",
        },
    )

    with pytest.raises(
        fb_orders.frappe.ValidationError,
        match="not yet confirmed",
    ):
        fb_orders._finalize_prepared_automatic_qr_order(
            _normalized(payments=[incoming]),
            order,
        )

    assert order.submitted is False


def test_manual_receipt_confirmation_submits_sale_to_pending_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _prepared_order()
    incoming = _normalized_payment(
        reference_no="MB-REF-1",
        external_transaction_id="MB-REF-1",
        manual_confirmation_evidence_json='{"evidence_kind":"receipt_photo"}',
        reconciliation_idempotency_key="RECONCILE-1",
        is_manual_confirmation=1,
        settlement_status="pending_reconciliation",
    )
    transaction = {
        "name": "MBQR-1",
        "status": "pending",
        "maybank_status": 0,
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FB-ORDER-PAYMENT-1",
    }
    set_value_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(fb_orders.frappe.db, "sql", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        fb_orders.frappe.db,
        "get_value",
        lambda *args, **kwargs: transaction,
    )
    monkeypatch.setattr(
        fb_orders.frappe.db,
        "set_value",
        lambda *args, **kwargs: set_value_calls.append(args),
    )
    monkeypatch.setattr(
        fb_orders,
        "_resolve_order_payment",
        lambda payment, index: {
            "reference_no": payment["reference_no"],
            "external_transaction_id": payment["external_transaction_id"],
            "manual_confirmation_evidence_json": payment[
                "manual_confirmation_evidence_json"
            ],
            "reconciliation_idempotency_key": payment[
                "reconciliation_idempotency_key"
            ],
        },
    )
    monkeypatch.setattr(
        fb_orders,
        "_build_submit_response",
        lambda status, document: {"status": status, "fb_order": document.name},
    )

    result = fb_orders._finalize_prepared_automatic_qr_order(
        _normalized(payments=[incoming]),
        order,
    )

    assert result == {"status": "ok", "fb_order": "FB-ORDER-1"}
    assert order.submitted is True
    assert order.payments[0].is_manual_confirmation == 1
    assert order.payments[0].settlement_status == "pending_reconciliation"
    assert order.automatic_qr_state == "finalized"
    assert set_value_calls


def test_submitted_retry_must_name_the_same_exact_provider_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _prepared_order(docstatus=1)
    order.payments[0].external_transaction_id = "MB-REF-1"
    order.payments[0].reference_no = "MB-REF-1"
    order.payments[0].maybank_qr_transaction = "MBQR-1"
    incoming = _normalized_payment(
        reference_no="MB-REF-2",
        external_transaction_id="MB-REF-2",
    )
    monkeypatch.setattr(fb_orders.frappe.db, "sql", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        fb_orders.frappe.db,
        "get_value",
        lambda *args, **kwargs: _exact_provider_transaction(
            name="MBQR-2",
            transaction_refno="MB-REF-2",
        ),
    )

    with pytest.raises(
        fb_orders.frappe.ValidationError,
        match="different provider transaction",
    ):
        fb_orders._finalize_prepared_automatic_qr_order(
            _normalized(payments=[incoming]),
            order,
        )


def test_submitted_manual_retry_requires_exact_settlement_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_json = '{"captured_at":"2026-07-17T12:00:00","evidence_kind":"receipt_photo"}'
    order = _prepared_order(docstatus=1)
    payment = order.payments[0]
    payment.reference_no = "MB-REF-1"
    payment.external_transaction_id = "MB-REF-1"
    payment.maybank_qr_transaction = "MBQR-1"
    payment.manual_confirmation_evidence_json = evidence_json
    payment.reconciliation_idempotency_key = "RECONCILE-1"
    payment.is_manual_confirmation = 1
    payment.settlement_status = "pending_reconciliation"
    incoming = _normalized_payment(
        reference_no="MB-REF-1",
        external_transaction_id="MB-REF-1",
        manual_confirmation_evidence_json=evidence_json,
        reconciliation_idempotency_key="RECONCILE-1",
        is_manual_confirmation=1,
        settlement_status="pending_reconciliation",
    )
    _patch_finalization_dependencies(
        monkeypatch,
        _exact_provider_transaction(status="pending", maybank_status=0, paid_at=None),
    )

    result = fb_orders._finalize_prepared_automatic_qr_order(
        _normalized(payments=[incoming]),
        order,
    )

    assert result == {"status": "duplicate", "fb_order": "FB-ORDER-1"}
    assert order.submitted is False


@pytest.mark.parametrize(
    ("fieldname", "changed_value"),
    [
        (
            "manual_confirmation_evidence_json",
            '{"captured_at":"2026-07-17T12:00:01","evidence_kind":"receipt_photo"}',
        ),
        ("reconciliation_idempotency_key", "RECONCILE-2"),
    ],
)
def test_submitted_manual_retry_rejects_changed_settlement_identity(
    monkeypatch: pytest.MonkeyPatch,
    fieldname: str,
    changed_value: str,
) -> None:
    evidence_json = '{"captured_at":"2026-07-17T12:00:00","evidence_kind":"receipt_photo"}'
    order = _prepared_order(docstatus=1)
    payment = order.payments[0]
    payment.reference_no = "MB-REF-1"
    payment.external_transaction_id = "MB-REF-1"
    payment.maybank_qr_transaction = "MBQR-1"
    payment.manual_confirmation_evidence_json = evidence_json
    payment.reconciliation_idempotency_key = "RECONCILE-1"
    payment.is_manual_confirmation = 1
    payment.settlement_status = "pending_reconciliation"
    incoming = _normalized_payment(
        reference_no="MB-REF-1",
        external_transaction_id="MB-REF-1",
        manual_confirmation_evidence_json=evidence_json,
        reconciliation_idempotency_key="RECONCILE-1",
        is_manual_confirmation=1,
        settlement_status="pending_reconciliation",
    )
    incoming[fieldname] = changed_value
    _patch_finalization_dependencies(
        monkeypatch,
        _exact_provider_transaction(status="pending", maybank_status=0, paid_at=None),
    )

    with pytest.raises(
        fb_orders.frappe.ValidationError,
        match="different settlement evidence",
    ):
        fb_orders._finalize_prepared_automatic_qr_order(
            _normalized(payments=[incoming]),
            order,
        )


def test_exact_authenticated_provider_paid_truth_wins_over_manual_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _prepared_order()
    evidence_json = '{"evidence_kind":"receipt_photo"}'
    incoming = _normalized_payment(
        reference_no="MB-REF-1",
        external_transaction_id="MB-REF-1",
        manual_confirmation_evidence_json=evidence_json,
        reconciliation_idempotency_key="RECONCILE-1",
        is_manual_confirmation=1,
        settlement_status="pending_reconciliation",
    )
    _patch_finalization_dependencies(monkeypatch, _exact_provider_transaction())

    result = fb_orders._finalize_prepared_automatic_qr_order(
        _normalized(payments=[incoming]),
        order,
    )

    assert result == {"status": "ok", "fb_order": "FB-ORDER-1"}
    assert order.payments[0].is_manual_confirmation == 0
    assert order.payments[0].settlement_status == "verified"
    assert order.payments[0].manual_confirmation_evidence_json == evidence_json
    assert order.payments[0].reconciliation_idempotency_key == "RECONCILE-1"


@pytest.mark.parametrize(
    "provider_overrides",
    [
        {"status": "pending", "maybank_status": 0, "paid_at": None},
        {"status": "paid", "maybank_status": 0},
        {"status": "pending", "maybank_status": 1},
        {"paid_at": None},
    ],
)
def test_manual_confirmation_stays_pending_without_exact_provider_paid_truth(
    monkeypatch: pytest.MonkeyPatch,
    provider_overrides: dict[str, Any],
) -> None:
    order = _prepared_order()
    incoming = _normalized_payment(
        reference_no="MB-REF-1",
        external_transaction_id="MB-REF-1",
        manual_confirmation_evidence_json='{"evidence_kind":"receipt_photo"}',
        reconciliation_idempotency_key="RECONCILE-1",
        is_manual_confirmation=1,
        settlement_status="pending_reconciliation",
    )
    _patch_finalization_dependencies(
        monkeypatch,
        _exact_provider_transaction(**provider_overrides),
    )

    fb_orders._finalize_prepared_automatic_qr_order(
        _normalized(payments=[incoming]),
        order,
    )

    assert order.payments[0].is_manual_confirmation == 1
    assert order.payments[0].settlement_status == "pending_reconciliation"
