"""Allocated part refunds: request binding, pricing, credit note shape and settlement tender rules."""
from __future__ import annotations

import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
install_fake_frappe_modules()

api = importlib.import_module("kopos_connector.api.fb_returns")
guard = importlib.import_module("kopos_connector.kopos.services.operations.return_guard_service")
invoices = importlib.import_module("kopos_connector.kopos.services.accounting.return_invoice_service")
settlement = importlib.import_module("kopos_connector.kopos.services.accounting.return_settlement_service")
allocation_module = importlib.import_module("kopos_connector.kopos.services.accounting.refund_allocation")


class FakeDoc(SimpleNamespace):
    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)

    def set(self, fieldname: str, value: Any) -> None:
        setattr(self, fieldname, value)

    def append(self, fieldname: str, value: dict[str, Any]) -> None:
        rows = getattr(self, fieldname, None) or []
        rows.append(FakeDoc(**value))
        setattr(self, fieldname, rows)


def _invoice(**overrides: Any) -> FakeDoc:
    values = {
        "name": "SINV-1",
        "company": "JiJi",
        "items": [
            FakeDoc(name="SINV-ITEM-1", custom_fb_order_line_ref="LINE-1", qty=3, rate=Decimal("3.343333"), amount=Decimal("10.03"), income_account="Sales"),
            FakeDoc(name="SINV-ITEM-2", custom_fb_order_line_ref="LINE-2", qty=1, rate=Decimal("12.00"), amount=Decimal("12.00"), income_account="Sales"),
        ],
        "taxes": [],
        "total_taxes_and_charges": Decimal("0"),
        "write_off_amount": Decimal("-0.02"),
        "grand_total": Decimal("22.03"),
        "write_off_account": "Write Off",
        "write_off_cost_center": "Main",
    }
    values.update(overrides)
    return FakeDoc(**values)


def _order(**overrides: Any) -> FakeDoc:
    values = {
        "items": [FakeDoc(line_id="LINE-1", line_total=Decimal("10.03")), FakeDoc(line_id="LINE-2", line_total=Decimal("12.00"))],
        "tax_total": Decimal("0"),
        "rounding_adjustment": Decimal("0.02"),
        "grand_total": Decimal("22.05"),
    }
    values.update(overrides)
    return FakeDoc(**values)


def _validated(**overrides: Any) -> dict[str, Any]:
    values = {
        "return_id": "RETURN-2",
        "device_id": "D",
        "reason_code": "Other",
        "reason_text": None,
        "return_to_stock": 0,
        "inventory_evaluation": "included_evaluated",
        "manager_approval_token": "",
        "original_sales_invoice": "SINV-1",
        "fb_order": "FB-ORDER-1",
        "refund_method": "cash",
        "refund_allocation_version": allocation_module.REFUND_ALLOCATION_VERSION,
        "amount_sen": 335,
        "lines": [{"original_sales_invoice_item": "SINV-ITEM-1", "original_fb_order_line_ref": "LINE-1", "qty_returned": 1.0}],
    }
    values.update(overrides)
    return values


