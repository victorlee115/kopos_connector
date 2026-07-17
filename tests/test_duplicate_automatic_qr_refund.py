from __future__ import annotations

import copy
import importlib
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
service = importlib.import_module(
    "kopos_connector.kopos.services.accounting.duplicate_qr_payment_service"
)
contract = importlib.import_module(
    "kopos_connector.kopos.services.accounting._duplicate_qr_contract"
)
incident = importlib.import_module(
    "kopos_connector.kopos.services.accounting._duplicate_qr_incident"
)
refund_service = importlib.import_module(
    "kopos_connector.kopos.services.accounting._duplicate_qr_refund"
)
journal_entry_extension = importlib.import_module(
    "kopos_connector.extensions.journal_entry"
)
sales_invoice_extension = importlib.import_module(
    "kopos_connector.extensions.sales_invoice"
)
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_BYTES = b"Maybank provider refund confirmation MBQR-DUPLICATE"
EVIDENCE_SHA256 = hashlib.sha256(EVIDENCE_BYTES).hexdigest()


class FakeDoc(SimpleNamespace):
    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)

    def append(self, fieldname: str, values: dict[str, Any]) -> FakeDoc:
        row = FakeDoc(**values)
        rows = list(getattr(self, fieldname, []) or [])
        rows.append(row)
        setattr(self, fieldname, rows)
        return row


@dataclass
class DuplicateAccountingEnv:
    docs: dict[tuple[str, str], FakeDoc]
    journals_created: int = 0
    journal_gl: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    source_updates: list[dict[str, Any]] = field(default_factory=list)
    clearing_account: str | None = "Maybank Clearing - TC"
    liability_account: str | None = "Customer Refund Liability - TC"
    created_doctypes: list[str] = field(default_factory=list)
    emit_gl_on_submit: bool = True
    sql_queries: list[str] = field(default_factory=list)


