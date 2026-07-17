from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
install_fake_frappe_modules()


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
class AccountingEnv:
    docs: dict[tuple[str, str], FakeDoc]
    invoice_gl: list[dict[str, Any]]
    journal_gl: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    journals_created: int = 0
    journal_inserts: int = 0
    journal_submits: int = 0
    failure_account_config: str | None = "QR Failure Variance - TC"
    default_cost_center: str | None = "Main - TC"
    source_updates: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)


@pytest.fixture
def accounting_env(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, AccountingEnv]:
    service = importlib.import_module(
        "kopos_connector.kopos.services.accounting.qr_reconciliation_service"
    )
    context_module = importlib.import_module(
        "kopos_connector.kopos.services.accounting._qr_reconciliation_context"
    )
    payment = FakeDoc(
        doctype="FB Order Payment",
        name="FB-PAY-1",
        source_payment_id="PAY-LOCAL-1",
        payment_method="DuitNow QR",
        payment_channel_code="maybank_qr",
        amount="12.00",
        reference_no="TXN-1",
        external_transaction_id="TXN-1",
        maybank_qr_transaction="MBQR-TXN-1",
        manual_qr_reconciliation=None,
        settlement_status="pending_reconciliation",
        suspense_account="Manual QR Suspense - TC",
    )
    order = FakeDoc(
        doctype="FB Order",
        name="FB-ORDER-1",
        docstatus=1,
        company="Test Company",
        currency="MYR",
        grand_total="12.00",
        sales_invoice="SINV-1",
        payments=[payment],
    )
    invoice_payment = FakeDoc(
        doctype="Sales Invoice Payment",
        custom_fb_source_payment_id="PAY-LOCAL-1",
        mode_of_payment="DuitNow QR",
        account="Manual QR Suspense - TC",
        amount="12.00",
    )
    invoice = FakeDoc(
        doctype="Sales Invoice",
        name="SINV-1",
        docstatus=1,
        is_return=0,
        company="Test Company",
        currency="MYR",
        conversion_rate=1,
        custom_fb_order="FB-ORDER-1",
        grand_total="12.00",
        rounded_total="12.00",
        write_off_amount="0.00",
        outstanding_amount="0.00",
        posting_date="2026-07-17",
        payments=[invoice_payment],
    )
    source = FakeDoc(
        doctype="Maybank QR Transaction",
        name="MBQR-TXN-1",
        transaction_refno="TXN-1",
        manual_reconciliation_status="pending_reconciliation",
        reconciliation_idempotency_key="qr-reconcile:PAY-LOCAL-1",
        reclassification_journal_entry=None,
        failure_journal_entry=None,
        failure_accounting_key=None,
        failure_variance_account=None,
        failure_cost_center=None,
        failure_accounting_reason=None,
        fb_order="FB-ORDER-1",
        sales_invoice="SINV-1",
        fb_order_payment="FB-PAY-1",
        company="Test Company",
        currency="MYR",
        suspense_account="Manual QR Suspense - TC",
        sale_amount_sen=1200,
        business_date="2026-07-17",
    )
    env = AccountingEnv(
        docs={
            (source.doctype, source.name): source,
            (order.doctype, order.name): order,
            (invoice.doctype, invoice.name): invoice,
        },
        invoice_gl=[
            {
                "account": "Manual QR Suspense - TC",
                "debit_in_account_currency": "12.00",
                "credit_in_account_currency": "0.00",
            }
        ],
    )

    accounts = {
        "Manual QR Suspense - TC": {
            "company": "Test Company",
            "is_group": 0,
            "disabled": 0,
            "root_type": "Asset",
            "account_type": "",
            "account_currency": "MYR",
        },
        "Maybank Clearing - TC": {
            "company": "Test Company",
            "is_group": 0,
            "disabled": 0,
            "root_type": "Asset",
            "account_type": "Bank",
            "account_currency": "MYR",
        },
        "QR Failure Variance - TC": {
            "company": "Test Company",
            "is_group": 0,
            "disabled": 0,
            "root_type": "Expense",
            "account_type": "Expense Account",
            "account_currency": "MYR",
        },
    }

    class FakeDB:
        def sql(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return []

        def savepoint(self, _name: str) -> None:
            return None

        def rollback(self, **_kwargs: Any) -> None:
            return None

        def get_value(
            self,
            doctype: str,
            name_or_filters: Any,
            fieldname: Any,
            *,
            as_dict: bool = False,
        ) -> Any:
            if (doctype, name_or_filters, fieldname) == (
                "Company",
                "Test Company",
                "default_currency",
            ):
                return "MYR"
            if (doctype, name_or_filters, fieldname) == (
                "Company",
                "Test Company",
                service.COMPANY_FAILURE_ACCOUNT_FIELD,
            ):
                return env.failure_account_config
            if (doctype, name_or_filters, fieldname) == (
                "Company",
                "Test Company",
                "cost_center",
            ):
                return env.default_cost_center
            if (
                doctype == "Cost Center"
                and name_or_filters == "Main - TC"
                and as_dict
            ):
                return {
                    "company": "Test Company",
                    "is_group": 0,
                    "disabled": 0,
                }
            if doctype == "Account" and isinstance(name_or_filters, str):
                row = accounts.get(name_or_filters)
                if as_dict:
                    return row
                return row.get(str(fieldname)) if row else None
            if doctype == "Journal Entry" and isinstance(name_or_filters, dict):
                key_field = next(iter(name_or_filters))
                key = name_or_filters[key_field]
                for (row_doctype, row_name), doc in env.docs.items():
                    if (
                        row_doctype == "Journal Entry"
                        and getattr(doc, key_field, None) == key
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
            fieldname_or_values: Any,
            value: Any = None,
            **_kwargs: Any,
        ) -> None:
            values = (
                dict(fieldname_or_values)
                if isinstance(fieldname_or_values, dict)
                else {str(fieldname_or_values): value}
            )
            doc = env.docs[(doctype, name)]
            for fieldname, field_value in values.items():
                setattr(doc, fieldname, field_value)
            env.source_updates.append((doctype, name, values))

    def get_doc(doctype: str, name: str) -> FakeDoc:
        return env.docs[(doctype, name)]

    def get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert doctype == "GL Entry"
        filters = kwargs["filters"]
        if filters["voucher_type"] == "Sales Invoice":
            return list(env.invoice_gl)
        return list(env.journal_gl.get(filters["voucher_no"], []))

    def new_doc(doctype: str) -> FakeDoc:
        assert doctype == "Journal Entry"
        env.journals_created += 1
        journal_name = f"JV-QR-{env.journals_created}"
        journal = FakeDoc(
            doctype="Journal Entry",
            name=journal_name,
            docstatus=0,
            accounts=[],
        )

        def insert(ignore_permissions: bool = False) -> FakeDoc:
            assert ignore_permissions is True
            env.journal_inserts += 1
            env.docs[("Journal Entry", journal_name)] = journal
            return journal

        def submit() -> FakeDoc:
            env.journal_submits += 1
            journal.docstatus = 1
            env.journal_gl[journal_name] = [
                {
                    "account": row.account,
                    "debit_in_account_currency": row.debit_in_account_currency,
                    "credit_in_account_currency": row.credit_in_account_currency,
                    "cost_center": getattr(row, "cost_center", None),
                }
                for row in journal.accounts
            ]
            return journal

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
        lambda doctype: SimpleNamespace(
            has_field=lambda fieldname: (
                fieldname
                in (
                    service.REQUIRED_JOURNAL_FIELDS
                    + service.REQUIRED_FAILURE_JOURNAL_FIELDS
                    + service.REQUIRED_FAILURE_RECOVERY_JOURNAL_FIELDS
                )
                if doctype == "Journal Entry"
                else fieldname == service.COMPANY_FAILURE_ACCOUNT_FIELD
                if doctype == "Company"
                else fieldname in service.REQUIRED_FAILURE_SOURCE_FIELDS
                if doctype
                in {"Maybank QR Transaction", "Manual QR Reconciliation"}
                else False
            )
        ),
    )
    monkeypatch.setattr(
        context_module,
        "_configured_mode_of_payment_account",
        lambda mode_of_payment, company: "Maybank Clearing - TC",
    )
    return service, env


