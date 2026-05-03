from __future__ import annotations

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()


def test_public_submit_payload_maps_mobile_contract_to_pos_invoice_contract():
    from kopos_connector.api import _to_pos_invoice_submit_payload

    payload = {
        "order_id": "order-001",
        "idempotency_key": "idem-001",
        "device_id": "DEVICE-1",
        "shift_id": "SHIFT-1",
        "staff_id": "staff@example.test",
        "warehouse": "KoPOS Store - WP",
        "company": "Wind Power LLC",
        "currency": "USD",
        "order": {
            "display_number": "A002",
            "order_type": "takeaway",
            "created_at": "2026-04-28T12:30:00",
            "subtotal": 12,
            "tax_amount": 0,
            "discount_amount": 0,
            "rounding_adj": 0,
            "total": 12,
            "items": [
                {
                    "line_id": "line-1",
                    "item_code": "STRAWBERRY-MATCHA-LATTE",
                    "item_name": "Strawberry Matcha Latte",
                    "qty": 1,
                    "rate": 10,
                    "discount_amount": 0,
                    "modifier_total": 2,
                    "amount": 12,
                    "modifiers": [
                        {
                            "modifier_group": "grp-size",
                            "modifier": "mod-large",
                            "price_adjustment": 2,
                            "id": "mod-large",
                            "group_id": "grp-size",
                            "name": "Large",
                            "price": 2,
                        }
                    ],
                }
            ],
            "payments": [
                {
                    "payment_method": "cash",
                    "amount": 12,
                    "tendered_amount": 12,
                    "change_amount": 0,
                }
            ],
        },
    }

    converted = _to_pos_invoice_submit_payload(payload)

    assert converted["idempotency_key"] == "idem-001"
    assert converted["device_id"] == "DEVICE-1"
    assert converted["warehouse"] == "KoPOS Store - WP"
    assert converted["order"]["display_number"] == "A002"
    assert converted["order"]["items"] == [
        {
            "item_code": "STRAWBERRY-MATCHA-LATTE",
            "item_name": "Strawberry Matcha Latte",
            "qty": 1,
            "rate": 10,
            "base_rate": 10,
            "modifier_total": 2,
            "base_amount": 12,
            "discount_amount": 0,
            "amount": 12,
            "modifiers": payload["order"]["items"][0]["modifiers"],
        }
    ]
    assert converted["order"]["payments"] == [
        {"method": "cash", "amount": 12, "tendered": 12, "change": 0}
    ]


def test_public_submit_response_is_fb_compatible_and_exposes_pos_invoice():
    from kopos_connector.api import _to_public_submit_response

    payload = {"order_id": "order-001", "idempotency_key": "idem-001"}
    result = {
        "status": "ok",
        "pos_invoice": "ACC-PSINV-2026-00003",
        "idempotency_key": "idem-001",
    }

    response = _to_public_submit_response(payload, result)

    assert response["status"] == "ok"
    assert response["fb_order"] == "ACC-PSINV-2026-00003"
    assert response["sales_invoice"] == "ACC-PSINV-2026-00003"
    assert response["pos_invoice"] == "ACC-PSINV-2026-00003"
    assert response["order_id"] == "order-001"
    assert response["idempotency_key"] == "idem-001"
    assert response["order_status"] == "Submitted"
    assert response["invoice_status"] == "Posted"
    assert response["stock_status"] == "Pending"
