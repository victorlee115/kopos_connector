from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kopos_connector.kopos.services.accounting.secondary_static_claim_resolution import (
    resolve_secondary_static_claim,
)


def resolve_secondary_static_qr_claim_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve one Maybank-winner/static-claim finance review."""

    return resolve_secondary_static_claim(payload)
