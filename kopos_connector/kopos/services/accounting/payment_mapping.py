from __future__ import annotations

from typing import Any


def normalize_payment_rows(payments: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for payment in payments or []:
        rows.append(
            {
                "payment_method": getattr(payment, "payment_method", None),
                "amount": getattr(payment, "amount", 0),
                "tendered_amount": getattr(payment, "tendered_amount", 0),
                "change_amount": getattr(payment, "change_amount", 0),
                "payment_channel_code": getattr(payment, "payment_channel_code", None),
                "reference_no": getattr(payment, "reference_no", None),
                "external_transaction_id": getattr(
                    payment, "external_transaction_id", None
                ),
            }
        )
    return rows