@pytest.fixture
def duplicate_env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc]:
    payment = FakeDoc(
        name="FBPAY-1",
        payment_method="DuitNow QR",
        payment_channel_code="maybank",
        amount="12.50",
        reference_no="MBB-WINNER",
        external_transaction_id="MBB-WINNER",
        maybank_qr_transaction="MBQR-WINNER",
    )
    order = FakeDoc(
        doctype="FB Order",
        name="FB-ORDER-1",
        docstatus=1,
        automatic_qr_payment="FBPAY-1",
        automatic_qr_state="finalized",
        device_id="TAB-A001",
        company="Test Company",
        currency="MYR",
        sales_invoice="SINV-1",
        external_idempotency_key="SALE-IDEMPOTENCY-1",
        status="Submitted",
        invoice_status="Posted",
        payments=[payment],
    )
    source_values: dict[str, Any] = {
        "doctype": "Maybank QR Transaction",
        "name": "MBQR-DUPLICATE",
        "transaction_refno": "MBB-DUPLICATE",
        "status": "paid",
        "maybank_status": 1,
        "provider": "maybank_qr",
        "company": "Test Company",
        "device_id": "TAB-A001",
        "currency": "MYR",
        "sale_amount_sen": 1250,
        "business_date": "2026-07-17",
        "paid_at": "2026-03-12 23:59:00",
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FBPAY-1",
        "sales_invoice": None,
        "consumption_key": None,
        "invoice_consumption_key": None,
        "manual_reconciliation_status": None,
        "reconciliation_failed_reason": None,
        "failure_journal_entry": None,
    }
    for fieldname in service.REQUIRED_SOURCE_FIELDS:
        source_values.setdefault(fieldname, None)
    source = FakeDoc(**source_values)
    winner = FakeDoc(
        doctype="Maybank QR Transaction",
        name="MBQR-WINNER",
        transaction_refno="MBB-WINNER",
        status="paid",
        maybank_status=1,
        provider="maybank_qr",
        company="Test Company",
        device_id="TAB-A001",
        currency="MYR",
        sale_amount_sen=1250,
        fb_order="FB-ORDER-1",
        fb_order_payment="FBPAY-1",
        consumption_key="FB-ORDER-1",
        sales_invoice="SINV-1",
        invoice_consumption_key="SINV-1",
    )
    invoice = FakeDoc(
        doctype="Sales Invoice",
        name="SINV-1",
        docstatus=1,
        is_return=0,
        custom_fb_order="FB-ORDER-1",
        custom_fb_idempotency_key="SALE-IDEMPOTENCY-1",
        custom_fb_device_id="TAB-A001",
        custom_fb_void_idempotency_key=None,
        custom_fb_void_request_fingerprint=None,
        custom_fb_void_manager=None,
        custom_fb_void_approval_token_id=None,
        company="Test Company",
        currency="MYR",
    )
    evidence_file = FakeDoc(
        doctype="File",
        name="FILE-MBB-REFUND-1",
        is_private=1,
        attached_to_doctype="Maybank QR Transaction",
        attached_to_name="MBQR-DUPLICATE",
        file_size=len(EVIDENCE_BYTES),
        get_content=lambda: EVIDENCE_BYTES,
    )
    env = DuplicateAccountingEnv(
        docs={
            ("FB Order", order.name): order,
            ("Maybank QR Transaction", source.name): source,
            ("Maybank QR Transaction", winner.name): winner,
            ("Sales Invoice", invoice.name): invoice,
            ("File", evidence_file.name): evidence_file,
        }
    )
    accounts = {
        "Maybank Clearing - TC": {
            "name": "Maybank Clearing - TC",
            "company": "Test Company",
            "is_group": 0,
            "disabled": 0,
            "root_type": "Asset",
            "account_currency": "MYR",
        },
        "Customer Refund Liability - TC": {
            "name": "Customer Refund Liability - TC",
            "company": "Test Company",
            "is_group": 0,
            "disabled": 0,
            "root_type": "Liability",
            "account_currency": "MYR",
        },
    }

    class FakeDB:
        def __init__(self) -> None:
            self.savepoints: dict[
                str,
                tuple[
                    dict[tuple[str, str], dict[str, Any]],
                    dict[str, list[dict[str, Any]]],
                ],
            ] = {}

        def get_value(
            self,
            doctype: str,
            name_or_filters: Any,
            fieldname: Any,
            *,
            as_dict: bool = False,
        ) -> Any:
            if doctype == "Maybank QR Transaction":
                doc = env.docs.get((doctype, str(name_or_filters)))
                if not doc:
                    return None
                if as_dict:
                    return {
                        name: getattr(doc, name, None)
                        for name in list(fieldname)
                    }
                return getattr(doc, str(fieldname), None)
            if doctype == "Sales Invoice":
                doc = env.docs.get((doctype, str(name_or_filters)))
                if not doc:
                    return None
                if as_dict:
                    return {
                        name: getattr(doc, name, None)
                        for name in list(fieldname)
                    }
                return getattr(doc, str(fieldname), None)
            if doctype == "Company" and fieldname == "default_currency":
                return "MYR"
            if (
                doctype == "Company"
                and fieldname == service.COMPANY_CLEARING_ACCOUNT_FIELD
            ):
                return env.clearing_account
            if (
                doctype == "Company"
                and fieldname == service.COMPANY_LIABILITY_ACCOUNT_FIELD
            ):
                return env.liability_account
            if doctype == "Account":
                account = accounts.get(str(name_or_filters))
                if as_dict:
                    return account
                return account.get(str(fieldname)) if account else None
            if doctype == "Journal Entry" and isinstance(name_or_filters, dict):
                expected_key = name_or_filters["custom_kopos_qr_duplicate_key"]
                for (row_doctype, row_name), doc in env.docs.items():
                    if (
                        row_doctype == "Journal Entry"
                        and getattr(doc, "custom_kopos_qr_duplicate_key", None)
                        == expected_key
                    ):
                        return row_name
                return None
            raise AssertionError(
                f"unexpected get_value({doctype!r}, {name_or_filters!r}, {fieldname!r})"
            )

        def set_value(
            self,
            doctype: str,
            name: str,
            values: dict[str, Any],
            **_kwargs: Any,
        ) -> None:
            doc = env.docs[(doctype, name)]
            for fieldname, value in values.items():
                setattr(doc, fieldname, value)
            env.source_updates.append(dict(values))

        def sql(
            self,
            query: str,
            values: tuple[Any, ...],
            **_kwargs: Any,
        ) -> list[tuple[Any, ...]]:
            env.sql_queries.append(" ".join(query.split()))
            return [(values[0],)]

        def savepoint(self, name: str) -> None:
            self.savepoints[name] = (
                {
                    key: copy.deepcopy(vars(document))
                    for key, document in env.docs.items()
                },
                copy.deepcopy(env.journal_gl),
            )

        def rollback(self, *, save_point: str | None = None) -> None:
            if not save_point:
                return
            document_snapshots, gl_snapshot = self.savepoints[save_point]
            for key in list(env.docs):
                if key not in document_snapshots:
                    del env.docs[key]
            for key, attributes in document_snapshots.items():
                document = env.docs[key]
                vars(document).clear()
                vars(document).update(copy.deepcopy(attributes))
            env.journal_gl.clear()
            env.journal_gl.update(copy.deepcopy(gl_snapshot))

    def get_doc(doctype: str, name: str) -> FakeDoc:
        return env.docs[(doctype, name)]

    def get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert doctype == "GL Entry"
        assert kwargs["limit_page_length"] == 3
        return list(env.journal_gl.get(kwargs["filters"]["voucher_no"], []))

    def new_doc(doctype: str) -> FakeDoc:
        assert doctype == "Journal Entry"
        env.created_doctypes.append(doctype)
        env.journals_created += 1
        journal_name = f"JV-DUP-{env.journals_created}"
        journal = FakeDoc(
            doctype=doctype,
            name=journal_name,
            docstatus=0,
            accounts=[],
        )

        def insert(*, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            env.docs[(doctype, journal_name)] = journal

        def submit() -> None:
            journal.docstatus = 1
            env.journal_gl[journal_name] = (
                [
                    {
                        "account": row.account,
                        "debit": row.debit_in_account_currency,
                        "credit": row.credit_in_account_currency,
                        "debit_in_account_currency": row.debit_in_account_currency,
                        "credit_in_account_currency": row.credit_in_account_currency,
                    }
                    for row in journal.accounts
                ]
                if env.emit_gl_on_submit
                else []
            )

        journal.insert = insert
        journal.submit = submit
        return journal

    monkeypatch.setattr(service.frappe, "db", FakeDB())
    monkeypatch.setattr(service.frappe, "get_doc", get_doc)
    monkeypatch.setattr(service.frappe, "get_all", get_all)
    monkeypatch.setattr(service.frappe, "new_doc", new_doc)
    monkeypatch.setattr(
        service.frappe,
        "get_meta",
        lambda _doctype: FakeDoc(has_field=lambda _fieldname: True),
    )
    monkeypatch.setattr(
        service.frappe,
        "session",
        FakeDoc(user="manager@example.test"),
    )
    monkeypatch.setattr(
        refund_service,
        "lock_device_for_operational_mutation",
        lambda device_id, **_kwargs: (
            env.sql_queries.append(f"LOCK DEVICE {device_id}")
            or FakeDoc(name=device_id, device_id=device_id)
        ),
    )
    monkeypatch.setattr(incident, "log_sanitized_error", lambda *_args: None)
    monkeypatch.setattr(
        contract,
        "_load_consumed_void_approval_proof",
        lambda **_kwargs: {
            "approval_manager_id": "manager@example.test",
            "approval_token_id": "APPROVAL-TOKEN-1",
            "approval_context_hash": "c" * 64,
        },
    )
    return env, order, source


def _register(order: FakeDoc, source: FakeDoc) -> dict[str, Any]:
    return service.register_duplicate_paid_incident(
        source,
        order_doc=order,
        winning_transaction_name="MBQR-WINNER",
    )


def _refund_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "transaction": "MBQR-DUPLICATE",
        "provider_transaction_refno": "MBB-DUPLICATE",
        "provider_refund_status": "refunded",
        "provider_refund_reference": "MBB-REFUND-1250",
        "provider_refund_amount_sen": 1250,
        "provider_refund_currency": "MYR",
        "provider_evidence_reference": "MAYBANK-CASE-20260717-001",
        "provider_evidence_file": "FILE-MBB-REFUND-1",
        "provider_evidence_sha256": EVIDENCE_SHA256,
        "provider_refund_date": "2026-03-13",
        "note": "Provider portal confirms the exact duplicate payment refund.",
    }
    payload.update(overrides)
    return payload


