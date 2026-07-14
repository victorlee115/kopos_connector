from __future__ import annotations

import importlib
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()

frappe = sys.modules["frappe"]
frappe.utils.now = lambda: "2026-03-13 18:05:00"

fb_orders = importlib.import_module("kopos_connector.kopos.api.fb_orders")
fb_shift = importlib.import_module("kopos_connector.kopos.doctype.fb_shift.fb_shift")
shifts = importlib.import_module("kopos_connector.api.shifts")


class MutableShift(SimpleNamespace):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.save_calls: list[tuple[str, bool]] = []

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def save(self, ignore_permissions: bool = False) -> "MutableShift":
        self.save_calls.append((self.status, ignore_permissions))
        return self


def _submit_payload() -> dict[str, Any]:
    return {
        "money_contract_version": "sen_v1",
        "order_id": "ORDER-1",
        "idempotency_key": "idem-1",
        "device_id": "DEVICE-1",
        "shift": "SHIFT-1",
        "staff_id": "staff@example.test",
        "booth_warehouse": "WH-1",
        "company": "JiJi",
        "currency": "MYR",
        "order": {
            "display_number": "ORDER-1",
            "order_type": "takeaway",
            "created_at": "2026-03-13T18:00:00",
            "subtotal_sen": 1000,
            "tax_amount_sen": 0,
            "rounding_adjustment_sen": 0,
            "total_sen": 1000,
            "items": [
                {
                    "line_id": "LINE-1",
                    "item_code": "ITEM-1",
                    "item_name": "Item 1",
                    "qty": 1,
                    "unit_price_sen": 1000,
                    "modifier_total_sen": 0,
                    "discount_amount_sen": 0,
                    "line_total_sen": 1000,
                    "modifiers": [],
                }
            ],
            "payments": [{"payment_method": "Cash", "amount_sen": 1000}],
        },
    }


def _patch_submit_dependencies(monkeypatch: pytest.MonkeyPatch, shift_status: str) -> dict[str, bool]:
    built = {"value": False}

    monkeypatch.setattr(
        fb_orders,
        "_resolve_order_item",
        lambda _row, _index: {
            "line_id": "LINE-1",
            "backend_line_uuid": None,
            "item": "ITEM-1",
            "item_name_snapshot": "Item 1",
            "qty": 1.0,
            "uom": "Nos",
            "unit_price": 10.0,
            "modifier_total": 0.0,
            "discount_amount": 0.0,
            "line_total": 10.0,
            "recipe": None,
            "recipe_version": None,
            "is_recipe_managed": 0,
            "remarks": None,
            "selected_modifiers": [],
        },
    )
    monkeypatch.setattr(
        fb_orders,
        "_resolve_order_payment",
        lambda _row, _index: {"payment_method": "Cash", "amount": 10.0},
    )
    monkeypatch.setattr(fb_orders, "_resolve_fb_shift_name", lambda _value: "FB-SHIFT-1")
    monkeypatch.setattr(fb_orders, "_require_doc", lambda *_args, **_kwargs: None)

    shift_doc = SimpleNamespace(
        name="FB-SHIFT-1",
        device_id="DEVICE-1",
        staff_id="staff@example.test",
        status=shift_status,
    )
    monkeypatch.setattr(
        fb_orders.frappe,
        "get_doc",
        lambda doctype, name: shift_doc
        if (doctype, name) == ("FB Shift", "FB-SHIFT-1")
        else SimpleNamespace(name=name),
    )

    def build_order(_validated: dict[str, Any]) -> SimpleNamespace:
        built["value"] = True
        return SimpleNamespace()

    monkeypatch.setattr(fb_orders, "_build_fb_order", build_order)
    return built


@pytest.mark.parametrize("shift_status", ["Closed", "Closing", "Exception", "Cancelled"])
def test_submit_order_payload_rejects_non_open_shift_before_creating_order(
    monkeypatch: pytest.MonkeyPatch,
    shift_status: str,
) -> None:
    built = _patch_submit_dependencies(monkeypatch, shift_status)

    with pytest.raises(
        fb_orders.frappe.ValidationError,
        match="new FB Orders require an Open FB Shift",
    ):
        fb_orders.submit_order_payload(_submit_payload())

    assert built["value"] is False


