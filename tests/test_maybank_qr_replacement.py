from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.api import _maybank_qr_contract as contract
from kopos_connector.api import _maybank_qr_generation as generation
from kopos_connector.api import _maybank_qr_replacement as replacement
from kopos_connector.api import _maybank_qr_persistence as persistence
from kopos_connector.api import _maybank_qr_status as status_service
from kopos_connector.api import maybank_qr
from kopos_connector.tasks import poll_maybank


DEVICE_ID = "DEVICE-TAB-A11"
COMPANY = "Test Company"
CURRENCY = "MYR"
AMOUNT_SEN = 1250
FB_ORDER = "FB-ORDER-REPLACEMENT-1"
FB_ORDER_PAYMENT = "FBPAY-REPLACEMENT-1"
SALE_FINGERPRINT = "a" * 64
NOW = datetime.fromisoformat("2026-07-19T14:00:00+08:00")


def _request(
    reason: str = "unrenderable_display",
    reference: str = "MBB-REF-1",
) -> replacement.MaybankQrReplacementRequest:
    return replacement.MaybankQrReplacementRequest(
        replacement_reason=reason,
        replaces_transaction_refno=reference,
    )


def _attempt(
    number: int,
    *,
    status: str = "pending",
    expires_at: str = "2026-07-19T13:59:59+08:00",
    idempotency_key: str | None = None,
    replacement_reason: str = "",
    replaces_transaction_refno: str = "",
    **overrides: Any,
) -> dict[str, Any]:
    resolved_key = idempotency_key or f"QR-ATTEMPT-{number}"
    request_fingerprint = contract._request_fingerprint(
        DEVICE_ID,
        resolved_key,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency=CURRENCY,
        replacement_reason=replacement_reason,
        replaces_transaction_refno=replaces_transaction_refno,
    )
    row: dict[str, Any] = {
        "name": f"MBQR-{number}",
        "transaction_refno": f"MBB-REF-{number}",
        "status": status,
        "maybank_status": 2,
        "qr_data": f"provider-qr-{number}",
        "expires_at": expires_at,
        "sale_amount_sen": AMOUNT_SEN,
        "currency": CURRENCY,
        "provider": "maybank_qr",
        "company": COMPANY,
        "device_id": DEVICE_ID,
        "idempotency_key": resolved_key,
        "request_fingerprint": request_fingerprint,
        "replacement_reason": replacement_reason or None,
        "replaces_transaction_refno": replaces_transaction_refno or None,
        "round_number": number,
        "fb_order": FB_ORDER,
        "fb_order_payment": FB_ORDER_PAYMENT,
        "creation": f"2026-07-19 13:5{number}:00",
        "created_at": f"2026-07-19 13:5{number}:00",
        "paid_at": None,
        "raw_response": "{}",
    }
    row.update(overrides)
    return row


def _order(*, docstatus: int = 0, order_status: str = "Draft") -> Any:
    return SimpleNamespace(
        name=FB_ORDER,
        company=COMPANY,
        docstatus=docstatus,
        status=order_status,
        automatic_qr_payment=FB_ORDER_PAYMENT,
        automatic_qr_state="provider_pending",
    )


def _validate(
    attempts: list[Any],
    request: replacement.MaybankQrReplacementRequest,
    *,
    now: Any = NOW,
) -> int:
    return replacement.validate_maybank_qr_display_replacement(
        attempts=attempts,
        request=request,
        accepted_sale_fingerprint=SALE_FINGERPRINT,
        device_id=DEVICE_ID,
        company=COMPANY,
        currency=CURRENCY,
        amount_sen=AMOUNT_SEN,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        now=now,
    )


def test_replacement_request_is_optional_but_its_fields_are_an_exact_pair() -> None:
    assert replacement.parse_maybank_qr_replacement_request({}) is None
    parsed = replacement.parse_maybank_qr_replacement_request(
        {
            "replacement_reason": "expired_display",
            "replaces_transaction_refno": "MBB-REF-1",
        }
    )
    assert parsed == _request("expired_display")

    with pytest.raises(frappe.ValidationError, match="provided together"):
        replacement.parse_maybank_qr_replacement_request(
            {"replacement_reason": "expired_display"}
        )
    with pytest.raises(frappe.ValidationError, match="must be expired_display"):
        replacement.parse_maybank_qr_replacement_request(
            {
                "replacement_reason": "broken_qr",
                "replaces_transaction_refno": "MBB-REF-1",
            }
        )


