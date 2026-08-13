from __future__ import annotations

import importlib
import json
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

api = importlib.import_module("kopos_connector.api")
devices = importlib.import_module("kopos_connector.api.devices")
maybank_qr = importlib.import_module("kopos_connector.api.maybank_qr_simulation")
maybank_persistence = importlib.import_module(
    "kopos_connector.api._maybank_qr_persistence"
)
maybank_client = importlib.import_module("kopos_connector.services.maybank.client")
transaction_controller = importlib.import_module(
    "kopos_connector.kopos.doctype.maybank_qr_transaction.maybank_qr_transaction"
)

ROOT = Path(__file__).resolve().parents[1]
SIMULATION_CONFIRMATION = "SIMULATE MAYBANK PAYMENT"
ACCEPTED_SALE_FINGERPRINT = "b" * 64


def _transaction(**overrides: object) -> dict[str, object]:
    request_fingerprint = maybank_qr._request_fingerprint(
        "SMOKE-TAB-A001",
        "MOCK-QR-IDEMPOTENCY-1",
        fb_order="FB-ORDER-MOCK-1",
        fb_order_payment="FBPAY-MOCK-1",
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=1234,
        currency="MYR",
    )
    values: dict[str, object] = {
        "name": "MBQR-MOCK-TXN-0123456789ABCDEF",
        "transaction_refno": "MOCK-TXN-0123456789ABCDEF",
        "status": "pending",
        "qr_data": "000201010212-MOCK-QR",
        "sale_amount": "12.34",
        "sale_amount_sen": 1234,
        "device_id": "SMOKE-TAB-A001",
        "provider": "maybank_qr",
        "company": "Test Company",
        "currency": "MYR",
        "pos_profile": "Test POS Profile",
        "maybank_qrpaybiz_account": "Maybank QRPayBiz Account - Test",
        "suspense_account": "Manual QR Suspense - TC",
        "clearing_account": "QR Clearing - TC",
        "settlement_bank_account": "Settlement Bank - TC",
        "outlet_id": "SMOKE-MOCK-OUTLET",
        "idempotency_key": "MOCK-QR-IDEMPOTENCY-1",
        "request_fingerprint": request_fingerprint,
        "fb_order": "FB-ORDER-MOCK-1",
        "fb_order_payment": "FBPAY-MOCK-1",
        "poll_count": 0,
        "is_test_simulation": 0,
        "test_simulation_key": None,
        "test_simulation_identity_sha256": None,
        "test_simulated_by": None,
        "test_simulated_at": None,
    }
    values.update(overrides)
    return values


def _prepared_order(**overrides: object) -> SimpleNamespace:
    payment = SimpleNamespace(
        name="FBPAY-MOCK-1",
        payment_channel_code="maybank",
        amount="12.34",
        settlement_status="awaiting_provider",
        reference_no=None,
        external_transaction_id=None,
        is_manual_confirmation=0,
    )
    values: dict[str, object] = {
        "name": "FB-ORDER-MOCK-1",
        "device_id": "SMOKE-TAB-A001",
        "company": "Test Company",
        "currency": "MYR",
        "accepted_sale_fingerprint": ACCEPTED_SALE_FINGERPRINT,
        "automatic_qr_payment": "FBPAY-MOCK-1",
        "automatic_qr_state": "provider_pending",
        "docstatus": 0,
        "payments": [payment],
    }
    values.update(overrides)
    payments = values.pop("payments")
    order = SimpleNamespace(**values)
    order.get = lambda fieldname: payments if fieldname == "payments" else None
    return order


def _locked_sale(transaction: dict[str, object]) -> tuple[object, object, list[object]]:
    return transaction, _prepared_order(), [transaction]


@contextmanager
def _enabled_simulation_context():
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(maybank_qr, "_explicit_mock_mode_enabled", return_value=True)
        )
        stack.enter_context(
            patch.object(maybank_qr, "_config_value", return_value=1)
        )
        stack.enter_context(
            patch.object(
                maybank_qr,
                "_mock_payment_mode",
                return_value=maybank_client.MAYBANK_MOCK_PAYMENT_MODE_MANUAL,
            )
        )
        stack.enter_context(
            patch.object(
                maybank_qr.MaybankClient,
                "from_settings",
                return_value=SimpleNamespace(base_url="mock://"),
            )
        )
        stack.enter_context(
            patch.object(maybank_qr.frappe.db, "exists", return_value=True)
        )
        yield


