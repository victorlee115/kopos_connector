from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()


class FakeDoc(SimpleNamespace):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.insert_count = 0
        self.submit_count = 0
        self.db_updates: list[tuple[str, Any]] = []
        self._submit_hook: Callable[[], None] | None = None

    def get(self, key: str) -> Any:
        return getattr(self, key, None)

    def set(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def append(self, key: str, row: dict[str, Any]) -> FakeDoc:
        child = FakeDoc(**row)
        rows = list(getattr(self, key, []) or [])
        rows.append(child)
        setattr(self, key, rows)
        return child

    def insert(self, ignore_permissions: bool = False) -> FakeDoc:
        self.insert_count += 1
        return self

    def submit(self) -> FakeDoc:
        self.submit_count += 1
        self.docstatus = 1
        if self._submit_hook:
            self._submit_hook()
        return self

    def db_set(
        self,
        fieldname: str,
        value: Any,
        update_modified: bool = True,
    ) -> None:
        setattr(self, fieldname, value)
        self.db_updates.append((fieldname, value))

    def reload(self) -> FakeDoc:
        return self


def _settlement_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_settlement_gl: bool = True,
    payment_rows: list[FakeDoc] | None = None,
) -> tuple[Any, FakeDoc, FakeDoc, FakeDoc, FakeDoc]:
    service = importlib.import_module(
        "kopos_connector.kopos.services.accounting.return_settlement_service"
    )
    original_invoice = FakeDoc(
        doctype="Sales Invoice",
        name="SINV-1",
        docstatus=1,
        is_return=0,
        grand_total="12.00",
        outstanding_amount="0.00",
        company="Company A",
        currency="MYR",
        conversion_rate=1,
        customer="Walk-in Customer",
        debit_to="Debtors - CO",
        payments=payment_rows
        or [
            FakeDoc(
                mode_of_payment="Cash",
                type="Cash",
                account="Cash - CO",
                amount="12.00",
                base_amount="12.00",
            )
        ],
    )
    return_invoice = FakeDoc(
        doctype="Sales Invoice",
        name="SINV-RETURN-1",
        docstatus=1,
        is_return=1,
        return_against="SINV-1",
        grand_total="-12.00",
        outstanding_amount="-12.00",
        company="Company A",
        currency="MYR",
        conversion_rate=1,
        customer="Walk-in Customer",
        debit_to="Debtors - CO",
        posting_date="2026-07-10",
    )
    return_event = FakeDoc(
        doctype="FB Return Event",
        name="FB-RETURN-1",
        original_sales_invoice="SINV-1",
        return_sales_invoice="SINV-RETURN-1",
        refund_method="cash",
        request_fingerprint="f" * 64,
        settlement_doctype=None,
        settlement_document=None,
        settlement_status="Pending",
    )
    journal = FakeDoc(
        doctype="Journal Entry",
        name="JV-REFUND-1",
        docstatus=0,
        accounts=[],
    )
    journal._submit_hook = lambda: setattr(
        return_invoice, "outstanding_amount", "0.00"
    )

    class FakeDB:
        def get_value(self, doctype: str, name_or_filters: Any, fieldname: str) -> Any:
            if (doctype, name_or_filters, fieldname) == (
                "Company",
                "Company A",
                "default_currency",
            ):
                return "MYR"
            if (doctype, name_or_filters, fieldname) == (
                "Account",
                "Cash - CO",
                "account_type",
            ):
                return "Cash"
            if doctype == "Journal Entry":
                return None
            raise AssertionError(
                f"unexpected get_value({doctype!r}, {name_or_filters!r}, {fieldname!r})"
            )

    def get_doc(doctype: str, name: str) -> FakeDoc:
        docs = {
            ("Sales Invoice", "SINV-1"): original_invoice,
            ("Sales Invoice", "SINV-RETURN-1"): return_invoice,
            ("FB Return Event", "FB-RETURN-1"): return_event,
            ("Journal Entry", "JV-REFUND-1"): journal,
        }
        return docs[(doctype, name)]

    def get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        if doctype != "GL Entry":
            return []
        filters = kwargs["filters"]
        if filters["voucher_type"] == "Sales Invoice":
            return [
                {
                    "account": "Cash - CO",
                    "debit_in_account_currency": "12.00",
                    "credit_in_account_currency": "0.00",
                }
            ]
        if not include_settlement_gl or journal.docstatus != 1:
            return []
        return [
            {
                "account": "Cash - CO",
                "party_type": None,
                "party": None,
                "debit_in_account_currency": "0.00",
                "credit_in_account_currency": "12.00",
                "against_voucher_type": None,
                "against_voucher": None,
            },
            {
                "account": "Debtors - CO",
                "party_type": "Customer",
                "party": "Walk-in Customer",
                "debit_in_account_currency": "12.00",
                "credit_in_account_currency": "0.00",
                "against_voucher_type": "Sales Invoice",
                "against_voucher": "SINV-RETURN-1",
            },
        ]

    monkeypatch.setattr(service.frappe, "db", FakeDB())
    monkeypatch.setattr(service.frappe, "get_doc", get_doc)
    monkeypatch.setattr(service.frappe, "get_all", get_all)
    monkeypatch.setattr(service.frappe, "new_doc", lambda doctype: journal)
    monkeypatch.setattr(
        service.frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(has_field=lambda fieldname: True),
    )
    return service, original_invoice, return_invoice, return_event, journal


