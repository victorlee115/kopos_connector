from __future__ import annotations

import json
import sys
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()


@pytest.fixture
def order_history_module(monkeypatch):
    import kopos_connector.api.order_history as order_history

    monkeypatch.setattr(
        order_history,
        "get_device_doc",
        lambda device_id=None: SimpleNamespace(
            device_id=device_id,
            enabled=1,
            pos_profile="POS-MAIN",
        ),
    )
    monkeypatch.setattr(
        order_history,
        "get_authenticated_device_doc",
        lambda: SimpleNamespace(
            device_id="DEVICE-1",
            enabled=1,
            pos_profile="POS-MAIN",
        ),
    )
    monkeypatch.setattr(
        order_history.frappe,
        "get_cached_doc",
        lambda doctype, name: SimpleNamespace(
            name=name,
            company="KoPOS Cafe",
        ),
    )
    monkeypatch.setattr(order_history, "nowdate", lambda: "2026-03-13")
    monkeypatch.setattr(
        order_history,
        "get_history_cursor_secret",
        lambda: b"order-history-test-secret-not-for-production",
    )
    return order_history


@pytest.fixture
def order_history_api(monkeypatch):
    import kopos_connector.api as api
    import kopos_connector.api.devices as devices
    import kopos_connector.api.order_history as order_history

    frappe: Any = sys.modules["frappe"]
    monkeypatch.setattr(
        frappe,
        "local",
        SimpleNamespace(
            request=SimpleNamespace(
                path="/api/method/kopos_connector.api.get_order_history"
            ),
            response={},
        ),
        raising=False,
    )
    monkeypatch.setattr(frappe, "flags", SimpleNamespace(), raising=False)
    monkeypatch.setattr(api, "mark_device_seen", lambda *args, **kwargs: None)
    return SimpleNamespace(
        api=api,
        devices=devices,
        frappe=frappe,
        order_history=order_history,
    )


def test_get_order_history_rejects_unauthenticated_access(
    order_history_api, monkeypatch
):
    monkeypatch.setattr(
        order_history_api.frappe,
        "session",
        SimpleNamespace(user="Guest"),
        raising=False,
    )

    result = order_history_api.api.get_order_history()

    assert result["status"] == "error"
    assert result["message"] == "Authentication required"
    assert "orders" not in result
    assert order_history_api.frappe.local.response["http_status_code"] == 400


def test_get_order_history_rejects_mismatched_device_access(
    order_history_api, monkeypatch
):
    configure_order_history_device_auth(
        order_history_api,
        monkeypatch,
        session_user="device-a@example.com",
        roles_by_user={"device-a@example.com": ["KoPOS Device API"]},
        devices_by_id={
            "DEVICE-A": SimpleNamespace(
                name="KoPOS Device A",
                device_id="DEVICE-A",
                enabled=1,
                pos_profile="Counter 1",
                api_user="device-a@example.com",
            ),
            "DEVICE-B": SimpleNamespace(
                name="KoPOS Device B",
                device_id="DEVICE-B",
                enabled=1,
                pos_profile="Counter 2",
                api_user="device-b@example.com",
            ),
        },
    )

    result = order_history_api.api.get_order_history(device_id="DEVICE-B")

    assert result["status"] == "error"
    assert "not authorized for KoPOS Device DEVICE-B" in result["message"]
    assert "orders" not in result
    assert order_history_api.frappe.local.response["http_status_code"] == 400