def test_mock_manual_mode_stays_pending_until_explicit_desk_action() -> None:
    client = object.__new__(maybank_client.MaybankClient)
    with (
        patch.object(
            maybank_client,
            "_config_value",
            side_effect=lambda name, default=None: (
                "manual" if name == "maybank_mock_payment_mode" else default
            ),
        ),
        patch.object(
            maybank_client.frappe.db,
            "get_value",
            return_value="12.34",
        ),
    ):
        result = client._mock_check_status("MOCK-TXN-0123456789ABCDEF")

    assert result["data"][0]["status"] == 2


def test_mock_auto_paid_mode_remains_available_for_explicit_smoke_use() -> None:
    client = object.__new__(maybank_client.MaybankClient)
    with (
        patch.object(
            maybank_client,
            "_config_value",
            side_effect=lambda name, default=None: default,
        ),
        patch.object(
            maybank_client.frappe.db,
            "get_value",
            return_value="12.34",
        ),
    ):
        result = client._mock_check_status("MOCK-TXN-0123456789ABCDEF")

    assert result["data"][0]["status"] == 1


@pytest.mark.parametrize(
    ("mock_enabled", "desk_enabled", "payment_mode", "base_url"),
    [
        (False, True, "manual", "mock://"),
        (True, False, "manual", "mock://"),
        (True, True, "auto_paid", "mock://"),
        (True, True, "manual", maybank_client.DEFAULT_BASE_URL),
    ],
)
def test_simulation_context_fails_closed_unless_every_guard_is_true(
    mock_enabled: bool,
    desk_enabled: bool,
    payment_mode: str,
    base_url: str,
) -> None:
    with (
        patch.object(
            maybank_qr,
            "_explicit_mock_mode_enabled",
            return_value=mock_enabled,
        ),
        patch.object(
            maybank_qr,
            "_config_value",
            return_value=1 if desk_enabled else 0,
        ),
        patch.object(maybank_qr, "_mock_payment_mode", return_value=payment_mode),
        patch.object(
            maybank_qr.MaybankClient,
            "from_settings",
            return_value=SimpleNamespace(base_url=base_url),
        ),
    ):
        assert maybank_qr._maybank_desk_simulation_context_enabled() is False


def test_historical_simulation_warning_survives_test_config_cleanup() -> None:
    transaction = _transaction(status="paid", is_test_simulation=1)
    with (
        patch.object(
            devices,
            "get_session_roles",
            return_value={"System Manager"},
        ),
        patch.object(
            maybank_qr,
            "_maybank_desk_simulation_context_enabled",
            return_value=False,
        ),
    ):
        capability = maybank_qr.get_maybank_qr_simulation_capability(transaction)

    assert capability == {
        "enabled": False,
        "test_context": False,
        "already_simulated": True,
    }


@pytest.mark.parametrize(
    ("order_overrides", "message"),
    [
        ({"device_id": "OTHER-DEVICE"}, "belongs to another device"),
        ({"currency": "USD"}, "currency does not match"),
        ({"automatic_qr_payment": "OTHER-PAYMENT"}, "payment binding"),
        ({"automatic_qr_state": "finalized"}, "not awaiting provider payment"),
        ({"docstatus": 1}, "Only a draft prepared"),
    ],
)
def test_simulation_prevalidates_exact_prepared_sale_before_paid_transition(
    order_overrides: dict[str, object],
    message: str,
) -> None:
    transaction = _transaction()
    identity, _digest, _key = maybank_qr._build_maybank_test_simulation_identity(
        transaction
    )
    order = _prepared_order(**order_overrides)

    with pytest.raises(maybank_qr.frappe.ValidationError, match=message):
        maybank_qr._validate_maybank_simulation_prepared_sale(
            transaction,
            identity,
            order,
            [transaction],
        )


def test_simulation_rejects_transaction_fingerprint_not_bound_to_prepared_sale() -> None:
    transaction = _transaction(request_fingerprint="a" * 64)
    identity, _digest, _key = maybank_qr._build_maybank_test_simulation_identity(
        transaction
    )

    with pytest.raises(
        maybank_qr.frappe.ValidationError,
        match="fingerprint does not match",
    ):
        maybank_qr._validate_maybank_simulation_prepared_sale(
            transaction,
            identity,
            _prepared_order(),
            [transaction],
        )