def test_full_cash_refund_posts_one_idempotent_journal_with_exact_gl_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, return_invoice, return_event, journal = _settlement_fixture(monkeypatch)

    first = service.ensure_return_settlement(return_event, return_invoice.name)
    second = service.ensure_return_settlement(return_event, return_invoice.name)
    evidence = service.assert_return_settlement_posted(return_event)

    assert first == "JV-REFUND-1"
    assert second == first
    assert journal.insert_count == 1
    assert journal.submit_count == 1
    assert return_event.settlement_doctype == "Journal Entry"
    assert return_event.settlement_document == "JV-REFUND-1"
    assert return_event.settlement_status == "Posted"
    assert evidence["settlement_amount_sen"] == 1200
    assert evidence["tender_credit_sen"] == 1200
    assert evidence["customer_debit_sen"] == 1200
    assert evidence["return_outstanding_sen"] == 0
    assert service.get_settlement_cash_adjustment_sen(return_event) == -1200

    receivable, tender = journal.accounts
    assert receivable.account == "Debtors - CO"
    assert receivable.party_type == "Customer"
    assert receivable.reference_type == "Sales Invoice"
    assert receivable.reference_name == "SINV-RETURN-1"
    assert receivable.debit_in_account_currency == service.Decimal("12")
    assert tender.account == "Cash - CO"
    assert tender.credit_in_account_currency == service.Decimal("12")
    provenance = json.loads(return_event.settlement_tenders_json)
    assert provenance["settlement_amount_sen"] == 1200
    assert provenance["tenders"] == [
        {"account": "Cash - CO", "amount_sen": 1200, "refund_method": "cash"}
    ]