def test_replacement_provider_preflight_keeps_existing_reason_in_both_fields() -> None:
    response = persistence._build_preflight_rejection_response(
        device_id=DEVICE_ID,
        idempotency_key="QR-REPLACEMENT-CONFIG-1",
        amount_sen=AMOUNT_SEN,
        reason_code=contract.PREFLIGHT_REASON_PROVIDER_CONFIGURATION,
        message="Automatic QR provider configuration is not ready",
        checked_at="2026-07-19T14:00:00+08:00",
        replacement_reason="unrenderable_display",
        replaces_transaction_refno="MBB-REF-1",
        replacement_rejection_code=(
            contract.PREFLIGHT_REASON_PROVIDER_CONFIGURATION
        ),
    )

    assert response["preflight_reason_code"] == (
        "provider_configuration_rejected"
    )
    assert response["replacement_rejection_code"] == (
        "provider_configuration_rejected"
    )
    assert response["release_scope"] == "replacement_intent_only"


def test_unrenderable_provider_qr_can_be_replaced_immediately() -> None:
    attempt = _attempt(1, expires_at="2026-07-19T14:01:00+08:00")

    assert _validate([attempt], _request()) == 2


def test_blank_provider_display_remains_issued_pollable_and_replaceable() -> None:
    attempt = _attempt(
        1,
        qr_data="",
        expires_at="2026-07-19T14:01:00+08:00",
    )

    assert replacement._provider_issued_attempt(attempt) is True
    assert poll_maybank.is_maybank_attempt_pollable(
        SimpleNamespace(**attempt)
    ) is True
    assert _validate([attempt], _request()) == 2


def test_expired_replacement_uses_erp_clock_and_exact_expiry() -> None:
    attempt = _attempt(1, expires_at="2026-07-19T14:00:00+08:00")
    assert _validate([attempt], _request("expired_display")) == 2

    with pytest.raises(frappe.ValidationError, match="has not expired yet"):
        _validate(
            [_attempt(1, expires_at="2026-07-19T14:00:01+08:00")],
            _request("expired_display"),
        )


@pytest.mark.parametrize("blocked_status", ["creating", "scanned", "paid", "unknown"])
def test_replacement_rejects_unsafe_provider_states(blocked_status: str) -> None:
    attempt = _attempt(1, status=blocked_status)
    if blocked_status == "creating":
        attempt.update(
            {
                "transaction_refno": "REQUEST-" + "A" * 64,
                "qr_data": None,
            }
        )

    with pytest.raises(frappe.ValidationError, match="cannot be replaced"):
        _validate([attempt], _request())


@pytest.mark.parametrize(
    ("fieldname", "bad_value"),
    [
        ("device_id", "OTHER-DEVICE"),
        ("company", "Other Company"),
        ("currency", "USD"),
        ("sale_amount_sen", 1251),
        ("fb_order", "FB-ORDER-OTHER"),
        ("fb_order_payment", "FBPAY-OTHER"),
        ("provider", "other_provider"),
    ],
)
def test_replacement_target_must_match_every_prepared_sale_binding(
    fieldname: str,
    bad_value: Any,
) -> None:
    with pytest.raises(frappe.ValidationError, match="does not match"):
        _validate([_attempt(1, **{fieldname: bad_value})], _request())


def test_only_exact_latest_issued_reference_can_be_replaced() -> None:
    first = _attempt(1)
    second = _attempt(
        2,
        replacement_reason="unrenderable_display",
        replaces_transaction_refno="MBB-REF-1",
    )

    with pytest.raises(frappe.ValidationError, match="Only the latest QR"):
        _validate([first, second], _request(reference="MBB-REF-1"))
    with pytest.raises(frappe.ValidationError, match="was not found"):
        _validate([first, second], _request(reference="MBB-NOT-LINKED"))
    assert _validate([first, second], _request(reference="MBB-REF-2")) == 3