def test_get_order_history_blocks_cross_profile_leakage(
    order_history_api, monkeypatch
):
    configure_order_history_device_auth(
        order_history_api,
        monkeypatch,
        session_user="counter-1@example.com",
        roles_by_user={"counter-1@example.com": ["KoPOS Device API"]},
        devices_by_id={
            "DEVICE-1": SimpleNamespace(
                name="KoPOS Device 1",
                device_id="DEVICE-1",
                enabled=1,
                pos_profile="Counter 1",
                api_user="counter-1@example.com",
            )
        },
    )
    invoices = [
        {
            "name": "PINV-COUNTER-1",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "Counter 1",
            "posting_date": "2026-03-13",
            "posting_time": "10:00:00",
            "creation": datetime(2026, 3, 13, 10, 0),
            "custom_fb_device_id": "DEVICE-1",
            "grand_total": 10,
            "paid_amount": 10,
        },
        {
            "name": "PINV-COUNTER-2",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "Counter 2",
            "posting_date": "2026-03-13",
            "posting_time": "10:05:00",
            "creation": datetime(2026, 3, 13, 10, 5),
            "custom_fb_device_id": "DEVICE-1",
            "grand_total": 20,
            "paid_amount": 20,
        },
    ]
    captured_invoice_filters: list[dict[str, Any]] = []

    def fake_get_cached_doc(doctype: str, name: str) -> SimpleNamespace:
        assert doctype == "POS Profile"
        return SimpleNamespace(name=name, company="KoPOS Cafe")

    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        filters = kwargs.get("filters") or {}
        if doctype == "FB Shift":
            return []
        if doctype == "Sales Invoice" and filters.get("is_return") == 0:
            captured_invoice_filters.append(filters)
            rows = [row for row in invoices if matches_filters(row, filters)]
            start = kwargs.get("limit_start") or 0
            limit = kwargs.get("limit_page_length")
            return rows[start : start + limit] if limit else rows[start:]
        if doctype in ("Sales Invoice", "Sales Invoice Item", "Sales Invoice Payment"):
            return []
        return []

    monkeypatch.setattr(order_history_api.frappe, "get_cached_doc", fake_get_cached_doc)
    monkeypatch.setattr(order_history_api.frappe, "get_all", fake_get_all)

    result = order_history_api.api.get_order_history(
        device_id="DEVICE-1",
        since_date="2026-03-01",
    )

    assert captured_invoice_filters == [
        {
            "docstatus": ["in", [1, 2]],
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "Counter 1",
                "posting_date": [">=", "2026-03-01"],
                "custom_fb_device_id": "DEVICE-1",
                "creation": ["<=", "2026-03-13 18:05:00.000000"],
            }
        ]
    assert result["status"] == "ok"
    assert [order["name"] for order in result["orders"]] == ["PINV-COUNTER-1"]
    assert all(order["pos_profile"] == "Counter 1" for order in result["orders"])
    assert order_history_api.frappe.local.response["http_status_code"] == 200


def configure_order_history_device_auth(
    order_history_api: SimpleNamespace,
    monkeypatch,
    *,
    session_user: str,
    roles_by_user: dict[str, list[str]],
    devices_by_id: dict[str, SimpleNamespace],
) -> None:
    monkeypatch.setattr(
        order_history_api.frappe,
        "session",
        SimpleNamespace(user=session_user),
        raising=False,
    )
    monkeypatch.setattr(
        order_history_api.frappe,
        "get_roles",
        lambda user=None: roles_by_user.get(user or session_user, []),
        raising=False,
    )

    def fake_get_device_doc(device_id=None, name=None):
        lookup_key = str(device_id or name or "").strip()
        device_doc = devices_by_id.get(lookup_key)
        if device_doc is None:
            device_doc = next(
                (
                    device
                    for device in devices_by_id.values()
                    if getattr(device, "name", None) == lookup_key
                ),
                None,
            )
        if device_doc is None:
            order_history_api.frappe.throw(
                f"KoPOS Device {lookup_key} was not found",
                order_history_api.frappe.ValidationError,
            )
        return device_doc

    monkeypatch.setattr(
        order_history_api.devices,
        "get_device_doc",
        fake_get_device_doc,
    )
    monkeypatch.setattr(
        order_history_api.devices,
        "ensure_unique_device_api_user",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        order_history_api.order_history,
        "get_device_doc",
        fake_get_device_doc,
    )


def test_get_order_history_filters_by_server_device_context_and_paginates(
    order_history_module, monkeypatch
):
    captured_filters: list[dict[str, Any]] = []
    invoices = [
        {
            "name": "PINV-001",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": "2026-03-13",
            "posting_time": "10:00:00",
            "creation": datetime(2026, 3, 13, 10, 0),
            "modified": datetime(2026, 3, 13, 10, 1),
            "custom_fb_idempotency_key": "idem-001",
            "custom_fb_device_id": "DEVICE-1",
            "grand_total": 12.345,
            "rounded_total": 12.35,
            "paid_amount": 12.35,
        },
        {
            "name": "PINV-002",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": "2026-03-13",
            "posting_time": "09:30:00",
            "creation": datetime(2026, 3, 13, 9, 30),
            "modified": datetime(2026, 3, 13, 9, 31),
            "custom_fb_idempotency_key": "idem-002",
            "custom_fb_device_id": "DEVICE-1",
            "grand_total": 8,
            "rounded_total": 8,
            "paid_amount": 8,
        },
        {
            "name": "PINV-OTHER-PROFILE",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-OTHER",
            "posting_date": "2026-03-13",
        },
        {
            "name": "PINV-DRAFT",
            "docstatus": 0,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": "2026-03-13",
        },
        {
            "name": "PINV-OLD",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": "2026-03-12",
        },
    ]

    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        filters = kwargs.get("filters") or {}
        if doctype == "FB Shift":
            return [
                {
                    "name": "FB-SHIFT-001",
                    "opened_at": datetime(2026, 3, 13, 8, 0),
                    "creation": datetime(2026, 3, 13, 8, 0),
                }
            ]
        if doctype == "Sales Invoice":
            if filters.get("is_return") == 0:
                captured_filters.append(filters)
                rows = [row for row in invoices if matches_filters(row, filters)]
                start = kwargs.get("limit_start") or 0
                limit = kwargs.get("limit_page_length")
                return rows[start : start + limit] if limit else rows[start:]
            return []
        return []

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)

    result = order_history_module.get_order_history_payload(
        device_id="DEVICE-1",
        since_date="2026-03-01",
        limit=1,
    )

    assert result["timestamp_contract_version"] == "utc-ms-v1"
    assert captured_filters == [
        {
            "docstatus": ["in", [1, 2]],
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": [">=", "2026-03-13"],
            "custom_fb_device_id": "DEVICE-1",
            "creation": ["<=", "2026-03-13 18:05:00.000000"],
        }
    ]
    assert [order["name"] for order in result["orders"]] == ["PINV-001"]
    assert result["orders"][0]["grand_total"] == "12.35"
    assert isinstance(result["next_cursor"], str)
    assert "." in result["next_cursor"]


