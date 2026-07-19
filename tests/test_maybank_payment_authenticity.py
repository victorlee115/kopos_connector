from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

from kopos_connector.kopos.services.accounting import maybank_payment_service as service
from kopos_connector.kopos.api import fb_orders


def _payment(**overrides: Any) -> SimpleNamespace:
    values = {
        "payment_method": "DuitNow QR",
        "payment_channel_code": "maybank",
        "amount": "12.50",
        "reference_no": "MB-REF-1",
        "external_transaction_id": "MB-REF-1",
        "is_manual_confirmation": 0,
        "maybank_qr_transaction": None,
        "name": "PAY-1",
        "settlement_status": "verified",
        "manual_confirmation_evidence_json": None,
        "reconciliation_idempotency_key": None,
        "manual_qr_reconciliation": None,
        "suspense_account": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _order(payment: SimpleNamespace | None = None, **overrides: Any) -> SimpleNamespace:
    values = {
        "name": "FB-ORDER-1",
        "device_id": "DEVICE-1",
        "currency": "MYR",
        "company": "Test Company",
        "staff_id": "staff@example.com",
        "sale_datetime": datetime(2026, 3, 13, 18, 5, 0),
        "payments": [payment or _payment()],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_verified_qr_account_policy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account_type: str,
    root_type: str = "Asset",
    mode_type: str = "Bank",
    account_currency: str = "MYR",
) -> None:
    mode_module = SimpleNamespace(
        get_mode_of_payment_info=lambda mode, company: [
            {
                "default_account": "QR Clearing - TC",
                "type": mode_type,
            }
        ]
    )
    monkeypatch.setattr(service, "import_module", lambda name: mode_module)
    monkeypatch.setattr(
        service.frappe.db,
        "get_value",
        lambda *args, **kwargs: {
            "name": "QR Clearing - TC",
            "account_type": account_type,
            "root_type": root_type,
            "is_group": 0,
            "disabled": 0,
            "company": "Test Company",
            "account_currency": account_currency,
        },
    )


@pytest.mark.parametrize(
    "value",
    ["Maybank-QR", "MAYBANK_QR", "  maybank   qr  "],
)
def test_qr_channel_normalizer_accepts_supported_separator_variants(
    value: str,
) -> None:
    assert service.normalize_qr_token(value) == "maybank qr"


@pytest.mark.parametrize("account_type", ["Bank", ""])
def test_verified_qr_account_accepts_bank_or_untyped_asset_clearing(
    monkeypatch: pytest.MonkeyPatch,
    account_type: str,
) -> None:
    _install_verified_qr_account_policy(
        monkeypatch,
        account_type=account_type,
    )

    assert service.resolve_verified_qr_settlement_account(
        "DuitNow QR",
        "Test Company",
        "MYR",
    ) == {"account": "QR Clearing - TC", "type": "Bank"}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"account_type": "Cash"}, "Cash is not allowed"),
        ({"account_type": "Receivable"}, "Receivable is not allowed"),
        ({"account_type": "Bank", "root_type": "Liability"}, "Asset account"),
        ({"account_type": "Bank", "mode_type": "Cash"}, "must use type Bank"),
        (
            {"account_type": "Bank", "account_currency": "USD"},
            "currency does not match MYR",
        ),
    ],
)
def test_verified_qr_account_rejects_unsafe_accounting_destination(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    message: str,
) -> None:
    _install_verified_qr_account_policy(monkeypatch, **overrides)

    with pytest.raises(service.frappe.ValidationError, match=message):
        service.resolve_verified_qr_settlement_account(
            "DuitNow QR",
            "Test Company",
            "MYR",
        )


def _transaction(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "MBQR-1",
        "transaction_refno": "MB-REF-1",
        "status": "paid",
        "maybank_status": 1,
        "sale_amount_sen": 1250,
        "device_id": "DEVICE-1",
        "outlet_id": "OUTLET-1",
        "company": "Test Company",
        "currency": "MYR",
        "provider": "maybank_qr",
        "expires_at": datetime(2026, 3, 13, 18, 6, 0),
        "paid_at": datetime(2026, 3, 13, 18, 5, 30),
        "fb_order": None,
        "sales_invoice": None,
        "consumption_key": None,
        "invoice_consumption_key": None,
        "consumed_at": None,
        "qr_data": "000201010212...",
        "fb_order_payment": None,
        "reconciliation_idempotency_key": None,
        "manual_reconciliation_status": None,
    }
    values.update(overrides)
    return values


