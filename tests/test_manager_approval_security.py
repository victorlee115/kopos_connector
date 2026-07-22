from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.utils import manager_approval
from kopos_connector.utils.pin import hash_pin, verify_pin


NOW_UNIX = 1_700_000_000
CONTEXT_HASH = manager_approval.canonical_context_hash({"reason": "cash count"})
ORIGINAL_PIN_LIMIT_CHECK = manager_approval._assert_manager_pin_rate_limit
ORIGINAL_PIN_LIMIT_FAILURE = manager_approval._record_manager_pin_rate_limit_failure
ORIGINAL_PIN_LIMIT_CLEAR = manager_approval._clear_manager_pin_rate_limit


@pytest.fixture(autouse=True)
def _default_available_manager_pin_limiter(monkeypatch):
    """Keep unrelated unit tests focused; dedicated tests exercise real Redis logic."""
    monkeypatch.setattr(
        manager_approval,
        "_assert_manager_pin_rate_limit",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        manager_approval,
        "_record_manager_pin_rate_limit_failure",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        manager_approval,
        "_clear_manager_pin_rate_limit",
        lambda *_args: None,
    )


@pytest.mark.parametrize(
    "invalid_value",
    [1.0, True, float("nan"), float("inf"), 9_007_199_254_740_992],
)
def test_public_manager_amount_sen_requires_safe_wire_integer(
    invalid_value: object,
) -> None:
    with pytest.raises(
        manager_approval.frappe.ValidationError,
        match="integer number of sen|safe integer range",
    ):
        manager_approval.parse_integer_sen(invalid_value)


def test_persisted_manager_amount_accepts_integral_frappe_decimal() -> None:
    assert manager_approval.parse_integer_sen("1200") == 1200
    assert manager_approval.parse_persisted_integer_sen(
        Decimal("1200.000000")
    ) == 1200


def _approval_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "device_id": "DEVICE-1",
        "staff_id": "cashier@example.com",
        "action": "close_shift",
        "manager_id": "manager@example.com",
        "shift_id": "SHIFT-1",
        "resource_id": "SHIFT-1",
        "amount_sen": 12_500,
        "context_hash": CONTEXT_HASH,
        "issued_at": NOW_UNIX,
        "expires_at": NOW_UNIX + 300,
        "token_id": "approval-token-1",
    }
    payload.update(overrides)
    return payload


def _token_and_row() -> tuple[str, dict[str, object]]:
    payload = _approval_payload()
    with patch.object(
        manager_approval, "_get_signing_secret", return_value="s" * 64
    ):
        token = manager_approval._encode_token(
            payload,
            manager_approval._create_token_signature(payload),
        )
    row = {
        "name": "approval-token-1",
        **payload,
        "status": "issued",
        "token_digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "consumed_idempotency_key": None,
    }
    return token, row


def test_token_consumption_is_database_locked_atomic_and_single_use(monkeypatch):
    token, row = _token_and_row()
    sql_calls: list[str] = []

    def sql(query: str, params: tuple[str], *, as_dict: bool = False):
        assert params == ("approval-token-1",)
        assert as_dict is True
        sql_calls.append(query)
        return [row]

    def set_value(doctype: str, name: str, updates: dict, **_kwargs: object):
        assert doctype == manager_approval.APPROVAL_DOCTYPE
        assert name == "approval-token-1"
        row.update(updates)

    monkeypatch.setattr(manager_approval.frappe.db, "sql", sql)
    monkeypatch.setattr(manager_approval.frappe.db, "set_value", set_value)

    with (
        patch.object(manager_approval, "_get_signing_secret", return_value="s" * 64),
        patch.object(manager_approval.time, "time", return_value=NOW_UNIX),
    ):
        first = manager_approval.verify_manager_approval_token(
            token,
            device_id="DEVICE-1",
            staff_id="cashier@example.com",
            action="close_shift",
            shift_id="SHIFT-1",
            resource_id="SHIFT-1",
            amount_sen=12_500,
            context_hash=CONTEXT_HASH,
            idempotency_key="close-idem-1",
        )
        assert first["manager_id"] == "manager@example.com"
        assert row["status"] == "consumed"
        assert row["consumed_idempotency_key"] == "close-idem-1"

        with pytest.raises(
            manager_approval.ManagerApprovalTokenVerificationError,
            match="already been used",
        ):
            manager_approval.verify_manager_approval_token(
                token,
                device_id="DEVICE-1",
                staff_id="cashier@example.com",
                action="close_shift",
                shift_id="SHIFT-1",
                resource_id="SHIFT-1",
                amount_sen=12_500,
                context_hash=CONTEXT_HASH,
            )

    assert len(sql_calls) == 2
    assert all("FOR UPDATE" in query for query in sql_calls)