def test_replacement_attempt_cap_is_three_provider_issued_qrs() -> None:
    attempts = [
        _attempt(1),
        _attempt(
            2,
            replacement_reason="unrenderable_display",
            replaces_transaction_refno="MBB-REF-1",
        ),
        _attempt(
            3,
            replacement_reason="expired_display",
            replaces_transaction_refno="MBB-REF-2",
        ),
    ]

    with pytest.raises(frappe.ValidationError, match="replacement limit"):
        _validate(attempts, _request(reference="MBB-REF-3"))


@pytest.mark.parametrize(
    ("docstatus", "order_status", "message"),
    [
        (1, "Completed", "Submitted Automatic QR"),
        (0, "Cancelled", "Cancelled Automatic QR"),
    ],
)
def test_replacement_is_forbidden_after_sale_submission_or_cancellation(
    docstatus: int,
    order_status: str,
    message: str,
) -> None:
    new_key = "QR-REPLACEMENT-2"
    request = _request()
    fingerprint = contract._request_fingerprint(
        DEVICE_ID,
        new_key,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency=CURRENCY,
        replacement_reason=request.replacement_reason,
        replaces_transaction_refno=request.replaces_transaction_refno,
    )
    with pytest.raises(frappe.ValidationError, match=message):
        generation._validate_new_generation_attempt(
            order_doc=_order(docstatus=docstatus, order_status=order_status),
            attempts=[_attempt(1)],
            device_id=DEVICE_ID,
            idempotency_key=new_key,
            request_fingerprint=fingerprint,
            amount_sen=AMOUNT_SEN,
            currency=CURRENCY,
            accepted_sale_fingerprint=SALE_FINGERPRINT,
            replacement_request=request,
            now=NOW,
        )


def test_concurrent_exact_replay_reuses_one_creating_reservation() -> None:
    key = "QR-REPLACEMENT-2"
    request = _request()
    fingerprint = contract._request_fingerprint(
        DEVICE_ID,
        key,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency=CURRENCY,
        replacement_reason=request.replacement_reason,
        replaces_transaction_refno=request.replaces_transaction_refno,
    )
    creating = _attempt(
        2,
        status="creating",
        idempotency_key=key,
        replacement_reason=request.replacement_reason,
        replaces_transaction_refno=request.replaces_transaction_refno,
        request_fingerprint=fingerprint,
        transaction_refno=contract._reservation_reference(fingerprint),
        qr_data=None,
    )

    assert generation._validate_new_generation_attempt(
        order_doc=_order(docstatus=1, order_status="Completed"),
        attempts=[_attempt(1), creating],
        device_id=DEVICE_ID,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        amount_sen=AMOUNT_SEN,
        currency=CURRENCY,
        accepted_sale_fingerprint=SALE_FINGERPRINT,
        replacement_request=request,
        now=NOW,
    ) == 2

    changed_fingerprint = contract._request_fingerprint(
        DEVICE_ID,
        key,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency=CURRENCY,
        replacement_reason="expired_display",
        replaces_transaction_refno="MBB-REF-1",
    )
    with pytest.raises(frappe.ValidationError, match="another prepared sale request"):
        generation._validate_new_generation_attempt(
            order_doc=_order(),
            attempts=[_attempt(1), creating],
            device_id=DEVICE_ID,
            idempotency_key=key,
            request_fingerprint=changed_fingerprint,
            amount_sen=AMOUNT_SEN,
            currency=CURRENCY,
            accepted_sale_fingerprint=SALE_FINGERPRINT,
            replacement_request=_request("expired_display"),
            now=NOW,
        )