def test_serialize_invoice_row_returns_kopos_display_number_and_modifiers(
    order_history_module,
):
    row = {
        "name": "ACC-PSINV-2026-00001",
        "custom_kopos_display_number": "A001",
        "custom_kopos_idempotency_key": "idem-001",
        "custom_kopos_device_id": "DEVICE-1",
        "company": "KoPOS Cafe",
        "pos_profile": "POS-MAIN",
        "creation": datetime(2026, 3, 13, 10, 0),
        "modified": datetime(2026, 3, 13, 10, 1),
        "grand_total": 14,
        "paid_amount": 14,
    }
    item = {
        "idx": 1,
        "item_code": "LATTE",
        "item_name": "Latte",
        "qty": 1,
        "rate": 12,
        "amount": 14,
        "custom_kopos_modifier_total": 2,
        "custom_kopos_modifiers": '{"modifiers":[{"id":"MOD-LARGE","group_id":"SIZE","name":"Large","price":2}],"total":2}',
    }

    payload = order_history_module.serialize_invoice_row(row, items=[item], payments=[])

    assert payload["display_number"] == "A001"
    assert payload["created_at"] == "2026-03-13T02:00:00.000Z"
    assert payload["modified_at"] == "2026-03-13T02:01:00.000Z"
    assert payload["items"][0]["modifier_total"] == "2.00"
    assert payload["items"][0]["modifiers"] == [
        {
            "id": "MOD-LARGE",
            "name": "Large",
            "group_id": "SIZE",
            "price_adjustment": "2.00",
        }
    ]


def test_fb_order_history_uses_the_commercial_modifier_snapshot(
    order_history_module,
    monkeypatch,
) -> None:
    requested_line_fields: list[str] = []

    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        if doctype == "FB Order Line":
            requested_line_fields.extend(kwargs["fields"])
            return [
                {
                    "name": "FB-ORDER-LINE-1",
                    "parent": "FB-ORDER-1",
                    "idx": 1,
                    "item": "AMERICANO",
                    "item_name_snapshot": "Americano",
                    "qty": 1,
                    "unit_price": "8.00",
                    "modifier_total": "2.00",
                    "discount_amount": "0.00",
                    "line_total": "10.00",
                    "commercial_modifier_snapshot_json": json.dumps(
                        [
                            {
                                "modifier_group": "SIZE",
                                "modifier": "MOD-LARGE",
                                "price_adjustment": "2.00",
                                "sort_order": 1,
                                "affects_stock": 0,
                                "affects_recipe": 0,
                            }
                        ]
                    ),
                    "resolved_sale": "FB-RESOLVED-SALE-1",
                }
            ]
        if doctype == "FB Selected Modifier":
            raise AssertionError(
                "durable commercial snapshots must not depend on nested child rows"
            )
        raise AssertionError(f"unexpected history query: {doctype}")

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)
    monkeypatch.setattr(
        order_history_module.frappe.db,
        "get_value",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("history queried optional modifier metadata")
        ),
    )

    grouped = order_history_module.query_fb_order_items_by_order(
        [{"custom_fb_order": "FB-ORDER-1"}]
    )
    item = order_history_module.serialize_item_row(grouped["FB-ORDER-1"][0])

    assert "commercial_modifier_snapshot_json" in requested_line_fields
    assert item["modifier_total"] == "2.00"
    assert item["modifiers"] == [
        {
            "id": "MOD-LARGE",
            "name": "MOD-LARGE",
            "group_id": "SIZE",
            "price_adjustment": "2.00",
        }
    ]


def test_fb_order_history_omits_a_corrupt_commercial_modifier_snapshot(
    order_history_module,
    monkeypatch,
) -> None:
    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        if doctype != "FB Order Line":
            raise AssertionError(f"unexpected history query: {doctype}")
        return [
            {
                "name": "FB-ORDER-LINE-1",
                "parent": "FB-ORDER-1",
                "commercial_modifier_snapshot_json": '{"modifier":"MOD-LARGE"}',
            }
        ]

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)

    grouped = order_history_module.query_fb_order_items_by_order(
        [{"custom_fb_order": "FB-ORDER-1"}]
    )

    assert order_history_module.serialize_item_row(grouped["FB-ORDER-1"][0])["modifiers"] == []