def _device_doc() -> SimpleNamespace:
    return SimpleNamespace(
        name="DEVICE-1",
        device_id="DEVICE-1",
        pos_profile="Counter 1",
        device_users=[
            SimpleNamespace(
                user="staff@example.test",
                active=1,
                can_open_shift=1,
                can_close_shift=1,
            )
        ],
    )


def _close_payload() -> dict[str, Any]:
    return {
        "idempotency_key": "close-idem-1",
        "device_id": "DEVICE-1",
        "staff_id": "staff@example.test",
        "shift_id": "SHIFT-1",
        "fb_shift": "FB-SHIFT-1",
        "counted_cash_sen": 1250,
        "closed_at": "2026-03-13T18:05:00",
    }


def _patch_open_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    existing_shift: SimpleNamespace,
) -> None:
    monkeypatch.setattr(shifts, "get_device_doc", lambda device_id: _device_doc())
    monkeypatch.setattr(
        shifts.frappe.db,
        "get_value",
        lambda doctype, *_args, **_kwargs: 1
        if doctype in {"KoPOS Device", "User"}
        else None,
    )
    monkeypatch.setattr(
        shifts.frappe.db,
        "exists",
        lambda doctype, *_args, **_kwargs: doctype == "User",
    )
    monkeypatch.setattr(
        shifts.frappe,
        "get_cached_doc",
        lambda *_args, **_kwargs: SimpleNamespace(
            company="JiJi",
            warehouse="WH-1",
        ),
    )
    monkeypatch.setattr(shifts, "_lock_open_shift_scope", lambda *_args: None)
    monkeypatch.setattr(
        shifts,
        "_find_fb_shift_for_update",
        lambda _shift_id: existing_shift,
    )


