from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.api import (
    _maybank_qr_contract as maybank_qr_contract,
    _maybank_qr_generation as maybank_qr_generation,
    _maybank_qr_persistence as maybank_qr_persistence,
    _maybank_qr_rate_limit as maybank_qr_rate_limit,
    _maybank_qr_status as maybank_qr_status,
    maybank_qr,
)


DEVICE_ID = "TEST-DEVICE-A11"
IDEMPOTENCY_KEY = "QR-PREFLIGHT-001"
AMOUNT_SEN = 1250
FB_ORDER = "FB-ORDER-PREPARED-001"
FB_ORDER_PAYMENT = "FBPAY-PREPARED-001"
ACCEPTED_SALE_FINGERPRINT = "a" * 64


def _prepared_sale() -> dict[str, str]:
    return {
        "fb_order": FB_ORDER,
        "fb_order_payment": FB_ORDER_PAYMENT,
        "accepted_sale_fingerprint": ACCEPTED_SALE_FINGERPRINT,
        "payment_method": "DuitNow QR",
        "company": "Test Company",
        "currency": "MYR",
    }


@pytest.fixture(autouse=True)
def verified_qr_account_policy(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        maybank_qr_generation,
        "resolve_verified_qr_settlement_account",
        lambda *_args: {"account": "QR Clearing - TC", "type": "Bank"},
    )


def _install_fence_capture(
    monkeypatch: Any,
) -> tuple[list[str], dict[str, Any]]:
    events: list[str] = []
    captured: dict[str, Any] = {}

    class FenceDocument:
        def insert(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            events.append("insert")

    def get_doc(payload: dict[str, Any]) -> FenceDocument:
        captured.update(payload)
        return FenceDocument()

    monkeypatch.setattr(maybank_qr_generation.frappe, "get_doc", get_doc)
    monkeypatch.setattr(
        maybank_qr_generation.frappe.db,
        "commit",
        lambda: events.append("commit"),
    )
    return events, captured


def _configuration_failure(_cls: type[Any]) -> Any:
    raise frappe.ValidationError("Maybank QRPayBiz is not enabled")


def _account_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "company": "Test Company",
        "is_group": 0,
        "disabled": 0,
        "root_type": "Asset",
        "account_currency": "MYR",
    }
    row.update(overrides)
    return row


def test_configuration_rejection_is_bound_and_durably_fenced_before_release(
    monkeypatch: Any,
) -> None:
    events, captured = _install_fence_capture(monkeypatch)
    monkeypatch.setattr(
        maybank_qr_generation,
        "_resolve_existing_txn",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_load_prepared_automatic_qr_sale",
        lambda **_kwargs: _prepared_sale(),
    )
    monkeypatch.setattr(
        maybank_qr_generation.MaybankClient,
        "from_settings",
        classmethod(_configuration_failure),
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "resolve_manual_qr_suspense_account",
        lambda _order: "Manual QR Suspense - TC",
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_generate_qr_payload",
        lambda *_args, **_kwargs: pytest.fail("provider request must not run"),
    )

    response = maybank_qr.generate_maybank_qr_payload(
        {
            "amount_sen": AMOUNT_SEN,
            "device_id": DEVICE_ID,
            "idempotency_key": IDEMPOTENCY_KEY,
        }
    )

    assert events == ["insert", "commit"]
    assert response == {
        "status": "rejected",
        "error_code": "MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT",
        "message": (
            "Automatic QR is unavailable because its ERP provider configuration "
            "is not ready"
        ),
        "preflight_reason_code": "provider_configuration_rejected",
        "provider_request_attempted": False,
        "rejection_fence_registered": True,
        "local_release_authorized": True,
        "recovery_action": "release_local_provider_intent",
        "device_id": DEVICE_ID,
        "idempotency_key": IDEMPOTENCY_KEY,
        "amount_sen": AMOUNT_SEN,
        "currency": "MYR",
        "checked_at": "2026-03-13T18:05:00+08:00",
    }
    assert captured["status"] == "failed"
    assert captured.get("maybank_status") is None
    assert captured["transaction_refno"].startswith("REQUEST-")
    assert captured["device_id"] == DEVICE_ID
    assert captured["idempotency_key"] == IDEMPOTENCY_KEY
    assert captured["sale_amount_sen"] == AMOUNT_SEN
    assert json.loads(captured["raw_response"]) == response