def test_posts_one_exact_idempotent_suspense_to_bank_journal(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    order = env.docs[("FB Order", "FB-ORDER-1")]
    invoice = env.docs[("Sales Invoice", "SINV-1")]
    order_before = dict(vars(order))
    invoice_before = dict(vars(invoice))

    first = service.ensure_qr_suspense_reclassification(source)
    second = service.ensure_qr_suspense_reclassification(source)
    proven = service.assert_qr_suspense_reclassification(source)

    assert first == second == proven
    assert first == {
        "journal_entry": "JV-QR-1",
        "reconciliation_key": "qr-reconcile:PAY-LOCAL-1",
        "source_doctype": "Maybank QR Transaction",
        "source_name": "MBQR-TXN-1",
        "fb_order": "FB-ORDER-1",
        "sales_invoice": "SINV-1",
        "amount_sen": 1200,
        "company": "Test Company",
        "currency": "MYR",
        "suspense_account": "Manual QR Suspense - TC",
        "target_account": "Maybank Clearing - TC",
    }
    assert env.journal_inserts == 1
    assert env.journal_submits == 1

    journal = env.docs[("Journal Entry", "JV-QR-1")]
    target, suspense = journal.accounts
    assert target.account == "Maybank Clearing - TC"
    assert target.debit_in_account_currency == Decimal("12")
    assert target.credit_in_account_currency == Decimal("0")
    assert suspense.account == "Manual QR Suspense - TC"
    assert suspense.debit_in_account_currency == Decimal("0")
    assert suspense.credit_in_account_currency == Decimal("12")
    assert journal.custom_kopos_qr_reconciliation_key == (
        "qr-reconcile:PAY-LOCAL-1"
    )
    assert journal.custom_kopos_qr_source_name == "MBQR-TXN-1"
    assert journal.custom_kopos_qr_fb_order == "FB-ORDER-1"
    assert journal.custom_kopos_qr_sales_invoice == "SINV-1"
    assert journal.custom_kopos_qr_amount_sen == 1200
    assert journal.custom_kopos_qr_currency == "MYR"
    assert vars(order) == order_before
    assert vars(invoice) == invoice_before


def test_failure_posts_one_exact_idempotent_suspense_to_variance_journal(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    order = env.docs[("FB Order", "FB-ORDER-1")]
    invoice = env.docs[("Sales Invoice", "SINV-1")]
    order_before = dict(vars(order))
    invoice_before = dict(vars(invoice))

    first = service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )
    second = service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )
    proven = service.assert_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )

    assert first == second == proven
    assert first["journal_entry"] == "JV-QR-1"
    assert first["disposition"] == "reconciliation_failed"
    assert first["failure_reason"] == "no_bank_transaction"
    assert first["target_account"] == "QR Failure Variance - TC"
    assert first["reconciliation_key"].startswith("kopos:qr-failure:v1:")
    assert len(first["reconciliation_key"]) == len(
        "kopos:qr-failure:v1:"
    ) + 64
    assert env.journal_inserts == 1
    assert env.journal_submits == 1

    journal = env.docs[("Journal Entry", "JV-QR-1")]
    target, suspense = journal.accounts
    assert target.account == "QR Failure Variance - TC"
    assert target.debit_in_account_currency == Decimal("12")
    assert target.credit_in_account_currency == Decimal("0")
    assert target.cost_center == "Main - TC"
    assert suspense.account == "Manual QR Suspense - TC"
    assert suspense.debit_in_account_currency == Decimal("0")
    assert suspense.credit_in_account_currency == Decimal("12")
    assert journal.custom_kopos_qr_failure_key == first["reconciliation_key"]
    assert getattr(journal, "custom_kopos_qr_reconciliation_key", None) is None
    assert journal.custom_kopos_qr_disposition == "reconciliation_failed"
    assert journal.custom_kopos_qr_fb_order_payment == "FB-PAY-1"
    assert journal.custom_kopos_qr_target_account == "QR Failure Variance - TC"
    assert journal.custom_kopos_qr_cost_center == "Main - TC"
    assert journal.custom_kopos_qr_failure_reason == "no_bank_transaction"
    assert source.failure_accounting_key == first["reconciliation_key"]
    assert source.failure_variance_account == "QR Failure Variance - TC"
    assert source.failure_cost_center == "Main - TC"
    assert source.failure_accounting_reason == "no_bank_transaction"
    assert source.failure_journal_entry == "JV-QR-1"
    assert vars(order) == order_before
    assert vars(invoice) == invoice_before