def test_original_tender_proof_matches_erpnext_v16_pos_gl_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, original, _, _, _ = _settlement_fixture(monkeypatch)
    observed_filters: list[dict[str, Any]] = []
    voucher_rows = [
        {
            "name": "GL-DEBTOR-DEBIT",
            "account": "Debtors - CO",
            "account_currency": "MYR",
            "party_type": "Customer",
            "party": "Walk-in Customer",
            "debit": "12.00",
            "credit": "0.00",
            "debit_in_account_currency": "12.00",
            "credit_in_account_currency": "0.00",
            "against": "Sales - CO",
            "against_voucher_type": "Sales Invoice",
            "against_voucher": "SINV-1",
        },
        {
            "name": "GL-SALES-CREDIT",
            "account": "Sales - CO",
            "account_currency": "MYR",
            "party_type": None,
            "party": None,
            "debit": "0.00",
            "credit": "12.00",
            "debit_in_account_currency": "0.00",
            "credit_in_account_currency": "12.00",
            "against": "Walk-in Customer",
            "against_voucher_type": None,
            "against_voucher": None,
        },
        {
            "name": "GL-DEBTOR-CREDIT",
            "account": "Debtors - CO",
            "account_currency": "MYR",
            "party_type": "Customer",
            "party": "Walk-in Customer",
            "debit": "0.00",
            "credit": "12.00",
            "debit_in_account_currency": "0.00",
            "credit_in_account_currency": "12.00",
            "against": "Cash - CO",
            "against_voucher_type": "Sales Invoice",
            "against_voucher": "SINV-1",
        },
        {
            "name": "GL-CASH-DEBIT",
            "account": "Cash - CO",
            "account_currency": "MYR",
            "party_type": None,
            "party": None,
            "debit": "12.00",
            "credit": "0.00",
            "debit_in_account_currency": "12.00",
            "credit_in_account_currency": "0.00",
            "against": "Walk-in Customer",
            "against_voucher_type": None,
            "against_voucher": None,
        },
    ]

    def get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert doctype == "GL Entry"
        filters = kwargs["filters"]
        observed_filters.append(filters)
        account_filter = filters["account"]
        assert account_filter[0] == "in"
        allowed_accounts = set(account_filter[1])
        return [row for row in voucher_rows if row["account"] in allowed_accounts]

    monkeypatch.setattr(service.frappe, "get_all", get_all)

    tenders = service._resolve_original_tenders(original, "cash", 1200)

    assert observed_filters == [
        {
            "voucher_type": "Sales Invoice",
            "voucher_no": "SINV-1",
            "is_cancelled": 0,
            "account": ["in", ["Cash - CO"]],
        }
    ]
    assert tenders == [
        {"account": "Cash - CO", "refund_method": "cash", "amount_sen": 1200}
    ]


def test_original_tender_mismatch_reports_exact_observed_sen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, original, _, _, _ = _settlement_fixture(monkeypatch)

    monkeypatch.setattr(
        service.frappe,
        "get_all",
        lambda doctype, **kwargs: [
            {
                "account": "Cash - CO",
                "debit": "11.99",
                "credit": "0.00",
                "debit_in_account_currency": "11.99",
                "credit_in_account_currency": "0.00",
            }
        ],
    )

    with pytest.raises(
        service.frappe.ValidationError,
        match=(
            r"SINV-1: expected 1200 sen, observed 1199 sen across "
            r'\{"Cash - CO":1199\}'
        ),
    ):
        service._resolve_original_tenders(original, "cash", 1200)


def test_settlement_fails_closed_without_submitted_gl_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, return_invoice, return_event, journal = _settlement_fixture(
        monkeypatch, include_settlement_gl=False
    )

    with pytest.raises(
        service.frappe.ValidationError, match="has no submitted GL evidence"
    ):
        service.ensure_return_settlement(return_event, return_invoice.name)

    assert journal.submit_count == 1
    assert return_event.settlement_status == "Pending"
    assert return_event.settlement_document is None


