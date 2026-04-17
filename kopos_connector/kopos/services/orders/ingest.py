from __future__ import annotations

from typing import Any

from kopos_connector.kopos.api.fb_orders import submit_order


def ingest_order() -> dict[str, Any]:
    return submit_order()