def _patch_close_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    order_rows: list[dict[str, Any]],
    projection_rows: list[dict[str, Any]],
    resolved_sales_by_order: dict[str, list[SimpleNamespace]] | None = None,
) -> MutableShift:
    shift_doc = MutableShift(
        name="FB-SHIFT-1",
        shift_code="SHIFT-1",
        device_id="DEVICE-1",
        staff_id="staff@example.test",
        status="Open",
        opened_at="2026-03-13T18:00:00",
        opening_float=10.0,
        expected_cash=10.0,
        counted_cash=None,
        cash_variance=0.0,
        remarks="",
    )

    def get_value(doctype: str, *_args: Any, **_kwargs: Any) -> Any:
        if doctype == "KoPOS Device":
            return 1
        if doctype == "User":
            return 1
        return None

    resolved_sales_by_order = resolved_sales_by_order or {}
    resolved_sale_docs = {
        resolved_sale.name: resolved_sale
        for sales in resolved_sales_by_order.values()
        for resolved_sale in sales
    }

    def get_all(
        doctype: str,
        filters: dict[str, Any] | None = None,
        *_args: Any,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        if doctype == "FB Order":
            return order_rows
        if doctype == "FB Resolved Sale":
            order_name = str((filters or {}).get("fb_order") or "")
            return [
                {"name": resolved_sale.name}
                for resolved_sale in resolved_sales_by_order.get(order_name, [])
            ]
        if doctype == "FB Projection Log":
            return projection_rows
        return []

    monkeypatch.setattr(shifts, "get_device_doc", lambda device_id: _device_doc())
    monkeypatch.setattr(shifts.frappe.db, "get_value", get_value)
    monkeypatch.setattr(
        shifts.frappe.db,
        "exists",
        lambda doctype, *_args, **_kwargs: doctype == "User",
    )
    monkeypatch.setattr(shifts, "elevate_device_api_user", lambda: nullcontext())
    monkeypatch.setattr(shifts, "_resolve_fb_shift_reference", lambda value: value)
    def get_doc(doctype: str, name: str) -> Any:
        if doctype == "FB Resolved Sale":
            return resolved_sale_docs[name]
        return shift_doc

    monkeypatch.setattr(shifts.frappe, "get_doc", get_doc)
    monkeypatch.setattr(fb_shift.frappe, "get_all", get_all)
    monkeypatch.setattr(fb_shift.frappe, "get_doc", get_doc)
    return shift_doc


def _resolved_sale(
    *,
    affects_stock: int,
    item: str | None = "ITEM-1",
    warehouse: str | None = "WH-1",
    stock_qty: float = 1.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        name="RESOLVED-1",
        booth_warehouse="WH-1",
        resolved_components=[
            SimpleNamespace(
                affects_stock=affects_stock,
                item=item,
                warehouse=warehouse,
                stock_qty=stock_qty,
            )
        ],
    )


@pytest.mark.parametrize(
    ("order_rows", "projection_rows"),
    [
        ([{"name": "FB-ORDER-1", "invoice_status": "Pending", "stock_status": "Posted"}], []),
        ([{"name": "FB-ORDER-1", "invoice_status": "Failed", "stock_status": "Posted"}], []),
        ([{"name": "FB-ORDER-1", "invoice_status": "Posted", "stock_status": "Failed"}], []),
        (
            [{"name": "FB-ORDER-1", "invoice_status": "Posted", "stock_status": "Posted"}],
            [{"source_name": "FB-ORDER-1", "projection_type": "FB Shift", "state": "Pending"}],
        ),
        (
            [{"name": "FB-ORDER-1", "invoice_status": "Posted", "stock_status": "Posted"}],
            [{"source_name": "FB-ORDER-1", "projection_type": "FB Shift", "state": "Failed"}],
        ),
    ],
)
def test_close_shift_payload_blocks_pending_or_failed_order_projections_before_status_change(
    monkeypatch: pytest.MonkeyPatch,
    order_rows: list[dict[str, Any]],
    projection_rows: list[dict[str, Any]],
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=order_rows,
        projection_rows=projection_rows,
    )

    with pytest.raises(
        shifts.frappe.ValidationError,
        match="cannot close while",
    ):
        shifts.close_shift_payload(_close_payload())

    assert shift_doc.status == "Open"
    assert shift_doc.save_calls == []


def test_close_shift_payload_allows_no_stock_required_pending_stock_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[
            {"name": "FB-ORDER-1", "invoice_status": "Posted", "stock_status": "Pending"}
        ],
        projection_rows=[
            {"source_name": "FB-ORDER-1", "projection_type": "Stock Issue", "state": "Pending"}
        ],
        resolved_sales_by_order={"FB-ORDER-1": [_resolved_sale(affects_stock=0)]},
    )

    result = shifts.close_shift_payload(_close_payload())

    assert result == {"status": "ok", "fb_shift": "FB-SHIFT-1", "shift_id": "SHIFT-1"}
    assert shift_doc.status == "Closed"
    assert shift_doc.save_calls == [("Closing", True), ("Closed", True)]


def test_close_shift_payload_blocks_actual_pending_stock_projection_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[
            {"name": "FB-ORDER-1", "invoice_status": "Posted", "stock_status": "Pending"}
        ],
        projection_rows=[
            {"source_name": "FB-ORDER-1", "projection_type": "Stock Issue", "state": "Pending"}
        ],
        resolved_sales_by_order={"FB-ORDER-1": [_resolved_sale(affects_stock=1)]},
    )

    with pytest.raises(
        shifts.frappe.ValidationError,
        match="cannot close while",
    ):
        shifts.close_shift_payload(_close_payload())

    assert shift_doc.status == "Open"
    assert shift_doc.save_calls == []


def test_close_shift_payload_blocks_actual_failed_stock_projection_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[
            {"name": "FB-ORDER-1", "invoice_status": "Posted", "stock_status": "Posted"}
        ],
        projection_rows=[
            {"source_name": "FB-ORDER-1", "projection_type": "Stock Entry", "state": "Failed"}
        ],
        resolved_sales_by_order={"FB-ORDER-1": [_resolved_sale(affects_stock=1)]},
    )

    with pytest.raises(
        shifts.frappe.ValidationError,
        match="cannot close while",
    ):
        shifts.close_shift_payload(_close_payload())

    assert shift_doc.status == "Open"
    assert shift_doc.save_calls == []


