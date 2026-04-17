from __future__ import annotations

from typing import Any

from kopos_connector.kopos.services.inventory.transfer_service import (
    create_material_transfer,
)


def fulfill_refill_request(doc: Any) -> str:
    items = []
    for line in doc.get("lines") or []:
        items.append({"item_code": line.item, "qty": line.qty, "uom": line.uom})
    return create_material_transfer(
        doc.company if hasattr(doc, "company") else "",
        doc.from_warehouse,
        doc.to_warehouse,
        items,
    )