def test_completed_mutation_proof_comes_from_consumed_approval_row(monkeypatch):
    row = {
        "token_id": "approval-token-original",
        "status": "consumed",
        "manager_id": "original-manager@example.com",
        "action": "refund_order",
        "resource_id": "SINV-1",
        "context_hash": "a" * 64,
        "consumed_idempotency_key": "refund-idem-1",
    }
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "sql",
        lambda query, params, as_dict=False: [row],
    )

    assert manager_approval.load_consumed_manager_approval_proof(
        approval_token_id="approval-token-original",
        approval_manager_id="original-manager@example.com",
        action="refund_order",
        idempotency_key="refund-idem-1",
        resource_id="SINV-1",
    ) == {
        "approval_manager_id": "original-manager@example.com",
        "approval_token_id": "approval-token-original",
        "approval_context_hash": "a" * 64,
    }

    with pytest.raises(
        manager_approval.frappe.ValidationError,
        match="does not match the mutation",
    ):
        manager_approval.load_consumed_manager_approval_proof(
            approval_token_id="approval-token-original",
            approval_manager_id="retrying-manager@example.com",
            action="refund_order",
            idempotency_key="refund-idem-1",
            resource_id="SINV-1",
        )


def test_token_requires_exact_resource_amount_and_context(monkeypatch):
    token, _row = _token_and_row()
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "sql",
        lambda *args, **kwargs: pytest.fail("mismatch must fail before consumption"),
    )
    with (
        patch.object(manager_approval, "_get_signing_secret", return_value="s" * 64),
        patch.object(manager_approval.time, "time", return_value=NOW_UNIX),
    ):
        with pytest.raises(
            manager_approval.ManagerApprovalTokenVerificationError,
            match="amount_sen mismatch",
        ):
            manager_approval.verify_manager_approval_token(
                token,
                device_id="DEVICE-1",
                staff_id="cashier@example.com",
                action="close_shift",
                shift_id="SHIFT-1",
                resource_id="SHIFT-1",
                amount_sen=12_499,
                context_hash=CONTEXT_HASH,
            )


def test_missing_and_expired_tokens_have_stable_machine_codes(monkeypatch):
    with pytest.raises(manager_approval.ManagerApprovalRequiredError) as missing:
        manager_approval.verify_manager_approval_token(
            "",
            device_id="DEVICE-1",
            staff_id="cashier@example.com",
            action="refund_order",
            shift_id="SHIFT-1",
            resource_id="SINV-1",
            amount_sen=12_500,
            context_hash=CONTEXT_HASH,
        )
    assert missing.value.error_code == "manager_approval_required"

    token, _row = _token_and_row()
    with (
        patch.object(manager_approval, "_get_signing_secret", return_value="s" * 64),
        patch.object(
            manager_approval.time,
            "time",
            return_value=NOW_UNIX + 301,
        ),
    ):
        with pytest.raises(manager_approval.ManagerApprovalExpiredError) as expired:
            manager_approval.verify_manager_approval_token(
                token,
                device_id="DEVICE-1",
                staff_id="cashier@example.com",
                action="close_shift",
                shift_id="SHIFT-1",
                resource_id="SHIFT-1",
                amount_sen=12_500,
                context_hash=CONTEXT_HASH,
            )
    assert expired.value.error_code == "manager_approval_expired"

    api = __import__("kopos_connector.api", fromlist=["_validation_error_payload"])
    assert api._validation_error_payload(missing.value) == {
        "status": "error",
        "message": "Manager approval token is required",
        "error_code": "manager_approval_required",
    }