def test_rejected_replacement_never_constructs_or_calls_provider() -> None:
    client_factory = Mock(side_effect=AssertionError("provider must not be created"))
    inserted: list[dict[str, Any]] = []

    class FenceDocument:
        def insert(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True

    def get_doc(payload: dict[str, Any]) -> FenceDocument:
        inserted.append(payload)
        return FenceDocument()

    typed_rejection = replacement.MaybankQrReplacementRejection(
        "This sale has reached the Automatic QR replacement limit",
        contract.PREFLIGHT_REASON_REPLACEMENT_LIMIT,
    )

    with (
        patch.object(
            generation,
            "_load_prepared_automatic_qr_sale",
            return_value={
                **_prepared_sale(),
                "provider_round_number": 1,
                "replacement_rejection": typed_rejection,
            },
        ),
        patch.object(generation, "_load_existing_txn", return_value=None),
        patch.object(
            generation,
            "resolve_manual_qr_suspense_account",
            return_value="Manual QR Suspense - TC",
        ),
        patch.object(
            generation,
            "resolve_verified_qr_settlement_account",
            return_value={"account": "QR Clearing - TC", "type": "Bank"},
        ),
        patch.object(generation.frappe, "get_doc", side_effect=get_doc),
        patch.object(generation.frappe.db, "commit"),
        patch.object(
            generation.MaybankClient,
            "from_settings",
            client_factory,
        ),
    ):
        response = generation.generate_maybank_qr_payload(
            {
                "amount_sen": AMOUNT_SEN,
                "device_id": DEVICE_ID,
                "idempotency_key": "QR-REPLACEMENT-4",
                "fb_order": FB_ORDER,
                "fb_order_payment": FB_ORDER_PAYMENT,
                "accepted_sale_fingerprint": SALE_FINGERPRINT,
                "replacement_reason": "unrenderable_display",
                "replaces_transaction_refno": "MBB-REF-3",
            }
        )
    client_factory.assert_not_called()
    assert response["status"] == "rejected"
    assert response["error_code"] == (
        "MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT"
    )
    assert response["preflight_reason_code"] == (
        "replacement_request_rejected"
    )
    assert response["replacement_intent_rejected"] is True
    assert response["replacement_rejection_code"] == (
        "replacement_attempt_limit_reached"
    )
    assert response["prior_provider_reference_retained"] is True
    assert response["release_scope"] == "replacement_intent_only"
    assert response["replaces_transaction_refno"] == "MBB-REF-3"
    assert inserted[0]["transaction_refno"].startswith("REQUEST-")
    assert inserted[0]["replacement_reason"] == "unrenderable_display"
    assert inserted[0]["replaces_transaction_refno"] == "MBB-REF-3"

    persisted_fence = {
        **inserted[0],
        "name": "MBQR-REPLACEMENT-FENCE",
        "maybank_status": None,
        "raw_response": json.dumps(response),
    }
    assert persistence._build_persisted_preflight_rejection_response(
        persisted_fence,
        device_id=DEVICE_ID,
        idempotency_key="QR-REPLACEMENT-4",
        amount_sen=AMOUNT_SEN,
    ) == response
    persisted_fence["replaces_transaction_refno"] = "MBB-TAMPERED"
    assert persistence._build_persisted_preflight_rejection_response(
        persisted_fence,
        device_id=DEVICE_ID,
        idempotency_key="QR-REPLACEMENT-4",
        amount_sen=AMOUNT_SEN,
    ) is None


def test_generation_persists_replacement_identity_without_parsing_dynamic_qr() -> None:
    provider_client = Mock(outlet_id="OUTLET-1")
    provider_client.generate_qr.return_value = {
        "status": "QR000",
        "data": [
            {
                "transaction_refno": "MBB-REF-2",
                # Deliberately opaque. ERP must not reinterpret provider EMV.
                "qr_data": "provider-owned-opaque-dynamic-qr-payload",
                "expires_in_seconds": 60,
            }
        ],
    }
    inserted: list[dict[str, Any]] = []
    reservation = {
        **_attempt(2),
        "transaction_refno": "",
        "status": "creating",
        "qr_data": None,
    }

    class ReservationDocument:
        def insert(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True

    def get_doc(payload: dict[str, Any]) -> ReservationDocument:
        inserted.append(payload)
        reservation.update(payload)
        reservation["name"] = "MBQR-2"
        return ReservationDocument()

    with (
        patch.object(generation, "_check_rate_limit"),
        patch.object(
            generation,
            "_load_prepared_automatic_qr_sale",
            return_value={**_prepared_sale(), "provider_round_number": 2},
        ),
        patch.object(generation, "_load_existing_txn", return_value=None),
        patch.object(
            generation,
            "resolve_manual_qr_suspense_account",
            return_value="Manual QR Suspense - TC",
        ),
        patch.object(
            generation,
            "resolve_verified_qr_settlement_account",
            return_value={"account": "QR Clearing - TC", "type": "Bank"},
        ),
        patch.object(generation.frappe, "get_doc", side_effect=get_doc),
        patch.object(
            generation.MaybankClient,
            "from_settings",
            return_value=provider_client,
        ),
        patch.object(
            generation,
            "_load_reserved_txn_with_order_lock",
            return_value=reservation,
        ),
        patch.object(generation.frappe.db, "set_value"),
        patch.object(generation.frappe.db, "commit"),
        patch.object(generation, "now_datetime", return_value=NOW),
    ):
        result = maybank_qr.generate_maybank_qr_payload(
            {
                "amount_sen": AMOUNT_SEN,
                "device_id": DEVICE_ID,
                "idempotency_key": "QR-ATTEMPT-2",
                "fb_order": FB_ORDER,
                "fb_order_payment": FB_ORDER_PAYMENT,
                "accepted_sale_fingerprint": SALE_FINGERPRINT,
                "replacement_reason": "unrenderable_display",
                "replaces_transaction_refno": "MBB-REF-1",
            }
        )

    assert result["qr_data"] == "provider-owned-opaque-dynamic-qr-payload"
    assert inserted[0]["replacement_reason"] == "unrenderable_display"
    assert inserted[0]["replaces_transaction_refno"] == "MBB-REF-1"
    assert inserted[0]["round_number"] == 2
    provider_client.generate_qr.assert_called_once_with("12.50")


def _prepared_sale() -> dict[str, str]:
    return {
        "fb_order": FB_ORDER,
        "fb_order_payment": FB_ORDER_PAYMENT,
        "accepted_sale_fingerprint": SALE_FINGERPRINT,
        "payment_method": "DuitNow QR",
        "company": COMPANY,
        "currency": CURRENCY,
    }


def test_old_replaced_attempt_remains_pollable_and_can_win_late() -> None:
    old = _attempt(
        1,
        status="paid",
        paid_at="2026-07-19T14:02:00+08:00",
        expires_at="2026-07-19T13:59:00+08:00",
    )
    current = _attempt(
        2,
        replacement_reason="unrenderable_display",
        replaces_transaction_refno="MBB-REF-1",
        expires_at="2026-07-19T14:05:00+08:00",
    )
    response = status_service._aggregate_sale_payment_response(
        {
            "status": "pending",
            "transaction_refno": "MBB-REF-2",
            "sale_amount": "12.50",
            "sale_amount_sen": AMOUNT_SEN,
            "paid_at": None,
        },
        [old, current],
    )
    assert response["sale_payment_status"] == "paid"
    assert response["paid_transaction_refno"] == "MBB-REF-1"
    assert response["sale_attempt_count"] == 2

    old_timeout = SimpleNamespace(**_attempt(1, status="timeout"))
    current_pending = SimpleNamespace(**current)
    assert poll_maybank.is_maybank_attempt_pollable(old_timeout) is True
    assert poll_maybank.is_maybank_attempt_pollable(current_pending) is True


def test_linked_poll_dispatch_keeps_every_replacement_reference_live() -> None:
    old = SimpleNamespace(
        **_attempt(
            1,
            status="timeout",
            expires_at=datetime.fromisoformat("2026-07-19T13:00:00+08:00"),
            last_polled_at=datetime.fromisoformat(
                "2026-07-19T12:00:00+08:00"
            ),
            poll_count=5,
        )
    )
    current = SimpleNamespace(
        **_attempt(
            2,
            replacement_reason="unrenderable_display",
            replaces_transaction_refno="MBB-REF-1",
            expires_at=datetime.fromisoformat("2026-07-19T14:05:00+08:00"),
            last_polled_at=datetime.fromisoformat(
                "2026-07-19T13:00:00+08:00"
            ),
            poll_count=1,
        )
    )
    jobs: list[tuple[str, str]] = []
    with (
        patch.object(poll_maybank, "now_datetime", return_value=NOW),
        patch.object(
            poll_maybank,
            "_load_linked_poll_attempts",
            return_value=[old, current],
        ),
        patch.object(
            poll_maybank,
            "_enqueue_poll_job",
            side_effect=lambda name, *, lane: jobs.append((name, lane)),
        ),
    ):
        dispatched = poll_maybank.enqueue_linked_maybank_sale_poll_attempts(
            FB_ORDER,
            FB_ORDER_PAYMENT,
        )

    assert dispatched == 2
    assert jobs == [("MBQR-1", "stale"), ("MBQR-2", "active")]