def test_legacy_history_without_snapshot_never_queries_optional_modifier_rows(
    order_history_module,
    monkeypatch,
) -> None:
    def fake_get_all(doctype: str, **_kwargs: Any) -> list[dict[str, Any]]:
        if doctype == "FB Order Line":
            return [
                {
                    "name": "FB-ORDER-LINE-LEGACY",
                    "parent": "FB-ORDER-LEGACY",
                    "item": "AMERICANO",
                    "qty": 1,
                    "unit_price": "8.00",
                    "line_total": "8.00",
                    "commercial_modifier_snapshot_json": None,
                    "resolved_sale": "MISSING-OPTIONAL-ROW",
                }
            ]
        raise AssertionError(f"history queried optional DocType {doctype}")

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)

    grouped = order_history_module.query_fb_order_items_by_order(
        [{"custom_fb_order": "FB-ORDER-LEGACY"}]
    )

    assert order_history_module.serialize_item_row(grouped["FB-ORDER-LEGACY"][0])["modifiers"] == []


def test_format_datetime_uses_the_configured_site_timezone_and_emits_utc_z(
    order_history_module, monkeypatch
):
    monkeypatch.setattr(
        order_history_module,
        "get_system_timezone",
        lambda: "Europe/Berlin",
    )

    assert order_history_module.format_datetime(
        datetime(2026, 7, 20, 12, 34, 56, 987654)
    ) == "2026-07-20T10:34:56.987Z"
    assert order_history_module.format_datetime(
        datetime(
            2026,
            7,
            20,
            12,
            34,
            56,
            123456,
            tzinfo=timezone(timedelta(hours=-4)),
        )
    ) == "2026-07-20T16:34:56.123Z"


def test_serialize_invoice_row_falls_back_to_display_number_in_remarks(
    order_history_module,
):
    payload = order_history_module.serialize_invoice_row(
        {
            "name": "ACC-PSINV-2026-00002",
            "remarks": "KoPOS display number: A002\nKoPOS device: DEVICE-1",
        },
        items=[],
        payments=[],
    )

    assert payload["display_number"] == "A002"


def test_get_order_history_excludes_same_day_before_shift_open(
    order_history_module, monkeypatch
):
    invoices = [
        {
            "name": "PINV-BEFORE-SHIFT",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": "2026-03-13",
            "posting_time": "10:00:00",
            "creation": datetime(2026, 3, 13, 10, 0),
            "custom_fb_device_id": "DEVICE-1",
            "grand_total": 10,
            "paid_amount": 10,
        },
        {
            "name": "PINV-AFTER-SHIFT",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": "2026-03-13",
            "posting_time": "15:00:00",
            "creation": datetime(2026, 3, 13, 15, 0),
            "custom_fb_device_id": "DEVICE-1",
            "grand_total": 15,
            "paid_amount": 15,
        },
    ]

    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        filters = kwargs.get("filters") or {}
        if doctype == "FB Shift":
            return [
                {
                    "name": "FB-SHIFT-001",
                    "opened_at": datetime(2026, 3, 13, 14, 0),
                    "creation": datetime(2026, 3, 13, 14, 0),
                }
            ]
        if doctype == "Sales Invoice" and filters.get("is_return") == 0:
            rows = [row for row in invoices if matches_filters(row, filters)]
            start = kwargs.get("limit_start") or 0
            limit = kwargs.get("limit_page_length")
            return rows[start : start + limit] if limit else rows[start:]
        if doctype in ("Sales Invoice", "Sales Invoice Item", "Sales Invoice Payment"):
            return []
        return []

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)

    result = order_history_module.get_order_history_payload(device_id="DEVICE-1")

    assert result["since_date"] == "2026-03-13"
    assert result["since_datetime"] == "2026-03-13T06:00:00.000Z"
    assert [order["name"] for order in result["orders"]] == ["PINV-AFTER-SHIFT"]