def _mark_winning_sale_durably_voided(
    env: DuplicateAccountingEnv,
    order: FakeDoc,
) -> None:
    invoice = env.docs[("Sales Invoice", "SINV-1")]
    invoice.docstatus = 2
    invoice.custom_fb_void_idempotency_key = "VOID-IDEMPOTENCY-1"
    invoice.custom_fb_void_request_fingerprint = "d" * 64
    invoice.custom_fb_void_manager = "manager@example.test"
    invoice.custom_fb_void_approval_token_id = "APPROVAL-TOKEN-1"
    order.status = "Cancelled"
    order.invoice_status = "Reversed"


def test_duplicate_paid_attempt_posts_liability_without_second_invoice_or_sale_mutation(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env

    result = _register(order, source)

    assert result["duplicate_payment_status"] == "refund_required"
    assert result["liability_journal_entry"] == "JV-DUP-1"
    assert result["sales_invoice_created"] is False
    assert source.duplicate_payment_status == "refund_required"
    assert source.consumption_key is None
    assert source.invoice_consumption_key is None
    assert source.sales_invoice is None
    assert order.docstatus == 1
    assert order.sales_invoice == "SINV-1"
    assert env.created_doctypes == ["Journal Entry"]
    assert env.docs[("Journal Entry", "JV-DUP-1")].posting_date == "2026-03-12"
    assert env.journal_gl["JV-DUP-1"] == [
        {
            "account": "Maybank Clearing - TC",
            "debit": Decimal("12.50"),
            "credit": Decimal("0"),
            "debit_in_account_currency": Decimal("12.50"),
            "credit_in_account_currency": Decimal("0"),
        },
        {
            "account": "Customer Refund Liability - TC",
            "debit": Decimal("0"),
            "credit": Decimal("12.50"),
            "debit_in_account_currency": Decimal("0"),
            "credit_in_account_currency": Decimal("12.50"),
        },
    ]


def test_missing_liability_configuration_stays_accounting_pending_and_nonblocking(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    env.liability_account = None

    result = _register(order, source)

    assert result["duplicate_payment_status"] == "accounting_pending"
    assert result["liability_journal_entry"] is None
    assert source.duplicate_payment_status == "accounting_pending"
    assert env.journals_created == 0
    assert order.docstatus == 1
    assert order.sales_invoice == "SINV-1"


def test_missing_winning_invoice_context_stays_accounting_pending(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    order.sales_invoice = None

    result = _register(order, source)

    assert result["duplicate_payment_status"] == "accounting_pending"
    assert source.duplicate_payment_status == "accounting_pending"
    assert env.journals_created == 0
    assert order.docstatus == 1


def test_late_duplicate_after_durable_void_posts_liability_without_reopening_sale(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    _mark_winning_sale_durably_voided(env, order)

    result = _register(order, source)

    assert result["duplicate_payment_status"] == "refund_required"
    assert result["liability_journal_entry"] == "JV-DUP-1"
    assert order.status == "Cancelled"
    assert order.invoice_status == "Reversed"
    assert env.docs[("Sales Invoice", "SINV-1")].docstatus == 2
    assert source.sales_invoice is None


def test_refund_required_duplicate_remains_refundable_after_winning_sale_void(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    _register(order, source)
    _mark_winning_sale_durably_voided(env, order)

    result = service.resolve_duplicate_paid_refund(_refund_payload())

    assert result["status"] == "refunded"
    assert source.duplicate_payment_status == "refunded"
    assert env.journals_created == 2
    assert order.status == "Cancelled"
    assert env.docs[("Sales Invoice", "SINV-1")].docstatus == 2


def test_cancelled_winning_invoice_without_exact_void_proof_stays_pending(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    _mark_winning_sale_durably_voided(env, order)
    env.docs[("Sales Invoice", "SINV-1")].custom_fb_void_approval_token_id = None

    result = _register(order, source)

    assert result["duplicate_payment_status"] == "accounting_pending"
    assert source.duplicate_payment_status == "accounting_pending"
    assert env.journals_created == 0
    assert order.status == "Cancelled"


def test_liability_registration_exact_retry_reuses_one_verified_journal(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env

    first = _register(order, source)
    second = _register(order, source)

    assert first["liability_journal_entry"] == "JV-DUP-1"
    assert second["liability_journal_entry"] == "JV-DUP-1"
    assert env.journals_created == 1
    assert source.duplicate_payment_status == "refund_required"


def test_missing_gl_proof_rolls_back_partial_journal_then_recovers_exactly_once(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    env.emit_gl_on_submit = False

    pending = _register(order, source)

    assert pending["duplicate_payment_status"] == "accounting_pending"
    assert source.duplicate_payment_status == "accounting_pending"
    assert source.duplicate_liability_journal_entry is None
    assert env.journals_created == 1
    assert not any(key[0] == "Journal Entry" for key in env.docs)
    assert order.docstatus == 1

    env.emit_gl_on_submit = True

    recovered = _register(order, source)

    assert recovered["duplicate_payment_status"] == "refund_required"
    assert recovered["liability_journal_entry"] == "JV-DUP-2"
    assert env.journals_created == 2
    assert [
        key for key in env.docs if key[0] == "Journal Entry"
    ] == [("Journal Entry", "JV-DUP-2")]


@pytest.mark.parametrize(
    "overrides, message",
    [
        (
            {"provider_transaction_refno": "MBB-WRONG"},
            "does not match the duplicate payment reference",
        ),
        (
            {"provider_refund_amount_sen": 1249},
            "amount does not match the duplicate payment",
        ),
        (
            {"provider_refund_status": "pending"},
            "status must be exactly refunded",
        ),
        (
            {"provider_refund_currency": "SGD"},
            "currency does not match the duplicate payment",
        ),
        (
            {"provider_refund_date": "2026-03-11"},
            "must be on or after provider payment",
        ),
        (
            {"provider_refund_date": "2026-03-14"},
            "must be on or after provider payment",
        ),
        (
            {"provider_refund_date": "2026-03-13T00:00:00"},
            "must use exact YYYY-MM-DD format",
        ),
        (
            {"provider_evidence_file": "FILE-MISSING"},
            "evidence File was not found",
        ),
        (
            {"provider_evidence_sha256": "b" * 64},
            "SHA-256 does not match retained private File bytes",
        ),
        (
            {"provider_evidence_sha256": "not-a-sha256"},
            "must be exactly 64 lowercase hexadecimal characters",
        ),
        (
            {"provider_evidence_sha256": "A" * 64},
            "must be exactly 64 lowercase hexadecimal characters",
        ),
    ],
)
def test_malformed_or_mismatched_refund_evidence_fails_closed(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
    overrides: dict[str, Any],
    message: str,
) -> None:
    env, order, source = duplicate_env
    _register(order, source)

    with pytest.raises(service.frappe.ValidationError, match=message):
        service.resolve_duplicate_paid_refund(_refund_payload(**overrides))

    assert source.duplicate_payment_status == "refund_required"
    assert source.duplicate_refund_journal_entry is None
    assert env.journals_created == 1
    assert order.docstatus == 1


def test_exact_provider_refund_posts_inverse_entry_and_exact_retry_is_idempotent(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    _register(order, source)

    first = service.resolve_duplicate_paid_refund(_refund_payload())
    second = service.resolve_duplicate_paid_refund(_refund_payload())
    finalizer_replay = _register(order, source)

    assert first["status"] == "refunded"
    assert second["status"] == "already_refunded"
    assert finalizer_replay["duplicate_payment_status"] == "refunded"
    assert finalizer_replay["refund_journal_entry"] == "JV-DUP-2"
    assert first["refund_journal_entry"] == "JV-DUP-2"
    assert second["refund_journal_entry"] == "JV-DUP-2"
    assert source.duplicate_payment_status == "refunded"
    assert source.duplicate_refund_reference == "MBB-REFUND-1250"
    assert env.journals_created == 2
    assert env.docs[("Journal Entry", "JV-DUP-2")].posting_date == "2026-03-13"
    assert env.journal_gl["JV-DUP-2"] == [
        {
            "account": "Customer Refund Liability - TC",
            "debit": Decimal("12.50"),
            "credit": Decimal("0"),
            "debit_in_account_currency": Decimal("12.50"),
            "credit_in_account_currency": Decimal("0"),
        },
        {
            "account": "Maybank Clearing - TC",
            "debit": Decimal("0"),
            "credit": Decimal("12.50"),
            "debit_in_account_currency": Decimal("0"),
            "credit_in_account_currency": Decimal("12.50"),
        },
    ]
    assert order.docstatus == 1
    assert order.sales_invoice == "SINV-1"

    with pytest.raises(
        service.frappe.ValidationError,
        match="does not match this exact retry",
    ):
        service.resolve_duplicate_paid_refund(
            _refund_payload(
                provider_evidence_reference="MAYBANK-CASE-20260717-CHANGED"
            )
        )
    assert env.journals_created == 2


def test_system_manager_refund_locks_device_before_order_and_source(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    _register(order, source)
    env.sql_queries.clear()

    service.resolve_duplicate_paid_refund(_refund_payload())

    assert env.sql_queries[0] == "LOCK DEVICE TAB-A001"
    locked_tables = [
        query.split("FROM `tab", 1)[1].split("`", 1)[0]
        for query in env.sql_queries[1:3]
    ]
    assert locked_tables == ["FB Order", "Maybank QR Transaction"]
    assert all("FOR UPDATE" in query for query in env.sql_queries[1:3])


def test_system_manager_refund_revalidates_device_binding_after_source_lock(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, order, source = duplicate_env
    _register(order, source)
    writes_before = len(env.source_updates)

    def mutate_binding_after_device_lock(device_id: str, **_kwargs: Any) -> FakeDoc:
        source.device_id = "TAB-OTHER"
        return FakeDoc(name=device_id, device_id=device_id)

    monkeypatch.setattr(
        refund_service,
        "lock_device_for_operational_mutation",
        mutate_binding_after_device_lock,
    )
    with pytest.raises(
        service.frappe.ValidationError,
        match="binding changed while locks were acquired",
    ):
        service.resolve_duplicate_paid_refund(_refund_payload())

    assert len(env.source_updates) == writes_before
    assert source.duplicate_payment_status == "refund_required"


def test_terminal_refund_assertion_is_read_only_and_reproves_exact_evidence(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    _register(order, source)
    service.resolve_duplicate_paid_refund(_refund_payload())
    writes_before = len(env.source_updates)
    journals_before = env.journals_created

    evidence = service.assert_duplicate_refund_terminal_evidence(
        source,
        order_doc=order,
    )

    assert evidence["liability_journal_entry"] == "JV-DUP-1"
    assert evidence["refund_journal_entry"] == "JV-DUP-2"
    assert evidence["provider_evidence_file"] == "FILE-MBB-REFUND-1"
    assert evidence["provider_evidence_sha256"] == EVIDENCE_SHA256
    assert evidence["provider_evidence_byte_length"] == len(EVIDENCE_BYTES)
    assert len(env.source_updates) == writes_before
    assert env.journals_created == journals_before


@pytest.mark.parametrize(
    ("journal_field", "mutation", "message"),
    [
        (
            "duplicate_liability_journal_entry",
            "missing",
            "exactly two active GL rows",
        ),
        (
            "duplicate_refund_journal_entry",
            "extra_zero",
            "exactly two active GL rows",
        ),
        (
            "duplicate_liability_journal_entry",
            "base_mismatch",
            "base and account-currency GL amounts differ",
        ),
        (
            "duplicate_refund_journal_entry",
            "unexpected_account",
            "unexpected GL account",
        ),
        (
            "duplicate_refund_journal_entry",
            "wrong_amount",
            "does not exactly debit",
        ),
    ],
)
def test_terminal_refund_assertion_rejects_missing_extra_or_inexact_gl(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
    journal_field: str,
    mutation: str,
    message: str,
) -> None:
    env, order, source = duplicate_env
    _register(order, source)
    service.resolve_duplicate_paid_refund(_refund_payload())
    journal_name = str(getattr(source, journal_field))
    rows = env.journal_gl[journal_name]
    if mutation == "missing":
        rows.pop()
    elif mutation == "extra_zero":
        rows.append(
            {
                "account": "Maybank Clearing - TC",
                "debit": Decimal("0"),
                "credit": Decimal("0"),
                "debit_in_account_currency": Decimal("0"),
                "credit_in_account_currency": Decimal("0"),
            }
        )
    elif mutation == "base_mismatch":
        rows[0]["debit"] = Decimal("12.49")
    elif mutation == "unexpected_account":
        rows[0]["account"] = "Unexpected Ledger - TC"
    elif mutation == "wrong_amount":
        rows[0]["debit"] = Decimal("12.49")
        rows[0]["debit_in_account_currency"] = Decimal("12.49")
    else:
        raise AssertionError(f"unknown mutation {mutation}")

    with pytest.raises(service.frappe.ValidationError, match=message):
        service.assert_duplicate_refund_terminal_evidence(
            source,
            order_doc=order,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("bytes", "SHA-256 does not match"),
        ("size", "size does not match retained content"),
        ("privacy", "must be a private File"),
        ("attachment", "must be a private File"),
    ],
)
def test_terminal_refund_assertion_rejects_retained_file_tampering(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
    mutation: str,
    message: str,
) -> None:
    env, order, source = duplicate_env
    _register(order, source)
    service.resolve_duplicate_paid_refund(_refund_payload())
    evidence_file = env.docs[("File", "FILE-MBB-REFUND-1")]
    if mutation == "bytes":
        evidence_file.get_content = lambda: b"X" * len(EVIDENCE_BYTES)
    elif mutation == "size":
        evidence_file.file_size = len(EVIDENCE_BYTES) + 1
    elif mutation == "privacy":
        evidence_file.is_private = 0
    elif mutation == "attachment":
        evidence_file.attached_to_name = "MBQR-OTHER"
    else:
        raise AssertionError(f"unknown mutation {mutation}")

    with pytest.raises(service.frappe.ValidationError, match=message):
        service.assert_duplicate_refund_terminal_evidence(
            source,
            order_doc=order,
        )


@pytest.mark.parametrize("fieldname", ["duplicate_refunded_by", "duplicate_refunded_at"])
def test_terminal_refund_assertion_requires_terminal_audit_identity(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
    fieldname: str,
) -> None:
    env, order, source = duplicate_env
    _register(order, source)
    service.resolve_duplicate_paid_refund(_refund_payload())
    setattr(source, fieldname, None)

    with pytest.raises(
        service.frappe.ValidationError,
        match="exactly audited terminal state",
    ):
        service.assert_duplicate_refund_terminal_evidence(
            source,
            order_doc=order,
        )


def test_terminal_refund_assertion_rejects_cross_company_source_or_winner(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    _register(order, source)
    service.resolve_duplicate_paid_refund(_refund_payload())

    source.company = "Other Company"
    with pytest.raises(service.frappe.ValidationError, match="sale company"):
        service.assert_duplicate_refund_terminal_evidence(
            source,
            order_doc=order,
        )
    source.company = "Test Company"
    env.docs[("Maybank QR Transaction", "MBQR-WINNER")].company = "Other Company"
    with pytest.raises(
        service.frappe.ValidationError,
        match="Winning provider transaction company",
    ):
        service.assert_duplicate_refund_terminal_evidence(
            source,
            order_doc=order,
        )


def test_locked_terminal_reproof_uses_order_journals_then_file_order(
    duplicate_env: tuple[DuplicateAccountingEnv, FakeDoc, FakeDoc],
) -> None:
    env, order, source = duplicate_env
    _register(order, source)
    service.resolve_duplicate_paid_refund(_refund_payload())
    env.sql_queries.clear()

    service.lock_and_assert_duplicate_refund_terminal_evidence(
        source.name,
        expected_order_name=order.name,
        expected_device_id=order.device_id,
    )

    locked_tables = [
        query.split("FROM `tab", 1)[1].split("`", 1)[0]
        for query in env.sql_queries
    ]
    assert locked_tables == [
        "FB Order",
        "Maybank QR Transaction",
        "Journal Entry",
        "Journal Entry",
        "File",
    ]
    assert all("FOR UPDATE" in query for query in env.sql_queries)


def test_schema_and_install_contract_expose_first_class_duplicate_liability_fields() -> None:
    schema = json.loads(
        (
            ROOT
            / "kopos_connector/kopos/doctype/maybank_qr_transaction/"
            "maybank_qr_transaction.json"
        ).read_text(encoding="utf-8")
    )
    fields = {row["fieldname"]: row for row in schema["fields"]}

    assert fields["duplicate_payment_status"]["options"].splitlines()[1:] == [
        "accounting_pending",
        "refund_required",
        "refunded",
    ]
    assert fields["duplicate_accounting_key"]["unique"] == 1
    assert fields["duplicate_refund_key"]["unique"] == 1
    assert fields["duplicate_liability_journal_entry"]["options"] == "Journal Entry"
    assert fields["duplicate_refund_journal_entry"]["options"] == "Journal Entry"
    assert fields["duplicate_refund_evidence_file"]["options"] == "File"
    assert fields["duplicate_refund_date"]["fieldtype"] == "Date"

    custom_fields_source = (
        ROOT / "kopos_connector/kopos/install/fb_custom_fields.py"
    ).read_text(encoding="utf-8")
    assert service.COMPANY_CLEARING_ACCOUNT_FIELD in custom_fields_source
    assert service.COMPANY_LIABILITY_ACCOUNT_FIELD in custom_fields_source
    assert "custom_kopos_qr_duplicate_key" in custom_fields_source


def test_active_refund_api_is_non_device_system_manager_only() -> None:
    api_source = (ROOT / "kopos_connector/api/__init__.py").read_text(
        encoding="utf-8"
    )
    endpoint = api_source.split(
        "def resolve_duplicate_automatic_qr_refund(", 1
    )[1].split("def upload_manual_qr_receipt(", 1)[0]

    assert "require_system_manager()" in endpoint
    assert "KOPOS_DEVICE_API_ROLE in get_session_roles()" in endpoint
    assert "resolve_duplicate_automatic_qr_refund_payload" in endpoint
    assert "frappe.db.rollback()" in endpoint


def test_duplicate_qr_journal_entries_are_immutable_after_submission() -> None:
    parent_calls: list[str] = []

    class ParentJournalEntry:
        def before_cancel(self) -> None:
            parent_calls.append("parent")

    class ProtectedJournalEntry(
        journal_entry_extension.KoPOSJournalEntryIntegrityMixin,
        ParentJournalEntry,
    ):
        custom_kopos_qr_duplicate_key = "kopos:duplicate-qr-liability:v1:proof"

    with pytest.raises(
        journal_entry_extension.frappe.ValidationError,
        match="cannot be cancelled",
    ):
        ProtectedJournalEntry().before_cancel()
    assert parent_calls == []

    ordinary = ProtectedJournalEntry()
    ordinary.custom_kopos_qr_duplicate_key = None
    ordinary.before_cancel()
    assert parent_calls == ["parent"]

    hooks_source = (ROOT / "kopos_connector/hooks.py").read_text(encoding="utf-8")
    assert "KoPOSJournalEntryIntegrityMixin" in hooks_source


def test_original_kopos_invoice_cancel_requires_exact_consumed_void_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_calls: list[str] = []

    class ParentSalesInvoice:
        def before_cancel(self) -> None:
            parent_calls.append("parent")

    class ProtectedSalesInvoice(
        sales_invoice_extension.KoPOSSalesInvoiceIntegrityMixin,
        ParentSalesInvoice,
    ):
        name = "SINV-1"
        is_return = 0
        custom_fb_order = "FB-ORDER-1"
        custom_fb_idempotency_key = "SALE-IDEMPOTENCY-1"
        custom_fb_void_idempotency_key = None
        custom_fb_void_request_fingerprint = None
        custom_fb_void_manager = None
        custom_fb_void_approval_token_id = None
        company = "Test Company"
        currency = "MYR"

    invoice = ProtectedSalesInvoice()
    with pytest.raises(
        sales_invoice_extension.frappe.ValidationError,
        match="manager-approved void_order workflow",
    ):
        invoice.before_cancel()
    assert parent_calls == []

    invoice.custom_fb_void_idempotency_key = "VOID-IDEMPOTENCY-1"
    invoice.custom_fb_void_request_fingerprint = "d" * 64
    invoice.custom_fb_void_manager = "manager@example.test"
    invoice.custom_fb_void_approval_token_id = "APPROVAL-TOKEN-1"
    order = FakeDoc(
        docstatus=1,
        sales_invoice="SINV-1",
        external_idempotency_key="SALE-IDEMPOTENCY-1",
        company="Test Company",
        currency="MYR",
    )
    monkeypatch.setattr(
        sales_invoice_extension.frappe,
        "get_doc",
        lambda doctype, name: order
        if (doctype, name) == ("FB Order", "FB-ORDER-1")
        else None,
    )
    proof_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sales_invoice_extension,
        "_load_consumed_void_approval_proof",
        lambda **kwargs: proof_calls.append(kwargs) or {},
    )

    invoice.before_cancel()

    assert parent_calls == ["parent"]
    assert proof_calls == [
        {
            "approval_token_id": "APPROVAL-TOKEN-1",
            "approval_manager_id": "manager@example.test",
            "idempotency_key": "VOID-IDEMPOTENCY-1",
            "resource_id": "SINV-1",
        }
    ]
