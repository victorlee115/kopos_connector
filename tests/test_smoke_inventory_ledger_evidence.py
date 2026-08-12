from __future__ import annotations

import importlib
import inspect
import sys
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()

smoke = importlib.import_module("kopos_connector.smoke")


def test_receipt_file_evidence_recomputes_private_attachment_bytes(
    monkeypatch: Any,
) -> None:
    import frappe

    content = b"verified-private-receipt-bytes"
    file_doc = SimpleNamespace(
        name="FILE-1",
        file_name="receipt.jpg",
        is_private=1,
        attached_to_doctype="Manual QR Reconciliation",
        attached_to_name="MQR-1",
        get_content=lambda: content,
    )
    monkeypatch.setattr(frappe.db, "exists", lambda doctype, name: True)
    monkeypatch.setattr(frappe, "get_doc", lambda doctype, name: file_doc)

    evidence = smoke._collect_receipt_file_evidence("FILE-1")

    assert evidence == {
        "name": "FILE-1",
        "exists": True,
        "content_readable": True,
        "file_name": "receipt.jpg",
        "is_private": True,
        "attached_to_doctype": "Manual QR Reconciliation",
        "attached_to_name": "MQR-1",
        "content_sha256": sha256(content).hexdigest(),
        "byte_length": len(content),
    }


@pytest.mark.inventory_regression
def test_cancelled_stock_entry_dump_keeps_cancelled_sle_history(
    monkeypatch: Any,
) -> None:
    observed_filters: list[dict[str, Any]] = []

    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any],
        fields: list[str],
        order_by: str,
    ) -> list[dict[str, Any]]:
        del fields, order_by
        assert doctype == "Stock Ledger Entry"
        observed_filters.append(filters)
        return [{
            "name": "SLE-CANCELLED-1",
            "voucher_type": "Stock Entry",
            "voucher_no": "STE-CANCELLED-1",
            "voucher_detail_no": "STE-DETAIL-1",
            "item_code": "MATCHA",
            "warehouse": "Store - K",
            "actual_qty": "-1.000000",
            "qty_after_transaction": "9.000000",
            "is_cancelled": 1,
            "posting_datetime": "2026-07-12 10:00:00",
            "creation": "2026-07-12 10:00:01",
        }]

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)

    rows = smoke._collect_stock_ledger_entries(
        "STE-CANCELLED-1", include_cancelled=True
    )

    assert observed_filters == [{
        "voucher_type": "Stock Entry",
        "voucher_no": "STE-CANCELLED-1",
    }]
    assert rows[0]["is_cancelled"] is True
    assert rows[0]["actual_qty"] == "-1.000000"


@pytest.mark.inventory_regression
def test_default_seed_has_two_times_headroom_for_mandatory_soak_capacity() -> None:
    signature = inspect.signature(smoke.set_demo_ingredient_quantities)
    ingredient_targets = [
        (
            "matcha_qty",
            smoke.DEMO_MATCHA_QTY_PER_ORDER,
            smoke.SMOKE_ACCEPTANCE_MATCHA_TARGET_QTY,
        ),
        (
            "strawberry_qty",
            smoke.DEMO_STRAWBERRY_QTY_PER_ORDER,
            smoke.SMOKE_ACCEPTANCE_STRAWBERRY_TARGET_QTY,
        ),
        (
            "milk_qty",
            smoke.DEMO_MILK_QTY_PER_ORDER,
            smoke.SMOKE_ACCEPTANCE_MILK_TARGET_QTY,
        ),
        (
            "cup_qty",
            smoke.DEMO_CUP_QTY_PER_ORDER,
            smoke.SMOKE_ACCEPTANCE_CUP_TARGET_QTY,
        ),
    ]

    assert smoke.SMOKE_ACCEPTANCE_MINIMUM_ORDERS == 500
    assert smoke.SMOKE_ACCEPTANCE_STOCK_HEADROOM_MULTIPLIER == 2
    for parameter_name, per_order_quantity, target_quantity in ingredient_targets:
        assert target_quantity == (
            per_order_quantity
            * smoke.SMOKE_ACCEPTANCE_MINIMUM_ORDERS
            * smoke.SMOKE_ACCEPTANCE_STOCK_HEADROOM_MULTIPLIER
        )
        assert signature.parameters[parameter_name].default == target_quantity