def test_static_winner_late_generation_is_durably_fenced_and_replays_exactly(
    monkeypatch: Any,
) -> None:
    persisted: dict[str, Any] = {}
    commits: list[str] = []
    prepared = {
        **_prepared_sale(),
        "preflight_rejection": maybank_qr_contract.MaybankQrPreflightRejection(
            "Static QR already completed this prepared sale",
            maybank_qr_contract.PREFLIGHT_REASON_STATIC_WINNER,
        ),
    }

    class FenceDocument:
        def __init__(self, values: dict[str, Any]) -> None:
            self.values = values

        def insert(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            persisted.update(self.values)

    monkeypatch.setattr(
        maybank_qr_generation,
        "_load_prepared_automatic_qr_sale",
        lambda **_kwargs: prepared,
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_load_existing_txn",
        lambda *_args: dict(persisted) if persisted else None,
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe,
        "get_doc",
        lambda values: FenceDocument(values),
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe.db,
        "set_value",
        lambda *_args, **_kwargs: pytest.fail(
            "static winner state must not be regressed"
        ),
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe.db,
        "commit",
        lambda: commits.append("commit"),
    )
    monkeypatch.setattr(
        maybank_qr_generation.MaybankClient,
        "from_settings",
        classmethod(lambda _cls: pytest.fail("provider must not be contacted")),
    )

    payload = {
        "amount_sen": AMOUNT_SEN,
        "device_id": DEVICE_ID,
        "idempotency_key": IDEMPOTENCY_KEY,
        "fb_order": FB_ORDER,
        "fb_order_payment": FB_ORDER_PAYMENT,
        "accepted_sale_fingerprint": ACCEPTED_SALE_FINGERPRINT,
    }
    first = maybank_qr.generate_maybank_qr_payload(payload)
    replay = maybank_qr.generate_maybank_qr_payload(payload)

    assert first == replay
    assert first["status"] == "rejected"
    assert first["preflight_reason_code"] == "prepared_sale_static_winner"
    assert first["provider_request_attempted"] is False
    assert first["rejection_fence_registered"] is True
    assert first["local_release_authorized"] is True
    assert first["device_id"] == DEVICE_ID
    assert first["idempotency_key"] == IDEMPOTENCY_KEY
    assert first["amount_sen"] == AMOUNT_SEN
    assert first["currency"] == "MYR"
    assert persisted["fb_order"] == FB_ORDER
    assert persisted["fb_order_payment"] == FB_ORDER_PAYMENT
    assert persisted["transaction_refno"].startswith("REQUEST-")
    assert commits == ["commit"]


@pytest.mark.parametrize(
    ("configured_account", "account_row"),
    [
        (None, None),
        ("Manual QR Suspense - TC", None),
        ("Manual QR Suspense - TC", _account_row(company="Other Company")),
        ("Manual QR Suspense - TC", _account_row(is_group=1)),
        ("Manual QR Suspense - TC", _account_row(disabled=1)),
        ("Manual QR Suspense - TC", _account_row(root_type="Liability")),
        ("Manual QR Suspense - TC", _account_row(account_currency="USD")),
        ("Manual QR Suspense - TC", _account_row(account_currency="")),
        ("Manual QR Suspense - TC", _account_row(is_group=None)),
    ],
    ids=[
        "setting-missing",
        "account-missing",
        "company-mismatch",
        "group-account",
        "disabled-account",
        "non-asset-account",
        "currency-mismatch",
        "currency-missing",
        "ledger-flags-invalid",
    ],
)
def test_invalid_suspense_account_is_durably_fenced_without_provider_contact(
    monkeypatch: Any,
    configured_account: str | None,
    account_row: dict[str, Any] | None,
) -> None:
    events, captured = _install_fence_capture(monkeypatch)
    monkeypatch.setattr(
        maybank_qr_generation,
        "_load_existing_txn",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_resolve_existing_txn",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_load_prepared_automatic_qr_sale",
        lambda **_kwargs: _prepared_sale(),
    )

    def get_single_value(doctype: str, fieldname: str) -> str | None:
        assert (doctype, fieldname) == (
            "Maybank Settings",
            "manual_qr_suspense_account",
        )
        return configured_account

    def get_value(
        doctype: str,
        name: str,
        fields: list[str],
        *,
        as_dict: bool,
    ) -> dict[str, Any] | None:
        assert doctype == "Account"
        assert name == "Manual QR Suspense - TC"
        assert fields == [
            "company",
            "is_group",
            "disabled",
            "root_type",
            "account_currency",
        ]
        assert as_dict is True
        return account_row

    monkeypatch.setattr(
        maybank_qr_generation.frappe.db, "get_single_value", get_single_value
    )
    monkeypatch.setattr(maybank_qr_generation.frappe.db, "get_value", get_value)
    monkeypatch.setattr(
        maybank_qr_generation.MaybankClient,
        "from_settings",
        classmethod(lambda _cls: pytest.fail("provider client must not be created")),
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_generate_qr_payload",
        lambda *_args, **_kwargs: pytest.fail("provider request must not run"),
    )

    response = maybank_qr.generate_maybank_qr_payload(
        {
            "amount_sen": AMOUNT_SEN,
            "device_id": DEVICE_ID,
            "idempotency_key": IDEMPOTENCY_KEY,
        }
    )

    assert events == ["insert", "commit"]
    assert response["status"] == "rejected"
    assert response["preflight_reason_code"] == "provider_configuration_rejected"
    assert response["provider_request_attempted"] is False
    assert response["local_release_authorized"] is True
    assert captured["fb_order"] == FB_ORDER
    assert captured["fb_order_payment"] == FB_ORDER_PAYMENT
    assert captured["company"] == "Test Company"
    assert captured["outlet_id"] is None


def test_valid_suspense_account_is_proved_before_provider_contact(
    monkeypatch: Any,
) -> None:
    events: list[str] = []
    client = SimpleNamespace(outlet_id="OUTLET-1")

    class ReservationDocument:
        def insert(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            events.append("reservation_inserted")

    monkeypatch.setattr(
        maybank_qr_generation,
        "_load_existing_txn",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_resolve_existing_txn",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_load_prepared_automatic_qr_sale",
        lambda **_kwargs: _prepared_sale(),
    )

    def get_single_value(doctype: str, fieldname: str) -> str:
        assert (doctype, fieldname) == (
            "Maybank Settings",
            "manual_qr_suspense_account",
        )
        events.append("suspense_setting_checked")
        return "Manual QR Suspense - TC"

    def get_value(
        doctype: str,
        name: str,
        fields: list[str],
        *,
        as_dict: bool,
    ) -> dict[str, Any]:
        assert doctype == "Account"
        assert name == "Manual QR Suspense - TC"
        assert as_dict is True
        events.append("suspense_ledger_checked")
        return _account_row()

    def from_settings(_cls: type[Any]) -> Any:
        events.append("provider_settings_loaded")
        return client

    def generate_provider_qr(
        resolved_client: Any,
        amount_rm: str,
        _now: Any,
    ) -> tuple[dict[str, Any], str, str, Any]:
        assert resolved_client is client
        assert amount_rm == "12.50"
        events.append("provider_contacted")
        return (
            {"status": "QR000"},
            "MBB-VALID-REF",
            "VALID-QR-DATA",
            "2026-03-13T18:06:00+08:00",
        )

    monkeypatch.setattr(
        maybank_qr_generation.frappe.db, "get_single_value", get_single_value
    )
    monkeypatch.setattr(maybank_qr_generation.frappe.db, "get_value", get_value)
    monkeypatch.setattr(
        maybank_qr_generation.MaybankClient,
        "from_settings",
        classmethod(from_settings),
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_check_rate_limit",
        lambda *_args: events.append("rate_limit_checked"),
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe,
        "get_doc",
        lambda _payload: ReservationDocument(),
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe.db,
        "commit",
        lambda: events.append("commit"),
    )
    monkeypatch.setattr(
        maybank_qr_generation, "_generate_qr_payload", generate_provider_qr
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_finalize_reserved_generation",
        lambda *_args, **_kwargs: {"status": "ok"},
    )

    response = maybank_qr.generate_maybank_qr_payload(
        {
            "amount_sen": AMOUNT_SEN,
            "device_id": DEVICE_ID,
            "idempotency_key": IDEMPOTENCY_KEY,
        }
    )

    assert response == {"status": "ok"}
    assert events == [
        "suspense_setting_checked",
        "suspense_ledger_checked",
        "provider_settings_loaded",
        "rate_limit_checked",
        "reservation_inserted",
        "commit",
        "provider_contacted",
        "commit",
    ]


@pytest.mark.parametrize(
    ("result", "reason_code"),
    [
        ([11, 0], "rate_limit_exceeded"),
        ([0, 121], "rate_limit_exceeded"),
    ],
)
def test_rate_limit_rejections_are_typed_before_provider_access(
    monkeypatch: Any,
    result: list[int],
    reason_code: str,
) -> None:
    cache = SimpleNamespace(
        make_key=lambda value: value,
        eval=lambda *_args: result,
    )
    monkeypatch.setattr(maybank_qr_rate_limit.frappe, "cache", lambda: cache)

    with pytest.raises(maybank_qr_contract.MaybankQrPreflightRejection) as error:
        maybank_qr_rate_limit._check_rate_limit(DEVICE_ID, "OUTLET-1")

    assert error.value.reason_code == reason_code


def test_unavailable_rate_limiter_is_a_typed_preflight_rejection(
    monkeypatch: Any,
) -> None:
    def fail_eval(*_args: Any) -> Any:
        raise RuntimeError("Redis unavailable")

    cache = SimpleNamespace(make_key=lambda value: value, eval=fail_eval)
    monkeypatch.setattr(maybank_qr_rate_limit.frappe, "cache", lambda: cache)
    monkeypatch.setattr(
        maybank_qr_rate_limit, "log_sanitized_error", lambda *_args: None
    )

    with pytest.raises(maybank_qr_contract.MaybankQrPreflightRejection) as error:
        maybank_qr_rate_limit._check_rate_limit(DEVICE_ID, "OUTLET-1")

    assert error.value.reason_code == "rate_limiter_unavailable"


@pytest.mark.parametrize("maybank_status", [None, 0])
def test_persisted_rejection_replays_identical_release_authority(
    maybank_status: int | None,
) -> None:
    checked_at = "2026-03-13T18:05:00+08:00"
    response = maybank_qr_persistence._build_preflight_rejection_response(
        device_id=DEVICE_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        amount_sen=AMOUNT_SEN,
        reason_code="rate_limit_exceeded",
        message="Automatic QR request limit exceeded; try again shortly",
        checked_at=checked_at,
    )
    request_fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        IDEMPOTENCY_KEY,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )
    existing = {
        "status": "failed",
        "transaction_refno": maybank_qr_contract._reservation_reference(
            request_fingerprint
        ),
        "sale_amount_sen": AMOUNT_SEN,
        "device_id": DEVICE_ID,
        "idempotency_key": IDEMPOTENCY_KEY,
        "provider": "maybank_qr",
        "currency": "MYR",
        "request_fingerprint": request_fingerprint,
        "maybank_status": maybank_status,
        "raw_response": json.dumps(response),
    }

    replay = maybank_qr_status._resolve_existing_txn(
        DEVICE_ID,
        IDEMPOTENCY_KEY,
        AMOUNT_SEN,
        maybank_qr_contract._coerce_site_datetime("2026-03-13T18:05:10+08:00"),
        existing=existing,
    )

    assert replay == response


@pytest.mark.parametrize("maybank_status", [1, 2])
def test_persisted_rejection_rejects_real_provider_status(
    maybank_status: int,
) -> None:
    checked_at = "2026-03-13T18:05:00+08:00"
    response = maybank_qr_persistence._build_preflight_rejection_response(
        device_id=DEVICE_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        amount_sen=AMOUNT_SEN,
        reason_code="rate_limit_exceeded",
        message="Automatic QR request limit exceeded; try again shortly",
        checked_at=checked_at,
    )
    request_fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        IDEMPOTENCY_KEY,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )
    existing = {
        "status": "failed",
        "transaction_refno": maybank_qr_contract._reservation_reference(
            request_fingerprint
        ),
        "sale_amount_sen": AMOUNT_SEN,
        "device_id": DEVICE_ID,
        "idempotency_key": IDEMPOTENCY_KEY,
        "provider": "maybank_qr",
        "currency": "MYR",
        "request_fingerprint": request_fingerprint,
        "maybank_status": maybank_status,
        "raw_response": json.dumps(response),
    }

    with pytest.raises(frappe.ValidationError, match="already been used"):
        maybank_qr_status._resolve_existing_txn(
            DEVICE_ID,
            IDEMPOTENCY_KEY,
            AMOUNT_SEN,
            maybank_qr_contract._coerce_site_datetime(
                "2026-03-13T18:05:10+08:00"
            ),
            existing=existing,
        )