def test_get_order_history_returns_refunds_separately_with_decimal_strings(
    order_history_module, monkeypatch
):
    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        filters = kwargs.get("filters") or {}
        if doctype == "FB Shift":
            return [
                {
                    "name": "FB-SHIFT-001",
                    "opened_at": datetime(2026, 3, 13, 8, 0),
                    "creation": datetime(2026, 3, 13, 8, 0),
                }
            ]
        if doctype == "Sales Invoice" and filters.get("is_return") == 0:
            return [
                {
                    "name": "PINV-001",
                    "docstatus": 1,
                    "is_return": 0,
                    "company": "KoPOS Cafe",
                    "pos_profile": "POS-MAIN",
                        "posting_date": "2026-03-13",
                        "posting_time": "10:00:00",
                        "creation": datetime(2026, 3, 13, 10, 0),
                        "grand_total": "12.00",
                    "paid_amount": "12.00",
                }
            ]
        if doctype == "Sales Invoice" and filters.get("is_return") == 1:
            return [
                {
                    "name": "CN-001",
                    "docstatus": 1,
                    "is_return": 1,
                    "return_against": "PINV-001",
                    "company": "KoPOS Cafe",
                    "pos_profile": "POS-MAIN",
                        "posting_date": "2026-03-13",
                        "posting_time": "10:05:00",
                        "grand_total": -3.5,
                    "paid_amount": -3.5,
                    "custom_kopos_refund_reason_code": "other",
                    "custom_kopos_refund_reason": "Wrong order",
                }
            ]
        if doctype == "Sales Invoice Item":
            return [
                {
                    "parent": "PINV-001",
                    "idx": 1,
                    "item_code": "LATTE",
                    "item_name": "Latte",
                    "qty": 1,
                    "rate": 12,
                    "amount": 12,
                    "net_amount": 12,
                },
                {
                    "parent": "CN-001",
                    "idx": 1,
                    "item_code": "LATTE",
                    "item_name": "Latte",
                    "qty": -1,
                    "rate": 3.5,
                    "amount": -3.5,
                    "net_amount": -3.5,
                },
            ]
        if doctype == "Sales Invoice Payment":
            return [
                {
                    "parent": "PINV-001",
                    "idx": 1,
                    "mode_of_payment": "Cash",
                    "amount": 12,
                    "default": 1,
                },
                {
                    "parent": "CN-001",
                    "idx": 1,
                    "mode_of_payment": "Cash",
                    "amount": -3.5,
                    "default": 1,
                },
            ]
        return []

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)

    result = order_history_module.get_order_history_payload(device_id="DEVICE-1")

    assert result["orders"][0]["items"][0]["amount"] == "12.00"
    assert result["orders"][0]["payments"][0]["amount"] == "12.00"
    assert result["refunds"] == [
        {
            "name": "CN-001",
            "display_number": None,
            "idempotency_key": None,
            "device_id": None,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "customer": None,
            "currency": None,
            "posting_date": "2026-03-13",
            "posting_time": "10:05:00",
            "created_at": None,
            "modified_at": None,
            "net_total": "0.00",
            "total_taxes_and_charges": "0.00",
            "discount_amount": "0.00",
            "grand_total": "-3.50",
            "rounded_total": "0.00",
            "paid_amount": "-3.50",
            "change_amount": "0.00",
            "items": [
                {
                    "idx": 1,
                    "item_code": "LATTE",
                    "item_name": "Latte",
                    "description": None,
                    "qty": "-1",
                    "rate": "3.50",
                    "amount": "-3.50",
                    "net_amount": "-3.50",
                    "base_rate": "0.00",
                    "base_amount": "0.00",
                    "discount_amount": "0.00",
                    "warehouse": None,
                    "modifier_total": "0.00",
                    "modifiers": [],
                }
            ],
            "payments": [
                {
                    "idx": 1,
                    "mode_of_payment": "Cash",
                    "amount": "-3.50",
                    "account": None,
                    "type": None,
                    "default": True,
                }
            ],
            "return_against": "PINV-001",
            "refund_reason_code": "other",
            "refund_reason": "Wrong order",
        }
    ]


