# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kopos_connector.kopos.services.accounting.duplicate_qr_payment_service import (
    resolve_duplicate_paid_refund,
)


def resolve_duplicate_automatic_qr_refund_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve one duplicate provider payment through the refund-only path."""

    return resolve_duplicate_paid_refund(payload)