def test_token_issuance_is_persisted_with_exact_scope(monkeypatch):
    captured: dict[str, object] = {}

    class ApprovalDoc(SimpleNamespace):
        def insert(self, ignore_permissions: bool = False) -> None:
            assert ignore_permissions is True
            captured["inserted"] = True

    def get_doc(values: dict[str, object]) -> ApprovalDoc:
        captured.update(values)
        return ApprovalDoc(**values)

    issued_at = datetime(2026, 7, 12, 12, 0, 0)
    monkeypatch.setattr(manager_approval.frappe, "get_doc", get_doc)
    monkeypatch.setattr(
        manager_approval.frappe,
        "generate_hash",
        lambda length=16: "approval-token-issued",
    )
    monkeypatch.setattr(
        manager_approval.frappe,
        "session",
        SimpleNamespace(user="device-api@example.com"),
        raising=False,
    )
    monkeypatch.setattr(manager_approval, "now_datetime", lambda: issued_at)

    with (
        patch.object(manager_approval, "_get_signing_secret", return_value="s" * 64),
        patch.object(manager_approval.time, "time", return_value=NOW_UNIX),
    ):
        result = manager_approval.generate_manager_approval_token(
            device_id="DEVICE-1",
            staff_id="cashier@example.com",
            action="refund_order",
            manager_id="manager@example.com",
            shift_id="SHIFT-1",
            resource_id="SINV-1",
            amount_sen=12_500,
            context_hash=CONTEXT_HASH,
        )

    assert captured["doctype"] == manager_approval.APPROVAL_DOCTYPE
    assert captured["status"] == "issued"
    assert captured["resource_id"] == "SINV-1"
    assert captured["amount_sen"] == 12_500
    assert captured["context_hash"] == CONTEXT_HASH
    assert captured["authorization_mode"] == "device_manager"
    assert captured["issued_by_api_user"] == "device-api@example.com"
    assert captured["inserted"] is True
    assert result["action"] == "refund_order"
    assert result["device_id"] == "DEVICE-1"
    assert result["staff_id"] == "cashier@example.com"
    assert result["shift_id"] == "SHIFT-1"
    assert result["resource_id"] == "SINV-1"
    assert result["amount_sen"] == 12_500
    assert result["context_hash"] == CONTEXT_HASH
    assert result["authorization_mode"] == "device_manager"
    decoded = manager_approval._decode_token(result["token"])
    assert decoded is not None
    assert decoded[0]["resource_id"] == "SINV-1"