@pytest.mark.parametrize(
    ("fieldname", "value"),
    [
        ("rejection_fence_registered", False),
        ("checked_at", "2026-03-13T18:05:00"),
    ],
)
def test_tampered_rejection_evidence_remains_fail_closed(
    fieldname: str,
    value: object,
) -> None:
    response = maybank_qr_persistence._build_preflight_rejection_response(
        device_id=DEVICE_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        amount_sen=AMOUNT_SEN,
        reason_code="rate_limit_exceeded",
        message="Automatic QR request limit exceeded; try again shortly",
        checked_at="2026-03-13T18:05:00+08:00",
    )
    response[fieldname] = value
    request_fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        IDEMPOTENCY_KEY,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )
    existing = {
        "status": "failed",
        "transaction_refno": maybank_qr_contract._reservation_reference(
            request_fingerprint
        ),
        "sale_amount_sen": AMOUNT_SEN,
        "device_id": DEVICE_ID,
        "idempotency_key": IDEMPOTENCY_KEY,
        "provider": "maybank_qr",
        "currency": "MYR",
        "request_fingerprint": request_fingerprint,
        "maybank_status": None,
        "raw_response": json.dumps(response),
    }

    with pytest.raises(frappe.ValidationError, match="already been used"):
        maybank_qr_status._resolve_existing_txn(
            DEVICE_ID,
            IDEMPOTENCY_KEY,
            AMOUNT_SEN,
            maybank_qr_contract._coerce_site_datetime(
                "2026-03-13T18:05:10+08:00"
            ),
            existing=existing,
        )


