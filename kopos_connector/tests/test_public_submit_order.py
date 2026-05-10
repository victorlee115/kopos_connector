from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()


def _void_payload(device_id: str = "DEVICE-1") -> dict[str, str]:
    return {
        "idempotency_key": "void-idem-001",
        "device_id": device_id,
        "pos_invoice": "ACC-PSINV-2026-00001",
        "reason": "Operator correction",
        "manager_id": "manager-1",
        "staff_id": "staff-1",
    }


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


def test_process_void_payload_cancels_matching_kopos_pos_invoice(monkeypatch):
    import kopos_connector.api.orders as orders

    cancelled = {"value": False}
    comments: list[tuple[str, str]] = []

    invoice = SimpleNamespace(
        name="ACC-PSINV-2026-00001",
        docstatus=1,
        is_return=0,
        custom_kopos_idempotency_key="submit-idem-001",
        custom_kopos_device_id="DEVICE-1",
        add_comment=lambda kind, text: comments.append((kind, text)),
    )

    def cancel() -> None:
        cancelled["value"] = True
        invoice.docstatus = 2

    invoice.cancel = cancel
    monkeypatch.setattr(orders.frappe, "get_doc", lambda doctype, name: invoice)
    monkeypatch.setattr(orders, "elevate_device_api_user", lambda: nullcontext())

    result = orders.process_void_payload(
        {
            "idempotency_key": "void-idem-001",
            "device_id": "DEVICE-1",
            "pos_invoice": "ACC-PSINV-2026-00001",
            "reason": "Operator correction",
            "manager_id": "manager-1",
            "staff_id": "staff-1",
        }
    )

    assert result["status"] == "ok"
    assert result["pos_invoice"] == "ACC-PSINV-2026-00001"
    assert result["invoice_status"] == "Cancelled"
    assert cancelled["value"] is True
    assert comments[0][0] == "Comment"
    assert "Operator correction" in comments[0][1]


def test_process_void_payload_rejects_other_device_invoice(monkeypatch):
    import pytest
    import kopos_connector.api.orders as orders

    invoice = SimpleNamespace(
        name="ACC-PSINV-2026-00001",
        docstatus=1,
        is_return=0,
        custom_kopos_idempotency_key="submit-idem-001",
        custom_kopos_device_id="DEVICE-2",
    )
    monkeypatch.setattr(orders.frappe, "get_doc", lambda doctype, name: invoice)

    with pytest.raises(orders.frappe.ValidationError, match="belongs to another KoPOS device"):
        orders.process_void_payload(_void_payload())


def test_process_void_payload_treats_cancelled_matching_invoice_as_duplicate(monkeypatch):
    import kopos_connector.api.orders as orders

    invoice = SimpleNamespace(
        name="ACC-PSINV-2026-00001",
        docstatus=2,
        is_return=0,
        custom_kopos_idempotency_key="submit-idem-001",
        custom_kopos_device_id="DEVICE-1",
    )
    monkeypatch.setattr(orders.frappe, "get_doc", lambda doctype, name: invoice)

    result = orders.process_void_payload(_void_payload())

    assert result["status"] == "duplicate"
    assert result["pos_invoice"] == "ACC-PSINV-2026-00001"
    assert result["idempotency_key"] == "void-idem-001"


def test_process_void_payload_rejects_cancelled_other_device_invoice(monkeypatch):
    import pytest
    import kopos_connector.api.orders as orders

    invoice = SimpleNamespace(
        name="ACC-PSINV-2026-00001",
        docstatus=2,
        is_return=0,
        custom_kopos_idempotency_key="submit-idem-001",
        custom_kopos_device_id="DEVICE-2",
    )
    monkeypatch.setattr(orders.frappe, "get_doc", lambda doctype, name: invoice)

    with pytest.raises(orders.frappe.ValidationError, match="belongs to another KoPOS device"):
        orders.process_void_payload(_void_payload())


def test_process_void_payload_rejects_cancelled_non_kopos_invoice(monkeypatch):
    import pytest
    import kopos_connector.api.orders as orders

    invoice = SimpleNamespace(
        name="ACC-PSINV-2026-00001",
        docstatus=2,
        is_return=0,
        custom_kopos_idempotency_key="",
        custom_kopos_device_id="DEVICE-1",
    )
    monkeypatch.setattr(orders.frappe, "get_doc", lambda doctype, name: invoice)

    with pytest.raises(orders.frappe.ValidationError, match="was not created via KoPOS"):
        orders.process_void_payload(_void_payload())


def test_process_void_payload_rejects_cancelled_return_invoice(monkeypatch):
    import pytest
    import kopos_connector.api.orders as orders

    invoice = SimpleNamespace(
        name="ACC-PSINV-2026-00001",
        docstatus=2,
        is_return=1,
        custom_kopos_idempotency_key="submit-idem-001",
        custom_kopos_device_id="DEVICE-1",
    )
    monkeypatch.setattr(orders.frappe, "get_doc", lambda doctype, name: invoice)

    with pytest.raises(orders.frappe.ValidationError, match="Cannot void a return invoice"):
        orders.process_void_payload(_void_payload())
