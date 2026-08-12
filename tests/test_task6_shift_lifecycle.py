from __future__ import annotations

import importlib
import sys
from contextlib import nullcontext
from decimal import Decimal
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
cash_service = importlib.import_module(
    "kopos_connector.kopos.services.accounting.return_invoice_service"
)


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


def _empty_duplicate_qr_liability_summary() -> dict[str, Any]:
    return {
        "accounting_pending": {"count": 0, "amount_sen": 0},
        "refund_required": {"count": 0, "amount_sen": 0},
        "count": 0,
        "amount_sen": 0,
        "blocks_close": False,
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
    prepared_order_rows: list[dict[str, Any]] | None = None,
    payment_rows: list[dict[str, Any]] | None = None,
    duplicate_qr_transaction_rows: list[dict[str, Any]] | None = None,
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
        if doctype == "FB Shift":
            return shift_doc.expected_cash
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
            if (filters or {}).get("docstatus") == 0:
                return prepared_order_rows or []
            return order_rows
        if doctype == "FB Order Payment":
            requested_status = (filters or {}).get("settlement_status")
            parent_filter = (filters or {}).get("parent")
            requested_parents = (
                set(parent_filter[1])
                if isinstance(parent_filter, list)
                and len(parent_filter) == 2
                and parent_filter[0] == "in"
                else set()
            )
            return [
                payment
                for payment in payment_rows or []
                if (
                    not requested_status
                    or payment.get("settlement_status") == requested_status
                )
                and (
                    not requested_parents
                    or payment.get("parent") in requested_parents
                )
            ]
        if doctype == "Maybank QR Transaction":
            requested_statuses = (filters or {}).get("duplicate_payment_status")
            status_values = (
                set(requested_statuses[1])
                if isinstance(requested_statuses, list)
                and len(requested_statuses) == 2
                and requested_statuses[0] == "in"
                else set()
            )
            order_filter = (filters or {}).get("fb_order")
            requested_orders = (
                set(order_filter[1])
                if isinstance(order_filter, list)
                and len(order_filter) == 2
                and order_filter[0] == "in"
                else set()
            )
            return [
                transaction
                for transaction in duplicate_qr_transaction_rows or []
                if (
                    not status_values
                    or transaction.get("duplicate_payment_status") in status_values
                )
                and (
                    not requested_orders
                    or transaction.get("fb_order") in requested_orders
                )
            ]
        if doctype == "FB Resolved Sale":
            order_filter = (filters or {}).get("fb_order")
            requested_orders = (
                set(order_filter[1])
                if isinstance(order_filter, list)
                and len(order_filter) == 2
                and order_filter[0] == "in"
                else {str(order_filter or "")}
            )
            return [
                {
                    "name": resolved_sale.name,
                    "fb_order": order_name,
                    "booth_warehouse": resolved_sale.booth_warehouse,
                }
                for order_name, sales in resolved_sales_by_order.items()
                if order_name in requested_orders
                for resolved_sale in sales
            ]
        if doctype == "FB Resolved Component":
            parent_filter = (filters or {}).get("parent")
            requested_parents = (
                set(parent_filter[1])
                if isinstance(parent_filter, list)
                and len(parent_filter) == 2
                and parent_filter[0] == "in"
                else set()
            )
            return [
                {
                    "parent": resolved_sale.name,
                    "item": component.item,
                    "warehouse": component.warehouse,
                    "stock_qty": component.stock_qty,
                    "qty": getattr(component, "qty", None),
                }
                for sales in resolved_sales_by_order.values()
                for resolved_sale in sales
                if resolved_sale.name in requested_parents
                for component in resolved_sale.resolved_components
                if int(component.affects_stock or 0)
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
    monkeypatch.setattr(
        cash_service,
        "refresh_fb_shift_cash",
        lambda _shift_name: None,
    )
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


@pytest.mark.parametrize(
    "automatic_qr_state",
    [
        "prepared",
        "provider_pending",
        "provider_ambiguous",
        "provider_paid",
        "manual_pending_reconciliation",
        "finalized",
        "",
    ],
)
def test_close_shift_blocks_unsubmitted_prepared_automatic_qr_sale(
    monkeypatch: pytest.MonkeyPatch,
    automatic_qr_state: str,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[],
        projection_rows=[],
        prepared_order_rows=[
            {
                "name": "FB-ORDER-PREPARED-1",
                "automatic_qr_state": automatic_qr_state,
            }
        ],
    )

    with pytest.raises(
        shifts.frappe.ValidationError,
        match="Automatic QR Finalization",
    ):
        shifts.close_shift_payload(_close_payload())

    assert shift_doc.status == "Open"


def test_close_shift_ignores_durably_rejected_no_provider_attempt_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    automatic_qr = importlib.import_module("kopos_connector.api.automatic_qr")
    monkeypatch.setattr(
        automatic_qr,
        "has_durable_no_provider_release_fence",
        lambda fb_order: fb_order == "FB-ORDER-REJECTED-1",
    )
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[],
        projection_rows=[],
        prepared_order_rows=[
            {
                "name": "FB-ORDER-REJECTED-1",
                "automatic_qr_state": "provider_rejected",
            }
        ],
    )

    result = shifts.close_shift_payload(_close_payload())

    assert result["status"] == "ok"
    assert shift_doc.status == "Closed"


def test_close_shift_blocks_stale_provider_rejected_state_without_exact_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    automatic_qr = importlib.import_module("kopos_connector.api.automatic_qr")
    monkeypatch.setattr(
        automatic_qr,
        "has_durable_no_provider_release_fence",
        lambda _fb_order: False,
    )
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[],
        projection_rows=[],
        prepared_order_rows=[
            {
                "name": "FB-ORDER-STALE-REJECTED-1",
                "status": "Draft",
                "automatic_qr_state": "provider_rejected",
            }
        ],
    )

    with pytest.raises(
        shifts.frappe.ValidationError,
        match="Automatic QR Finalization",
    ):
        shifts.close_shift_payload(_close_payload())

    assert shift_doc.status == "Open"


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

    assert result == {
        "status": "ok",
        "fb_shift": "FB-SHIFT-1",
        "shift_id": "SHIFT-1",
        "pending_reconciliation": {
            "count": 0,
            "amount_sen": 0,
            "blocks_close": False,
        },
        "duplicate_qr_liabilities": _empty_duplicate_qr_liability_summary(),
    }
    assert shift_doc.status == "Closed"
    assert shift_doc.save_calls == [("Closing", True), ("Closed", True)]