def test_non_object_rejection_evidence_remains_fail_closed() -> None:
    assert (
        maybank_qr_persistence._parse_preflight_rejection_evidence(
            {"raw_response": json.dumps(["not", "an", "object"])}
        )
        is None
    )


def test_concurrent_generation_reservation_overrides_local_release(
    monkeypatch: Any,
) -> None:
    class DuplicateEntryError(Exception):
        pass

    class LosingFenceDocument:
        def insert(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            raise DuplicateEntryError("request fingerprint already exists")

    request_fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        IDEMPOTENCY_KEY,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )
    concurrent_reservation = {
        "status": "creating",
        "transaction_refno": maybank_qr_contract._reservation_reference(
            request_fingerprint
        ),
        "sale_amount_sen": AMOUNT_SEN,
        "device_id": DEVICE_ID,
        "idempotency_key": IDEMPOTENCY_KEY,
        "provider": "maybank_qr",
        "currency": "MYR",
        "request_fingerprint": request_fingerprint,
        "maybank_status": None,
        "created_at": "2026-03-13T18:05:00+08:00",
        "raw_response": json.dumps({"status": "creating"}),
    }
    monkeypatch.setattr(
        maybank_qr_generation.frappe,
        "DuplicateEntryError",
        DuplicateEntryError,
        raising=False,
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe,
        "get_doc",
        lambda _payload: LosingFenceDocument(),
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_load_reserved_txn_for_update",
        lambda _fingerprint: concurrent_reservation,
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe.db,
        "commit",
        lambda: pytest.fail("the losing rejection must not commit"),
    )

    response = maybank_qr_generation._register_preflight_rejection_fence(
        device_id=DEVICE_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
        outlet_id="OUTLET-1",
        rejection=maybank_qr_contract.MaybankQrPreflightRejection(
            "Automatic QR request limit exceeded; try again shortly",
            "rate_limit_exceeded",
        ),
        prepared_sale=_prepared_sale(),
    )

    assert response["status"] == "creating"
    assert response["provider_replay_blocked"] is True
    assert response["support_required"] is False
    assert "local_release_authorized" not in response