def test_failure_disposition_never_reopens_or_duplicates_submitted_sale(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    order = env.docs[("FB Order", "FB-ORDER-1")]
    invoice = env.docs[("Sales Invoice", "SINV-1")]
    sale_doc_keys_before = {
        key for key in env.docs if key[0] in {"FB Order", "Sales Invoice"}
    }

    service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )

    assert order.docstatus == 1
    assert invoice.docstatus == 1
    assert order.sales_invoice == invoice.name
    assert invoice.custom_fb_order == order.name
    assert order.payments[0].settlement_status == "pending_reconciliation"
    assert source.manual_reconciliation_status == "pending_reconciliation"
    assert {
        key for key in env.docs if key[0] in {"FB Order", "Sales Invoice"}
    } == sale_doc_keys_before


def test_failure_reason_change_cannot_reuse_existing_accounting_disposition(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )

    with pytest.raises(service.frappe.ValidationError, match="does not match"):
        service.ensure_qr_suspense_failure_reclassification(source, "duplicate")

    assert env.journal_inserts == 1
    assert env.journal_submits == 1


def test_failure_stays_pending_without_company_variance_account(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    env.failure_account_config = None
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]

    with pytest.raises(
        service.frappe.ValidationError,
        match="requires custom_kopos_qr_failure_variance_account",
    ):
        service.ensure_qr_suspense_failure_reclassification(
            source,
            "no_bank_transaction",
        )

    assert source.manual_reconciliation_status == "pending_reconciliation"
    assert source.failure_accounting_key is None
    assert source.failure_journal_entry is None
    assert env.journal_inserts == 0
    assert env.journal_submits == 0