def _install_transaction_db(
    monkeypatch: pytest.MonkeyPatch,
    transaction: dict[str, Any],
    *,
    current_settings_outlet: str = "OUTLET-1",
) -> tuple[list[str], list[dict[str, Any]]]:
    sql_calls: list[str] = []
    update_calls: list[dict[str, Any]] = []

    def sql(query: str, params: tuple[str], *, as_dict: bool = False):
        assert as_dict is True
        sql_calls.append(query)
        lookup_value = params[0]
        if "`transaction_refno`" in query:
            matches = transaction["transaction_refno"] == lookup_value
        else:
            matches = transaction["name"] == lookup_value
        return [transaction] if matches else []

    def set_value(doctype: str, name: str, updates: dict[str, Any]) -> None:
        assert doctype == "Maybank QR Transaction"
        assert name == transaction["name"]
        update_calls.append(dict(updates))
        transaction.update(updates)

    monkeypatch.setattr(service.frappe.db, "sql", sql)
    monkeypatch.setattr(service.frappe.db, "set_value", set_value)
    def get_single_value(doctype: str, fieldname: str):
        assert doctype == "Maybank Settings"
        if fieldname == "outlet_id":
            return current_settings_outlet
        if fieldname == "manual_qr_suspense_account":
            return "Manual QR Suspense - TC"
        raise AssertionError(f"unexpected Maybank setting {fieldname}")

    def get_value(
        doctype: str,
        name: Any,
        fields: Any,
        **kwargs: Any,
    ):
        if doctype == "Account" and name == "Manual QR Suspense - TC":
            return {
                "company": "Test Company",
                "is_group": 0,
                "disabled": 0,
                "root_type": "Asset",
                "account_currency": "MYR",
            }
        return None

    monkeypatch.setattr(service.frappe.db, "get_single_value", get_single_value)
    monkeypatch.setattr(service.frappe.db, "get_value", get_value)
    monkeypatch.setattr(
        service,
        "now_datetime",
        lambda: datetime(2026, 3, 13, 18, 5, 45),
    )
    return sql_calls, update_calls


def test_claims_and_binds_one_paid_transaction_under_row_lock(monkeypatch):
    transaction = _transaction()
    sql_calls, update_calls = _install_transaction_db(monkeypatch, transaction)
    payment = _payment()
    order = _order(payment)

    claimed = service.claim_paid_maybank_transaction(order)
    bound = service.bind_claimed_maybank_transaction(order, "SINV-1")

    assert claimed == "MBQR-1"
    assert bound == "MBQR-1"
    assert payment.maybank_qr_transaction == "MBQR-1"
    assert transaction["fb_order"] == "FB-ORDER-1"
    assert transaction["consumption_key"] == "FB-ORDER-1"
    assert transaction["sales_invoice"] == "SINV-1"
    assert transaction["invoice_consumption_key"] == "SINV-1"
    assert len(sql_calls) == 2
    assert all("FOR UPDATE" in query for query in sql_calls)
    assert len(update_calls) == 2


def test_claim_is_idempotent_for_the_same_order(monkeypatch):
    transaction = _transaction()
    _install_transaction_db(monkeypatch, transaction)
    order = _order()

    assert service.claim_paid_maybank_transaction(order) == "MBQR-1"
    assert service.claim_paid_maybank_transaction(order) == "MBQR-1"


def test_rejects_pending_transaction(monkeypatch):
    _install_transaction_db(monkeypatch, _transaction(status="pending"))

    with pytest.raises(service.frappe.ValidationError, match="is not paid"):
        service.claim_paid_maybank_transaction(_order())


def test_rejects_wrong_transaction_reference(monkeypatch):
    _install_transaction_db(monkeypatch, _transaction())
    payment = _payment(reference_no="MB-WRONG", external_transaction_id="MB-WRONG")

    with pytest.raises(service.frappe.ValidationError, match="was not found"):
        service.claim_paid_maybank_transaction(_order(payment))


def test_rejects_disagreeing_payment_references_before_lookup(monkeypatch):
    transaction = _transaction()
    sql_calls, _ = _install_transaction_db(monkeypatch, transaction)
    payment = _payment(reference_no="MB-REF-1", external_transaction_id="MB-REF-2")

    with pytest.raises(service.frappe.ValidationError, match="references do not match"):
        service.claim_paid_maybank_transaction(_order(payment))

    assert sql_calls == []