def test_close_shift_payload_ignores_optional_pending_stock_projection_work(
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

    result = shifts.close_shift_payload(_close_payload())

    assert result["status"] == "ok"
    assert shift_doc.status == "Closed"
    assert shift_doc.save_calls == [("Closing", True), ("Closed", True)]


def test_close_shift_payload_ignores_optional_failed_stock_projection_log(
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

    result = shifts.close_shift_payload(_close_payload())

    assert result["status"] == "ok"
    assert shift_doc.status == "Closed"
    assert shift_doc.save_calls == [("Closing", True), ("Closed", True)]


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

    assert result == {
        "status": "ok",
        "fb_shift": "FB-SHIFT-1",
        "shift_id": "SHIFT-1",
        "pending_reconciliation": {
            "count": 0,
            "amount_sen": 0,
            "blocks_close": False,
        },
        "duplicate_qr_liabilities": _empty_duplicate_qr_liability_summary(),
    }
    assert shift_doc.status == "Closed"
    assert shift_doc.save_calls == [("Closing", True), ("Closed", True)]
    assert shift_doc.counted_cash == 12.5
    assert shift_doc.cash_variance == 2.5


def test_close_shift_reports_submitted_pending_reconciliation_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[
            {
                "name": "FB-ORDER-1",
                "docstatus": 1,
                "status": "Submitted",
                "invoice_status": "Posted",
                "stock_status": "Posted",
            }
        ],
        projection_rows=[],
        payment_rows=[
            {
                "name": "FB-PAY-1",
                "parent": "FB-ORDER-1",
                "amount": Decimal("12.50"),
                "settlement_status": "pending_reconciliation",
            },
            {
                "name": "FB-PAY-2",
                "parent": "FB-ORDER-1",
                "amount": Decimal("3.25"),
                "settlement_status": "pending_reconciliation",
            },
            {
                "name": "FB-PAY-VERIFIED",
                "parent": "FB-ORDER-1",
                "amount": Decimal("99.00"),
                "settlement_status": "verified",
            },
        ],
    )

    result = shifts.close_shift_payload(_close_payload())

    assert shift_doc.status == "Closed"
    assert result["pending_reconciliation"] == {
        "count": 2,
        "amount_sen": 1575,
        "blocks_close": False,
    }
    duplicate = shifts.close_shift_payload(_close_payload())
    assert duplicate["status"] == "duplicate"
    assert duplicate["pending_reconciliation"] == result["pending_reconciliation"]


def test_close_shift_reports_unresolved_duplicate_qr_liabilities_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[
            {
                "name": "FB-ORDER-1",
                "docstatus": 1,
                "status": "Submitted",
                "invoice_status": "Posted",
                "stock_status": "Posted",
            }
        ],
        projection_rows=[],
        duplicate_qr_transaction_rows=[
            {
                "name": "MBQR-ACCOUNTING-PENDING",
                "fb_order": "FB-ORDER-1",
                "duplicate_payment_status": "accounting_pending",
                "sale_amount_sen": 1250,
            },
            {
                "name": "MBQR-REFUND-REQUIRED",
                "fb_order": "FB-ORDER-1",
                "duplicate_payment_status": "refund_required",
                "sale_amount_sen": "325",
            },
            {
                "name": "MBQR-REFUNDED",
                "fb_order": "FB-ORDER-1",
                "duplicate_payment_status": "refunded",
                "sale_amount_sen": 9900,
            },
            {
                "name": "MBQR-ANOTHER-SHIFT",
                "fb_order": "FB-ORDER-OTHER",
                "duplicate_payment_status": "refund_required",
                "sale_amount_sen": 5000,
            },
        ],
    )

    result = shifts.close_shift_payload(_close_payload())

    assert shift_doc.status == "Closed"
    assert result["duplicate_qr_liabilities"] == {
        "accounting_pending": {"count": 1, "amount_sen": 1250},
        "refund_required": {"count": 1, "amount_sen": 325},
        "count": 2,
        "amount_sen": 1575,
        "blocks_close": False,
    }
    duplicate = shifts.close_shift_payload(_close_payload())
    assert duplicate["status"] == "duplicate"
    assert (
        duplicate["duplicate_qr_liabilities"]
        == result["duplicate_qr_liabilities"]
    )