def test_failure_rejects_non_expense_target_account(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    env.failure_account_config = "Maybank Clearing - TC"
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]

    with pytest.raises(
        service.frappe.ValidationError,
        match="must be an Expense ledger",
    ):
        service.ensure_qr_suspense_failure_reclassification(
            source,
            "no_bank_transaction",
        )

    assert source.manual_reconciliation_status == "pending_reconciliation"
    assert source.failure_journal_entry is None
    assert env.journal_inserts == 0


def test_failure_stays_pending_without_company_default_cost_center(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    env.default_cost_center = None
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]

    with pytest.raises(
        service.frappe.ValidationError,
        match="requires a default Cost Center",
    ):
        service.ensure_qr_suspense_failure_reclassification(
            source,
            "no_bank_transaction",
        )

    assert source.manual_reconciliation_status == "pending_reconciliation"
    assert source.failure_journal_entry is None
    assert env.journal_inserts == 0


def test_failure_replay_uses_historical_account_and_cost_center_snapshots(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    first = service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )
    env.failure_account_config = "Maybank Clearing - TC"
    env.default_cost_center = None

    replay = service.assert_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )

    assert replay == first
    assert replay["target_account"] == "QR Failure Variance - TC"
    assert replay["cost_center"] == "Main - TC"


def test_late_provider_payment_reclassifies_variance_to_bank_not_suspense(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    order = env.docs[("FB Order", "FB-ORDER-1")]
    invoice = env.docs[("Sales Invoice", "SINV-1")]
    sale_doc_keys_before = {
        key for key in env.docs if key[0] in {"FB Order", "Sales Invoice"}
    }
    failure = service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )
    source.status = "paid"
    source.manual_reconciliation_status = "pending_reconciliation"
    source.reconciliation_failed_reason = None
    order.payments[0].settlement_status = "pending_reconciliation"

    recovered = service.ensure_qr_suspense_reclassification(source)
    replay = service.ensure_qr_suspense_reclassification(source)

    assert recovered == replay
    assert recovered["journal_entry"] == "JV-QR-2"
    assert recovered["target_account"] == "Maybank Clearing - TC"
    assert recovered["source_account"] == "QR Failure Variance - TC"
    assert recovered["prior_failure_journal_entry"] == failure["journal_entry"]
    assert recovered["cost_center"] == "Main - TC"
    recovery_journal = env.docs[("Journal Entry", "JV-QR-2")]
    bank, variance = recovery_journal.accounts
    assert bank.account == "Maybank Clearing - TC"
    assert bank.debit_in_account_currency == Decimal("12")
    assert variance.account == "QR Failure Variance - TC"
    assert variance.credit_in_account_currency == Decimal("12")
    assert variance.cost_center == "Main - TC"
    assert recovery_journal.custom_kopos_qr_source_account == (
        "QR Failure Variance - TC"
    )
    assert recovery_journal.custom_kopos_qr_prior_failure_journal == "JV-QR-1"
    assert recovery_journal.custom_kopos_qr_cost_center == "Main - TC"
    assert env.journal_inserts == 2
    assert env.journal_submits == 2
    assert order.docstatus == 1
    assert invoice.docstatus == 1
    assert {
        key for key in env.docs if key[0] in {"FB Order", "Sales Invoice"}
    } == sale_doc_keys_before