def test_rejects_wrong_exact_sen_amount(monkeypatch):
    _install_transaction_db(monkeypatch, _transaction(sale_amount_sen=1249))

    with pytest.raises(service.frappe.ValidationError, match="amount does not match"):
        service.claim_paid_maybank_transaction(_order())


def test_rejects_fractional_sen_payment_amount(monkeypatch):
    _install_transaction_db(monkeypatch, _transaction())

    with pytest.raises(service.frappe.ValidationError, match="fractional sen"):
        service.claim_paid_maybank_transaction(_order(_payment(amount="12.501")))


def test_rejects_wrong_device(monkeypatch):
    _install_transaction_db(monkeypatch, _transaction(device_id="DEVICE-2"))

    with pytest.raises(service.frappe.ValidationError, match="device does not match"):
        service.claim_paid_maybank_transaction(_order())


@pytest.mark.parametrize("company", [None, "Other Company"])
def test_automatic_claim_rejects_blank_or_cross_company_transaction(
    monkeypatch: pytest.MonkeyPatch,
    company: str | None,
) -> None:
    transaction = _transaction(company=company)
    _sql_calls, update_calls = _install_transaction_db(monkeypatch, transaction)

    with pytest.raises(service.frappe.ValidationError, match="company does not match"):
        service.claim_paid_maybank_transaction(_order())

    assert update_calls == []


@pytest.mark.parametrize(
    ("transaction_overrides", "message"),
    [
        ({"outlet_id": ""}, "outlet metadata is missing"),
        ({"currency": "USD"}, "currency does not match"),
        ({"provider": "other_provider"}, "provider is invalid"),
        ({"maybank_status": 2}, "lacks provider-paid status evidence"),
    ],
)
def test_rejects_wrong_transaction_scope_or_provider_evidence(
    monkeypatch, transaction_overrides, message
):
    _install_transaction_db(monkeypatch, _transaction(**transaction_overrides))

    with pytest.raises(service.frappe.ValidationError, match=message):
        service.claim_paid_maybank_transaction(_order())


def test_historical_transaction_survives_current_outlet_rotation(monkeypatch):
    transaction = _transaction(outlet_id="OUTLET-A")
    _install_transaction_db(
        monkeypatch,
        transaction,
        current_settings_outlet="OUTLET-B",
    )

    assert service.claim_paid_maybank_transaction(_order()) == "MBQR-1"
    assert transaction["outlet_id"] == "OUTLET-A"


def test_accepts_provider_paid_success_observed_after_network_outage(monkeypatch):
    transaction = _transaction(
        expires_at=datetime(2026, 3, 13, 18, 6, 0),
        paid_at=datetime(2026, 3, 14, 2, 6, 30),
    )
    _install_transaction_db(monkeypatch, transaction)

    assert service.claim_paid_maybank_transaction(_order()) == "MBQR-1"


def test_rejects_non_myr_order_even_when_transaction_currency_matches(monkeypatch):
    _install_transaction_db(monkeypatch, _transaction(currency="USD"))

    with pytest.raises(service.frappe.ValidationError, match="must be MYR"):
        service.claim_paid_maybank_transaction(_order(currency="USD"))


def test_rejects_transaction_reused_by_another_order(monkeypatch):
    _install_transaction_db(
        monkeypatch,
        _transaction(fb_order="FB-ORDER-2", consumption_key="FB-ORDER-2"),
    )

    with pytest.raises(service.frappe.ValidationError, match="another FB Order"):
        service.claim_paid_maybank_transaction(_order())


def test_rejects_transaction_reused_by_another_invoice(monkeypatch):
    transaction = _transaction()
    _install_transaction_db(monkeypatch, transaction)
    order = _order()

    service.claim_paid_maybank_transaction(order)
    service.bind_claimed_maybank_transaction(order, "SINV-1")

    with pytest.raises(service.frappe.ValidationError, match="another Sales Invoice"):
        service.bind_claimed_maybank_transaction(order, "SINV-2")


def test_bind_recovers_same_preexisting_invoice_link_idempotently(monkeypatch):
    transaction = _transaction(
        fb_order="FB-ORDER-1",
        consumption_key="FB-ORDER-1",
        sales_invoice="SINV-1",
        invoice_consumption_key="SINV-1",
    )
    _install_transaction_db(monkeypatch, transaction)
    order = _order()

    assert service.bind_claimed_maybank_transaction(order, "SINV-1") == "MBQR-1"
    assert order.payments[0].maybank_qr_transaction == "MBQR-1"


