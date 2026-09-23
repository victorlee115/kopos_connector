from __future__ import annotations

import importlib
from decimal import Decimal

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
order_history = importlib.import_module("kopos_connector.api.order_history")


def test_refund_history_carries_posted_settlement_evidence(monkeypatch) -> None:
    monkeypatch.setattr(order_history, "serialize_invoice_row", lambda *_args, **_kwargs: {"name": "SINV-RET-1"})
    row = {"name": "SINV-RET-1", "return_against": "SINV-SALE-1"}
    event = {
        "return_id": "RETURN-1",
        "refund_method": "cash",
        "settlement_status": "Posted",
        "settlement_amount": Decimal("5.94"),
        "reason_text": "Customer return",
        "approved_by_manager": "manager@example.com",
        "return_to_stock": 0,
    }

    result = order_history.serialize_refund_row(row, items=[], payments=[], return_event=event)

    assert result["return_id"] == "RETURN-1"
    assert result["refund_method"] == "cash"
    assert result["settlement_status"] == "Posted"
    assert result["settlement_amount_sen"] == 594
    assert result["refund_reason_text"] == "Customer return"
    assert result["refund_manager_id"] == "manager@example.com"
    assert result["return_to_stock"] is False
