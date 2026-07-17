from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()


class FakeDoc(SimpleNamespace):
    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)


def _payload(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "device_id": "TAB-A-001",
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FB-PAY-1",
        "order_id": "LOCAL-ORDER-1",
        "idempotency_key": "SALE-1",
        "accepted_sale_fingerprint": "a" * 64,
        "reason": "Cashier selected another payment method",
    }
    value.update(overrides)
    return value


@pytest.fixture
def cancellation_env(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    service = importlib.import_module("kopos_connector.api.automatic_qr")
    maybank_qr = service
    payment = FakeDoc(name="FB-PAY-1", amount="12.50")
    order = FakeDoc(
        name="FB-ORDER-1",
        order_id="LOCAL-ORDER-1",
        external_idempotency_key="SALE-1",
        accepted_sale_fingerprint="a" * 64,
        automatic_qr_payment="FB-PAY-1",
        automatic_qr_state="prepared",
        device_id="TAB-A-001",
        status="Draft",
        docstatus=0,
        payments=[payment],
    )

    class FakeDB:
        def __init__(self) -> None:
            self.writes: list[tuple[Any, ...]] = []
            self.commits = 0
            self.resolved_status = "Prepared"

        def sql(self, query: str, params: Any, **kwargs: Any) -> list[Any]:
            if "tabFB Order" in query:
                assert "FOR UPDATE" in query
                return [("FB-ORDER-1",)]
            if "tabFB Resolved Sale" in query:
                assert "FOR UPDATE" in query
                return [{"name": "FB-RESOLVED-1", "status": self.resolved_status}]
            raise AssertionError(f"unexpected SQL: {query}")

        def set_value(self, *args: Any, **kwargs: Any) -> None:
            self.writes.append((*args, kwargs))

        def commit(self) -> None:
            self.commits += 1

    db = FakeDB()
    audits: list[dict[str, Any]] = []

    def get_doc(*args: Any) -> Any:
        if len(args) == 1 and isinstance(args[0], dict):
            values = args[0]

            def insert(ignore_permissions: bool = False) -> None:
                assert ignore_permissions is True
                audits.append(values)

            return FakeDoc(**values, insert=insert)
        assert args == ("FB Order", "FB-ORDER-1")
        return order

    monkeypatch.setattr(service.frappe, "db", db)
    monkeypatch.setattr(service.frappe, "get_doc", get_doc)
    monkeypatch.setattr(
        service.frappe,
        "session",
        SimpleNamespace(user="device-tab-a-001@example.test"),
    )
    monkeypatch.setattr(service, "now_datetime", lambda: "2026-07-17 12:00:00")
    monkeypatch.setattr(
        service,
        "_load_linked_generation_attempts_for_update",
        lambda fb_order, fb_order_payment: [],
    )
    return service, SimpleNamespace(order=order, db=db, audits=audits), maybank_qr


def test_no_provider_attempt_cancellation_is_durable_before_local_authority(
    cancellation_env: tuple[Any, Any, Any],
) -> None:
    service, env, _maybank_qr = cancellation_env

    result = service.cancel_prepared_automatic_qr_sale_payload(_payload())

    assert result["status"] == "cancelled"
    assert result["local_cancellation_authorized"] is True
    assert result["provider_request_attempted"] is False
    assert result["cancellation_fence_registered"] is True
    assert result["amount_sen"] == 1250
    assert env.db.commits == 1
    assert env.db.writes[0][:3] == (
        "FB Order",
        "FB-ORDER-1",
        {"status": "Cancelled", "automatic_qr_state": "provider_rejected"},
    )
    assert env.db.writes[1][:4] == (
        "FB Resolved Sale",
        "FB-RESOLVED-1",
        "status",
        "Cancelled",
    )
    assert len(env.audits) == 1
    assert "prepared_automatic_qr_sale_cancelled" in env.audits[0]["content"]


def test_exact_cancelled_retry_reproves_fence_without_new_writes(
    cancellation_env: tuple[Any, Any, Any],
) -> None:
    service, env, _maybank_qr = cancellation_env
    env.order.status = "Cancelled"
    env.order.automatic_qr_state = "provider_rejected"
    env.db.resolved_status = "Cancelled"

    result = service.cancel_prepared_automatic_qr_sale_payload(_payload())

    assert result["local_cancellation_authorized"] is True
    assert env.db.writes == []
    assert env.db.commits == 0
    assert env.audits == []


def test_only_exact_durable_preflight_fences_allow_cancellation(
    cancellation_env: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, env, maybank_qr = cancellation_env
    env.order.automatic_qr_state = "provider_rejected"
    attempt = {
        "idempotency_key": "QR-ATTEMPT-1",
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FB-PAY-1",
        "device_id": "TAB-A-001",
        "request_fingerprint": "attempt-fingerprint",
    }
    monkeypatch.setattr(
        maybank_qr,
        "_load_linked_generation_attempts_for_update",
        lambda fb_order, fb_order_payment: [attempt],
    )
    monkeypatch.setattr(
        maybank_qr,
        "_request_fingerprint",
        lambda *args, **kwargs: "attempt-fingerprint",
    )
    monkeypatch.setattr(
        maybank_qr,
        "_build_persisted_preflight_rejection_response",
        lambda *args, **kwargs: {"provider_request_attempted": False},
    )

    result = service.cancel_prepared_automatic_qr_sale_payload(_payload())

    assert result["provider_attempt_fence_count"] == 1
    assert result["local_cancellation_authorized"] is True
    assert env.db.commits == 1


def test_pending_or_ambiguous_provider_attempt_fails_closed(
    cancellation_env: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, env, maybank_qr = cancellation_env
    env.order.automatic_qr_state = "provider_pending"
    attempt = {
        "idempotency_key": "QR-ATTEMPT-1",
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FB-PAY-1",
        "device_id": "TAB-A-001",
        "request_fingerprint": "attempt-fingerprint",
    }
    monkeypatch.setattr(
        maybank_qr,
        "_load_linked_generation_attempts_for_update",
        lambda fb_order, fb_order_payment: [attempt],
    )
    monkeypatch.setattr(
        maybank_qr,
        "_request_fingerprint",
        lambda *args, **kwargs: "attempt-fingerprint",
    )
    monkeypatch.setattr(
        maybank_qr,
        "_build_persisted_preflight_rejection_response",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(
        service.frappe.ValidationError,
        match="provider request may have started",
    ):
        service.cancel_prepared_automatic_qr_sale_payload(_payload())

    assert env.db.writes == []
    assert env.db.commits == 0


def test_identity_mismatch_or_submitted_sale_never_returns_authority(
    cancellation_env: tuple[Any, Any, Any],
) -> None:
    service, env, _maybank_qr = cancellation_env
    with pytest.raises(service.frappe.ValidationError, match="order_id does not match"):
        service.cancel_prepared_automatic_qr_sale_payload(
            _payload(order_id="OTHER-ORDER")
        )

    env.order.docstatus = 1
    with pytest.raises(service.frappe.ValidationError, match="Submitted"):
        service.cancel_prepared_automatic_qr_sale_payload(_payload())

    assert env.db.writes == []
    assert env.db.commits == 0


def test_public_endpoint_is_post_only_device_scoped_and_locks_before_mutation() -> None:
    api = importlib.import_module("kopos_connector.api")
    auth = importlib.import_module("kopos_connector.auth")
    path = "/api/method/kopos_connector.api.cancel_prepared_automatic_qr_sale"

    assert path in auth.ALLOWED_DEVICE_API_PATHS
    assert auth.DEVICE_API_HTTP_METHODS[path] == frozenset({"POST"})
    source = inspect.getsource(api.cancel_prepared_automatic_qr_sale)
    assert source.index("lock_device_for_operational_mutation") < source.index(
        "cancel_prepared_automatic_qr_sale_payload(payload)"
    )

    module_source = Path(inspect.getfile(api)).read_text()
    assert '"cancel_prepared_automatic_qr_sale"' in module_source
