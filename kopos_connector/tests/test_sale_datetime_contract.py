# pyright: reportMissingImports=false

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.api import fb_orders
from kopos_connector.kopos.services.accounting.sales_invoice_service import (
    _resolve_posting_datetime as resolve_invoice_posting_datetime,
)
from kopos_connector.kopos.services.inventory.stock_issue_service import (
    _resolve_posting_datetime as resolve_stock_posting_datetime,
)
from kopos_connector.kopos.services.orders.sale_datetime import (
    normalize_site_datetime,
    resolve_order_sale_datetime,
)


def _validated_payload(monkeypatch: Any, created_at: Any) -> dict[str, Any]:
    monkeypatch.setattr(
        fb_orders.frappe_utils,
        "now_datetime",
        lambda: datetime(2026, 7, 12, 1, 0, 0),
    )
    monkeypatch.setattr(
        fb_orders,
        "_resolve_order_item",
        lambda row, index: {"line_total": 10.0},
    )
    monkeypatch.setattr(
        fb_orders,
        "_resolve_order_payment",
        lambda row, index: {"amount": 10.0},
    )
    monkeypatch.setattr(fb_orders, "_resolve_fb_shift_name", lambda shift: "FB-SHIFT-1")
    monkeypatch.setattr(
        fb_orders,
        "_validate_submit_shift",
        lambda **kwargs: SimpleNamespace(opened_at=datetime(2026, 7, 11, 20, 0, 0)),
    )
    monkeypatch.setattr(fb_orders, "_require_doc", lambda *args: None)

    return fb_orders._validate_submit_order_payload(
        {
            "money_contract_version": "sen_v1",
            "order_id": "ORDER-1",
            "idempotency_key": "IDEMP-1",
            "device_id": "DEVICE-1",
            "shift_id": "SHIFT-1",
            "staff_id": "staff@example.com",
            "warehouse": "Booth - K",
            "company": "KoPOS Cafe",
            "currency": "MYR",
            "order": {
                "display_number": "ORDER-1",
                "order_type": "takeaway",
                "created_at": created_at,
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
    )


def test_submit_converts_utc_sale_time_to_frappe_site_timezone(monkeypatch: Any) -> None:
    validated = _validated_payload(monkeypatch, "2026-07-11T16:30:45.250Z")

    assert validated["sale_datetime"] == datetime(2026, 7, 12, 0, 30, 45, 250000)
    assert validated["sale_datetime"].tzinfo is None


def test_submit_preserves_naive_sale_time_as_site_local(monkeypatch: Any) -> None:
    validated = _validated_payload(monkeypatch, "2026-07-11 23:55:01")

    assert validated["sale_datetime"] == datetime(2026, 7, 11, 23, 55, 1)


@pytest.mark.parametrize("created_at", [None, "", "not-a-datetime"])
def test_submit_rejects_missing_or_invalid_sale_time(
    monkeypatch: Any,
    created_at: Any,
) -> None:
    with pytest.raises(frappe.ValidationError, match="order.created_at"):
        _validated_payload(monkeypatch, created_at)


def test_submit_rejects_sale_time_clearly_in_the_future(monkeypatch: Any) -> None:
    with pytest.raises(frappe.ValidationError, match="more than 5 minutes in the future"):
        _validated_payload(monkeypatch, "2026-07-11T17:05:01Z")


def test_submit_rejects_sale_time_materially_before_shift_open(monkeypatch: Any) -> None:
    with pytest.raises(
        frappe.ValidationError,
        match="more than 5 minutes before FB Shift FB-SHIFT-1 opened_at",
    ):
        _validated_payload(monkeypatch, "2026-07-11T11:54:59Z")


def test_submit_allows_five_minutes_of_tablet_clock_skew(monkeypatch: Any) -> None:
    validated = _validated_payload(monkeypatch, "2026-07-11T17:05:00Z")

    assert validated["sale_datetime"] == datetime(2026, 7, 12, 1, 5, 0)


def test_fb_order_builder_persists_normalized_sale_datetime(monkeypatch: Any) -> None:
    class FakeOrder(SimpleNamespace):
        def append(self, fieldname: str, value: dict[str, Any]) -> SimpleNamespace:
            row = SimpleNamespace(**value)
            getattr(self, fieldname).append(row)
            return row

    order_doc = FakeOrder(items=[], payments=[])
    monkeypatch.setattr(fb_orders.frappe, "new_doc", lambda doctype: order_doc)
    sale_datetime = datetime(2026, 7, 12, 0, 30, 45)

    built = fb_orders._build_fb_order(
        {
            "order_id": "ORDER-1",
            "display_number": "A001",
            "order_type": "dine_in",
            "catalog_version": "catalog-2026-07-12",
            "external_idempotency_key": "IDEMP-1",
            "request_fingerprint": "f" * 64,
            "source": "API",
            "sale_datetime": sale_datetime,
            "device_id": "DEVICE-1",
            "shift": "FB-SHIFT-1",
            "staff_id": "staff@example.com",
            "event_project": None,
            "booth_warehouse": "Booth - K",
            "company": "KoPOS Cafe",
            "currency": "MYR",
            "customer": None,
            "net_total": 10.0,
            "tax_total": 0.0,
            "tax_rate": 0,
            "rounding_adjustment": 0.0,
            "grand_total": 10.0,
            "notes": None,
            "items": [
                {
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
                }
            ],
            "payments": [{"payment_method": "Cash", "amount": 10.0}],
        }
    )

    assert built.sale_datetime == sale_datetime


def test_invoice_and_ingredient_stock_post_on_offline_sale_datetime() -> None:
    order = SimpleNamespace(
        name="FB-ORDER-1",
        sale_datetime=datetime(2026, 7, 12, 0, 30, 45),
        creation=datetime(2026, 7, 12, 9, 0, 0),
        modified=datetime(2026, 7, 12, 9, 1, 0),
    )

    assert resolve_invoice_posting_datetime(order) == order.sale_datetime
    assert resolve_stock_posting_datetime(order) == order.sale_datetime


def test_historical_fb_order_without_sale_datetime_falls_back_to_creation() -> None:
    order = SimpleNamespace(
        name="FB-ORDER-HISTORICAL",
        creation=datetime(2026, 7, 10, 14, 5, 0),
        modified=datetime(2026, 7, 12, 9, 1, 0),
    )

    assert resolve_order_sale_datetime(order) == order.creation


def test_normalizer_converts_explicit_offset_before_dropping_timezone() -> None:
    result = normalize_site_datetime(
        "2026-07-11T18:15:00+02:00",
        fieldname="test_datetime",
    )

    assert result == datetime(2026, 7, 12, 0, 15, 0)
    assert result.tzinfo is None