def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    for fieldname, expected in filters.items():
        actual = row.get(fieldname)
        if isinstance(expected, list) and expected[:1] == ["in"]:
            if actual not in expected[1]:
                return False
        elif actual != expected:
            return False
    return True


@pytest.mark.inventory_regression
def test_stock_entry_dump_carries_detail_and_exact_active_ledger_rows(
    monkeypatch: Any,
) -> None:
    import frappe

    ledger_rows = [
        {
            "name": "SLE-1",
            "voucher_type": "Stock Entry",
            "voucher_no": "STE-1",
            "voucher_detail_no": "STE-DETAIL-1",
            "item_code": "SMOKE-MATCHA-POWDER",
            "warehouse": "Store - K",
            "actual_qty": "-18.000000",
            "qty_after_transaction": "982.000000",
            "is_cancelled": 0,
            "posting_datetime": datetime(2026, 7, 12, 10, 1, 0),
            "creation": datetime(2026, 7, 12, 10, 1, 1),
        }
    ]

    def fake_get_all(
        doctype: str,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if doctype == "Stock Entry":
            return [
                {
                    "name": "STE-1",
                    "docstatus": 1,
                    "purpose": "Material Issue",
                    "stock_entry_type": "Material Issue",
                    "posting_date": "2026-07-12",
                    "posting_time": "10:01:00",
                    "custom_fb_order": "FB-ORDER-1",
                    "custom_fb_shift": "FB-SHIFT-1",
                }
            ]
        if doctype == "Stock Ledger Entry":
            return [
                dict(row)
                for row in ledger_rows
                if _matches(row, filters or {})
            ]
        return []

    item = SimpleNamespace(
        name="STE-DETAIL-1",
        item_code="SMOKE-MATCHA-POWDER",
        qty="18.000000",
        transfer_qty="18.000000",
        stock_uom="Gram",
        s_warehouse="Store - K",
        t_warehouse=None,
    )
    monkeypatch.setattr(frappe, "get_all", fake_get_all)
    monkeypatch.setattr(
        frappe,
        "get_doc",
        lambda doctype, name: SimpleNamespace(name=name, items=[item]),
    )

    rows = smoke._collect_ingredient_stock_entries(
        [{"ingredient_stock_entry": "STE-1"}]
    )

    assert rows[0]["items"] == [
        {
            "name": "STE-DETAIL-1",
            "item_code": "SMOKE-MATCHA-POWDER",
            "qty": "18.000000",
            "transfer_qty": "18.000000",
            "stock_uom": "Gram",
            "s_warehouse": "Store - K",
            "t_warehouse": None,
        }
    ]
    assert rows[0]["stock_ledger_entries"] == [
        {
            **ledger_rows[0],
            "actual_qty": "-18.000000",
            "qty_after_transaction": "982.000000",
            "is_cancelled": False,
            "posting_datetime": "2026-07-12 10:01:00",
            "creation": "2026-07-12 10:01:01",
        }
    ]


@pytest.mark.inventory_regression
def test_return_dump_carries_exact_lines_and_unique_reversal_ledger_evidence(
    monkeypatch: Any,
) -> None:
    import frappe

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        del fields, order_by
        calls.append((doctype, dict(filters or {})))
        if doctype == "FB Return Event":
            return [
                {
                    "name": "FB-RETURN-1",
                    "return_id": "tablet-order-1-refund-full",
                    "fb_order": "FB-ORDER-1",
                    "original_sales_invoice": "SINV-1",
                    "return_sales_invoice": "SINV-RETURN-1",
                    "refund_method": "cash",
                    "request_fingerprint": "a" * 64,
                    "approval_token_id": "APPROVAL-RETURN-1",
                    "approved_by_manager": "manager@example.com",
                    "settlement_doctype": "Journal Entry",
                    "settlement_document": "JV-RETURN-1",
                    "settlement_status": "Posted",
                    "settlement_amount": "12.950000",
                    "return_to_stock": 1,
                    "status": "Submitted",
                    "docstatus": 1,
                }
            ]
        if doctype == "FB Return Event Line":
            assert filters == {"parent": ["in", ["FB-RETURN-1"]]}
            return [
                {
                    "name": "RETURN-LINE-1",
                    "parent": "FB-RETURN-1",
                    "idx": 1,
                    "original_sales_invoice_item": "SINV-ITEM-1",
                    "original_fb_order_line_ref": "ORDER-LINE-1",
                    "original_resolved_sale": "RESOLVED-SALE-1",
                    "qty_returned": "1.250000",
                    "commercial_modifier_snapshot_json": "[]",
                    "reversal_stock_entry": "STE-RETURN-1",
                },
                {
                    "name": "RETURN-LINE-2",
                    "parent": "FB-RETURN-1",
                    "idx": 2,
                    "original_sales_invoice_item": "SINV-ITEM-2",
                    "original_fb_order_line_ref": "ORDER-LINE-2",
                    "original_resolved_sale": "RESOLVED-SALE-2",
                    "qty_returned": "2.000000",
                    "commercial_modifier_snapshot_json": "[]",
                    "reversal_stock_entry": "STE-RETURN-1",
                },
            ]
        if doctype == "Stock Entry":
            assert filters == {"name": ["in", ["STE-RETURN-1"]]}
            return [
                {
                    "name": "STE-RETURN-1",
                    "docstatus": 1,
                    "purpose": "Material Receipt",
                    "stock_entry_type": "Material Receipt",
                    "posting_date": "2026-07-12",
                    "posting_time": "10:05:00",
                    "custom_fb_order": "FB-ORDER-1",
                    "custom_fb_shift": "FB-SHIFT-1",
                }
            ]
        if doctype == "Stock Ledger Entry":
            assert filters == {
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-RETURN-1",
                "is_cancelled": 0,
            }
            return [
                {
                    "name": "SLE-RETURN-1",
                    "voucher_type": "Stock Entry",
                    "voucher_no": "STE-RETURN-1",
                    "voucher_detail_no": "STE-RETURN-DETAIL-1",
                    "item_code": "SMOKE-MATCHA-POWDER",
                    "warehouse": "Store - K",
                    "actual_qty": "22.500000",
                    "qty_after_transaction": "982.500000",
                    "is_cancelled": 0,
                    "posting_datetime": datetime(2026, 7, 12, 10, 5, 0),
                    "creation": datetime(2026, 7, 12, 10, 5, 1),
                }
            ]
        if doctype == "GL Entry":
            return []
        raise AssertionError(f"unexpected doctype {doctype}")

    reversal_item = SimpleNamespace(
        name="STE-RETURN-DETAIL-1",
        item_code="SMOKE-MATCHA-POWDER",
        qty="22.500000",
        transfer_qty="22.500000",
        stock_uom="Gram",
        s_warehouse=None,
        t_warehouse="Store - K",
    )

    def fake_get_value(doctype: str, name: str, fieldname: str) -> Any:
        values = {
            ("Sales Invoice", "SINV-RETURN-1", "outstanding_amount"): "0.00",
            ("Journal Entry", "JV-RETURN-1", "docstatus"): 1,
        }
        return values.get((doctype, name, fieldname))

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)
    monkeypatch.setattr(
        frappe,
        "get_doc",
        lambda doctype, name: SimpleNamespace(
            doctype=doctype, name=name, items=[reversal_item]
        ),
    )
    monkeypatch.setattr(frappe.db, "get_value", fake_get_value)

    records = smoke._collect_return_records(
        [{"name": "FB-ORDER-1"}],
        include_inventory_regression=True,
    )

    assert records[0]["request_fingerprint"] == "a" * 64
    assert records[0]["approval_token_id"] == "APPROVAL-RETURN-1"
    assert records[0]["approved_by_manager"] == "manager@example.com"
    assert records[0]["lines"] == [
        {
            "name": "RETURN-LINE-1",
            "original_sales_invoice_item": "SINV-ITEM-1",
            "original_fb_order_line_ref": "ORDER-LINE-1",
            "original_resolved_sale": "RESOLVED-SALE-1",
            "qty_returned": "1.250000",
            "commercial_modifier_snapshot_json": "[]",
            "reversal_stock_entry": "STE-RETURN-1",
        },
        {
            "name": "RETURN-LINE-2",
            "original_sales_invoice_item": "SINV-ITEM-2",
            "original_fb_order_line_ref": "ORDER-LINE-2",
            "original_resolved_sale": "RESOLVED-SALE-2",
            "qty_returned": "2.000000",
            "commercial_modifier_snapshot_json": "[]",
            "reversal_stock_entry": "STE-RETURN-1",
        },
    ]
    assert records[0]["reversal_stock_entries"] == [
        {
            "name": "STE-RETURN-1",
            "docstatus": 1,
            "purpose": "Material Receipt",
            "stock_entry_type": "Material Receipt",
            "posting_date": "2026-07-12",
            "posting_time": "10:05:00",
            "custom_fb_order": "FB-ORDER-1",
            "custom_fb_shift": "FB-SHIFT-1",
            "items": [
                {
                    "name": "STE-RETURN-DETAIL-1",
                    "item_code": "SMOKE-MATCHA-POWDER",
                    "qty": "22.500000",
                    "transfer_qty": "22.500000",
                    "stock_uom": "Gram",
                    "s_warehouse": None,
                    "t_warehouse": "Store - K",
                }
            ],
            "stock_ledger_entries": [
                {
                    "name": "SLE-RETURN-1",
                    "voucher_type": "Stock Entry",
                    "voucher_no": "STE-RETURN-1",
                    "voucher_detail_no": "STE-RETURN-DETAIL-1",
                    "item_code": "SMOKE-MATCHA-POWDER",
                    "warehouse": "Store - K",
                    "actual_qty": "22.500000",
                    "qty_after_transaction": "982.500000",
                    "is_cancelled": False,
                    "posting_datetime": "2026-07-12 10:05:00",
                    "creation": "2026-07-12 10:05:01",
                }
            ],
        }
    ]
    assert [call for call in calls if call[0] == "Stock Entry"] == [
        ("Stock Entry", {"name": ["in", ["STE-RETURN-1"]]})
    ]