def test_close_shift_payload_all_posted_transitions_open_to_closing_to_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[
            {"name": "FB-ORDER-1", "invoice_status": "Posted", "stock_status": "Posted"}
        ],
        projection_rows=[],
    )

    result = shifts.close_shift_payload(_close_payload())

    assert result == {"status": "ok", "fb_shift": "FB-SHIFT-1", "shift_id": "SHIFT-1"}
    assert shift_doc.status == "Closed"
    assert shift_doc.save_calls == [("Closing", True), ("Closed", True)]
    assert shift_doc.counted_cash == 12.5
    assert shift_doc.cash_variance == 2.5


@pytest.mark.parametrize(
    "invalid_counted_cash_sen",
    [1.5, "1.5", float("nan"), float("inf"), "NaN", "Infinity"],
)
def test_close_shift_rejects_non_integer_or_non_finite_counted_cash_sen(
    invalid_counted_cash_sen: object,
) -> None:
    payload = _close_payload()
    payload["counted_cash_sen"] = invalid_counted_cash_sen

    with pytest.raises(
        shifts.frappe.ValidationError,
        match="counted_cash_sen must be an integer number of sen",
    ):
        shifts.close_shift_payload(payload)


@pytest.mark.parametrize("missing_value", [None, "missing"])
def test_close_shift_requires_explicit_counted_cash_sen(missing_value: object) -> None:
    payload = _close_payload()
    if missing_value == "missing":
        payload.pop("counted_cash_sen")
    else:
        payload["counted_cash_sen"] = None

    with pytest.raises(
        shifts.frappe.ValidationError,
        match="counted_cash_sen is required",
    ):
        shifts.close_shift_payload(payload)


def test_open_shift_duplicate_requires_exact_idempotency_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "idempotency_key": "open-idem-1",
        "device_id": "DEVICE-1",
        "staff_id": "staff@example.test",
        "shift_id": "SHIFT-1",
        "opening_float_sen": 1000,
        "opened_at": "2026-03-13T18:00:00",
    }
    fingerprint = shifts._shift_request_fingerprint(
        "open",
        {
            "idempotency_key": "open-idem-1",
            "device_id": "DEVICE-1",
            "staff_id": "staff@example.test",
            "shift_id": "SHIFT-1",
            "opening_float_sen": 1000,
            "opened_at": "2026-03-13T18:00:00",
            "reason": None,
        },
    )
    existing = SimpleNamespace(
        name="FB-SHIFT-1",
        device_id="DEVICE-1",
        staff_id="staff@example.test",
        open_idempotency_key="open-idem-1",
        open_request_fingerprint=fingerprint,
    )
    _patch_open_dependencies(monkeypatch, existing)

    assert shifts.open_shift_payload(dict(payload)) == {
        "status": "duplicate",
        "fb_shift": "FB-SHIFT-1",
        "shift_id": "SHIFT-1",
        "message": "Shift already opened",
    }

    conflicting_payload = dict(payload)
    conflicting_payload["opening_float_sen"] = 2000
    with pytest.raises(
        shifts.frappe.ValidationError,
        match="different open shift payload",
    ):
        shifts.open_shift_payload(conflicting_payload)

    conflicting_key = dict(payload)
    conflicting_key["idempotency_key"] = "open-idem-2"
    with pytest.raises(
        shifts.frappe.ValidationError,
        match="another idempotency_key",
    ):
        shifts.open_shift_payload(conflicting_key)


def test_close_shift_duplicate_requires_exact_idempotency_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[],
        projection_rows=[],
    )
    payload = _close_payload()

    assert shifts.close_shift_payload(dict(payload))["status"] == "ok"
    assert shift_doc.close_idempotency_key == "close-idem-1"
    assert len(shift_doc.close_request_fingerprint) == 64
    assert shifts.close_shift_payload(dict(payload))["status"] == "duplicate"

    conflicting_payload = dict(payload)
    conflicting_payload["counted_cash_sen"] = 1300
    with pytest.raises(
        shifts.frappe.ValidationError,
        match="different close shift payload",
    ):
        shifts.close_shift_payload(conflicting_payload)

    conflicting_key = dict(payload)
    conflicting_key["idempotency_key"] = "close-idem-2"
    with pytest.raises(
        shifts.frappe.ValidationError,
        match="another idempotency_key",
    ):
        shifts.close_shift_payload(conflicting_key)