def test_manual_flag_cannot_bypass_automatic_maybank_verification(monkeypatch):
    _install_transaction_db(monkeypatch, _transaction(status="pending"))
    order = _order(_payment(is_manual_confirmation=1))

    with pytest.raises(service.frappe.ValidationError, match="is not paid"):
        service.claim_paid_maybank_transaction(order)


def _manual_maybank_payment(**overrides: Any) -> SimpleNamespace:
    evidence = _static_evidence()
    evidence.update(
        {
            "local_confirmation_reference": "MB-REF-1",
            "reconciliation_idempotency_key": "manual-maybank-reconciliation-1",
        }
    )
    values = {
        "is_manual_confirmation": 1,
        "settlement_status": "pending_reconciliation",
        "manual_confirmation_evidence_json": json.dumps(evidence),
        "reconciliation_idempotency_key": "manual-maybank-reconciliation-1",
    }
    values.update(overrides)
    return _payment(**values)


def test_manual_maybank_posts_as_pending_reconciliation_and_binds_once(monkeypatch):
    transaction = _transaction(status="pending", maybank_status=0, paid_at=None)
    _install_transaction_db(monkeypatch, transaction)
    payment = _manual_maybank_payment()
    order = _order(payment)

    registered = service.register_qr_payment_settlement(order)
    bound = service.bind_qr_payment_settlement(order, "SINV-MANUAL-1")

    assert registered == "MBQR-1"
    assert bound == "MBQR-1"
    assert payment.maybank_qr_transaction == "MBQR-1"
    assert payment.settlement_status == "pending_reconciliation"
    assert payment.suspense_account == "Manual QR Suspense - TC"
    assert transaction["fb_order"] == "FB-ORDER-1"
    assert transaction["fb_order_payment"] == "PAY-1"
    assert transaction["sales_invoice"] == "SINV-MANUAL-1"
    assert transaction["manual_reconciliation_status"] == "pending_reconciliation"
    assert transaction["reconciliation_idempotency_key"] == (
        "manual-maybank-reconciliation-1"
    )


@pytest.mark.parametrize("company", [None, "Other Company"])
def test_manual_claim_rejects_blank_or_cross_company_transaction(
    monkeypatch: pytest.MonkeyPatch,
    company: str | None,
) -> None:
    transaction = _transaction(
        status="pending",
        maybank_status=0,
        paid_at=None,
        company=company,
    )
    _sql_calls, update_calls = _install_transaction_db(monkeypatch, transaction)

    with pytest.raises(service.frappe.ValidationError, match="company does not match"):
        service.register_qr_payment_settlement(_order(_manual_maybank_payment()))

    assert update_calls == []


def test_legacy_submitted_claim_allows_blank_company_only_when_already_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _transaction(
        company=None,
        fb_order="FB-ORDER-1",
        consumption_key="FB-ORDER-1",
    )
    _install_transaction_db(monkeypatch, transaction)
    payment = _payment(maybank_qr_transaction="MBQR-1")
    order = _order(payment, docstatus=1)

    assert service.bind_claimed_maybank_transaction(order, "SINV-1") == "MBQR-1"


def test_manual_maybank_registration_is_idempotent_for_same_sale(monkeypatch):
    transaction = _transaction(status="timeout", maybank_status=0, paid_at=None)
    _install_transaction_db(monkeypatch, transaction)
    payment = _manual_maybank_payment()
    order = _order(payment)

    assert service.register_qr_payment_settlement(order) == "MBQR-1"
    assert service.register_qr_payment_settlement(order) == "MBQR-1"


def test_manual_maybank_rejects_naked_flag_without_structured_evidence(monkeypatch):
    _install_transaction_db(
        monkeypatch,
        _transaction(status="pending", maybank_status=0, paid_at=None),
    )
    payment = _payment(is_manual_confirmation=1)

    with pytest.raises(service.frappe.ValidationError, match="evidence is invalid"):
        service.register_qr_payment_settlement(_order(payment))