def test_provider_call_failure_never_returns_local_release_authority(
    monkeypatch: Any,
) -> None:
    events: list[str] = []

    class ReservationDocument:
        def insert(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            events.append("reservation_inserted")

    monkeypatch.setattr(
        maybank_qr_generation,
        "_resolve_existing_txn",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_load_prepared_automatic_qr_sale",
        lambda **_kwargs: _prepared_sale(),
    )
    monkeypatch.setattr(
        maybank_qr_generation.MaybankClient,
        "from_settings",
        classmethod(lambda _cls: SimpleNamespace(outlet_id="OUTLET-1")),
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "resolve_manual_qr_suspense_account",
        lambda _order: "Manual QR Suspense - TC",
    )
    monkeypatch.setattr(
        maybank_qr_generation, "_check_rate_limit", lambda *_args: None
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe,
        "get_doc",
        lambda _payload: ReservationDocument(),
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe.db,
        "commit",
        lambda: events.append("commit"),
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_generate_qr_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timeout")),
    )
    monkeypatch.setattr(
        maybank_qr_generation,
        "_mark_generation_ambiguous",
        lambda *_args: events.append("ambiguous"),
    )

    with pytest.raises(TimeoutError, match="timeout"):
        maybank_qr.generate_maybank_qr_payload(
            {
                "amount_sen": AMOUNT_SEN,
                "device_id": DEVICE_ID,
                "idempotency_key": IDEMPOTENCY_KEY,
            }
        )

    assert events == ["reservation_inserted", "commit", "ambiguous", "commit"]


def test_public_endpoint_emits_bound_rejection_as_top_level_http_409(
    monkeypatch: Any,
) -> None:
    from kopos_connector import api

    captured: dict[str, Any] = {}
    response = maybank_qr_persistence._build_preflight_rejection_response(
        device_id=DEVICE_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        amount_sen=AMOUNT_SEN,
        reason_code="provider_configuration_rejected",
        message="Automatic QR provider configuration is not ready",
        checked_at="2026-03-13T18:05:00+08:00",
    )
    monkeypatch.setattr(
        api,
        "_get_submit_payload",
        lambda _kwargs: {
            "amount_sen": AMOUNT_SEN,
            "device_id": DEVICE_ID,
            "idempotency_key": IDEMPOTENCY_KEY,
        },
    )
    monkeypatch.setattr(
        api,
        "require_device_operational_scope",
        lambda **_kwargs: (
            SimpleNamespace(name="DEVICE-DOC-1", device_id=DEVICE_ID, config_version=7),
            SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        maybank_qr,
        "generate_maybank_qr_payload",
        lambda _payload: response,
    )
    monkeypatch.setattr(
        api,
        "_revalidate_maybank_device_authority",
        lambda _authority: pytest.fail("release response needs no authority revalidation"),
    )
    monkeypatch.setattr(
        api,
        "_write_response",
        lambda payload, http_status_code=200: captured.update(
            {"payload": payload, "http_status_code": http_status_code}
        ),
    )

    api.generate_maybank_qr()

    assert captured == {"payload": response, "http_status_code": 409}


def _linked_attempt(
    *,
    name: str = "MBQR-OLD",
    idempotency_key: str = "QR-OLD-001",
    request_fingerprint: str = "b" * 64,
    status: str = "pending",
    raw_response: str = "{}",
) -> dict[str, Any]:
    return {
        "name": name,
        "transaction_refno": "MBB-OLD-REF",
        "status": status,
        "maybank_status": 2,
        "sale_amount_sen": AMOUNT_SEN,
        "currency": "MYR",
        "provider": "maybank_qr",
        "device_id": DEVICE_ID,
        "idempotency_key": idempotency_key,
        "request_fingerprint": request_fingerprint,
        "fb_order": FB_ORDER,
        "fb_order_payment": FB_ORDER_PAYMENT,
        "raw_response": raw_response,
    }


def _prepared_order(
    *,
    docstatus: int,
    state: str,
    status: str = "Draft",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=FB_ORDER,
        docstatus=docstatus,
        status=status,
        automatic_qr_payment=FB_ORDER_PAYMENT,
        automatic_qr_state=state,
    )


def test_exact_attempt_replay_is_allowed_after_order_submission() -> None:
    fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        IDEMPOTENCY_KEY,
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )
    attempt = _linked_attempt(
        idempotency_key=IDEMPOTENCY_KEY,
        request_fingerprint=fingerprint,
        status="paid",
    )

    maybank_qr_generation._validate_new_generation_attempt(
        order_doc=_prepared_order(docstatus=1, state="finalized"),
        attempts=[attempt],
        device_id=DEVICE_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        request_fingerprint=fingerprint,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )


def test_new_idempotency_is_denied_while_any_prior_attempt_is_unresolved() -> None:
    new_fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        "QR-NEW-001",
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )

    with pytest.raises(frappe.ValidationError, match="previous Automatic QR attempt"):
        maybank_qr_generation._validate_new_generation_attempt(
            order_doc=_prepared_order(docstatus=0, state="provider_pending"),
            attempts=[_linked_attempt()],
            device_id=DEVICE_ID,
            idempotency_key="QR-NEW-001",
            request_fingerprint=new_fingerprint,
            amount_sen=AMOUNT_SEN,
            currency="MYR",
        )