def test_legacy_open_shift_fails_closed_for_open_retry_but_accepts_first_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_open = SimpleNamespace(
        name="FB-SHIFT-1",
        device_id="DEVICE-1",
        staff_id="staff@example.test",
        open_idempotency_key="c" * 64,
        open_request_fingerprint="a" * 64,
    )
    _patch_open_dependencies(monkeypatch, legacy_open)
    with pytest.raises(
        shifts.frappe.ValidationError,
        match="another idempotency_key",
    ):
        shifts.open_shift_payload(
            {
                "idempotency_key": "open-idem-original-unknown",
                "device_id": "DEVICE-1",
                "staff_id": "staff@example.test",
                "shift_id": "SHIFT-1",
                "opening_float_sen": 1000,
                "opened_at": "2026-03-13T18:00:00",
            }
        )

    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[],
        projection_rows=[],
    )
    shift_doc.open_idempotency_key = legacy_open.open_idempotency_key
    shift_doc.open_request_fingerprint = legacy_open.open_request_fingerprint
    shift_doc.close_idempotency_key = None
    shift_doc.close_request_fingerprint = None

    assert shifts.close_shift_payload(_close_payload())["status"] == "ok"
    assert shifts.close_shift_payload(_close_payload())["status"] == "duplicate"

    conflict = _close_payload()
    conflict["discrepancy_note"] = "different retry"
    with pytest.raises(
        shifts.frappe.ValidationError,
        match="different close shift payload",
    ):
        shifts.close_shift_payload(conflict)


def test_no_order_shift_closes_at_opening_float_without_variance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[],
        projection_rows=[],
    )
    payload = _close_payload()
    payload["counted_cash_sen"] = 1000

    result = shifts.close_shift_payload(payload)

    assert result == {
        "status": "ok",
        "fb_shift": "FB-SHIFT-1",
        "shift_id": "SHIFT-1",
    }
    assert shift_doc.status == "Closed"
    assert shift_doc.opening_float == 10.0
    assert shift_doc.expected_cash == 10.0
    assert shift_doc.counted_cash == 10.0
    assert shift_doc.cash_variance == 0.0


def test_order_submission_locks_and_rechecks_shift_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_submit_dependencies(monkeypatch, "Open")
    sql_calls: list[str] = []

    class OrderDoc:
        name = "FB-ORDER-1"

        def insert(self, ignore_permissions: bool = False) -> None:
            assert ignore_permissions is True

        def submit(self) -> None:
            return None

    monkeypatch.setattr(
        fb_orders.frappe.db,
        "sql",
        lambda query, params, **kwargs: sql_calls.append(query) or [],
    )
    monkeypatch.setattr(fb_orders, "_build_fb_order", lambda validated: OrderDoc())
    monkeypatch.setattr(
        fb_orders,
        "_build_submit_response",
        lambda status, order: {"status": status, "fb_order": order.name},
    )

    assert fb_orders.submit_order_payload(_submit_payload()) == {
        "status": "ok",
        "fb_order": "FB-ORDER-1",
    }
    assert any("tabFB Shift" in query and "FOR UPDATE" in query for query in sql_calls)


def test_close_shift_locks_shift_row_before_status_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_close_dependencies(monkeypatch, order_rows=[], projection_rows=[])
    sql_calls: list[str] = []
    monkeypatch.setattr(
        shifts.frappe.db,
        "sql",
        lambda query, params, **kwargs: sql_calls.append(query) or [],
    )

    shifts.close_shift_payload(_close_payload())

    assert sql_calls
    assert "tabFB Shift" in sql_calls[0]
    assert "FOR UPDATE" in sql_calls[0]


def test_open_shift_scope_locks_device_and_staff_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql_calls: list[str] = []
    monkeypatch.setattr(
        shifts.frappe.db,
        "sql",
        lambda query, params, **kwargs: sql_calls.append(query) or [],
    )

    shifts._lock_open_shift_scope(_device_doc(), "staff@example.test")

    assert len(sql_calls) == 2
    assert "tabKoPOS Device" in sql_calls[0] and "FOR UPDATE" in sql_calls[0]
    assert "tabUser" in sql_calls[1] and "FOR UPDATE" in sql_calls[1]
