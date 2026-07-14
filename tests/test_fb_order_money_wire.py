from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.api import fb_orders


def _sen_payload() -> dict[str, Any]:
    return {
        "money_contract_version": "sen_v1",
        "order_id": "ORDER-1",
        "idempotency_key": "idem-1",
        "device_id": "DEVICE-1",
        "shift_id": "SHIFT-1",
        "staff_id": "cashier@example.test",
        "warehouse": "Booth - J",
        "company": "JiJi Cafe",
        "currency": "MYR",
        "order": {
            "display_number": "A001",
            "order_type": "takeaway",
            "created_at": "2026-07-12T00:30:00+08:00",
            "subtotal_sen": 4150,
            "tax_amount_sen": 332,
            "rounding_adjustment_sen": 0,
            "total_sen": 4482,
            "items": [
                {
                    "line_id": "LINE-1",
                    "item_code": "LATTE",
                    "item_name": "Latte",
                    "qty": 2,
                    "unit_price_sen": 1500,
                    "modifier_total_sen": 200,
                    "discount_amount_sen": 100,
                    "line_total_sen": 3300,
                    "modifiers": [
                        {
                            "modifier_group": "FB-GRP-MILK",
                            "modifier": "FB-MOD-OAT",
                            "price_adjustment_sen": 200,
                        }
                    ],
                },
                {
                    "line_id": "LINE-2",
                    "item_code": "COOKIE",
                    "item_name": "Cookie",
                    "qty": 1,
                    "unit_price_sen": 850,
                    "modifier_total_sen": 0,
                    "discount_amount_sen": 0,
                    "line_total_sen": 850,
                    "modifiers": [],
                },
            ],
            "payments": [
                {
                    "payment_method": "Cash",
                    "amount_sen": 2000,
                    "tendered_amount_sen": 2000,
                    "change_amount_sen": 0,
                },
                {"payment_method": "Card", "amount_sen": 2482},
            ],
        },
    }


def _legacy_decimal_payload() -> dict[str, Any]:
    payload = _sen_payload()
    payload["money_contract_version"] = "decimal_v1"
    order = payload["order"]
    order["subtotal"] = "41.50"
    order["tax_amount"] = "3.32"
    order["rounding_adjustment"] = "0.00"
    order["total"] = "44.82"
    for fieldname in (
        "subtotal_sen",
        "tax_amount_sen",
        "rounding_adjustment_sen",
        "total_sen",
    ):
        del order[fieldname]

    first_item, second_item = order["items"]
    for item, values in (
        (
            first_item,
            {
                "unit_price": "15.00",
                "modifier_total": "2.00",
                "discount_amount": "1.00",
                "line_total": "33.00",
            },
        ),
        (
            second_item,
            {
                "unit_price": "8.50",
                "modifier_total": "0.00",
                "discount_amount": "0.00",
                "line_total": "8.50",
            },
        ),
    ):
        item.update(values)
        for fieldname in (
            "unit_price_sen",
            "modifier_total_sen",
            "discount_amount_sen",
            "line_total_sen",
        ):
            del item[fieldname]
    first_item["modifiers"][0]["price_adjustment"] = "2.00"
    del first_item["modifiers"][0]["price_adjustment_sen"]

    first_payment, second_payment = order["payments"]
    first_payment.update(
        {
            "amount": "20.00",
            "tendered_amount": "20.00",
            "change_amount": "0.00",
        }
    )
    second_payment["amount"] = "24.82"
    for payment in order["payments"]:
        for fieldname in (
            "amount_sen",
            "tendered_amount_sen",
            "change_amount_sen",
        ):
            payment.pop(fieldname, None)
    return payload


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fb_orders.frappe_utils,
        "now_datetime",
        lambda: datetime(2026, 7, 12, 1, 0, 0),
    )


def test_live_sen_and_explicit_legacy_decimal_share_the_same_fingerprint() -> None:
    live = fb_orders._normalize_submit_order_payload(_sen_payload())
    legacy = fb_orders._normalize_submit_order_payload(_legacy_decimal_payload())

    assert live["request_fingerprint"] == legacy["request_fingerprint"]
    assert live["grand_total_sen"] == 4482
    assert live["items"][0]["line_total_sen"] == 3300
    assert live["payments"][1]["amount_sen"] == 2482