def test_simulation_accepts_exact_display_replacement_fingerprint() -> None:
    replacement_reason = "expired_display"
    replaced_reference = "MOCK-TXN-FEDCBA9876543210"
    replacement_idempotency_key = "MOCK-QR-IDEMPOTENCY-2"
    request_fingerprint = maybank_qr._request_fingerprint(
        "SMOKE-TAB-A001",
        replacement_idempotency_key,
        fb_order="FB-ORDER-MOCK-1",
        fb_order_payment="FBPAY-MOCK-1",
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=1234,
        currency="MYR",
        replacement_reason=replacement_reason,
        replaces_transaction_refno=replaced_reference,
    )
    transaction = _transaction(
        idempotency_key=replacement_idempotency_key,
        request_fingerprint=request_fingerprint,
        replacement_reason=replacement_reason,
        replaces_transaction_refno=replaced_reference,
        round_number=2,
    )
    previous_attempt = _transaction(
        name="MBQR-MOCK-TXN-FEDCBA9876543210",
        transaction_refno=replaced_reference,
        round_number=1,
    )
    identity, _digest, _key = maybank_qr._build_maybank_test_simulation_identity(
        transaction
    )

    maybank_qr._validate_maybank_simulation_prepared_sale(
        transaction,
        identity,
        _prepared_order(),
        [previous_attempt, transaction],
    )


def test_locked_transaction_projection_includes_display_replacement_identity() -> None:
    locked_row = {"name": "MBQR-MOCK-TXN-0123456789ABCDEF"}
    with patch.object(
        maybank_persistence.frappe.db,
        "sql",
        return_value=[locked_row],
    ) as sql:
        assert maybank_persistence._load_txn_for_update(locked_row["name"]) == locked_row

    query = sql.call_args.args[0]
    assert "replacement_reason" in query
    assert "replaces_transaction_refno" in query
    assert "FOR UPDATE" in query


@pytest.mark.parametrize(
    ("fieldname", "tampered_value"),
    [
        ("replacement_reason", "unrenderable_display"),
        ("replaces_transaction_refno", "MOCK-TXN-0000000000000000"),
    ],
)
def test_simulation_rejects_tampered_display_replacement_identity(
    fieldname: str,
    tampered_value: str,
) -> None:
    replacement_reason = "expired_display"
    replaced_reference = "MOCK-TXN-FEDCBA9876543210"
    replacement_idempotency_key = "MOCK-QR-IDEMPOTENCY-2"
    request_fingerprint = maybank_qr._request_fingerprint(
        "SMOKE-TAB-A001",
        replacement_idempotency_key,
        fb_order="FB-ORDER-MOCK-1",
        fb_order_payment="FBPAY-MOCK-1",
        accepted_sale_fingerprint=ACCEPTED_SALE_FINGERPRINT,
        amount_sen=1234,
        currency="MYR",
        replacement_reason=replacement_reason,
        replaces_transaction_refno=replaced_reference,
    )
    transaction = _transaction(
        idempotency_key=replacement_idempotency_key,
        request_fingerprint=request_fingerprint,
        replacement_reason=replacement_reason,
        replaces_transaction_refno=replaced_reference,
        round_number=2,
    )
    transaction[fieldname] = tampered_value
    identity, _digest, _key = maybank_qr._build_maybank_test_simulation_identity(
        transaction
    )

    with pytest.raises(
        maybank_qr.frappe.ValidationError,
        match="fingerprint does not match",
    ):
        maybank_qr._validate_maybank_simulation_prepared_sale(
            transaction,
            identity,
            _prepared_order(),
            [transaction],
        )