def test_get_order_history_returns_only_exact_durably_voided_kopos_invoices(
    order_history_module, monkeypatch
):
    valid_void = {
        "name": "PINV-VOID-VALID",
        "docstatus": 2,
        "is_return": 0,
        "company": "KoPOS Cafe",
        "currency": "MYR",
        "pos_profile": "POS-MAIN",
        "posting_date": "2026-03-13",
        "posting_time": "10:10:00",
        "creation": datetime(2026, 3, 13, 10, 10),
        "modified": datetime(2026, 3, 13, 10, 12),
        "custom_fb_order": "FB-ORDER-VOID-VALID",
        "custom_fb_shift": "FB-SHIFT-001",
        "custom_fb_device_id": "DEVICE-1",
        "custom_fb_idempotency_key": "sale-idem-valid",
        "custom_fb_void_idempotency_key": "void-idem-valid",
        "custom_fb_void_request_fingerprint": "a" * 64,
        "custom_fb_void_manager": "manager@example.com",
        "custom_fb_void_approval_token_id": "APPROVAL-VALID",
        "grand_total": 12,
        "paid_amount": 12,
    }
    tampered_void = {
        **valid_void,
        "name": "PINV-VOID-TAMPERED",
        "custom_fb_order": "FB-ORDER-VOID-TAMPERED",
        "custom_fb_idempotency_key": "sale-idem-tampered",
        "custom_fb_void_idempotency_key": "void-idem-tampered",
        "custom_fb_void_approval_token_id": "APPROVAL-TAMPERED",
    }

    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        filters = kwargs.get("filters") or {}
        if doctype == "FB Shift":
            return [
                {
                    "name": "FB-SHIFT-001",
                    "opened_at": datetime(2026, 3, 13, 8, 0),
                    "creation": datetime(2026, 3, 13, 8, 0),
                }
            ]
        if doctype == "Sales Invoice" and filters.get("is_return") == 0:
            rows = [
                row
                for row in [valid_void, tampered_void]
                if matches_filters(row, filters)
            ]
            start = kwargs.get("limit_start") or 0
            limit = kwargs.get("limit_page_length")
            return rows[start : start + limit] if limit else rows[start:]
        return []

    orders = {
        "FB-ORDER-VOID-VALID": {
            "name": "FB-ORDER-VOID-VALID",
            "docstatus": 1,
            "sales_invoice": "PINV-VOID-VALID",
            "external_idempotency_key": "sale-idem-valid",
            "device_id": "DEVICE-1",
            "shift": "FB-SHIFT-001",
            "company": "KoPOS Cafe",
            "currency": "MYR",
            "status": "Cancelled",
            "invoice_status": "Reversed",
        },
        "FB-ORDER-VOID-TAMPERED": {
            "name": "FB-ORDER-VOID-TAMPERED",
            "docstatus": 1,
            "sales_invoice": "PINV-VOID-TAMPERED",
            "external_idempotency_key": "sale-idem-tampered",
            "device_id": "DEVICE-1",
            "shift": "FB-SHIFT-001",
            "company": "KoPOS Cafe",
            "currency": "MYR",
            "status": "Cancelled",
            "invoice_status": "Reversed",
        },
    }
    approvals = {
        "APPROVAL-VALID": {
            "token_id": "APPROVAL-VALID",
            "status": "consumed",
            "manager_id": "manager@example.com",
            "action": "void_order",
            "resource_id": "PINV-VOID-VALID",
            "context_hash": "b" * 64,
            "consumed_idempotency_key": "void-idem-valid",
        },
        "APPROVAL-TAMPERED": {
            "token_id": "APPROVAL-TAMPERED",
            "status": "consumed",
            "manager_id": "manager@example.com",
            "action": "void_order",
            "resource_id": "A-DIFFERENT-INVOICE",
            "context_hash": "b" * 64,
            "consumed_idempotency_key": "void-idem-tampered",
        },
    }

    def fake_get_value(
        doctype: str, name_or_filters: Any, fields: Any, **kwargs: Any
    ) -> dict[str, Any] | None:
        assert kwargs.get("as_dict") is True
        if doctype == "FB Order":
            return orders.get(str(name_or_filters))
        if doctype == "KoPOS Manager Approval":
            return approvals.get(str(name_or_filters.get("token_id")))
        raise AssertionError(f"Unexpected proof doctype: {doctype}")

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)
    monkeypatch.setattr(order_history_module.frappe.db, "get_value", fake_get_value)

    result = order_history_module.get_order_history_payload(device_id="DEVICE-1")

    assert [order["name"] for order in result["orders"]] == ["PINV-VOID-VALID"]
    assert result["orders"][0]["status"] == "voided"
    assert result["orders"][0]["idempotency_key"] == "sale-idem-valid"


def test_unproven_cancelled_invoice_does_not_hide_next_history_page(
    order_history_module, monkeypatch
):
    invoices = [
        {
            "name": "PINV-001",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": "2026-03-13",
            "posting_time": timedelta(hours=10, minutes=10),
            "creation": datetime(2026, 3, 13, 10, 10),
            "custom_fb_device_id": "DEVICE-1",
        },
        {
            "name": "PINV-UNPROVEN-CANCEL",
            "docstatus": 2,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": "2026-03-13",
            "posting_time": timedelta(hours=10, minutes=5),
            "creation": datetime(2026, 3, 13, 10, 5),
            "custom_fb_device_id": "DEVICE-1",
        },
        {
            "name": "PINV-002",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": "2026-03-13",
            "posting_time": timedelta(hours=10),
            "creation": datetime(2026, 3, 13, 10, 0),
            "custom_fb_device_id": "DEVICE-1",
        },
    ]

    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        filters = kwargs.get("filters") or {}
        if doctype == "FB Shift":
            return []
        if doctype == "Sales Invoice" and filters.get("is_return") == 0:
            rows = [row for row in invoices if matches_filters(row, filters)]
            start = kwargs.get("limit_start") or 0
            limit = kwargs.get("limit_page_length")
            return rows[start : start + limit] if limit else rows[start:]
        return []

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)

    def fake_keyset_batch(*, filters, fields, snapshot_ceiling, after, limit):
        del fields
        rows = [row for row in invoices if matches_filters(row, filters)]
        rows = [
            row for row in rows
            if order_history_module._db_datetime(row["creation"], "creation")
            <= snapshot_ceiling
        ]
        if after is not None:
            after_tuple = (
                after["posting_date"],
                after["posting_time"],
                after["creation"],
                after["name"],
            )
            rows = [
                row for row in rows
                if tuple(order_history_module._history_sort_key(row).values())
                < after_tuple
            ]
        rows.sort(
            key=lambda row: tuple(order_history_module._history_sort_key(row).values()),
            reverse=True,
        )
        return rows[:limit]

    monkeypatch.setattr(
        order_history_module,
        "query_invoice_keyset_batch",
        fake_keyset_batch,
    )

    first_page = order_history_module.get_order_history_payload(
        device_id="DEVICE-1", limit=1
    )
    second_page = order_history_module.get_order_history_payload(
        device_id="DEVICE-1", limit=1, cursor=first_page["next_cursor"]
    )

    assert [order["name"] for order in first_page["orders"]] == ["PINV-001"]
    assert isinstance(first_page["next_cursor"], str)
    assert "." in first_page["next_cursor"]
    assert [order["name"] for order in second_page["orders"]] == ["PINV-002"]
    assert second_page["next_cursor"] is None