def test_late_provider_payment_refuses_unproven_failure_journal(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    order = env.docs[("FB Order", "FB-ORDER-1")]
    service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )
    source.status = "paid"
    source.manual_reconciliation_status = "pending_reconciliation"
    source.reconciliation_failed_reason = None
    order.payments[0].settlement_status = "pending_reconciliation"
    env.journal_gl["JV-QR-1"][0]["debit_in_account_currency"] = "11.00"

    with pytest.raises(
        service.frappe.ValidationError,
        match="does not exactly debit",
    ):
        service.ensure_qr_suspense_reclassification(source)

    assert env.journal_inserts == 1
    assert env.journal_submits == 1


@pytest.mark.parametrize(
    ("fieldname", "bad_value"),
    [
        ("custom_kopos_qr_failure_key", "kopos:qr-failure:v1:wrong"),
        ("custom_kopos_qr_disposition", "reconciled"),
        ("custom_kopos_qr_fb_order_payment", "FB-PAY-OTHER"),
        ("custom_kopos_qr_target_account", "Other Expense - TC"),
        ("custom_kopos_qr_cost_center", "Other - TC"),
        ("custom_kopos_qr_failure_reason", "duplicate"),
        ("custom_kopos_qr_source_name", "MBQR-TXN-OTHER"),
    ],
)
def test_failure_replay_rejects_changed_journal_identity_or_reason(
    accounting_env: tuple[Any, AccountingEnv],
    fieldname: str,
    bad_value: str,
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )
    setattr(env.docs[("Journal Entry", "JV-QR-1")], fieldname, bad_value)

    with pytest.raises(service.frappe.ValidationError, match="does not match"):
        service.assert_qr_suspense_failure_reclassification(
            source,
            "no_bank_transaction",
        )


def test_terminal_failure_replay_requires_exact_submitted_gl(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )
    source.manual_reconciliation_status = "reconciliation_failed"
    source.reconciliation_failed_reason = "no_bank_transaction"
    env.docs[("FB Order", "FB-ORDER-1")].payments[0].settlement_status = (
        "reconciliation_failed"
    )
    env.journal_gl["JV-QR-1"][0]["debit_in_account_currency"] = "11.00"

    with pytest.raises(
        service.frappe.ValidationError,
        match="does not exactly debit",
    ):
        service.assert_qr_suspense_failure_reclassification(
            source,
            "no_bank_transaction",
        )


def test_failure_replay_rejects_changed_submitted_gl_cost_center(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )
    env.journal_gl["JV-QR-1"][0]["cost_center"] = "Other - TC"

    with pytest.raises(
        service.frappe.ValidationError,
        match="Cost Center does not match",
    ):
        service.assert_qr_suspense_failure_reclassification(
            source,
            "no_bank_transaction",
        )