def test_simulation_transitions_exact_server_identity_and_writes_audit() -> None:
    transaction = _transaction()
    simulated_at = datetime(2026, 7, 17, 17, 30, 0)
    transition = Mock(return_value="paid")
    comment = Mock()

    with (
        _enabled_simulation_context(),
        patch.object(
            maybank_qr,
            "_load_maybank_simulation_sale_for_update",
            return_value=_locked_sale(transaction),
        ),
        patch.object(
            maybank_qr,
            "_transition_txn_status_locked",
            transition,
        ),
        patch.object(maybank_qr, "now_datetime", return_value=simulated_at),
        patch.object(maybank_qr.frappe.db, "set_value") as set_value,
        patch.object(maybank_qr.frappe, "get_doc", return_value=comment) as get_doc,
        patch.object(
            maybank_qr.frappe,
            "session",
            SimpleNamespace(user="manager@example.test"),
        ),
    ):
        result = maybank_qr.simulate_maybank_qr_payment_payload(
            transaction_name=str(transaction["name"]),
            confirmation=SIMULATION_CONFIRMATION,
        )

    assert result == {
        "status": "paid",
        "result": "simulated",
        "transaction_name": transaction["name"],
        "transaction_refno": transaction["transaction_refno"],
        "sale_amount_sen": 1234,
        "test_only": True,
        "simulated_by": "manager@example.test",
        "simulated_at": "2026-07-17T17:30:00+08:00",
        "finalization": "queued_or_recoverable",
    }
    transition.assert_called_once()
    transition_args = transition.call_args.args
    assert transition_args[0] is transaction
    assert transition_args[1:3] == ("paid", 1)
    provider_result = transition_args[3]
    assert provider_result["data"] == [
        {
            "status": 1,
            "transaction_refno": transaction["transaction_refno"],
            "sale_amount": "12.34",
            "outlet_id": transaction["outlet_id"],
            "currency": "MYR",
        }
    ]
    updates = set_value.call_args.args[2]
    assert updates["is_test_simulation"] == 1
    assert len(updates["test_simulation_key"]) == 64
    assert len(updates["test_simulation_identity_sha256"]) == 64
    assert updates["test_simulated_by"] == "manager@example.test"
    audit_doc = get_doc.call_args.args[0]
    assert audit_doc["reference_name"] == transaction["name"]
    assert "maybank_qr_mock_payment_simulated" in audit_doc["content"]
    assert "qr_data" not in audit_doc["content"]
    comment.insert.assert_called_once_with(ignore_permissions=True)


def test_paid_simulation_replay_is_idempotent() -> None:
    transaction = _transaction(status="paid", is_test_simulation=1)
    _, identity_sha256, simulation_key = (
        maybank_qr._build_maybank_test_simulation_identity(transaction)
    )
    transaction.update(
        {
            "test_simulation_key": simulation_key,
            "test_simulation_identity_sha256": identity_sha256,
            "test_simulated_by": "manager@example.test",
            "test_simulated_at": datetime(2026, 7, 17, 17, 30, 0),
        }
    )
    with (
        _enabled_simulation_context(),
        patch.object(
            maybank_qr,
            "_load_maybank_simulation_sale_for_update",
            return_value=_locked_sale(transaction),
        ),
        patch.object(maybank_qr, "_transition_txn_status_locked") as transition,
        patch.object(maybank_qr.frappe.db, "set_value") as set_value,
        patch.object(maybank_qr.frappe, "get_doc") as get_doc,
    ):
        result = maybank_qr.simulate_maybank_qr_payment_payload(
            transaction_name=str(transaction["name"]),
            confirmation=SIMULATION_CONFIRMATION,
        )

    assert result["result"] == "already_simulated"
    transition.assert_not_called()
    set_value.assert_not_called()
    get_doc.assert_not_called()


def test_real_provider_paid_transaction_cannot_be_relabelled_as_simulated() -> None:
    transaction = _transaction(status="paid", is_test_simulation=0)
    with (
        _enabled_simulation_context(),
        patch.object(
            maybank_qr,
            "_load_maybank_simulation_sale_for_update",
            return_value=_locked_sale(transaction),
        ),
    ):
        with pytest.raises(
            maybank_qr.frappe.ValidationError,
            match="cannot be relabelled",
        ):
            maybank_qr.simulate_maybank_qr_payment_payload(
                transaction_name=str(transaction["name"]),
                confirmation=SIMULATION_CONFIRMATION,
            )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"transaction_refno": "LIVE-TXN-1"}, "only generated MOCK-TXN"),
        ({"sale_amount": "12.35"}, "amounts do not match"),
        ({"sale_amount_sen": 0}, "outside the supported range"),
        ({"provider": "other"}, "provider is invalid"),
        ({"company": "Other Company"}, "company does not match"),
        ({"currency": "USD"}, "currency must be MYR"),
        ({"device_id": ""}, "identity is incomplete"),
        ({"fb_order": ""}, "identity is incomplete"),
        ({"status": "failed"}, "pending or scanned"),
    ],
)
def test_simulation_rejects_tampered_or_terminal_transaction(
    overrides: dict[str, object],
    message: str,
) -> None:
    transaction = _transaction(**overrides)
    with (
        _enabled_simulation_context(),
        patch.object(
            maybank_qr,
            "_load_maybank_simulation_sale_for_update",
            return_value=_locked_sale(transaction),
        ),
        patch.object(maybank_qr, "_transition_txn_status_locked") as transition,
    ):
        with pytest.raises(maybank_qr.frappe.ValidationError, match=message):
            maybank_qr.simulate_maybank_qr_payment_payload(
                transaction_name=str(transaction["name"]),
                confirmation=SIMULATION_CONFIRMATION,
            )
    transition.assert_not_called()


