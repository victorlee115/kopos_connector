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
        items=[FakeDoc(resolved_sale="FB-RESOLVED-1")],
    )

    class FakeDB:
        def __init__(self) -> None:
            self.writes: list[tuple[Any, ...]] = []
            self.sql_calls: list[str] = []
            self.commits = 0
            self.resolved_status = "Prepared"
            self.include_resolved_sale = True

        def sql(self, query: str, params: Any, **kwargs: Any) -> list[Any]:
            self.sql_calls.append(query)
            if "tabFB Order" in query:
                assert "FOR UPDATE" in query
                return [("FB-ORDER-1",)]
            if "tabFB Resolved Sale" in query:
                assert "FOR UPDATE" in query
                if not self.include_resolved_sale:
                    return []
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
        "_load_generation_attempt_candidates_for_release_for_update",
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
    assert [write[0] for write in env.db.writes] == ["FB Order"]
    assert not any("tabFB Resolved Sale" in query for query in env.db.sql_calls)
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
    assert not any("tabFB Resolved Sale" in query for query in env.db.sql_calls)


def test_non_inventory_prepared_sale_cancels_without_resolved_sale(
    cancellation_env: tuple[Any, Any, Any],
) -> None:
    service, env, _maybank_qr = cancellation_env
    env.order.items = [
        FakeDoc(
            resolved_sale=None,
            commercial_modifier_snapshot_json="[]",
        )
    ]
    env.db.include_resolved_sale = False

    result = service.cancel_prepared_automatic_qr_sale_payload(_payload())

    assert result["local_cancellation_authorized"] is True
    assert env.db.commits == 1
    assert [write[0] for write in env.db.writes] == ["FB Order"]
    assert not any("tabFB Resolved Sale" in query for query in env.db.sql_calls)


def test_cancelled_non_inventory_sale_reproves_durable_release_fence(
    cancellation_env: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, env, maybank_qr = cancellation_env
    env.order.status = "Cancelled"
    env.order.automatic_qr_state = "provider_rejected"
    env.order.items = [FakeDoc(resolved_sale=None)]
    env.db.include_resolved_sale = False
    attempt = {
        "idempotency_key": "QR-ATTEMPT-1",
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FB-PAY-1",
        "device_id": "TAB-A-001",
        "request_fingerprint": "attempt-fingerprint",
    }
    monkeypatch.setattr(
        maybank_qr,
        "_load_generation_attempt_candidates_for_release_for_update",
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

    assert service.has_durable_no_provider_release_fence("FB-ORDER-1") is True
    assert not any("tabFB Resolved Sale" in query for query in env.db.sql_calls)


def test_missing_referenced_legacy_resolution_cannot_block_release(
    cancellation_env: tuple[Any, Any, Any],
) -> None:
    service, env, _maybank_qr = cancellation_env
    env.db.include_resolved_sale = False

    result = service.cancel_prepared_automatic_qr_sale_payload(_payload())

    assert result["local_cancellation_authorized"] is True
    assert [write[0] for write in env.db.writes] == ["FB Order"]
    assert env.db.commits == 1
    assert not any("tabFB Resolved Sale" in query for query in env.db.sql_calls)


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
        "_load_generation_attempt_candidates_for_release_for_update",
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


@pytest.mark.parametrize(
    ("attempt_order", "attempt_payment"),
    [
        ("OTHER-ORDER", "FB-PAY-1"),
        ("FB-ORDER-1", "OTHER-PAYMENT"),
    ],
)
def test_corrupt_attempt_links_remain_visible_and_fail_closed(
    cancellation_env: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    attempt_order: str,
    attempt_payment: str,
) -> None:
    service, env, maybank_qr = cancellation_env
    monkeypatch.setattr(
        maybank_qr,
        "_load_generation_attempt_candidates_for_release_for_update",
        lambda fb_order, fb_order_payment: [
            {
                "idempotency_key": "QR-ATTEMPT-TAMPERED",
                "fb_order": attempt_order,
                "fb_order_payment": attempt_payment,
                "device_id": "TAB-A-001",
                "request_fingerprint": "tampered-link-fingerprint",
            }
        ],
    )

    with pytest.raises(
        service.frappe.ValidationError,
        match="provider request may have started",
    ):
        service.cancel_prepared_automatic_qr_sale_payload(_payload())

    assert env.db.writes == []
    assert env.db.commits == 0


def test_release_candidate_loader_locks_union_of_order_and_payment_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence = importlib.import_module(
        "kopos_connector.api._maybank_qr_persistence"
    )
    observed: list[tuple[str, tuple[str, str], bool]] = []

    def sql(
        query: str,
        values: tuple[str, str],
        *,
        as_dict: bool,
    ) -> list[dict[str, Any]]:
        observed.append((query, values, as_dict))
        return []

    monkeypatch.setattr(persistence.frappe.db, "sql", sql)

    assert persistence._load_generation_attempt_candidates_for_release_for_update(
        "FB-ORDER-1",
        "FB-PAY-1",
    ) == []
    assert len(observed) == 1
    query, values, as_dict = observed[0]
    assert "WHERE fb_order = %s OR fb_order_payment = %s" in query
    assert "FOR UPDATE" in query
    assert values == ("FB-ORDER-1", "FB-PAY-1")
    assert as_dict is True


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
        "_load_generation_attempt_candidates_for_release_for_update",
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