def test_pin_lockout_survives_endpoint_database_rollback(monkeypatch):
    pin_hash = hash_pin("1234", cost=256)
    row: dict[str, object] = {
        "name": "DEVICE-USER-1",
        "user": "manager@example.com",
        "active": 1,
        "can_manager_override": 1,
        "pin_hash": pin_hash,
        "pin_failed_attempts": 0,
        "pin_last_failed_at": None,
        "pin_locked_until": None,
    }
    now = datetime(2026, 7, 12, 12, 0, 0)

    monkeypatch.setattr(
        manager_approval.frappe.db,
        "sql",
        lambda query, params, as_dict=False: [row],
    )
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "get_value",
        lambda doctype, name, fieldname: 1,
    )

    def set_value(doctype: str, name: str, updates: dict, **_kwargs: object):
        assert doctype == "KoPOS Device User"
        assert name == "DEVICE-USER-1"
        row.update(updates)

    monkeypatch.setattr(manager_approval.frappe.db, "set_value", set_value)
    monkeypatch.setattr(manager_approval, "now_datetime", lambda: now)
    monkeypatch.setattr(manager_approval, "pin_hash_needs_upgrade", lambda value: False)
    monkeypatch.setattr(
        manager_approval.frappe,
        "session",
        SimpleNamespace(user="device-api@example.com"),
        raising=False,
    )
    monkeypatch.setattr(
        "kopos_connector.api.devices.get_session_roles",
        lambda user=None: ["KoPOS Device API"],
    )
    rate_state = {"failures": 0, "window_started": 0, "locked_until": 0}

    class AtomicCache:
        @staticmethod
        def make_key(key: str) -> str:
            return key

        @staticmethod
        def eval(script: str, _key_count: int, _key: str, *args: int):
            now_epoch = int(args[0]) if args else 0
            if script == manager_approval.PIN_RATE_LIMIT_CHECK_SCRIPT:
                return [
                    int(rate_state["locked_until"] > now_epoch),
                    rate_state["locked_until"],
                ]
            if script == manager_approval.PIN_RATE_LIMIT_FAILURE_SCRIPT:
                window_seconds = int(args[1])
                max_failures = int(args[2])
                lockout_seconds = int(args[3])
                if (
                    not rate_state["window_started"]
                    or now_epoch - rate_state["window_started"] >= window_seconds
                ):
                    rate_state.update(
                        failures=0,
                        window_started=now_epoch,
                        locked_until=0,
                    )
                rate_state["failures"] += 1
                if rate_state["failures"] >= max_failures:
                    rate_state["locked_until"] = now_epoch + lockout_seconds
                return [rate_state["failures"], rate_state["locked_until"]]
            if script == manager_approval.PIN_RATE_LIMIT_CLEAR_SCRIPT:
                rate_state.update(failures=0, window_started=0, locked_until=0)
                return 1
            raise AssertionError("unexpected Redis script")

    epoch = [1_700_000_000]
    monkeypatch.setattr(manager_approval.frappe, "cache", lambda: AtomicCache())
    monkeypatch.setattr(manager_approval.time, "time", lambda: epoch[0])
    monkeypatch.setattr(
        manager_approval,
        "_assert_manager_pin_rate_limit",
        ORIGINAL_PIN_LIMIT_CHECK,
    )
    monkeypatch.setattr(
        manager_approval,
        "_record_manager_pin_rate_limit_failure",
        ORIGINAL_PIN_LIMIT_FAILURE,
    )
    monkeypatch.setattr(
        manager_approval,
        "_clear_manager_pin_rate_limit",
        ORIGINAL_PIN_LIMIT_CLEAR,
    )
    device = SimpleNamespace(name="KOPOS-DEVICE-1", enabled=1)

    for expected_attempts in range(1, manager_approval.MAX_PIN_FAILURES + 1):
        with pytest.raises(manager_approval.frappe.ValidationError):
            manager_approval.authorize_manager_for_device(
                device,
                manager_id="manager@example.com",
                manager_pin="9999",
            )
        assert rate_state["failures"] == expected_attempts
        # Simulate request exception handling rolling back the DB mirror.
        row["pin_failed_attempts"] = 0
        row["pin_last_failed_at"] = None
        row["pin_locked_until"] = None

    assert rate_state["locked_until"] == epoch[0] + manager_approval.PIN_LOCKOUT_SECONDS
    with pytest.raises(manager_approval.frappe.ValidationError, match="temporarily locked"):
        manager_approval.authorize_manager_for_device(
            device,
            manager_id="manager@example.com",
            manager_pin="1234",
        )

    epoch[0] += manager_approval.PIN_LOCKOUT_SECONDS + 1
    assert (
        manager_approval.authorize_manager_for_device(
            device,
            manager_id="manager@example.com",
            manager_pin="1234",
        )
        == "manager@example.com"
    )
    assert rate_state == {"failures": 0, "window_started": 0, "locked_until": 0}
    assert row["pin_failed_attempts"] == 0
    assert verify_pin("1234", pin_hash)
    assert not verify_pin("1235", pin_hash)