def test_simulation_requires_exact_typed_confirmation_before_row_lookup() -> None:
    with (
        _enabled_simulation_context(),
        patch.object(
            maybank_qr,
            "_load_maybank_simulation_sale_for_update",
        ) as load,
    ):
        with pytest.raises(
            maybank_qr.frappe.ValidationError,
            match="confirmation must be exactly",
        ):
            maybank_qr.simulate_maybank_qr_payment_payload(
                transaction_name="MBQR-1",
                confirmation="yes",
            )
    load.assert_not_called()


@pytest.mark.parametrize(
    "confirmation",
    [
        f" {SIMULATION_CONFIRMATION}",
        f"{SIMULATION_CONFIRMATION} ",
        SIMULATION_CONFIRMATION.lower(),
    ],
)
def test_simulation_confirmation_rejects_whitespace_and_case_variants(
    confirmation: str,
) -> None:
    with (
        _enabled_simulation_context(),
        patch.object(
            maybank_qr,
            "_load_maybank_simulation_sale_for_update",
        ) as load,
    ):
        with pytest.raises(
            maybank_qr.frappe.ValidationError,
            match="confirmation must be exactly",
        ):
            maybank_qr.simulate_maybank_qr_payment_payload(
                transaction_name="MBQR-1",
                confirmation=confirmation,
            )
    load.assert_not_called()


def test_public_action_rejects_dual_device_system_manager_session() -> None:
    with (
        patch.object(api, "require_system_manager"),
        patch.object(
            api,
            "get_session_roles",
            return_value={"System Manager", "KoPOS Device API"},
        ),
        patch.object(api.frappe.db, "rollback") as rollback,
    ):
        with pytest.raises(
            api.frappe.ValidationError,
            match="non-device System Manager",
        ):
            api.simulate_maybank_qr_payment(
                transaction_name="MBQR-1",
                confirmation=SIMULATION_CONFIRMATION,
            )
    rollback.assert_called_once_with()


def test_public_action_requires_system_manager_before_simulation() -> None:
    denied = api.frappe.ValidationError("Only a System Manager can perform this action")
    with (
        patch.object(api, "require_system_manager", side_effect=denied),
        patch.object(
            maybank_qr,
            "simulate_maybank_qr_payment_payload",
        ) as payload,
        patch.object(api.frappe.db, "rollback") as rollback,
    ):
        with pytest.raises(api.frappe.ValidationError, match="System Manager"):
            api.simulate_maybank_qr_payment(
                transaction_name="MBQR-1",
                confirmation=SIMULATION_CONFIRMATION,
            )
    payload.assert_not_called()
    rollback.assert_called_once_with()


def test_public_action_passes_only_server_lookup_and_confirmation() -> None:
    expected = {"status": "paid", "result": "simulated", "test_only": True}
    with (
        patch.object(api, "require_system_manager"),
        patch.object(api, "get_session_roles", return_value={"System Manager"}),
        patch.object(
            maybank_qr,
            "simulate_maybank_qr_payment_payload",
            return_value=expected,
        ) as payload,
    ):
        result = api.simulate_maybank_qr_payment(
            transaction_name="MBQR-1",
            confirmation=SIMULATION_CONFIRMATION,
        )

    assert result == expected
    payload.assert_called_once_with(
        transaction_name="MBQR-1",
        confirmation=SIMULATION_CONFIRMATION,
    )


