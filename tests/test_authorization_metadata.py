import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _cashier_permission(doctype_json: str) -> dict[str, object]:
    payload = json.loads((ROOT / doctype_json).read_text(encoding="utf8"))
    return next(
        permission
        for permission in payload["permissions"]
        if permission.get("role") == "KoPOS Cashier"
    )


def test_cashier_cannot_mutate_fb_order_through_frappe_resource_api() -> None:
    permission = _cashier_permission(
        "kopos_connector/kopos/doctype/fb_order/fb_order.json"
    )

    assert permission.get("read") == 1
    assert not permission.get("create")
    assert not permission.get("write")
    assert not permission.get("submit")
    assert not permission.get("delete")


def test_cashier_cannot_mutate_fb_shift_through_frappe_resource_api() -> None:
    permission = _cashier_permission(
        "kopos_connector/kopos/doctype/fb_shift/fb_shift.json"
    )

    assert permission.get("read") == 1
    assert not permission.get("create")
    assert not permission.get("write")
    assert not permission.get("submit")
    assert not permission.get("delete")