def test_manager_pin_limiter_fails_closed_without_atomic_redis(monkeypatch):
    monkeypatch.setattr(
        manager_approval.frappe,
        "cache",
        lambda: SimpleNamespace(),
    )

    with pytest.raises(
        manager_approval.frappe.ValidationError,
        match="temporarily unavailable",
    ):
        ORIGINAL_PIN_LIMIT_CHECK("KOPOS-DEVICE-1", "manager@example.com")

    key = manager_approval._manager_pin_rate_limit_key(
        "KOPOS-DEVICE-1",
        "manager@example.com",
    )
    assert "manager@example.com" not in key
    assert "KOPOS-DEVICE-1" not in key


def test_manager_pin_upgrade_bumps_locked_parent_config_version(monkeypatch):
    legacy_hash = hash_pin("1234", cost=16_384)
    upgraded_hash = (
        "scrypt$256$00112233445566778899aabbccddeeff$"
        "c7e2546b0c0257ce6bec0fcd48d39b871e58619a042f2360ee46596373e1c093"
    )
    row: dict[str, object] = {
        "name": "DEVICE-USER-1",
        "user": "manager@example.com",
        "active": 1,
        "can_manager_override": 1,
        "pin_hash": legacy_hash,
        "pin_failed_attempts": 0,
        "pin_last_failed_at": None,
        "pin_locked_until": None,
        "device_config_version": 7,
    }
    writes: list[tuple[str, str, dict[str, object], dict[str, object]]] = []

    monkeypatch.setattr(
        manager_approval.frappe.db,
        "sql",
        lambda query, params, as_dict=False: [row],
    )
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "get_value",
        lambda doctype, name, fieldname: 1,
    )

    def set_value(
        doctype: str,
        name: str,
        updates: dict[str, object],
        **kwargs: object,
    ) -> None:
        writes.append((doctype, name, updates, kwargs))
        if doctype == "KoPOS Device User":
            row.update(updates)

    monkeypatch.setattr(manager_approval.frappe.db, "set_value", set_value)
    monkeypatch.setattr(manager_approval, "hash_pin", lambda value: upgraded_hash)
    monkeypatch.setattr(
        manager_approval.frappe,
        "session",
        SimpleNamespace(user="device-api@example.com"),
        raising=False,
    )
    monkeypatch.setattr(
        "kopos_connector.api.devices.get_session_roles",
        lambda user=None: ["KoPOS Device API"],
    )
    device = SimpleNamespace(
        name="KOPOS-DEVICE-1",
        enabled=1,
        config_version=7,
    )

    assert manager_approval.authorize_manager_for_device(
        device,
        manager_id="manager@example.com",
        manager_pin="1234",
    ) == "manager@example.com"

    assert writes[0][0:2] == ("KoPOS Device User", "DEVICE-USER-1")
    assert writes[0][2]["pin_hash"] == upgraded_hash
    assert writes[0][3] == {"update_modified": False}
    assert writes[1] == (
        "KoPOS Device",
        "KOPOS-DEVICE-1",
        {"config_version": 8},
        {"update_modified": True},
    )
    assert device.config_version == 8