def test_failure_key_namespace_cannot_recover_success_reconciliation_journal(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    context = service._build_context(
        source,
        disposition="reconciliation_failed",
        failure_reason="no_bank_transaction",
    )
    env.docs[("Journal Entry", "JV-SUCCESS-NAMESPACE-COLLISION")] = FakeDoc(
        doctype="Journal Entry",
        name="JV-SUCCESS-NAMESPACE-COLLISION",
        docstatus=1,
        custom_kopos_qr_reconciliation_key=context["reconciliation_key"],
        custom_kopos_qr_failure_key=None,
    )

    result = service.ensure_qr_suspense_failure_reclassification(
        source,
        "no_bank_transaction",
    )

    assert result["journal_entry"] == "JV-QR-1"
    assert source.failure_journal_entry == "JV-QR-1"
    assert env.docs[("Journal Entry", "JV-SUCCESS-NAMESPACE-COLLISION")].docstatus == 1


@pytest.mark.parametrize(
    ("fieldname", "bad_value"),
    [
        ("custom_kopos_qr_amount_sen", 1300),
        ("company", "Another Company"),
        ("custom_kopos_qr_currency", "USD"),
        ("custom_kopos_qr_fb_order", "FB-ORDER-OTHER"),
        ("custom_kopos_qr_sales_invoice", "SINV-OTHER"),
        ("custom_kopos_qr_source_name", "MBQR-TXN-OTHER"),
    ],
)
def test_replay_rejects_mismatched_amount_company_currency_order_invoice_or_transaction(
    accounting_env: tuple[Any, AccountingEnv],
    fieldname: str,
    bad_value: Any,
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    service.ensure_qr_suspense_reclassification(source)
    journal = env.docs[("Journal Entry", "JV-QR-1")]
    setattr(journal, fieldname, bad_value)

    with pytest.raises(service.frappe.ValidationError, match="does not match"):
        service.assert_qr_suspense_reclassification(source)

    assert env.journal_inserts == 1
    assert env.journal_submits == 1


def test_refuses_reclassification_without_exact_invoice_suspense_gl(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    env.invoice_gl = []
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]

    with pytest.raises(
        service.frappe.ValidationError, match="no posted suspense GL receipt"
    ):
        service.ensure_qr_suspense_reclassification(source)

    assert env.journal_inserts == 0
    assert env.journal_submits == 0


@pytest.mark.parametrize(
    ("invoice_grand_total", "write_off_amount"),
    [
        ("12.03", "0.03"),
        ("11.98", "-0.02"),
    ],
)
def test_reconciliation_uses_explicit_pos_write_off_payable_total(
    accounting_env: tuple[Any, AccountingEnv],
    invoice_grand_total: str,
    write_off_amount: str,
) -> None:
    service, env = accounting_env
    invoice = env.docs[("Sales Invoice", "SINV-1")]
    invoice.grand_total = invoice_grand_total
    invoice.rounded_total = "0.00"
    invoice.write_off_amount = write_off_amount
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]

    result = service.ensure_qr_suspense_reclassification(source)

    assert result["amount_sen"] == 1200
    assert result["journal_entry"] == "JV-QR-1"


def test_replay_rejects_changed_gl_even_when_journal_metadata_matches(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    source = env.docs[("Maybank QR Transaction", "MBQR-TXN-1")]
    service.ensure_qr_suspense_reclassification(source)
    env.journal_gl["JV-QR-1"][0]["debit_in_account_currency"] = "11.00"

    with pytest.raises(
        service.frappe.ValidationError, match="does not exactly debit"
    ):
        service.assert_qr_suspense_reclassification(source)


def test_static_qr_source_uses_same_accounting_invariant(
    accounting_env: tuple[Any, AccountingEnv],
) -> None:
    service, env = accounting_env
    dynamic_source = env.docs.pop(("Maybank QR Transaction", "MBQR-TXN-1"))
    static_source = FakeDoc(
        **{
            **vars(dynamic_source),
            "doctype": "Manual QR Reconciliation",
            "name": "MQR-STATIC-1",
            "status": "pending_reconciliation",
            "provider_session_id": "TXN-1",
            "amount_sen": 1200,
        }
    )
    env.docs[(static_source.doctype, static_source.name)] = static_source
    order = env.docs[("FB Order", "FB-ORDER-1")]
    payment = order.payments[0]
    payment.maybank_qr_transaction = None
    payment.manual_qr_reconciliation = static_source.name

    result = service.ensure_qr_suspense_reclassification(static_source)

    assert result["source_doctype"] == "Manual QR Reconciliation"
    assert result["source_name"] == "MQR-STATIC-1"
    assert result["journal_entry"] == "JV-QR-1"


def test_reconciliation_accounting_fields_are_installed_and_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_module = importlib.import_module(
        "kopos_connector.kopos.install.fb_custom_fields"
    )
    captured: dict[str, list[dict[str, Any]]] = {}
    monkeypatch.setattr(
        install_module,
        "create_custom_fields",
        lambda fields: captured.update(fields),
    )

    install_module.create_fb_custom_fields()

    journal_fields = {
        row["fieldname"]: row for row in captured["Journal Entry"]
    }
    expected = {
        "custom_kopos_qr_reconciliation_key",
        "custom_kopos_qr_source_doctype",
        "custom_kopos_qr_source_name",
        "custom_kopos_qr_fb_order",
        "custom_kopos_qr_sales_invoice",
        "custom_kopos_qr_amount_sen",
        "custom_kopos_qr_currency",
        "custom_kopos_qr_failure_key",
        "custom_kopos_qr_disposition",
        "custom_kopos_qr_fb_order_payment",
        "custom_kopos_qr_target_account",
        "custom_kopos_qr_cost_center",
        "custom_kopos_qr_source_account",
        "custom_kopos_qr_prior_failure_journal",
        "custom_kopos_qr_failure_reason",
    }
    assert expected <= journal_fields.keys()
    assert journal_fields["custom_kopos_qr_reconciliation_key"]["unique"] == 1
    assert journal_fields["custom_kopos_qr_failure_key"]["unique"] == 1
    assert journal_fields["custom_kopos_qr_source_name"]["fieldtype"] == (
        "Dynamic Link"
    )
    assert journal_fields["custom_kopos_qr_source_name"]["options"] == (
        "custom_kopos_qr_source_doctype"
    )
    assert captured["Company"][0]["fieldname"] == (
        "custom_kopos_qr_failure_variance_account"
    )
    assert captured["Company"][0]["options"] == "Account"
    assert journal_fields["custom_kopos_qr_target_account"]["options"] == "Account"
    assert journal_fields["custom_kopos_qr_cost_center"]["options"] == "Cost Center"
    assert journal_fields["custom_kopos_qr_source_account"]["options"] == "Account"
    assert journal_fields["custom_kopos_qr_prior_failure_journal"]["options"] == (
        "Journal Entry"
    )

    root = Path(__file__).resolve().parents[1]
    maybank_schema = json.loads(
        (
            root
            / "kopos_connector/kopos/doctype/maybank_qr_transaction/maybank_qr_transaction.json"
        ).read_text()
    )
    manual_schema = json.loads(
        (
            root
            / "kopos_connector/kopos/doctype/manual_qr_reconciliation/manual_qr_reconciliation.json"
        ).read_text()
    )
    maybank_fields = {row["fieldname"]: row for row in maybank_schema["fields"]}
    manual_fields = {row["fieldname"]: row for row in manual_schema["fields"]}

    assert maybank_fields["company"]["options"] == "Company"
    assert maybank_fields["suspense_account"]["options"] == "Account"
    assert maybank_fields["reclassification_journal_entry"]["options"] == (
        "Journal Entry"
    )
    assert manual_fields["reclassification_journal_entry"]["options"] == (
        "Journal Entry"
    )
    for fields in (maybank_fields, manual_fields):
        assert fields["failure_journal_entry"]["options"] == "Journal Entry"
        assert fields["failure_accounting_key"]["unique"] == 1
        assert fields["failure_variance_account"]["options"] == "Account"
        assert fields["failure_cost_center"]["options"] == "Cost Center"
        assert "no_bank_transaction" in fields["failure_accounting_reason"][
            "options"
        ]