def test_duplicate_qr_liability_report_failure_never_blocks_shift_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[],
        projection_rows=[],
    )
    logged: list[tuple[str, str]] = []

    def unavailable(_shift_name: str) -> dict[str, object]:
        raise RuntimeError("report storage unavailable")

    monkeypatch.setattr(
        fb_shift,
        "get_shift_duplicate_qr_liability_summary",
        unavailable,
    )
    monkeypatch.setattr(
        shifts,
        "log_sanitized_error",
        lambda title, error: logged.append((title, str(error))),
    )

    result = shifts.close_shift_payload(_close_payload())

    assert shift_doc.status == "Closed"
    assert result["status"] == "ok"
    assert result["duplicate_qr_liabilities"] == {
        "accounting_pending": {"count": None, "amount_sen": None},
        "refund_required": {"count": None, "amount_sen": None},
        "count": None,
        "amount_sen": None,
        "blocks_close": False,
        "report_status": "unavailable",
    }
    assert logged == [
        (
            "KoPOS shift duplicate QR liability report unavailable",
            "report storage unavailable",
        )
    ]


def test_pending_reconciliation_report_failure_never_blocks_shift_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[],
        projection_rows=[],
    )
    logged: list[tuple[str, str]] = []

    def unavailable(_shift_name: str) -> dict[str, int | bool]:
        raise RuntimeError("settlement report storage unavailable")

    monkeypatch.setattr(
        fb_shift,
        "get_shift_pending_reconciliation_summary",
        unavailable,
    )
    monkeypatch.setattr(
        shifts,
        "log_sanitized_error",
        lambda title, error: logged.append((title, str(error))),
    )

    result = shifts.close_shift_payload(_close_payload())

    assert shift_doc.status == "Closed"
    assert result["status"] == "ok"
    assert result["pending_reconciliation"] == {
        "count": None,
        "amount_sen": None,
        "blocks_close": False,
        "report_status": "unavailable",
    }
    assert logged == [
        (
            "KoPOS shift pending reconciliation report unavailable",
            "settlement report storage unavailable",
        )
    ]


