from __future__ import annotations

import importlib
import inspect
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
install_fake_frappe_modules()


class FakeDoc(SimpleNamespace):
    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)


def _order_modifier_snapshot() -> str:
    return json.dumps(
        [
            {
                "modifier_group": "MILK",
                "modifier": "OAT",
                "price_adjustment": "1.50",
                "instruction_text": None,
                "sort_order": 1,
                "affects_stock": 0,
                "affects_recipe": 0,
            }
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _invoice_modifier_snapshot() -> str:
    return json.dumps(
        {
            "modifiers": [
                {
                    "id": "OAT",
                    "name": "Oat Milk",
                    "group_id": "MILK",
                    "price_adjustment": "1.50",
                    "price_adjustment_sen": 150,
                }
            ]
        },
        separators=(",", ":"),
    )


def test_return_event_accepts_invoice_item_identity_without_order_line_ref() -> None:
    controller = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_return_event.fb_return_event"
    )
    event = FakeDoc(
        return_id="RETURN-LEGACY-INVOICE-1",
        refund_method="cash",
        lines=[
            FakeDoc(
                original_sales_invoice_item="SINV-ITEM-LEGACY-1",
                original_fb_order_line_ref=None,
                original_resolved_sale=None,
            )
        ],
    )

    controller.FBReturnEvent.validate(event)


def test_full_commercial_refund_uses_invoice_items_without_optional_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    observed_doctypes: list[str] = []

    def get_all(doctype: str, **_kwargs: Any) -> list[dict[str, Any]]:
        observed_doctypes.append(doctype)
        if doctype == "Sales Invoice Item":
            return [
                {
                    "name": "SINV-ITEM-1",
                    "item_code": "AMERICANO",
                    "qty": "2",
                    "custom_fb_order_line_ref": "LINE-1",
                    "custom_fb_resolved_sale": None,
                    "custom_kopos_modifiers": _invoice_modifier_snapshot(),
                }
            ]
        raise AssertionError(f"cashier refund queried unexpected DocType {doctype}")

    monkeypatch.setattr(api.frappe, "get_all", get_all)

    lines = api._build_full_commercial_return_lines("SINV-1", "FB-ORDER-1")

    assert observed_doctypes == ["Sales Invoice Item"]
    assert lines == [
        {
            "original_sales_invoice_item": "SINV-ITEM-1",
            "original_fb_order_line_ref": "LINE-1",
            "original_resolved_sale": None,
            "qty_returned": 2.0,
            "commercial_modifier_snapshot_json": "",
        }
    ]


def test_full_commercial_refund_ignores_tampered_modifier_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")

    def get_all(doctype: str, **_kwargs: Any) -> list[dict[str, Any]]:
        if doctype == "Sales Invoice Item":
            return [
                {
                    "name": "SINV-ITEM-1",
                    "item_code": "AMERICANO",
                    "qty": 1,
                    "custom_fb_order_line_ref": "LINE-1",
                    "custom_fb_resolved_sale": None,
                    "custom_kopos_modifiers": _invoice_modifier_snapshot(),
                }
            ]
        raise AssertionError(f"refund queried optional DocType {doctype}")

    monkeypatch.setattr(api.frappe, "get_all", get_all)

    assert api._build_full_commercial_return_lines("SINV-1", "FB-ORDER-1") == [
        {
            "original_sales_invoice_item": "SINV-ITEM-1",
            "original_fb_order_line_ref": "LINE-1",
            "original_resolved_sale": None,
            "qty_returned": 1.0,
            "commercial_modifier_snapshot_json": "",
        }
    ]


def test_historical_order_line_never_loads_modifier_or_recipe_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    observed_doctypes: list[str] = []

    def get_all(doctype: str, **_kwargs: Any) -> list[dict[str, Any]]:
        observed_doctypes.append(doctype)
        if doctype == "Sales Invoice Item":
            return [
                {
                    "name": "SINV-ITEM-1",
                    "item_code": "AMERICANO",
                    "qty": 1,
                    "custom_fb_order_line_ref": "LINE-1",
                    "custom_fb_resolved_sale": None,
                    "custom_kopos_modifiers": _invoice_modifier_snapshot(),
                }
            ]
        raise AssertionError(f"unexpected recipe/inventory read: {doctype}")

    monkeypatch.setattr(api.frappe, "get_all", get_all)

    lines = api._build_full_commercial_return_lines("SINV-1", "FB-ORDER-1")

    assert observed_doctypes == ["Sales Invoice Item"]
    assert lines == [
        {
            "original_sales_invoice_item": "SINV-ITEM-1",
            "original_fb_order_line_ref": "LINE-1",
            "original_resolved_sale": None,
            "qty_returned": 1.0,
            "commercial_modifier_snapshot_json": "",
        }
    ]


def test_historical_invoice_item_without_order_line_is_complete_refund_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    observed_doctypes: list[str] = []

    def get_all(doctype: str, **_kwargs: Any) -> list[dict[str, Any]]:
        observed_doctypes.append(doctype)
        if doctype != "Sales Invoice Item":
            raise AssertionError(f"historical refund queried optional {doctype}")
        return [
            {
                "name": "SINV-HISTORICAL-ITEM-1",
                "item_code": "AMERICANO",
                "qty": "1",
                "custom_fb_order_line_ref": None,
                "custom_fb_resolved_sale": "STALE-RESOLVED-SALE",
                "custom_kopos_modifiers": _invoice_modifier_snapshot(),
            }
        ]

    monkeypatch.setattr(api.frappe, "get_all", get_all)
    monkeypatch.setattr(
        api.frappe.db,
        "get_value",
        lambda *_args, **_kwargs: None,
    )

    lines = api._build_full_commercial_return_lines("SINV-HISTORICAL", None)

    assert observed_doctypes == ["Sales Invoice Item"]
    assert lines == [
        {
            "original_sales_invoice_item": "SINV-HISTORICAL-ITEM-1",
            "original_fb_order_line_ref": None,
            "original_resolved_sale": None,
            "qty_returned": 1.0,
            "commercial_modifier_snapshot_json": "",
        }
    ]


def test_legacy_offline_refund_payload_is_rebuilt_from_sales_invoice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    rebuilt_lines = [
        {
            "original_sales_invoice_item": "SINV-ITEM-1",
            "original_fb_order_line_ref": None,
            "original_resolved_sale": None,
            "qty_returned": 1.0,
            "commercial_modifier_snapshot_json": "[]",
        }
    ]
    rebuilds: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        api,
        "_build_full_commercial_return_lines",
        lambda invoice, order: rebuilds.append((invoice, order)) or rebuilt_lines,
    )
    monkeypatch.setattr(api, "_lock_sales_invoice_cash_shift", lambda _invoice: "SHIFT-1")
    monkeypatch.setattr(
        api,
        "_validate_full_return_lines",
        lambda lines, *_args: lines,
    )
    monkeypatch.setattr(api.frappe.db, "exists", lambda *_args: True)
    monkeypatch.setattr(
        api.frappe.db,
        "sql",
        lambda *_args, **_kwargs: [
            {
                "name": "SINV-1",
                "docstatus": 1,
                "is_return": 0,
                "custom_fb_order": "FB-ORDER-1",
                "custom_fb_device_id": "DEVICE-1",
                "custom_fb_shift": "SHIFT-1",
                "grand_total": 12,
            }
        ],
    )
    monkeypatch.setattr(
        api.frappe,
        "get_doc",
        lambda *_args: FakeDoc(
            docstatus=1,
            is_return=0,
            custom_fb_order="FB-ORDER-1",
            custom_fb_device_id="DEVICE-1",
            custom_fb_shift="SHIFT-1",
            grand_total=12,
        ),
    )

    validated = api._validate_payload(
        {
            "return_id": "RETURN-1",
            "device_id": "DEVICE-1",
            "original_sales_invoice": "SINV-1",
            "refund_method": "cash",
            "return_to_stock": 1,
            "lines": [
                {
                    "original_resolved_sale": "REMOVED-OPTIONAL-ROW",
                    "qty_returned": 1,
                }
            ],
        }
    )

    assert rebuilds == [("SINV-1", None)]
    assert validated["return_to_stock"] == 0
    assert validated["inventory_evaluation"] == "excluded_not_evaluated"
    assert validated["legacy_return_to_stock_normalized"] is True
    assert validated["lines"] == [
        {
            **rebuilt_lines[0],
            "original_fb_order_line_ref": "",
            "commercial_modifier_snapshot_json": "",
        }
    ]