def test_manual_maybank_rejects_unissued_transaction(monkeypatch):
    _install_transaction_db(
        monkeypatch,
        _transaction(
            status="creating",
            maybank_status=0,
            paid_at=None,
            qr_data=None,
        ),
    )

    with pytest.raises(service.frappe.ValidationError, match="was not issued"):
        service.register_qr_payment_settlement(_order(_manual_maybank_payment()))


def test_manual_maybank_rejects_evidence_from_another_device(monkeypatch):
    _install_transaction_db(
        monkeypatch,
        _transaction(status="pending", maybank_status=0, paid_at=None),
    )
    evidence = _static_evidence()
    evidence.update(
        {
            "local_confirmation_reference": "MB-REF-1",
            "reconciliation_idempotency_key": "manual-maybank-reconciliation-1",
            "evidence_captured_device_id": "DEVICE-2",
        }
    )
    payment = _manual_maybank_payment(
        manual_confirmation_evidence_json=json.dumps(evidence)
    )

    with pytest.raises(service.frappe.ValidationError, match="device does not match"):
        service.register_qr_payment_settlement(_order(payment))


def test_qr_channel_cannot_bypass_verification_with_cash_payment_method():
    payment = _payment(payment_method="Cash", payment_channel_code="maybank")

    with pytest.raises(
        service.frappe.ValidationError,
        match="requires the DuitNow QR payment method",
    ):
        service.register_qr_payment_settlement(_order(payment))


def _static_evidence() -> dict[str, Any]:
    return {
        "evidence_kind": "no_receipt_acknowledgement",
        "captured_at": "2026-03-13T18:05:00",
        "upload_status": "not_required",
        "reconciliation_status": "pending_reconciliation",
        "local_confirmed_at": "2026-03-13T18:05:00",
        "local_confirmed_by": "staff@example.com",
        "local_confirmation_reference": "STATIC-REF-1",
        "reconciliation_idempotency_key": "manual-reconciliation-1",
        "evidence_captured_device_id": "DEVICE-1",
        "no_receipt_acknowledged": True,
        "no_receipt_reason_code": "receipt_unavailable",
    }


def _static_payment(**overrides: Any) -> SimpleNamespace:
    evidence = _static_evidence()
    values = {
        "payment_channel_code": "static_qr",
        "external_transaction_id": "static-session-1",
        "reference_no": "STATIC-REF-1",
        "is_manual_confirmation": 1,
        "settlement_status": "pending_reconciliation",
        "manual_confirmation_evidence_json": json.dumps(evidence),
        "reconciliation_idempotency_key": "manual-reconciliation-1",
    }
    values.update(overrides)
    return _payment(**values)