def test_cancelled_sale_cannot_generate_again_after_safe_prior_fence() -> None:
    new_fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        "QR-NEW-CANCELLED",
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )
    evidence = {
        "status": "generation_abandoned",
        "resolution": "provider_transaction_absent",
        "reason": "Provider portal proves no transaction exists",
        "evidence_reference": "support-case-cancelled-sale",
        "resolved_by": "support@example.com",
        "resolved_at": "2026-03-13T18:30:00+08:00",
        "provider_replay_blocked": True,
    }
    attempt = _linked_attempt(
        status="unknown",
        raw_response=json.dumps(evidence),
    )

    with pytest.raises(frappe.ValidationError, match="Cancelled Automatic QR"):
        maybank_qr_generation._validate_new_generation_attempt(
            order_doc=_prepared_order(
                docstatus=0,
                state="provider_rejected",
                status="Cancelled",
            ),
            attempts=[attempt],
            device_id=DEVICE_ID,
            idempotency_key="QR-NEW-CANCELLED",
            request_fingerprint=new_fingerprint,
            amount_sen=AMOUNT_SEN,
            currency="MYR",
        )


@pytest.mark.parametrize(
    ("status", "evidence"),
    [
        (
            "unknown",
            {
                "status": "generation_abandoned",
                "resolution": "provider_transaction_absent",
                "reason": "Provider portal proves no transaction exists",
                "evidence_reference": "support-case-absent-001",
                "resolved_by": "support@example.com",
                "resolved_at": "2026-03-13T18:30:00+08:00",
                "provider_replay_blocked": True,
            },
        ),
        (
            "timeout",
            {
                "status": "timeout",
                "resolution": "provider_transaction_cancelled",
                "reason": "Provider confirms the expired transaction is cancelled",
                "evidence_reference": "support-case-cancelled-001",
                "resolved_by": "support@example.com",
                "resolved_at": "2026-03-14T18:30:00+08:00",
                "provider_reference": "MBB-OLD-REF",
            },
        ),
    ],
)
def test_new_attempt_requires_exact_durable_release_evidence(
    status: str,
    evidence: dict[str, Any],
) -> None:
    new_fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        "QR-NEW-001",
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )
    attempt = _linked_attempt(
        status=status,
        raw_response=json.dumps(evidence),
    )

    maybank_qr_generation._validate_new_generation_attempt(
        order_doc=_prepared_order(docstatus=0, state="provider_ambiguous"),
        attempts=[attempt],
        device_id=DEVICE_ID,
        idempotency_key="QR-NEW-001",
        request_fingerprint=new_fingerprint,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )


def test_paid_exact_attempt_replays_after_sale_finalization() -> None:
    existing = {
        **_linked_attempt(
            name="MBQR-PAID",
            idempotency_key=IDEMPOTENCY_KEY,
            status="paid",
        ),
        "transaction_refno": "MBB-PAID-REF",
        "sale_amount": "12.50",
        "qr_data": "issued-provider-qr",
        "expires_at": "2026-03-13T18:06:00+08:00",
        "paid_at": "2026-03-13T18:05:30+08:00",
        "maybank_status": 1,
        "sales_invoice": "SINV-1",
    }

    replay = maybank_qr_status._resolve_existing_txn(
        DEVICE_ID,
        IDEMPOTENCY_KEY,
        AMOUNT_SEN,
        maybank_qr_contract._coerce_site_datetime("2026-03-13T18:07:00+08:00"),
        existing=existing,
        allow_paid_replay=True,
    )

    assert replay["status"] == "paid"
    assert replay["transaction_refno"] == "MBB-PAID-REF"
    assert replay["sale_amount_sen"] == AMOUNT_SEN
    assert replay["sale_amount"] == "12.50"
    assert replay["fb_order"] == FB_ORDER
    assert replay["fb_order_payment"] == FB_ORDER_PAYMENT
    assert replay["sales_invoice"] == "SINV-1"