def test_new_commercial_refund_rejects_stock_return_claim_before_database_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    database_read = False

    def unexpected_database_read(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal database_read
        database_read = True
        raise AssertionError("contradictory inventory claim reached the database")

    monkeypatch.setattr(api.frappe.db, "get_value", unexpected_database_read)
    monkeypatch.setattr(api.frappe.db, "exists", unexpected_database_read)
    monkeypatch.setattr(api.frappe.db, "sql", unexpected_database_read)

    with pytest.raises(
        api.frappe.ValidationError,
        match="This refund does not change stock",
    ):
        api._validate_payload(
            {
                "return_id": "RETURN-NEW-1",
                "device_id": "DEVICE-1",
                "fb_order": "FB-ORDER-1",
                "refund_method": "cash",
                "return_to_stock": 1,
                "inventory_evaluation": "excluded_not_evaluated",
            }
        )

    assert database_read is False


def test_exact_legacy_stock_flag_retry_remains_idempotent() -> None:
    api = importlib.import_module("kopos_connector.api.fb_returns")
    line = FakeDoc(
        original_sales_invoice_item="SINV-ITEM-1",
        original_fb_order_line_ref="LINE-1",
        original_resolved_sale=None,
        qty_returned=1,
        commercial_modifier_snapshot_json="",
    )
    existing = FakeDoc(
        request_fingerprint="legacy-fingerprint",
        original_sales_invoice="SINV-1",
        fb_order="FB-ORDER-1",
        return_to_stock=1,
        refund_method="cash",
        lines=[line],
    )
    validated = {
        "request_fingerprint": "current-fingerprint",
        "legacy_request_fingerprint": "legacy-fingerprint",
        "original_sales_invoice": "SINV-1",
        "fb_order": "FB-ORDER-1",
        "return_to_stock": 0,
        "refund_method": "cash",
        "lines": [line],
    }

    api._validate_existing_return_matches(validated, existing)


def test_commercial_return_guard_locks_invoice_items_not_resolved_sales(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = importlib.import_module(
        "kopos_connector.kopos.services.operations.return_guard_service"
    )
    queries: list[str] = []

    class FakeDB:
        def sql(
            self,
            query: str,
            values: tuple[str, ...],
            *,
            as_dict: bool,
        ) -> list[dict[str, Any]]:
            assert as_dict is True
            assert "FOR UPDATE" in query
            assert "tabFB Resolved Sale" not in query
            queries.append(query)
            if "tabSales Invoice Item" in query:
                assert values == ("SINV-1",)
                return [
                    {
                        "name": "SINV-ITEM-1",
                        "qty": 1,
                        "custom_fb_order_line_ref": "LINE-1",
                    }
                ]
            assert values == ("SINV-ITEM-1",)
            return []

    monkeypatch.setattr(guard.frappe, "db", FakeDB())

    guard.lock_and_validate_return_quantities(
        "RETURN-1",
        [
            {
                "original_sales_invoice_item": "SINV-ITEM-1",
                "original_fb_order_line_ref": "LINE-1",
                "original_resolved_sale": None,
                "qty_returned": 1,
                "commercial_modifier_snapshot_json": "[]",
            }
        ],
        "SINV-1",
    )

    assert len(queries) == 2
    assert "original_sales_invoice_item" in queries[1]


def test_cashier_return_has_no_inventory_import_or_stock_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = importlib.import_module(
        "kopos_connector.kopos.services.operations.return_service"
    )
    assert "kopos_connector.kopos.services.inventory" not in inspect.getsource(service)
    assert "create_reversal_stock_entry" not in inspect.getsource(service)
    original_invoice = FakeDoc(name="SINV-1", custom_fb_shift="SHIFT-1")
    return_event = FakeDoc(
        name="FB-RETURN-1",
        original_sales_invoice="SINV-1",
        return_to_stock=1,
        lines=[],
    )
    refreshes: list[str] = []

    monkeypatch.setattr(
        service.frappe,
        "get_doc",
        lambda doctype, name: original_invoice,
    )
    monkeypatch.setattr(service, "lock_fb_shift_cash_scope", lambda shift: None)
    monkeypatch.setattr(service, "elevate_device_api_user", lambda: nullcontext())
    monkeypatch.setattr(service, "create_return_sales_invoice", lambda doc: "SINV-RETURN-1")
    monkeypatch.setattr(
        service,
        "ensure_return_settlement",
        lambda doc, invoice: "JV-RETURN-1",
    )
    monkeypatch.setattr(
        service,
        "refresh_fb_shift_cash",
        lambda shift: refreshes.append(shift),
    )

    assert service.process_return_event(return_event) == ("SINV-RETURN-1", None)
    assert refreshes == ["SHIFT-1"]


def test_credit_note_validates_commercial_line_identity_without_resolved_sale() -> None:
    service = importlib.import_module(
        "kopos_connector.kopos.services.accounting.return_invoice_service"
    )
    return_event = FakeDoc(
        lines=[
            FakeDoc(
                original_sales_invoice_item="SINV-ITEM-1",
                original_fb_order_line_ref="LINE-1",
                original_resolved_sale=None,
                qty_returned=1,
                commercial_modifier_snapshot_json="[]",
            )
        ]
    )
    return_invoice = FakeDoc(
        items=[
            FakeDoc(
                sales_invoice_item="SINV-ITEM-1",
                custom_fb_order_line_ref="LINE-1",
                qty=-1,
            )
        ]
    )

    service._validate_full_standard_return_items(return_event, return_invoice)

    return_invoice.items[0].custom_fb_order_line_ref = "LINE-TAMPERED"
    service._validate_full_standard_return_items(return_event, return_invoice)


def test_credit_note_clears_stale_optional_resolved_sale_link() -> None:
    service = importlib.import_module(
        "kopos_connector.kopos.services.accounting.return_invoice_service"
    )
    original_item = FakeDoc(
        name="SINV-ITEM-1",
        custom_fb_order_line_ref=None,
        custom_kopos_modifiers="{corrupt optional decoration",
        custom_kopos_modifier_total=0,
        custom_kopos_has_modifiers=0,
    )
    return_item = FakeDoc(
        sales_invoice_item="SINV-ITEM-1",
        custom_fb_order_line_ref=None,
        custom_fb_resolved_sale="STALE-DELETED-OPTIONAL-ROW",
        custom_kopos_modifiers=None,
        custom_kopos_modifier_total=0,
        custom_kopos_has_modifiers=0,
    )

    service._copy_commercial_line_provenance(
        FakeDoc(items=[original_item]),
        FakeDoc(items=[return_item]),
    )
    service._clear_optional_return_links(
        FakeDoc(
            lines=[
                FakeDoc(original_sales_invoice_item="SINV-ITEM-1")
            ]
        ),
        FakeDoc(items=[return_item]),
    )

    assert return_item.custom_fb_resolved_sale is None
    assert return_item.custom_kopos_modifiers == "{corrupt optional decoration"


def test_legacy_resolved_sale_lines_remain_supported() -> None:
    guard = importlib.import_module(
        "kopos_connector.kopos.services.operations.return_guard_service"
    )

    assert guard.aggregate_return_lines(
        [
            {"original_resolved_sale": "RS-1", "qty_returned": "0.25"},
            {"original_resolved_sale": "RS-1", "qty_returned": "0.75"},
        ]
    ) == [{"original_resolved_sale": "RS-1", "qty_returned": 1.0}]