def test_live_sen_contract_accepts_catalog_discount_modifier_adjustments() -> None:
    payload = _sen_payload()
    order = payload["order"]
    first_item = order["items"][0]
    first_item["modifiers"][0]["price_adjustment_sen"] = -200
    first_item["modifier_total_sen"] = -200
    first_item["line_total_sen"] = 2500
    order["subtotal_sen"] = 3350
    order["tax_amount_sen"] = 0
    order["total_sen"] = 3350
    order["payments"][1]["amount_sen"] = 1350

    normalized = fb_orders._normalize_submit_order_payload(payload)

    assert normalized["items"][0]["modifier_total_sen"] == -200
    assert normalized["items"][0]["line_total_sen"] == 2500


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["order"].__setitem__("total_sen", 4482.0),
            "integer number of sen",
        ),
        (
            lambda payload: payload["order"].__setitem__("total", "44.82"),
            "remove legacy",
        ),
        (
            lambda payload: payload["order"]["payments"][1].__setitem__(
                "amount_sen", 2481
            ),
            "payments total",
        ),
        (
            lambda payload: payload["order"]["items"][0].__setitem__(
                "line_total_sen", 3299
            ),
            "does not match",
        ),
    ],
)
def test_live_sen_contract_fails_closed_on_ambiguous_or_inexact_money(
    mutate: Any,
    message: str,
) -> None:
    payload = deepcopy(_sen_payload())
    mutate(payload)

    with pytest.raises(fb_orders.frappe.ValidationError, match=message):
        fb_orders._normalize_submit_order_payload(payload)


def test_new_order_converts_integer_sen_to_decimal_only_after_reference_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = fb_orders._normalize_submit_order_payload(_sen_payload())
    monkeypatch.setattr(fb_orders, "_resolve_fb_shift_name", lambda value: "FB-SHIFT-1")
    monkeypatch.setattr(
        fb_orders,
        "_validate_submit_shift",
        lambda **kwargs: SimpleNamespace(opened_at=datetime(2026, 7, 11, 20, 0, 0)),
    )
    monkeypatch.setattr(fb_orders, "_require_doc", lambda *args: None)
    monkeypatch.setattr(
        fb_orders,
        "_resolve_order_item",
        lambda row, index: {
            "line_total": fb_orders.sen_to_decimal(row["line_total_sen"]),
            "unit_price": fb_orders.sen_to_decimal(row["unit_price_sen"]),
        },
    )
    monkeypatch.setattr(
        fb_orders,
        "_resolve_order_payment",
        lambda row, index: {
            "amount": fb_orders.sen_to_decimal(row["amount_sen"]),
        },
    )

    validated = fb_orders._validate_new_submit_order_state(normalized)

    assert validated["net_total"] == Decimal("41.5")
    assert validated["tax_total"] == Decimal("3.32")
    assert validated["grand_total"] == Decimal("44.82")
    assert validated["items"][0]["line_total"] == Decimal("33")
    assert validated["payments"][1]["amount"] == Decimal("24.82")


def test_accepted_timeout_duplicate_reconciles_before_closed_shift_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = fb_orders._normalize_submit_order_payload(_sen_payload())
    order = SimpleNamespace(
        name="FB-ORDER-1",
        request_fingerprint=normalized["request_fingerprint"],
    )
    monkeypatch.setattr(
        fb_orders, "_get_existing_fb_order_name", lambda key: "FB-ORDER-1"
    )
    monkeypatch.setattr(fb_orders.frappe, "get_doc", lambda *args: order)
    monkeypatch.setattr(
        fb_orders,
        "_validate_new_submit_order_state",
        lambda value: (_ for _ in ()).throw(
            AssertionError("mutable shift/catalog validation must not run for a duplicate")
        ),
    )
    monkeypatch.setattr(
        fb_orders,
        "_build_submit_response",
        lambda status, doc: {"status": status, "fb_order": doc.name},
    )

    assert fb_orders.submit_order_payload(_sen_payload()) == {
        "status": "duplicate",
        "fb_order": "FB-ORDER-1",
    }