def test_late_old_provider_result_stays_fenced_after_replacement_reservation(
    monkeypatch: Any,
) -> None:
    """creating -> audited unknown -> replacement -> old result stays hidden."""

    old_fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        "QR-OLD-LATE",
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )
    release_evidence = {
        "status": "generation_abandoned",
        "resolution": "provider_transaction_absent",
        "reason": "Provider portal showed no transaction for the timed-out request",
        "evidence_reference": "support-case-old-late-001",
        "resolved_by": "support@example.com",
        "resolved_at": "2026-03-13T18:30:00+08:00",
        "provider_replay_blocked": True,
    }
    old_attempt = {
        **_linked_attempt(
            name="MBQR-OLD-LATE",
            idempotency_key="QR-OLD-LATE",
            request_fingerprint=old_fingerprint,
            status="unknown",
            raw_response=json.dumps(release_evidence),
        ),
        "transaction_refno": maybank_qr_contract._reservation_reference(
            old_fingerprint
        ),
        "sale_amount": "12.50",
        "qr_data": "",
        "expires_at": "2026-03-13T18:06:00+08:00",
    }
    replacement_fingerprint = maybank_qr_contract._request_fingerprint(
        DEVICE_ID,
        "QR-REPLACEMENT-001",
        fb_order=FB_ORDER,
        fb_order_payment=FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )

    # The audited absence fence permits a replacement reservation.
    maybank_qr_generation._validate_new_generation_attempt(
        order_doc=_prepared_order(docstatus=0, state="provider_ambiguous"),
        attempts=[old_attempt],
        device_id=DEVICE_ID,
        idempotency_key="QR-REPLACEMENT-001",
        request_fingerprint=replacement_fingerprint,
        amount_sen=AMOUNT_SEN,
        currency="MYR",
    )
    replacement_attempt = _linked_attempt(
        name="MBQR-REPLACEMENT",
        idempotency_key="QR-REPLACEMENT-001",
        request_fingerprint=replacement_fingerprint,
        status="pending",
    )

    lock_events: list[str] = []
    persisted_updates: dict[str, Any] = {}

    def get_value(
        doctype: str,
        filters: dict[str, str],
        fields: list[str],
        *,
        as_dict: bool,
    ) -> dict[str, str]:
        assert doctype == "Maybank QR Transaction"
        assert filters == {"request_fingerprint": old_fingerprint}
        assert fields == ["name", "fb_order"]
        assert as_dict is True
        lock_events.append("snapshot")
        return {"name": old_attempt["name"], "fb_order": FB_ORDER}

    def sql(query: str, values: tuple[str]) -> list[tuple[str]]:
        assert "tabFB Order" in query
        assert values == (FB_ORDER,)
        lock_events.append("order_lock")
        return [(FB_ORDER,)]

    def load_attempt(_fingerprint: str) -> dict[str, Any]:
        lock_events.append("attempt_lock")
        return old_attempt

    def set_value(
        doctype: str,
        name: str,
        updates: dict[str, Any],
        *,
        update_modified: bool,
    ) -> None:
        assert doctype == "Maybank QR Transaction"
        assert name == old_attempt["name"]
        assert update_modified is False
        lock_events.append("incident_write")
        persisted_updates.update(updates)
        old_attempt.update(updates)

    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(maybank_qr_generation.frappe.db, "get_value", get_value)
    monkeypatch.setattr(maybank_qr_generation.frappe.db, "sql", sql)
    monkeypatch.setattr(
        maybank_qr_persistence,
        "_load_reserved_txn_for_update",
        load_attempt,
    )
    monkeypatch.setattr(maybank_qr_generation.frappe.db, "set_value", set_value)
    monkeypatch.setattr(
        maybank_qr_generation,
        "_audit_generation_resolution",
        lambda transaction_name, **kwargs: audits.append(
            {"transaction_name": transaction_name, **kwargs}
        ),
    )
    monkeypatch.setattr(
        maybank_qr_generation.frappe, "log_error", lambda *_args: None
    )

    response = maybank_qr_generation._finalize_reserved_generation(
        old_fingerprint,
        result={
            "status": "QR000",
            "data": [
                {
                    "transaction_refno": "MBB-LATE-OLD-REF",
                    "qr_data": "late-old-qr-must-not-display",
                }
            ],
        },
        transaction_refno="MBB-LATE-OLD-REF",
        qr_data="late-old-qr-must-not-display",
        expires_at=maybank_qr_contract._coerce_site_datetime(
            "2026-03-13T18:31:00+08:00"
        ),
    )

    assert lock_events == [
        "snapshot",
        "order_lock",
        "attempt_lock",
        "incident_write",
    ]
    assert response["status"] == "late_provider_result_fenced"
    assert response["display_authorized"] is False
    assert response["new_generation_authorized"] is False
    assert response["settlement_status"] == "pending_reconciliation"
    assert "qr_data" not in response
    assert "status" not in persisted_updates
    assert "maybank_status" not in persisted_updates
    assert old_attempt["status"] == "unknown"
    incident = json.loads(persisted_updates["raw_response"])
    assert incident["resolution"] == "late_provider_result_after_release"
    assert incident["display_authorized"] is False
    assert maybank_qr_persistence._durable_generation_release(old_attempt) is None
    assert audits[0]["resolution"] == "late_provider_result_after_release"

    # Exact replay of the old key is now fail-closed and can never return its
    # stored QR data.  The replacement remains the only displayable attempt.
    with pytest.raises(frappe.ValidationError, match="already been used"):
        maybank_qr_status._resolve_existing_txn(
            DEVICE_ID,
            "QR-OLD-LATE",
            AMOUNT_SEN,
            maybank_qr_contract._coerce_site_datetime(
                "2026-03-13T18:31:01+08:00"
            ),
            existing=old_attempt,
        )
    assert replacement_attempt["status"] == "pending"