def _install_sale(monkeypatch: pytest.MonkeyPatch, *, invoice: FakeDoc | None = None, order: FakeDoc | None = None, posted_amounts: list[str] | None = None) -> None:
    docs = {"Sales Invoice": invoice or _invoice(), "FB Order": order or _order()}
    monkeypatch.setattr(api.frappe, "get_doc", lambda doctype, _name=None: docs[doctype])
    posted_rows = [
        {"name": f"FB-RET-{index}", "return_id": f"RETURN-OLD-{index}", "settlement_amount": amount}
        for index, amount in enumerate(posted_amounts or [])
    ]

    def sql(query: str, values: tuple[Any, ...] = (), **_kwargs: Any) -> list[dict[str, Any]]:
        # Earlier refunds' amounts must be a locking read, like the guard's
        # quantities: a plain read misses a refund committed during the lock wait.
        assert "tabFB Return Event" in query and "FOR UPDATE" in query
        return posted_rows

    monkeypatch.setattr(api.frappe.db, "sql", sql)


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([], "Choose at least one item"),
        ([{"qty_returned": 1}], "requires original_fb_order_line_ref"),
        ([{"original_fb_order_line_ref": "LINE-1", "qty_returned": 1}, {"original_fb_order_line_ref": "LINE-1", "qty_returned": 1}], "listed twice"),
        ([{"original_fb_order_line_ref": "LINE-1", "qty_returned": True}], "whole numbers"),
        ([{"original_fb_order_line_ref": "LINE-1", "qty_returned": 1.5}], "whole numbers"),
        ([{"original_fb_order_line_ref": "LINE-1", "qty_returned": 0}], "whole numbers"),
    ],
)
def test_part_refund_lines_are_tablet_line_identities_with_whole_quantities(lines: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(api.frappe.ValidationError, match=message):
        api._parse_partial_refund_lines(lines)


def test_part_refund_request_requires_the_supported_version_and_an_amount_before_database_work(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("invalid part refund reached the database")

    monkeypatch.setattr(api.frappe.db, "sql", unexpected)
    base = {"return_id": "R", "device_id": "D", "original_sales_invoice": "SINV-1", "refund_method": "cash",
            "lines": [{"original_fb_order_line_ref": "LINE-1", "qty_returned": 1}]}
    with pytest.raises(api.frappe.ValidationError, match="not supported"):
        api._validate_payload({**base, "refund_allocation_version": "refund-allocation-v0", "amount_sen": 335})
    with pytest.raises(api.frappe.ValidationError, match="amount_sen"):
        api._validate_payload({**base, "refund_allocation_version": allocation_module.REFUND_ALLOCATION_VERSION, "amount_sen": 0})


def test_tablet_line_identities_bind_to_the_invoice_items_only() -> None:
    mapped = api._map_partial_refund_lines([("LINE-2", 1), ("LINE-1", 2)], _invoice())
    assert [(line["original_sales_invoice_item"], line["original_fb_order_line_ref"], line["qty_returned"]) for line in mapped] == [
        ("SINV-ITEM-1", "LINE-1", 2.0),
        ("SINV-ITEM-2", "LINE-2", 1.0),
    ]
    with pytest.raises(api.frappe.ValidationError, match="not part of this sale"):
        api._map_partial_refund_lines([("LINE-9", 1)], _invoice())
    ambiguous = _invoice(items=[FakeDoc(name="A", custom_fb_order_line_ref="LINE-1"), FakeDoc(name="B", custom_fb_order_line_ref="LINE-1")])
    with pytest.raises(api.frappe.ValidationError, match="not part of this sale"):
        api._map_partial_refund_lines([("LINE-1", 1)], ambiguous)


def test_part_refund_is_priced_from_the_posted_invoice_after_earlier_refunds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two earlier units refunded RM6.65: 668 sen of value, floored to 5-sen cash.
    _install_sale(monkeypatch, posted_amounts=["6.65"])
    allocation = api._allocate_partial_refund(_validated(), {"SINV-ITEM-1": Decimal("2")})
    # Cash sale of RM22.05: the last of three 10.03 units carries the leftover sen.
    assert allocation.to_wire() == {
        "version": "refund-allocation-v1",
        "lines": [{"lineId": "LINE-1", "quantity": 1, "netSen": 335}],
        "netSen": 335, "taxSen": 0, "roundingSen": 0, "amountSen": 335, "isFinal": False,
    }


def test_part_refund_refuses_a_sale_whose_order_disagrees_with_its_invoice(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sale(monkeypatch, order=_order(grand_total=Decimal("22.00")))
    with pytest.raises(api.frappe.ValidationError, match="cannot be used for a part refund"):
        api._allocate_partial_refund(_validated(), {})


def test_part_refund_refuses_when_earlier_refund_money_does_not_match_the_units(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sale(monkeypatch, posted_amounts=["6.75"])
    with pytest.raises(api.frappe.ValidationError, match="Earlier refunds of this sale do not match"):
        api._allocate_partial_refund(_validated(), {"SINV-ITEM-1": Decimal("2")})


def test_earlier_refund_money_counts_each_refund_once_and_never_this_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # The read joins refund lines, so a refund of two items comes back twice.
    rows = [
        {"name": "FB-RET-1", "return_id": "RETURN-1", "settlement_amount": Decimal("15.05")},
        {"name": "FB-RET-1", "return_id": "RETURN-1", "settlement_amount": Decimal("15.05")},
        {"name": "FB-RET-9", "return_id": "RETURN-2", "settlement_amount": Decimal("3.35")},
    ]
    monkeypatch.setattr(api.frappe.db, "sql", lambda *_args, **_kwargs: rows)
    assert api._posted_refund_amount_sen(_validated(), ["SINV-ITEM-1", "SINV-ITEM-2"]) == 1505


def test_allocation_errors_become_plain_cashier_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sale(monkeypatch, posted_amounts=["6.70", "3.35"])
    with pytest.raises(api.frappe.ValidationError, match="already been refunded"):
        api._allocate_partial_refund(_validated(), {"SINV-ITEM-1": Decimal("3")})


def test_part_refund_approval_context_adds_the_version_and_keeps_full_refund_hashes() -> None:
    full = {"reason_code": "Other", "reason_text": None, "refund_method": "cash", "return_to_stock": 0,
            "lines": [{"original_sales_invoice_item": "SINV-ITEM-1", "qty_returned": 3.0}]}
    assert "refund_allocation_version" not in api._return_approval_context(full)
    part = api._return_approval_context({**full, "refund_allocation_version": "refund-allocation-v1"})
    assert part["refund_allocation_version"] == "refund-allocation-v1"


def test_a_stale_part_refund_amount_is_refused_before_any_approval_is_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    validated = _validated(amount_sen=330)
    monkeypatch.setattr(api, "_validate_payload", lambda _payload: dict(validated))
    monkeypatch.setattr(api, "_build_refund_approval_scope", lambda _validated: {
        "device_id": "D", "staff_id": "S", "shift_id": "SH", "resource_id": "SINV-1", "amount_sen": 330, "fb_order": "FB-ORDER-1", "context_hash": "0" * 64})
    monkeypatch.setattr(api, "lock_and_validate_return_quantities", lambda *_args, **kwargs: kwargs.get("partial") and {"SINV-ITEM-1": Decimal("2")})
    monkeypatch.setattr(api, "_get_existing_return_name_current", lambda _return_id: None)
    monkeypatch.setattr(api, "_allocate_partial_refund", lambda *_args: allocation_module.parse_stored_allocation(
        '{"version":"refund-allocation-v1","lines":[{"lineId":"LINE-1","quantity":1,"netSen":335}],"netSen":335,"taxSen":0,"roundingSen":0,"amountSen":335,"isFinal":false}'))
    monkeypatch.setattr(api, "verify_manager_approval_token", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("approval consumed")))
    with pytest.raises(api.frappe.ValidationError, match="refund amount changed") as refused:
        api.process_return_payload({}, require_manager_approval=True)
    assert refused.value.error_code == "refund_changed"


def test_a_part_refund_replay_is_not_priced_again(monkeypatch: pytest.MonkeyPatch) -> None:
    validated = _validated()
    existing = FakeDoc(name="FB-RETURN-1")
    monkeypatch.setattr(api, "_validate_payload", lambda _payload: dict(validated))
    monkeypatch.setattr(api, "_build_refund_approval_scope", lambda _validated: {
        "device_id": "D", "staff_id": "S", "shift_id": "SH", "resource_id": "SINV-1", "amount_sen": 335, "fb_order": "FB-ORDER-1", "context_hash": "0" * 64})
    monkeypatch.setattr(api, "lock_and_validate_return_quantities", lambda *_args, **_kwargs: {"SINV-ITEM-1": Decimal("2")})
    monkeypatch.setattr(api, "_get_existing_return_name_current", lambda _return_id: "FB-RETURN-1")
    monkeypatch.setattr(api.frappe, "get_doc", lambda *_args: existing)
    monkeypatch.setattr(api, "_validate_existing_return_matches", lambda *_args: None)
    monkeypatch.setattr(api, "_allocate_partial_refund", lambda *_args: (_ for _ in ()).throw(AssertionError("replay repriced")))
    existing.reload = lambda: None
    service = importlib.import_module("kopos_connector.kopos.services.operations.return_service")
    monkeypatch.setattr(service, "ensure_existing_return_event_settlement", lambda _doc: "JE-1")
    monkeypatch.setattr(api, "_serialize_return_response", lambda status, doc, **_kwargs: {"status": status, "return_event": doc.name})
    assert api.process_return_payload({}, require_manager_approval=False) == {"status": "duplicate", "return_event": "FB-RETURN-1"}


def test_part_refund_guard_allows_a_subset_and_reports_earlier_returned_quantities(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDB:
        def sql(self, query: str, values: tuple[str, ...], *, as_dict: bool) -> list[dict[str, Any]]:
            if "tabSales Invoice Item" in query:
                return [{"name": "SINV-ITEM-1", "qty": 3}, {"name": "SINV-ITEM-2", "qty": 1}]
            return [{"original_sales_invoice_item": "SINV-ITEM-1", "qty_returned": 2, "return_id": "RETURN-1"}]

    monkeypatch.setattr(guard.frappe, "db", FakeDB())
    request = [{"original_sales_invoice_item": "SINV-ITEM-1", "qty_returned": 1}]
    assert guard.lock_and_validate_return_quantities("RETURN-2", request, "SINV-1", partial=True) == {"SINV-ITEM-1": Decimal("2")}
    with pytest.raises(guard.frappe.ValidationError, match="exactly match the full Sales Invoice"):
        guard.lock_and_validate_return_quantities("RETURN-2", request, "SINV-1")
    with pytest.raises(guard.frappe.ValidationError, match="exceeds purchased quantity"):
        guard.lock_and_validate_return_quantities("RETURN-2", [{"original_sales_invoice_item": "SINV-ITEM-1", "qty_returned": 2}], "SINV-1", partial=True)


def _allocation(lines: list[tuple[str, int, int]], tax: int = 0, rounding: int = 0) -> Any:
    net = sum(line[2] for line in lines)
    return allocation_module.parse_stored_allocation(json.dumps({
        "version": "refund-allocation-v1",
        "lines": [{"lineId": line_id, "quantity": quantity, "netSen": net_sen} for line_id, quantity, net_sen in lines],
        "netSen": net, "taxSen": tax, "roundingSen": rounding, "amountSen": net + tax + rounding, "isFinal": False,
    }))


def test_credit_note_keeps_refunded_lines_at_or_below_the_sold_rate() -> None:
    original = _invoice()
    credit = FakeDoc(items=[FakeDoc(sales_invoice_item="SINV-ITEM-1", qty=-3), FakeDoc(sales_invoice_item="SINV-ITEM-2", qty=-1)])
    leftover = invoices._apply_allocated_return_items(original, credit, _allocation([("LINE-1", 1, 335)]))
    assert leftover == 1
    assert len(credit.items) == 1
    assert (credit.items[0].qty, credit.items[0].rate, credit.items[0].price_list_rate) == (-1, Decimal("3.343333"), Decimal("3.343333"))
    exact = FakeDoc(items=[FakeDoc(sales_invoice_item="SINV-ITEM-1", qty=-3)])
    assert invoices._apply_allocated_return_items(original, exact, _allocation([("LINE-1", 2, 668)])) == 0
    assert exact.items[0].rate == Decimal("3.340000")
    with pytest.raises(invoices.frappe.ValidationError, match="no longer match"):
        invoices._apply_allocated_return_items(original, FakeDoc(items=[FakeDoc(sales_invoice_item="SINV-ITEM-2", qty=-1)]), _allocation([("LINE-1", 1, 334)]))


def test_credit_note_totals_use_the_allocated_tax_rounding_and_leftover_discount(monkeypatch: pytest.MonkeyPatch) -> None:
    sales = importlib.import_module("kopos_connector.kopos.services.accounting.sales_invoice_service")
    monkeypatch.setattr(sales, "_resolve_write_off_defaults", lambda _company: {"account": "Company Write Off", "cost_center": "Company CC"})
    original = _invoice(taxes=[FakeDoc(charge_type="Actual", account_head="SST", description="KoPOS SST", cost_center="Main")], write_off_account=None)
    credit = FakeDoc(taxes=[FakeDoc(charge_type="Actual", tax_amount=Decimal("-2.50"))])
    invoices._apply_allocated_return_totals(original, credit, _allocation([("LINE-1", 1, 335)], tax=19, rounding=-4), leftover_sen=1)
    assert [(row.account_head, row.tax_amount, row.dont_recompute_tax) for row in credit.taxes] == [("SST", Decimal("-0.19"), 1)]
    assert (credit.is_pos, credit.write_off_amount, credit.write_off_account) == (1, Decimal("-0.04"), "Company Write Off")
    assert (credit.apply_discount_on, credit.discount_amount, credit.disable_rounded_total) == ("Net Total", 0.01, 1)
    with pytest.raises(invoices.frappe.ValidationError, match="tax cannot be split"):
        invoices._apply_allocated_return_totals(_invoice(taxes=[FakeDoc(charge_type="On Net Total")]), FakeDoc(), _allocation([("LINE-1", 1, 335)], tax=19), 0)


def test_credit_note_must_post_the_allocation_exactly() -> None:
    allocation = _allocation([("LINE-1", 1, 335)], tax=19, rounding=-4)
    credit = FakeDoc(items=[FakeDoc(net_amount=Decimal("-3.35"), income_account="Sales", custom_fb_order_line_ref="LINE-1")],
                     grand_total=Decimal("-3.54"), write_off_amount=Decimal("-0.04"), total_taxes_and_charges=Decimal("-0.19"))
    invoices.assert_allocated_return_totals(credit, allocation)
    credit.total_taxes_and_charges = Decimal("-0.20")
    with pytest.raises(invoices.frappe.ValidationError, match="does not match the refund allocation"):
        invoices.assert_allocated_return_totals(credit, allocation)


def test_part_refund_settles_from_the_single_original_tender_account(monkeypatch: pytest.MonkeyPatch) -> None:
    original = _invoice(rounded_total=0)
    monkeypatch.setattr(settlement, "_resolve_original_tenders", lambda **kwargs: [{"account": "Cash", "refund_method": "cash", "amount_sen": kwargs["expected_total_sen"]}])
    assert settlement._refund_tenders(original, "cash", 335) == [{"account": "Cash", "refund_method": "cash", "amount_sen": 335}]
    assert settlement._refund_tenders(original, "cash", 2205) == [{"account": "Cash", "refund_method": "cash", "amount_sen": 2205}]
    monkeypatch.setattr(settlement, "_resolve_original_tenders", lambda **_kwargs: [{"account": "Cash A", "amount_sen": 1000}, {"account": "Cash B", "amount_sen": 1205}])
    with pytest.raises(settlement.frappe.ValidationError, match="single original tender account"):
        settlement._refund_tenders(original, "cash", 335)


def test_order_refunds_list_submitted_refunds_oldest_first_with_their_allocations(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {
        "version": "refund-allocation-v1", "lines": [{"lineId": "LINE-1", "quantity": 1, "netSen": 335}],
        "netSen": 335, "taxSen": 0, "roundingSen": 0, "amountSen": 335, "isFinal": False,
    }
    queries: list[dict[str, Any]] = []

    def get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        queries.append({"doctype": doctype, **kwargs})
        return [
            {"name": "FB-RET-1", "return_id": "order-1-refund-a", "return_sales_invoice": "SINV-RET-1", "settlement_status": "Posted",
             "settlement_amount": Decimal("3.35"), "refund_method": "Cash", "reason_text": " Wrong size ", "approved_by_manager": "manager@example.com",
             "refund_allocation": json.dumps(stored)},
            {"name": "FB-RET-2", "return_id": "order-1-refund-full", "return_sales_invoice": None, "settlement_status": "Pending",
             "settlement_amount": None, "refund_method": "cash", "reason_text": "", "approved_by_manager": None, "refund_allocation": None},
        ]

    monkeypatch.setattr(api.frappe, "get_all", get_all)
    result = api.list_order_refunds("SINV-1")

    assert queries[0]["doctype"] == "FB Return Event"
    assert queries[0]["filters"] == {"original_sales_invoice": "SINV-1", "docstatus": 1}
    assert queries[0]["order_by"] == "name asc"
    assert result == {
        "status": "ok",
        "sales_invoice": "SINV-1",
        "refunds": [
            {"return_id": "order-1-refund-a", "return_event": "FB-RET-1", "return_sales_invoice": "SINV-RET-1", "settlement_status": "Posted",
             "amount_sen": 335, "refund_method": "cash", "reason_text": "Wrong size", "approval_manager_id": "manager@example.com",
             "refund_allocation": stored},
            {"return_id": "order-1-refund-full", "return_event": "FB-RET-2", "return_sales_invoice": None, "settlement_status": "Pending",
             "amount_sen": 0, "refund_method": "cash", "reason_text": None, "approval_manager_id": None, "refund_allocation": None},
        ],
    }
    monkeypatch.setattr(api.frappe, "get_all", lambda *_args, **_kwargs: [{"name": "FB-RET-3", "refund_allocation": '{"version": "v0"}'}])
    with pytest.raises(allocation_module.RefundAllocationError):
        api.list_order_refunds("SINV-1")


def test_order_refunds_are_read_only_by_the_tablet_that_made_the_sale(monkeypatch: pytest.MonkeyPatch) -> None:
    public = importlib.import_module("kopos_connector.api")
    captured: dict[str, Any] = {}
    devices: list[str] = []

    class FakeDB:
        def get_value(self, doctype: str, name: str, fieldname: str) -> Any:
            assert (doctype, fieldname) == ("Sales Invoice", "custom_fb_device_id")
            return {"SINV-1": "DEVICE-A"}.get(name)

        def rollback(self) -> None:
            captured["rolled_back"] = True

    def require_device_context(device_id: str) -> None:
        devices.append(device_id)
        if device_id != "DEVICE-A":
            raise public.frappe.ValidationError("not authorized for KoPOS Device")

    monkeypatch.setattr(public.frappe, "db", FakeDB())
    monkeypatch.setattr(public, "require_device_context", require_device_context)
    monkeypatch.setattr(api, "list_order_refunds", lambda name: {"status": "ok", "sales_invoice": name, "refunds": []})
    monkeypatch.setattr(public, "_write_response", lambda payload, http_status_code=200: captured.update(payload=payload, http_status_code=http_status_code))

    public.get_order_refunds(sales_invoice=" SINV-1 ")
    assert (devices, captured["http_status_code"], captured["payload"]) == (["DEVICE-A"], 200, {"status": "ok", "sales_invoice": "SINV-1", "refunds": []})

    for name, message in ((None, "sales_invoice is required"), ("SINV-UNKNOWN", "is not a tablet sale")):
        captured.clear()
        public.get_order_refunds(sales_invoice=name)
        assert captured["http_status_code"] == 400 and message in captured["payload"]["message"]
        assert captured["rolled_back"] is True