def test_close_shift_reconciles_cash_from_accounting_evidence_before_variance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_doc = _patch_close_dependencies(
        monkeypatch,
        order_rows=[
            {"name": "FB-ORDER-1", "invoice_status": "Posted", "stock_status": "Posted"}
        ],
        projection_rows=[],
    )
    refresh_calls: list[str] = []

    def refresh(shift_name: str) -> None:
        refresh_calls.append(shift_name)
        shift_doc.expected_cash = Decimal("12.00")

    monkeypatch.setattr(cash_service, "refresh_fb_shift_cash", refresh)

    shifts.close_shift_payload(_close_payload())

    assert refresh_calls == ["FB-SHIFT-1"]
    assert shift_doc.expected_cash == Decimal("12.00")
    assert shift_doc.cash_variance == Decimal("0.50")


@pytest.mark.inventory_regression
def test_shift_stock_requirement_is_bulk_bounded_for_two_thousand_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_names = [f"FB-ORDER-{index:04d}" for index in range(2_000)]
    calls: list[str] = []

    def get_all(doctype: str, **_kwargs: Any) -> list[dict[str, Any]]:
        calls.append(doctype)
        if doctype == "FB Resolved Sale":
            rows = [
                {
                    "name": f"SALE-{index:04d}",
                    "fb_order": order_name,
                    "booth_warehouse": "WH-1",
                }
                for index, order_name in enumerate(order_names)
            ]
        elif doctype == "FB Resolved Component":
            rows = [
                {
                    "parent": f"SALE-{index:04d}",
                    "item": "INGREDIENT-1",
                    "warehouse": "WH-1",
                    "stock_qty": 1,
                    "qty": 1,
                }
                for index in range(0, len(order_names), 2)
            ]
        else:
            raise AssertionError(f"unexpected doctype {doctype}")

        start = int(_kwargs.get("limit_start", 0))
        length = int(_kwargs.get("limit_page_length", len(rows)))
        return rows[start : start + length]

    monkeypatch.setattr(fb_shift.frappe, "get_all", get_all)
    monkeypatch.setattr(
        fb_shift.frappe,
        "get_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bulk stock classification must not hydrate documents")
        ),
    )

    required = fb_shift._orders_requiring_stock_projection(order_names)

    assert calls.count("FB Resolved Sale") == 5
    assert calls.count("FB Resolved Component") == 3
    assert required["FB-ORDER-0000"] is True
    assert required["FB-ORDER-0001"] is False
    assert len(required) == 2_000


def test_expected_cash_uses_accounting_reconciliation_including_refunds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "opening_float": Decimal("100.00"),
        "cash_sales": Decimal("18.00"),
        "cash_refunds": Decimal("3.00"),
        "net_cash": Decimal("15.00"),
        "expected_cash": Decimal("115.00"),
    }
    calls: list[str] = []
    monkeypatch.setattr(
        cash_service,
        "calculate_fb_shift_cash",
        lambda shift_name: calls.append(shift_name) or expected,
    )

    result = fb_shift.get_shift_expected_cash("FB-SHIFT-1")

    assert calls == ["FB-SHIFT-1"]
    assert result == expected


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
        "pending_reconciliation": {
            "count": 0,
            "amount_sen": 0,
            "blocks_close": False,
        },
        "duplicate_qr_liabilities": _empty_duplicate_qr_liability_summary(),
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
