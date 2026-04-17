from __future__ import annotations

from typing import Any

from kopos_connector.kopos.api.fb_orders import _validate_submit_order_payload


def validate_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _validate_submit_order_payload(payload)