def test_public_action_rolls_back_and_sanitizes_unexpected_failure() -> None:
    with (
        patch.object(api, "require_system_manager"),
        patch.object(api, "get_session_roles", return_value={"System Manager"}),
        patch.object(
            maybank_qr,
            "simulate_maybank_qr_payment_payload",
            side_effect=RuntimeError("secret provider failure"),
        ),
        patch.object(api.frappe.db, "rollback") as rollback,
        patch.object(api, "log_sanitized_error") as log_error,
    ):
        with pytest.raises(
            api.frappe.ValidationError,
            match="failed safely",
        ) as excinfo:
            api.simulate_maybank_qr_payment(
                transaction_name="MBQR-1",
                confirmation=SIMULATION_CONFIRMATION,
            )

    assert "secret provider failure" not in str(excinfo.value)
    rollback.assert_called_once_with()
    log_error.assert_called_once()


def test_transaction_doctype_is_read_only_and_form_action_is_guarded() -> None:
    schema = json.loads(
        (
            ROOT
            / "kopos_connector/kopos/doctype/maybank_qr_transaction/maybank_qr_transaction.json"
        ).read_text(encoding="utf-8")
    )
    permissions = {
        row["role"]: row for row in schema["permissions"]
    }
    manager = permissions["System Manager"]
    assert manager["read"] == 1
    assert manager.get("create", 0) == 0
    assert manager.get("write", 0) == 0
    assert manager.get("delete", 0) == 0

    fields = {row["fieldname"]: row for row in schema["fields"]}
    for fieldname in (
        "transaction_refno",
        "status",
        "maybank_status",
        "sale_amount",
        "sale_amount_sen",
        "qr_data",
        "fb_order",
        "sales_invoice",
        "outlet_id",
        "device_id",
        "provider",
        "currency",
        "idempotency_key",
        "round_number",
        "expires_at",
        "is_test_simulation",
        "test_simulation_key",
        "test_simulation_identity_sha256",
        "test_simulated_by",
        "test_simulated_at",
    ):
        assert fields[fieldname]["read_only"] == 1

    form_source = (
        ROOT
        / "kopos_connector/kopos/doctype/maybank_qr_transaction/maybank_qr_transaction.js"
    ).read_text(encoding="utf-8")
    assert 'includes("System Manager")' in form_source
    assert "maybank_qr_simulation" in form_source
    assert "Simulate Successful Payment (Test Only)" in form_source
    assert "frappe.prompt(" in form_source
    assert "values.confirmation !== MAYBANK_TEST_SIMULATION_CONFIRMATION" in form_source
    assert "kopos_connector.api.simulate_maybank_qr_payment" in form_source
    assert 'type: "POST"' in form_source
    assert "btn: button" in form_source
    assert "await frm.reload_doc()" in form_source
    assert "The action is idempotent" in form_source
    assert "The payment was recorded, but this form could not refresh" in form_source
    assert "frm.set_value" not in form_source

    api_source = (ROOT / "kopos_connector/api/maybank_qr_simulation.py").read_text(
        encoding="utf-8"
    )
    lock_helper = api_source.split(
        "def _load_maybank_simulation_sale_for_update(", 1
    )[1].split("\ndef ", 1)[0]
    assert lock_helper.index("tabFB Order") < lock_helper.index(
        "_load_linked_generation_attempts_for_update"
    )
    assert lock_helper.index("_load_linked_generation_attempts_for_update") < (
        lock_helper.index("_load_txn_for_update")
    )


def test_transaction_controller_rejects_direct_desk_or_rest_mutation() -> None:
    transaction = transaction_controller.MaybankQRTransaction()
    transaction.flags = SimpleNamespace(ignore_permissions=False)
    with pytest.raises(transaction_controller.frappe.PermissionError):
        transaction.before_save()

    transaction.flags = {"ignore_permissions": True}
    transaction.before_insert()
    transaction.before_save()
    transaction.on_trash()


def test_simulation_route_stays_out_of_device_api_allowlist() -> None:
    assert (
        "/api/method/kopos_connector.api.simulate_maybank_qr_payment"
        not in api.ALLOWED_DEVICE_API_PATHS
        if hasattr(api, "ALLOWED_DEVICE_API_PATHS")
        else True
    )
    auth_source = (ROOT / "kopos_connector/auth.py").read_text(encoding="utf-8")
    assert "simulate_maybank_qr_payment" not in auth_source
    api_source = (ROOT / "kopos_connector/api/__init__.py").read_text(
        encoding="utf-8"
    )
    route_source = api_source.split(
        "def simulate_maybank_qr_payment(", 1
    )[0].rsplit("\n\n", 1)[-1]
    assert '@frappe.whitelist(methods=["POST"])' in route_source
    assert "allow_guest" not in route_source