def test_system_manager_bypass_must_be_explicit(monkeypatch):
    monkeypatch.setattr(
        manager_approval.frappe,
        "session",
        SimpleNamespace(user="admin@example.com"),
        raising=False,
    )
    monkeypatch.setattr(
        "kopos_connector.api.devices.get_session_roles",
        lambda user=None: ["System Manager"],
    )
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "get_value",
        lambda doctype, name, fieldname: 1,
    )
    device = SimpleNamespace(name="KOPOS-DEVICE-1", enabled=1)

    with pytest.raises(manager_approval.frappe.ValidationError):
        manager_approval.authorize_manager_for_device(
            device,
            manager_id=None,
            manager_pin=None,
            admin_approval=False,
        )

    assert (
        manager_approval.authorize_manager_for_device(
            device,
            manager_id=None,
            manager_pin=None,
            admin_approval=True,
        )
        == "admin@example.com"
    )

    monkeypatch.setattr(
        "kopos_connector.api.devices.get_session_roles",
        lambda user=None: ["System Manager", "KoPOS Device API"],
    )
    with pytest.raises(
        manager_approval.frappe.ValidationError,
        match="non-device System Manager",
    ):
        manager_approval.authorize_manager_for_device(
            device,
            manager_id=None,
            manager_pin=None,
            admin_approval=True,
        )


def test_device_manager_requires_override_and_action_specific_permission(monkeypatch):
    pin_hash = hash_pin("1234", cost=256)
    row: dict[str, object] = {
        "name": "DEVICE-USER-1",
        "user": "manager@example.com",
        "active": 1,
        "can_manager_override": 1,
        "can_void": 0,
        "can_refund": 1,
        "pin_hash": pin_hash,
        "pin_failed_attempts": 0,
        "pin_last_failed_at": None,
        "pin_locked_until": None,
    }
    monkeypatch.setattr(
        manager_approval.frappe,
        "session",
        SimpleNamespace(user="device-api@example.com"),
        raising=False,
    )
    monkeypatch.setattr(
        "kopos_connector.api.devices.get_session_roles",
        lambda user=None: ["KoPOS Device API"],
    )
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "sql",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "get_value",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "set_value",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(manager_approval, "pin_hash_needs_upgrade", lambda value: False)
    device = SimpleNamespace(name="KOPOS-DEVICE-1", enabled=1)

    with pytest.raises(
        manager_approval.frappe.ValidationError,
        match="not authorized for voids",
    ):
        manager_approval.authorize_manager_for_device(
            device,
            manager_id="manager@example.com",
            manager_pin="1234",
            action="void_order",
        )

    assert manager_approval.authorize_manager_for_device(
        device,
        manager_id="manager@example.com",
        manager_pin="1234",
        action="refund_order",
    ) == "manager@example.com"

    row["can_manager_override"] = 0
    row["can_void"] = 1
    with pytest.raises(
        manager_approval.frappe.ValidationError,
        match="Manager credentials are invalid",
    ):
        manager_approval.authorize_manager_for_device(
            device,
            manager_id="manager@example.com",
            manager_pin="1234",
            action="void_order",
        )