def test_settlement_rejects_extra_or_gross_gl_postings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, return_invoice, return_event, _ = _settlement_fixture(monkeypatch)
    original_get_all = service.frappe.get_all

    def get_all_with_unrelated_posting(
        doctype: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        rows = original_get_all(doctype, **kwargs)
        if doctype == "GL Entry" and kwargs["filters"]["voucher_type"] == "Journal Entry":
            rows.append(
                {
                    "account": "Suspense - CO",
                    "debit_in_account_currency": "1.00",
                    "credit_in_account_currency": "0.00",
                }
            )
        return rows

    monkeypatch.setattr(service.frappe, "get_all", get_all_with_unrelated_posting)
    with pytest.raises(service.frappe.ValidationError, match="unexpected GL posting"):
        service.ensure_return_settlement(return_event, return_invoice.name)

    service, _, return_invoice, return_event, _ = _settlement_fixture(monkeypatch)
    original_get_all = service.frappe.get_all

    def get_all_with_tender_churn(
        doctype: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        rows = original_get_all(doctype, **kwargs)
        if doctype == "GL Entry" and kwargs["filters"]["voucher_type"] == "Journal Entry":
            rows[0]["debit_in_account_currency"] = "1.00"
            rows[0]["credit_in_account_currency"] = "13.00"
        return rows

    monkeypatch.setattr(service.frappe, "get_all", get_all_with_tender_churn)
    with pytest.raises(service.frappe.ValidationError, match="pure credit"):
        service.ensure_return_settlement(return_event, return_invoice.name)


def test_mixed_tender_and_fractional_sen_refunds_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, original, _, _, _ = _settlement_fixture(
        monkeypatch,
        payment_rows=[
            FakeDoc(
                mode_of_payment="Cash",
                type="Cash",
                account="Cash - CO",
                amount="6.00",
            ),
            FakeDoc(
                mode_of_payment="Credit Card",
                type="Bank",
                account="Card Clearing - CO",
                amount="6.00",
            ),
        ],
    )

    with pytest.raises(
        service.frappe.ValidationError,
        match="does not exactly match the original tender mix",
    ):
        service._resolve_original_tenders(original, "cash", 1200)
    with pytest.raises(service.frappe.ValidationError, match="fractional sen"):
        service._money_to_sen("0.001", "refund amount")


def test_settlement_rejects_mismatched_credit_note_context_and_tender_account_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, original, return_invoice, _, _ = _settlement_fixture(monkeypatch)
    return_invoice.company = "Company B"
    with pytest.raises(service.frappe.ValidationError, match="mismatched company"):
        service._validate_full_return_invoice(original, return_invoice)

    return_invoice.company = "Company A"
    existing_get_value = service.frappe.db.get_value

    def get_value(doctype: str, name_or_filters: Any, fieldname: str) -> Any:
        if (doctype, name_or_filters, fieldname) == (
            "Account",
            "Cash - CO",
            "account_type",
        ):
            return "Bank"
        return existing_get_value(doctype, name_or_filters, fieldname)

    monkeypatch.setattr(service.frappe.db, "get_value", get_value)
    with pytest.raises(service.frappe.ValidationError, match="must use a Cash account"):
        service._resolve_original_tenders(original, "cash", 1200)


def test_settlement_uses_rounded_payable_total_without_losing_grand_total_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, original, return_invoice, _, _ = _settlement_fixture(monkeypatch)
    original.grand_total = "12.03"
    original.rounded_total = "12.05"
    return_invoice.grand_total = "-12.03"
    return_invoice.rounded_total = "-12.05"

    assert service._validate_full_return_invoice(original, return_invoice) == 1205

    return_invoice.grand_total = "-12.02"
    with pytest.raises(
        service.frappe.ValidationError,
        match="must exactly reverse the original invoice",
    ):
        service._validate_full_return_invoice(original, return_invoice)


@pytest.mark.parametrize(
    ("original_write_off", "return_write_off", "expected_sen"),
    [
        ("0.01", "-0.01", 1202),
        ("-0.01", "0.01", 1204),
    ],
)
def test_settlement_uses_pos_write_off_payable_total_for_both_rounding_signs(
    monkeypatch: pytest.MonkeyPatch,
    original_write_off: str,
    return_write_off: str,
    expected_sen: int,
) -> None:
    service, original, return_invoice, _, _ = _settlement_fixture(monkeypatch)
    original.grand_total = "12.03"
    original.rounded_total = "0.00"
    original.write_off_amount = original_write_off
    return_invoice.grand_total = "-12.03"
    return_invoice.rounded_total = "0.00"
    return_invoice.write_off_amount = return_write_off

    assert service._validate_full_return_invoice(original, return_invoice) == expected_sen


def test_return_guard_uses_two_current_locks_and_aggregates_duplicate_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = importlib.import_module(
        "kopos_connector.kopos.services.operations.return_guard_service"
    )
    calls: list[tuple[str, tuple[str, ...]]] = []

    class FakeDB:
        submitted_rows: list[dict[str, Any]] = []

        def sql(
            self,
            query: str,
            values: tuple[str, ...],
            as_dict: bool = False,
        ) -> list[dict[str, Any]]:
            assert as_dict is True
            calls.append((query, values))
            if "FROM `tabFB Resolved Sale`" in query:
                return [{"name": "RS-1", "qty": "2"}]
            return list(self.submitted_rows)

    db = FakeDB()
    monkeypatch.setattr(guard.frappe, "db", db)
    duplicate_lines = [
        {"original_resolved_sale": "RS-1", "qty_returned": "0.75"},
        {"original_resolved_sale": "RS-1", "qty_returned": "1.25"},
    ]

    guard.lock_and_validate_return_quantities(
        "return-1", duplicate_lines, "SINV-1"
    )

    assert len(calls) == 2
    assert "sales_invoice = %s" in calls[0][0]
    assert calls[0][1] == ("SINV-1",)
    assert "return_event.docstatus = 1" in calls[1][0]
    assert "FOR UPDATE" in calls[1][0]
    assert guard.aggregate_return_lines(duplicate_lines) == [
        {"original_resolved_sale": "RS-1", "qty_returned": 2.0}
    ]

    db.submitted_rows = [
        {
            "original_resolved_sale": "RS-1",
            "qty_returned": "0.01",
            "return_id": "another-return",
        }
    ]
    with pytest.raises(guard.frappe.ValidationError, match="exceeds purchased"):
        guard.lock_and_validate_return_quantities(
            "return-2", duplicate_lines, "SINV-1"
        )


def test_duplicate_return_children_mark_the_resolved_sale_fully_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = importlib.import_module(
        "kopos_connector.kopos.services.operations.return_service"
    )
    resolved_sale = FakeDoc(name="RS-1", qty="1.00", status="Open")
    monkeypatch.setattr(
        service.frappe,
        "get_doc",
        lambda doctype, name: resolved_sale,
    )
    return_event = FakeDoc(
        lines=[
            FakeDoc(original_resolved_sale="RS-1", qty_returned="0.50"),
            FakeDoc(original_resolved_sale="RS-1", qty_returned="0.50"),
        ]
    )

    service._update_resolved_sale_statuses(return_event)

    assert resolved_sale.status == "Returned"


def test_duplicate_persisted_children_match_an_aggregated_idempotent_retry() -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    return_event = FakeDoc(
        original_sales_invoice="SINV-1",
        fb_order="FB-ORDER-1",
        return_to_stock=0,
        refund_method="cash",
        request_fingerprint="f" * 64,
        lines=[
            FakeDoc(original_resolved_sale="RS-1", qty_returned="0.50"),
            FakeDoc(original_resolved_sale="RS-1", qty_returned="0.50"),
        ],
    )
    validated = {
        "original_sales_invoice": "SINV-1",
        "fb_order": "FB-ORDER-1",
        "return_to_stock": 0,
        "refund_method": "cash",
        "request_fingerprint": "f" * 64,
        "lines": [{"original_resolved_sale": "RS-1", "qty_returned": 1.0}],
    }

    api._validate_existing_return_matches(validated, return_event)


def test_return_idempotency_lookup_is_a_post_lock_current_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    captured: dict[str, Any] = {}

    class FakeDB:
        def sql(
            self,
            query: str,
            values: tuple[str, ...],
            as_dict: bool = False,
        ) -> list[dict[str, str]]:
            captured.update({"query": query, "values": values, "as_dict": as_dict})
            return [{"name": "FB-RETURN-1"}]

    monkeypatch.setattr(api.frappe, "db", FakeDB())

    result = api._get_existing_return_name_current("return-idem-1")

    assert result == "FB-RETURN-1"
    assert "WHERE return_id = %s" in captured["query"]
    assert "FOR UPDATE" in captured["query"]
    assert captured["values"] == ("return-idem-1",)
    assert captured["as_dict"] is True


def test_completed_refund_retry_is_verified_before_token_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    return_doc = FakeDoc(
        name="FB-RETURN-1",
        request_fingerprint="f" * 64,
    )
    return_doc.reload = lambda: None
    validated = {
        "return_id": "refund-idem-1",
        "lines": [{"original_resolved_sale": "RS-1", "qty_returned": 1}],
        "original_sales_invoice": "SINV-1",
        "request_fingerprint": "f" * 64,
    }
    scope = {
        "device_id": "DEVICE-1",
        "staff_id": "cashier@example.com",
        "shift_id": "SHIFT-1",
        "resource_id": "SINV-1",
        "amount_sen": 1200,
        "context_hash": "c" * 64,
    }

    monkeypatch.setattr(api, "_validate_payload", lambda payload: dict(validated))
    monkeypatch.setattr(api, "_build_refund_approval_scope", lambda value: scope)
    monkeypatch.setattr(
        api,
        "_return_request_fingerprint",
        lambda value, approval_scope: "f" * 64,
    )
    monkeypatch.setattr(api, "lock_and_validate_return_quantities", lambda *args: None)
    monkeypatch.setattr(
        api,
        "_get_existing_return_name_current",
        lambda return_id: "FB-RETURN-1",
    )
    monkeypatch.setattr(api.frappe, "get_doc", lambda *args: return_doc)
    monkeypatch.setattr(api, "_validate_existing_return_matches", lambda *args: None)
    monkeypatch.setattr(
        api,
        "verify_manager_approval_token",
        lambda *args, **kwargs: pytest.fail("duplicate must not consume token"),
    )
    monkeypatch.setattr(
        "kopos_connector.kopos.services.operations.return_service.ensure_existing_return_event_settlement",
        lambda doc: None,
    )
    monkeypatch.setattr(
        api,
        "_serialize_return_response",
        lambda status, doc, **_kwargs: {"status": status, "return_event": doc.name},
    )

    assert api.process_return_payload(
        {"manager_approval_token": "already-consumed"},
        require_manager_approval=True,
    ) == {"status": "duplicate", "return_event": "FB-RETURN-1"}


def test_new_refund_consumes_token_with_exact_server_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    validated = {
        "return_id": "refund-idem-1",
        "lines": [{"original_resolved_sale": "RS-1", "qty_returned": 1}],
        "original_sales_invoice": "SINV-1",
        "manager_approval_token": "signed-token",
    }
    scope = {
        "device_id": "DEVICE-1",
        "staff_id": "cashier@example.com",
        "shift_id": "SHIFT-1",
        "resource_id": "SINV-1",
        "amount_sen": 1200,
        "context_hash": "c" * 64,
    }
    approval = {
        "manager_id": "manager@example.com",
        "token_id": "approval-token-1",
    }
    approval_calls: list[tuple[str, dict[str, Any]]] = []
    return_doc = FakeDoc(name="FB-RETURN-1")
    return_doc.insert = lambda ignore_permissions=False: return_doc
    return_doc.submit = lambda: return_doc
    return_doc.reload = lambda: None

    monkeypatch.setattr(api, "_validate_payload", lambda payload: dict(validated))
    monkeypatch.setattr(api, "_build_refund_approval_scope", lambda value: scope)
    monkeypatch.setattr(
        api,
        "_return_request_fingerprint",
        lambda value, approval_scope: "f" * 64,
    )
    monkeypatch.setattr(api, "lock_and_validate_return_quantities", lambda *args: None)
    monkeypatch.setattr(api, "_get_existing_return_name_current", lambda key: None)

    def verify_approval(token: str, **bindings: Any) -> dict[str, str]:
        approval_calls.append((token, bindings))
        return approval

    monkeypatch.setattr(api, "verify_manager_approval_token", verify_approval)
    monkeypatch.setattr(
        api,
        "_build_return_event",
        lambda value, *, approval=None: return_doc,
    )
    monkeypatch.setattr(
        api,
        "_serialize_return_response",
        lambda status, doc, **_kwargs: {"status": status, "return_event": doc.name},
    )

    assert api.process_return_payload(
        {"manager_approval_token": "signed-token"},
        require_manager_approval=True,
    ) == {"status": "ok", "return_event": "FB-RETURN-1"}
    assert approval_calls == [
        (
            "signed-token",
            {
                "device_id": "DEVICE-1",
                "staff_id": "cashier@example.com",
                "action": "refund_order",
                "shift_id": "SHIFT-1",
                "resource_id": "SINV-1",
                "amount_sen": 1200,
                "context_hash": "c" * 64,
                "idempotency_key": "refund-idem-1",
            },
        )
    ]


def test_refund_duplicate_rejects_same_key_with_different_fingerprint() -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    return_doc = FakeDoc(request_fingerprint="a" * 64)
    with pytest.raises(api.frappe.ValidationError, match="different canonical payload"):
        api._validate_existing_return_matches(
            {"request_fingerprint": "b" * 64},
            return_doc,
        )


def test_shift_cash_uses_posted_settlement_cash_outflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = importlib.import_module(
        "kopos_connector.kopos.services.accounting.return_invoice_service"
    )
    sql_calls: list[str] = []
    updates: list[tuple[str, str, dict[str, Any], bool]] = []

    def sql(query: str, _params: Any, *, as_dict: bool) -> list[dict[str, Any]]:
        sql_calls.append(query)
        assert as_dict is True
        if "FROM `tabFB Shift`" in query:
            return [
                {
                    "name": "SHIFT-1",
                    "opening_float": "100.00",
                    "counted_cash": "100.00",
                }
            ]
        if "`tabSales Invoice Payment`" in query:
            return [
                {
                    "sales_invoice": "SINV-1",
                    "change_amount": "8.00",
                    "payment_row": "PAY-1",
                    "mode_of_payment": "Cash",
                    "payment_amount": "20.00",
                }
            ]
        if "FROM `tabFB Return Event`" in query:
            return [
                {
                    "name": "FB-RETURN-1",
                    "refund_method": "cash",
                    "settlement_doctype": "Journal Entry",
                    "settlement_document": "JV-REFUND-1",
                    "settlement_amount": "12.00",
                    "settlement_tenders_json": json.dumps(
                        {
                            "refund_method": "cash",
                            "settlement_amount_sen": 1200,
                            "customer_debit_sen": 1200,
                            "return_outstanding_sen": 0,
                            "tenders": [
                                {
                                    "account": "Cash - CO",
                                    "amount_sen": 1200,
                                    "refund_method": "cash",
                                }
                            ],
                        }
                    ),
                    "settlement_name": "JV-REFUND-1",
                    "settlement_docstatus": 1,
                }
            ]
        raise AssertionError(f"Unexpected query: {query}")

    def set_value(
        doctype: str,
        name: str,
        values: dict[str, Any],
        *,
        update_modified: bool,
    ) -> None:
        updates.append((doctype, name, values, update_modified))

    monkeypatch.setattr(service.frappe.db, "sql", sql)
    monkeypatch.setattr(service.frappe.db, "set_value", set_value)

    service.refresh_fb_shift_cash("SHIFT-1")

    assert len(sql_calls) == 3
    assert all("FOR UPDATE" in query for query in sql_calls)
    assert updates == [
        (
            "FB Shift",
            "SHIFT-1",
            {
                "expected_cash": service.Decimal("100"),
                "cash_variance": service.Decimal("0"),
            },
            False,
        )
    ]


def test_success_and_duplicate_api_responses_expose_same_settlement_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    settlement = importlib.import_module(
        "kopos_connector.kopos.services.accounting.return_settlement_service"
    )
    return_event = FakeDoc(
        name="FB-RETURN-1",
        return_id="refund-idem-1",
        original_sales_invoice="SINV-1",
        return_sales_invoice="SINV-RETURN-1",
        settlement_doctype="Journal Entry",
        settlement_document="JV-REFUND-1",
        settlement_status="Posted",
        return_to_stock=0,
        lines=[],
        approval_token_id="approval-token-id-refund",
        approved_by_manager="original-manager@example.com",
    )
    verified: list[str] = []
    monkeypatch.setattr(
        settlement,
        "assert_return_settlement_posted",
        lambda doc: verified.append(doc.name) or {},
    )

    success = api._serialize_return_response("ok", return_event)
    duplicate = api._serialize_return_response("duplicate", return_event)

    for response in (success, duplicate):
        assert response["return_sales_invoice"] == "SINV-RETURN-1"
        assert response["settlement_doctype"] == "Journal Entry"
        assert response["settlement_document"] == "JV-REFUND-1"
        assert response["settlement_status"] == "Posted"
    assert verified == ["FB-RETURN-1", "FB-RETURN-1"]

    persisted_proof = {
        "approval_manager_id": "original-manager@example.com",
        "approval_token_id": "approval-token-id-refund",
        "approval_context_hash": "b" * 64,
    }
    monkeypatch.setattr(
        api,
        "load_consumed_manager_approval_proof",
        lambda **kwargs: persisted_proof,
    )
    recovered = api._serialize_return_response(
        "duplicate",
        return_event,
        require_approval_proof=True,
    )
    assert recovered | persisted_proof == recovered