def _install_static_reconciliation_db(monkeypatch):
    reconciliations: dict[str, dict[str, Any]] = {}
    db_updates: list[tuple[Any, ...]] = []

    def get_single_value(doctype: str, fieldname: str):
        assert (doctype, fieldname) == (
            "Maybank Settings",
            "manual_qr_suspense_account",
        )
        return "Manual QR Suspense - TC"

    def get_value(doctype: str, name: Any, fields: Any, **kwargs: Any):
        if doctype == "Account":
            return {
                "company": "Test Company",
                "is_group": 0,
                "disabled": 0,
                "root_type": "Asset",
                "account_currency": "MYR",
            }
        return None

    def sql(query: str, params: tuple[str], *, as_dict: bool = False):
        assert as_dict is True
        lookup = params[0]
        if "WHERE `reconciliation_idempotency_key`" in query:
            rows = [
                row
                for row in reconciliations.values()
                if row["reconciliation_idempotency_key"] == lookup
            ]
        else:
            rows = [row for row in reconciliations.values() if row["name"] == lookup]
        return rows

    class ReconciliationDoc(SimpleNamespace):
        def insert(self, **kwargs: Any) -> None:
            values = dict(vars(self))
            values["name"] = "MQR-1"
            self.name = "MQR-1"
            reconciliations[self.name] = values

    def get_doc(values: dict[str, Any]):
        return ReconciliationDoc(**values)

    def set_value(*args: Any, **kwargs: Any) -> None:
        db_updates.append(args)
        if args[0] == "Manual QR Reconciliation":
            reconciliations[args[1]][args[2]] = args[3]

    monkeypatch.setattr(service.frappe.db, "get_single_value", get_single_value)
    monkeypatch.setattr(service.frappe.db, "get_value", get_value)
    monkeypatch.setattr(service.frappe.db, "exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(service.frappe.db, "sql", sql)
    monkeypatch.setattr(service.frappe.db, "set_value", set_value)
    monkeypatch.setattr(service.frappe, "get_doc", get_doc)
    return reconciliations, db_updates


@pytest.mark.parametrize(
    ("captured_at", "expected"),
    [
        (
            "2026-07-19T19:53:56.376Z",
            datetime(2026, 7, 20, 3, 53, 56, 376000),
        ),
        (
            "2026-07-19T19:53:56.376+00:00",
            datetime(2026, 7, 20, 3, 53, 56, 376000),
        ),
        (
            "2026-07-20T03:53:56.376+08:00",
            datetime(2026, 7, 20, 3, 53, 56, 376000),
        ),
        (
            "2026-07-20 03:53:56.376",
            datetime(2026, 7, 20, 3, 53, 56, 376000),
        ),
    ],
    ids=("utc-z", "utc-offset", "site-offset", "site-naive"),
)
def test_static_qr_normalizes_wire_timestamp_for_frappe_datetime(
    monkeypatch: pytest.MonkeyPatch,
    captured_at: str,
    expected: datetime,
) -> None:
    reconciliations, _ = _install_static_reconciliation_db(monkeypatch)
    evidence = _static_evidence()
    evidence["captured_at"] = captured_at
    payment = _static_payment(
        manual_confirmation_evidence_json=json.dumps(evidence)
    )

    service.register_qr_payment_settlement(_order(payment))

    stored = reconciliations["MQR-1"]
    assert stored["evidence_captured_at"] == expected
    assert stored["evidence_captured_at"].tzinfo is None
    assert json.loads(stored["evidence_json"])["captured_at"] == captured_at


def test_static_qr_aware_timestamp_replay_remains_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciliations, _ = _install_static_reconciliation_db(monkeypatch)
    evidence = _static_evidence()
    evidence["captured_at"] = "2026-07-19T19:53:56.376Z"
    payment = _static_payment(
        manual_confirmation_evidence_json=json.dumps(evidence)
    )
    order = _order(payment)

    first = service.register_qr_payment_settlement(order)
    second = service.register_qr_payment_settlement(order)

    assert first == second == "MQR-1"
    assert list(reconciliations) == ["MQR-1"]
    assert reconciliations["MQR-1"]["evidence_captured_at"] == datetime(
        2026, 7, 20, 3, 53, 56, 376000
    )


def test_static_qr_creates_pending_reconciliation_and_binds_invoice(monkeypatch):
    reconciliations, db_updates = _install_static_reconciliation_db(monkeypatch)
    payment = _static_payment()
    order = _order(payment)

    registered = service.register_qr_payment_settlement(order)
    bound = service.bind_qr_payment_settlement(order, "SINV-STATIC-1")

    assert registered == "MQR-1"
    assert bound == "MQR-1"
    assert payment.settlement_status == "pending_reconciliation"
    assert payment.suspense_account == "Manual QR Suspense - TC"
    assert payment.manual_qr_reconciliation == "MQR-1"
    assert reconciliations["MQR-1"]["amount_sen"] == 1250
    assert reconciliations["MQR-1"]["sales_invoice"] == "SINV-STATIC-1"
    assert db_updates[-1] == (
        "Manual QR Reconciliation",
        "MQR-1",
        "sales_invoice",
        "SINV-STATIC-1",
    )


def test_static_qr_rejects_warning_without_structured_evidence(monkeypatch):
    _install_static_reconciliation_db(monkeypatch)
    payment = _static_payment(manual_confirmation_evidence_json=None)

    with pytest.raises(service.frappe.ValidationError, match="evidence is invalid"):
        service.register_qr_payment_settlement(_order(payment))


def test_static_qr_rejects_maybank_transaction_reference(monkeypatch):
    _install_static_reconciliation_db(monkeypatch)
    monkeypatch.setattr(
        service.frappe.db,
        "exists",
        lambda doctype, filters: filters["transaction_refno"] == "BANK-REF-1",
    )
    evidence = _static_evidence()
    evidence["local_confirmation_reference"] = "BANK-REF-1"
    payment = _static_payment(
        reference_no="BANK-REF-1",
        manual_confirmation_evidence_json=json.dumps(evidence),
    )

    with pytest.raises(service.frappe.ValidationError, match="must not use a Maybank"):
        service.register_qr_payment_settlement(_order(payment))


def _validated_static_payload() -> dict[str, Any]:
    return {
        "payment_method": "DuitNow QR",
        "payment_channel_code": "static_qr",
        "amount": "12.50",
        "reference_no": "STATIC-REF-1",
        "external_transaction_id": "static-session-1",
        "manual_confirmation_evidence": _static_evidence(),
        "manual_confirmation_warning_acknowledged": True,
    }


def test_static_channel_requires_structured_pending_evidence(monkeypatch):
    monkeypatch.setattr(
        fb_orders,
        "_resolve_mode_of_payment_name",
        lambda value: "DuitNow QR",
    )
    monkeypatch.setattr(fb_orders, "_require_doc", lambda *args: None)
    payload = _validated_static_payload()
    payload.pop("manual_confirmation_evidence")

    with pytest.raises(service.frappe.ValidationError, match="evidence is required"):
        fb_orders._validate_order_payment(payload, 1)


def test_static_channel_persists_server_validated_pending_evidence(monkeypatch):
    monkeypatch.setattr(
        fb_orders,
        "_resolve_mode_of_payment_name",
        lambda value: "DuitNow QR",
    )
    monkeypatch.setattr(fb_orders, "_require_doc", lambda *args: None)

    payment = fb_orders._validate_order_payment(_validated_static_payload(), 1)
    evidence = json.loads(payment["manual_confirmation_evidence_json"])

    assert payment["is_manual_confirmation"] == 1
    assert payment["settlement_status"] == "pending_reconciliation"
    assert payment["reconciliation_idempotency_key"] == "manual-reconciliation-1"
    assert evidence["reconciliation_status"] == "pending_reconciliation"


def test_static_channel_rejects_non_qr_payment_method(monkeypatch):
    monkeypatch.setattr(
        fb_orders,
        "_resolve_mode_of_payment_name",
        lambda value: "Cash",
    )
    monkeypatch.setattr(fb_orders, "_require_doc", lambda *args: None)

    with pytest.raises(
        service.frappe.ValidationError,
        match="payment_channel_code requires DuitNow QR",
    ):
        fb_orders._validate_order_payment(_validated_static_payload(), 1)


def test_duitnow_qr_requires_a_supported_channel(monkeypatch):
    monkeypatch.setattr(
        fb_orders,
        "_resolve_mode_of_payment_name",
        lambda value: "DuitNow QR",
    )
    monkeypatch.setattr(fb_orders, "_require_doc", lambda *args: None)
    payload = _validated_static_payload()
    payload["payment_channel_code"] = None

    with pytest.raises(
        service.frappe.ValidationError,
        match="payment_channel_code is required for DuitNow QR",
    ):
        fb_orders._validate_order_payment(payload, 1)


def test_maybank_channel_persists_server_validated_manual_evidence(monkeypatch):
    monkeypatch.setattr(
        fb_orders,
        "_resolve_mode_of_payment_name",
        lambda value: "DuitNow QR",
    )
    monkeypatch.setattr(fb_orders, "_require_doc", lambda *args: None)
    payload = _validated_static_payload()
    payload["payment_channel_code"] = "maybank"
    payload["external_transaction_id"] = "MB-REF-1"
    payload["reference_no"] = "MB-REF-1"
    payload["manual_confirmation_evidence"].update(
        {
            "local_confirmation_reference": "MB-REF-1",
            "reconciliation_idempotency_key": "manual-maybank-reconciliation-1",
        }
    )

    payment = fb_orders._validate_order_payment(payload, 1)

    assert payment["is_manual_confirmation"] == 1
    assert payment["settlement_status"] == "pending_reconciliation"
    assert payment["reconciliation_idempotency_key"] == (
        "manual-maybank-reconciliation-1"
    )


def test_maybank_channel_without_manual_evidence_remains_provider_verified(monkeypatch):
    monkeypatch.setattr(
        fb_orders,
        "_resolve_mode_of_payment_name",
        lambda value: "DuitNow QR",
    )
    monkeypatch.setattr(fb_orders, "_require_doc", lambda *args: None)
    payload = _validated_static_payload()
    payload.update(
        {
            "payment_channel_code": "maybank",
            "external_transaction_id": "MB-REF-1",
            "reference_no": "MB-REF-1",
        }
    )
    payload.pop("manual_confirmation_evidence")

    payment = fb_orders._validate_order_payment(payload, 1)

    assert payment["is_manual_confirmation"] == 0
    assert payment["settlement_status"] == "verified"