def test_get_order_history_handles_empty_current_shift(order_history_module, monkeypatch):
    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)

    result = order_history_module.get_order_history_payload(device_id="DEVICE-1")

    assert result["status"] == "ok"
    assert result["since_date"] == "2026-03-13"
    assert result["orders"] == []
    assert result["refunds"] == []
    assert result["next_cursor"] is None


def test_history_cursor_rejects_tampering_and_cross_scope_replay(
    order_history_module,
):
    scope = order_history_module._cursor_scope(
        company="KoPOS Cafe",
        pos_profile="POS-MAIN",
        device_id="DEVICE-1",
        since_date="2026-03-13",
        since_datetime=None,
    )
    after = {
        "posting_date": "2026-03-13",
        "posting_time": "10:00:00.000000",
        "creation": "2026-03-13 10:00:00.000000",
        "name": "SINV-0002",
    }
    cursor = order_history_module.encode_history_cursor(
        snapshot_ceiling="2026-03-13 18:05:00.000000",
        after=after,
        scope=scope,
    )

    assert order_history_module.decode_history_cursor(cursor, scope=scope) == (
        "2026-03-13 18:05:00.000000",
        after,
    )
    replacement = "A" if cursor[-1] != "A" else "B"
    with pytest.raises(order_history_module.frappe.ValidationError):
        order_history_module.decode_history_cursor(
            f"{cursor[:-1]}{replacement}", scope=scope
        )
    other_scope = {**scope, "device_id": "DEVICE-2"}
    with pytest.raises(order_history_module.frappe.ValidationError):
        order_history_module.decode_history_cursor(cursor, scope=other_scope)


def test_history_sort_key_preserves_posting_time_microseconds(
    order_history_module,
):
    row = {
        "name": "SINV-MICROSECOND",
        "posting_date": "2026-03-13",
        "posting_time": time(10, 0, 0, 500_000),
        "creation": datetime(2026, 3, 13, 10, 0),
    }

    assert order_history_module._history_sort_key(row)["posting_time"] == (
        "10:00:00.500000"
    )


def test_history_sort_key_accepts_mariadb_timedelta_posting_time(
    order_history_module,
):
    row = {
        "name": "SINV-MARIADB-TIME",
        "posting_date": "2026-03-13",
        "posting_time": timedelta(
            hours=2,
            minutes=16,
            seconds=14,
            microseconds=250_000,
        ),
        "creation": datetime(2026, 3, 13, 2, 16, 14),
    }

    assert order_history_module._history_sort_key(row)["posting_time"] == (
        "02:16:14.250000"
    )


def test_database_time_accepts_midnight_timedelta(order_history_module):
    assert order_history_module._db_time(timedelta(0), "posting time") == (
        "00:00:00.000000"
    )


@pytest.mark.parametrize(
    "invalid_time",
    [
        timedelta(microseconds=-1),
        timedelta(days=1),
    ],
)
def test_database_time_rejects_timedelta_outside_one_day(
    order_history_module,
    invalid_time,
):
    with pytest.raises(order_history_module.frappe.ValidationError):
        order_history_module._db_time(invalid_time, "posting time")


def test_history_cursor_rejects_invalid_posting_time(order_history_module):
    scope = order_history_module._cursor_scope(
        company="KoPOS Cafe",
        pos_profile="POS-MAIN",
        device_id="DEVICE-1",
        since_date="2026-03-13",
        since_datetime=None,
    )
    cursor = order_history_module.encode_history_cursor(
        snapshot_ceiling="2026-03-13 18:05:00.000000",
        after={
            "posting_date": "2026-03-13",
            "posting_time": "25:00:00",
            "creation": "2026-03-13 10:00:00.000000",
            "name": "SINV-BAD-TIME",
        },
        scope=scope,
    )

    with pytest.raises(order_history_module.frappe.ValidationError):
        order_history_module.decode_history_cursor(cursor, scope=scope)