@pytest.mark.inventory_regression
def test_bin_dump_includes_exact_zero_baseline_for_each_tracked_ingredient(
    monkeypatch: Any,
) -> None:
    import frappe

    def fake_get_all(
        doctype: str,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if doctype == "Item":
            assert filters == {
                "is_stock_item": 1,
                "custom_kopos_track_stock": 1,
            }
            return [
                {"item_code": "SMOKE-MATCHA-POWDER"},
                {"item_code": "SMOKE-MILK"},
            ]
        if doctype == "Bin":
            assert filters == {
                "warehouse": "Store - K",
                "item_code": [
                    "in",
                    ["SMOKE-MATCHA-POWDER", "SMOKE-MILK"],
                ],
            }
            return [
                {
                    "name": "BIN-MATCHA",
                    "item_code": "SMOKE-MATCHA-POWDER",
                    "warehouse": "Store - K",
                    "actual_qty": "1000.125000",
                }
            ]
        return []

    monkeypatch.setattr(frappe, "get_all", fake_get_all)

    assert smoke._collect_ingredient_bin_balances("Store - K") == [
        {
            "name": "BIN-MATCHA",
            "item_code": "SMOKE-MATCHA-POWDER",
            "warehouse": "Store - K",
            "actual_qty": "1000.125000",
        },
        {
            "name": None,
            "item_code": "SMOKE-MILK",
            "warehouse": "Store - K",
            "actual_qty": "0",
        },
    ]