def test_token_use_rechecks_current_void_permission_before_consumption(monkeypatch):
    payload = _approval_payload(
        action="void_order",
        amount_sen=1200,
        resource_id="SINV-1",
        authorization_mode="device_manager",
    )
    with patch.object(
        manager_approval, "_get_signing_secret", return_value="s" * 64
    ):
        token = manager_approval._encode_token(
            payload,
            manager_approval._create_token_signature(payload),
        )
    approval_row: dict[str, object] = {
        "name": "approval-token-1",
        **payload,
        "status": "issued",
        "token_digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "consumed_idempotency_key": None,
    }
    manager_row: dict[str, object] = {
        "name": "DEVICE-USER-1",
        "user": "manager@example.com",
        "active": 1,
        "can_manager_override": 1,
        "can_void": 0,
        "can_refund": 1,
        "pin_hash": hash_pin("1234", cost=256),
        "pin_failed_attempts": 0,
        "pin_last_failed_at": None,
        "pin_locked_until": None,
    }

    def sql(query: str, params: tuple[str, ...], *, as_dict: bool = False):
        assert as_dict is True
        if "tabKoPOS Manager Approval" in query:
            return [approval_row]
        if "tabKoPOS Device User" in query:
            return [manager_row]
        if "tabKoPOS Device`" in query:
            return [{"name": "KOPOS-DEVICE-1", "enabled": 1}]
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(manager_approval.frappe.db, "sql", sql)
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "get_value",
        lambda doctype, *_args, **_kwargs: 1 if doctype == "User" else None,
    )

    def set_value(doctype: str, name: str, updates: dict, **_kwargs: object):
        if doctype == manager_approval.APPROVAL_DOCTYPE:
            approval_row.update(updates)

    monkeypatch.setattr(manager_approval.frappe.db, "set_value", set_value)
    with (
        patch.object(manager_approval, "_get_signing_secret", return_value="s" * 64),
        patch.object(manager_approval.time, "time", return_value=NOW_UNIX),
    ):
        with pytest.raises(
            manager_approval.ManagerApprovalTokenVerificationError,
            match="no longer authorized for this action",
        ):
            manager_approval.verify_manager_approval_token(
                token,
                device_id="DEVICE-1",
                staff_id="cashier@example.com",
                action="void_order",
                shift_id="SHIFT-1",
                resource_id="SINV-1",
                amount_sen=1200,
                context_hash=CONTEXT_HASH,
                idempotency_key="void-idem-1",
            )
        assert approval_row["status"] == "issued"

        manager_row["can_void"] = 1
        verified = manager_approval.verify_manager_approval_token(
            token,
            device_id="DEVICE-1",
            staff_id="cashier@example.com",
            action="void_order",
            shift_id="SHIFT-1",
            resource_id="SINV-1",
            amount_sen=1200,
            context_hash=CONTEXT_HASH,
            idempotency_key="void-idem-1",
        )

    assert verified["manager_id"] == "manager@example.com"
    assert approval_row["status"] == "consumed"


def test_token_use_rechecks_refund_permission_and_explicit_admin_mode(monkeypatch):
    manager_row = {
        "name": "DEVICE-USER-1",
        "active": 1,
        "can_manager_override": 1,
        "can_void": 1,
        "can_refund": 0,
    }

    def sql(query: str, *_args, **_kwargs):
        if "tabKoPOS Device User" in query:
            return [manager_row]
        if "tabKoPOS Device`" in query:
            return [{"name": "KOPOS-DEVICE-1", "enabled": 1}]
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(manager_approval.frappe.db, "sql", sql)
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "get_value",
        lambda doctype, *_args, **_kwargs: 1 if doctype == "User" else None,
    )
    with pytest.raises(
        manager_approval.ManagerApprovalTokenVerificationError,
        match="no longer authorized for this action",
    ):
        manager_approval._validate_manager_action_authorization_at_use(
            device_id="DEVICE-1",
            manager_id="manager@example.com",
            action="refund_order",
            authorization_mode="device_manager",
        )

    manager_row["can_refund"] = 1
    manager_approval._validate_manager_action_authorization_at_use(
        device_id="DEVICE-1",
        manager_id="manager@example.com",
        action="refund_order",
        authorization_mode="device_manager",
    )

    monkeypatch.setattr(
        "kopos_connector.api.devices.get_session_roles",
        lambda user=None: ["System Manager"],
    )
    monkeypatch.setattr(
        manager_approval.frappe.db,
        "sql",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("explicit System Manager mode must not require a device row")
        ),
    )
    manager_approval._validate_manager_action_authorization_at_use(
        device_id="DEVICE-1",
        manager_id="admin@example.com",
        action="refund_order",
        authorization_mode="system_manager",
    )


def test_signing_secret_fails_closed_when_not_configured(monkeypatch):
    monkeypatch.setattr(manager_approval.frappe, "conf", {}, raising=False)
    with pytest.raises(manager_approval.frappe.ValidationError, match="must be configured"):
        manager_approval._get_signing_secret()