def test_history_keyset_sql_uses_strict_full_tuple(order_history_module, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_sql(query, values=None, as_dict=False):
        captured.update(query=query, values=list(values or []), as_dict=as_dict)
        return []

    monkeypatch.setattr(order_history_module.frappe.db, "sql", fake_sql)
    order_history_module.query_invoice_keyset_batch(
        filters={
            "company": "KoPOS Cafe",
            "pos_profile": "POS-MAIN",
            "posting_date": [">=", "2026-03-13"],
            "custom_fb_device_id": "DEVICE-1",
        },
        fields=order_history_module.get_invoice_fields(),
        snapshot_ceiling="2026-03-13 18:05:00.000000",
        after={
            "posting_date": "2026-03-13",
            "posting_time": "10:00:00",
            "creation": "2026-03-13 10:00:00.000000",
            "name": "SINV-0002",
        },
        limit=100,
    )

    query = captured["query"]
    assert "`creation` <= %s" in query
    assert "`posting_date` < %s" in query
    assert "COALESCE(`posting_time`, '00:00:00') < %s" in query
    assert "`creation` < %s" in query
    assert "`name` < %s" in query
    assert "limit_start" not in query.lower()
    assert captured["as_dict"] is True


def test_keyset_pages_one_thousand_orders_once_during_concurrent_insert(
    order_history_module, monkeypatch
):
    invoices: list[dict[str, Any]] = []
    for index in range(1_000):
        created_at = datetime(2026, 3, 13, 8, 0) + timedelta(seconds=index)
        if index < 3:
            created_at = datetime(2026, 3, 13, 8, 0)
        invoices.append(
            {
                "name": f"SINV-{index:04d}",
                "docstatus": 1,
                "is_return": 0,
                "company": "KoPOS Cafe",
                "pos_profile": "POS-MAIN",
                "posting_date": "2026-03-13",
                "posting_time": created_at.strftime("%H:%M:%S"),
                "creation": created_at,
                "modified": created_at,
                "custom_fb_device_id": "DEVICE-1",
                "custom_fb_idempotency_key": f"idem-{index:04d}",
                "grand_total": 1,
                "paid_amount": 1,
            }
        )

    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        filters = kwargs.get("filters") or {}
        if doctype == "FB Shift":
            return []
        if doctype == "Sales Invoice" and filters.get("is_return") == 1:
            return []
        return []

    def fake_keyset_batch(*, filters, fields, snapshot_ceiling, after, limit):
        del fields
        rows = [
            row for row in invoices
            if row["company"] == filters["company"]
            and row["pos_profile"] == filters["pos_profile"]
            and row["custom_fb_device_id"] == filters["custom_fb_device_id"]
            and row["posting_date"] >= filters["posting_date"][1]
            and order_history_module._db_datetime(row["creation"], "creation")
            <= snapshot_ceiling
        ]
        if after is not None:
            after_tuple = tuple(after[field] for field in (
                "posting_date", "posting_time", "creation", "name"
            ))
            rows = [
                row for row in rows
                if tuple(order_history_module._history_sort_key(row).values())
                < after_tuple
            ]
        rows.sort(
            key=lambda row: tuple(order_history_module._history_sort_key(row).values()),
            reverse=True,
        )
        return rows[:limit]

    monkeypatch.setattr(order_history_module.frappe, "get_all", fake_get_all)
    monkeypatch.setattr(
        order_history_module, "query_invoice_keyset_batch", fake_keyset_batch
    )

    names: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = order_history_module.get_order_history_payload(
            device_id="DEVICE-1",
            since_date="2026-03-13",
            cursor=cursor,
            limit=100,
        )
        pages += 1
        names.extend(order["name"] for order in page["orders"])
        cursor = page["next_cursor"]
        if pages == 1:
            invoices.append(
                {
                    **invoices[-1],
                    "name": "SINV-NEW-DURING-PAGING",
                    "creation": datetime(2026, 3, 13, 18, 6),
                    "modified": datetime(2026, 3, 13, 18, 6),
                    "posting_time": "18:06:00",
                    "custom_fb_idempotency_key": "idem-new-during-paging",
                }
            )
        if cursor is None:
            break
        assert pages <= 10

    assert pages == 10
    assert len(names) == 1_000
    assert len(set(names)) == 1_000
    assert "SINV-NEW-DURING-PAGING" not in names
    assert {"SINV-0000", "SINV-0001", "SINV-0002"}.issubset(names)


def matches_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    for fieldname, expected in filters.items():
        actual = row.get(fieldname)
        if isinstance(expected, list):
            operator, expected_value = expected
            if operator == ">=":
                if str(actual) < str(expected_value):
                    return False
            elif operator == "<=":
                if str(actual) > str(expected_value):
                    return False
            elif operator == "in":
                if actual not in expected_value:
                    return False
            else:
                raise AssertionError(f"Unhandled filter operator: {operator}")
        elif actual != expected:
            return False
    return True
