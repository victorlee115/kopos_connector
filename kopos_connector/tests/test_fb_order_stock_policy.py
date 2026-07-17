from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest


class FakeLogDoc:
    def __init__(self, doctype: str, sink: list["FakeLogDoc"]):
        self.doctype = doctype
        self._sink = sink
        self.name: str | None = None
        self.order_reference: str | None = None
        self.logged_at: datetime | None = None

    def insert(self, ignore_permissions: bool = False):
        self.name = f"FB-OVERRIDE-LOG-{len(self._sink) + 1}"
        self._sink.append(self)
        return self


@pytest.fixture
def fake_frappe(monkeypatch):
    created_logs: list[FakeLogDoc] = []
    stock_by_bin: dict[tuple[str, str], float] = {}
    stock_policy = {"allow_negative_stock": 1, "restricted_items": set()}
    timestamp = datetime(2026, 4, 20, 12, 0, 0)

    frappe_module: Any = types.ModuleType("frappe")
    frappe_utils_module: Any = types.ModuleType("frappe.utils")
    frappe_model_document_module: Any = types.ModuleType("frappe.model.document")

    class Document:
        pass

    def get_bin_value(doctype: str, filters: dict[str, str], fieldname: str):
        assert doctype == "Bin"
        assert fieldname == "actual_qty"
        return stock_by_bin.get((filters["item_code"], filters["warehouse"]), 0)

    frappe_module.db = SimpleNamespace(
        get_value=get_bin_value,
        get_single_value=lambda doctype, fieldname: stock_policy[
            "allow_negative_stock"
        ]
        if (doctype, fieldname) == ("Stock Settings", "allow_negative_stock")
        else None,
    )
    frappe_module.get_all = lambda doctype, **kwargs: [
        {
            "name": item_code,
            "has_serial_no": 1,
            "has_batch_no": 0,
        }
        for item_code in sorted(stock_policy["restricted_items"])
    ] if doctype == "Item" else []
    frappe_module.new_doc = lambda doctype: FakeLogDoc(doctype, created_logs)
    frappe_module.generate_hash = lambda length=8: "X" * length
    frappe_module.scrub = (
        lambda value: str(value).replace("_", "-").replace(" ", "-").lower()
    )
    frappe_module.ValidationError = type("ValidationError", (Exception,), {})
    frappe_module.throw = lambda message, exc=None: (_ for _ in ()).throw(
        (exc or frappe_module.ValidationError)(message)
    )

    frappe_utils_module.now_datetime = lambda: timestamp
    frappe_utils_module.flt = lambda value: float(value or 0)
    frappe_utils_module.cstr = lambda value: "" if value is None else str(value)
    frappe_model_document_module.Document = Document

    monkeypatch.setitem(sys.modules, "frappe", frappe_module)
    monkeypatch.setitem(sys.modules, "frappe.utils", frappe_utils_module)
    monkeypatch.setitem(
        sys.modules, "frappe.model.document", frappe_model_document_module
    )

    dependency_stubs = {
        "kopos_connector.kopos.doctype.fb_modifier_group.fb_modifier_group": {
            "filter_visible_allowed_modifier_groups": lambda *args, **kwargs: []
        },
        "kopos_connector.kopos.services.accounting.sales_invoice_service": {
            "create_sales_invoice": lambda *args, **kwargs: None
        },
        "kopos_connector.kopos.services.accounting.maybank_payment_service": {
            "register_qr_payment_settlement": lambda *args, **kwargs: None
        },
        "kopos_connector.kopos.services.inventory.stock_issue_service": {
            "create_ingredient_stock_entry": lambda *args, **kwargs: None
        },
        "kopos_connector.kopos.services.projection.log_service": {
            "create_projection_log": lambda *args, **kwargs: "PROJECTION-LOG",
            "update_projection_state": lambda *args, **kwargs: None,
        },
    }

    for module_name, attributes in dependency_stubs.items():
        module = types.ModuleType(module_name)
        for attr_name, attr_value in attributes.items():
            setattr(module, attr_name, attr_value)
        monkeypatch.setitem(sys.modules, module_name, module)

    for module_name in [
        "kopos_connector.kopos.services.inventory.warning_service",
        "kopos_connector.kopos.doctype.fb_order.fb_order",
    ]:
        sys.modules.pop(module_name, None)

    return SimpleNamespace(
        created_logs=created_logs,
        stock_by_bin=stock_by_bin,
        stock_policy=stock_policy,
        timestamp=timestamp,
    )


def test_detect_and_log_stock_shortfall(fake_frappe):
    fake_frappe.stock_by_bin[("ITEM-1", "WH-1")] = 1.0
    warning_service = importlib.import_module(
        "kopos_connector.kopos.services.inventory.warning_service"
    )

    shortfalls = warning_service.detect_stock_shortfall(
        [
            {
                "item": "ITEM-1",
                "warehouse": "WH-1",
                "stock_qty": 1.25,
                "affects_stock": 1,
            },
            {
                "item": "ITEM-1",
                "warehouse": "WH-1",
                "stock_qty": 0.75,
                "affects_stock": 1,
            },
        ]
    )

    assert len(shortfalls) == 1
    assert shortfalls[0]["item_code"] == "ITEM-1"
    assert shortfalls[0]["warehouse"] == "WH-1"
    assert shortfalls[0]["required_qty"] == 2.0
    assert shortfalls[0]["available_qty"] == 1.0
    assert shortfalls[0]["shortfall_qty"] == 1.0

    log_names = warning_service.log_stock_shortfall(
        SimpleNamespace(name="FB-ORDER-1", order_id="ORDER-1"),
        shortfalls,
        timestamp=fake_frappe.timestamp,
    )

    assert log_names == ["FB-OVERRIDE-LOG-1"]
    assert len(fake_frappe.created_logs) == 1

    log_doc = fake_frappe.created_logs[0]
    assert log_doc.fb_order == "FB-ORDER-1"
    assert log_doc.order_reference == "ORDER-1"
    assert log_doc.item == "ITEM-1"
    assert log_doc.warehouse == "WH-1"
    assert log_doc.requested_qty == 2.0
    assert log_doc.available_qty_before == 1.0
    assert log_doc.shortfall_qty == 1.0
    assert log_doc.logged_at == fake_frappe.timestamp
    assert log_doc.approved_at == fake_frappe.timestamp


def test_before_submit_logs_shortfall_without_throwing(fake_frappe):
    fake_frappe.stock_by_bin[("ITEM-1", "WH-1")] = 0.5
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )

    order = fb_order_module.FBOrder()
    order.name = "FB-ORDER-1"
    order.order_id = "ORDER-1"
    order.booth_warehouse = "WH-1"
    captured_resolutions: list[object] = []
    line_resolutions = [
        {
            "resolved_components": [
                {"item": "ITEM-1", "stock_qty": 1.0, "affects_stock": 1}
            ]
        }
    ]

    order.build_line_resolutions = lambda: line_resolutions
    order.create_resolved_sales = lambda resolutions: captured_resolutions.append(
        resolutions
    )

    order.before_submit()

    assert captured_resolutions == [line_resolutions]
    assert len(fake_frappe.created_logs) == 1
    assert fake_frappe.created_logs[0].item == "ITEM-1"
    assert fake_frappe.created_logs[0].order_reference == "ORDER-1"


def test_before_submit_rejects_shortfall_when_negative_stock_policy_is_disabled(
    fake_frappe,
):
    fake_frappe.stock_by_bin[("ITEM-1", "WH-1")] = 0
    fake_frappe.stock_policy["allow_negative_stock"] = 0
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )

    order = fb_order_module.FBOrder()
    order.name = "FB-ORDER-1"
    order.order_id = "ORDER-1"
    order.booth_warehouse = "WH-1"

    with pytest.raises(
        fb_order_module.frappe.ValidationError,
        match="Allow Negative Stock",
    ):
        order.validate_stock_availability(
            [
                {
                    "resolved_components": [
                        {"item": "ITEM-1", "stock_qty": 1, "affects_stock": 1}
                    ]
                }
            ]
        )

    assert fake_frappe.created_logs == []


def test_before_submit_rejects_serialised_shortfall_even_when_negative_stock_is_enabled(
    fake_frappe,
):
    fake_frappe.stock_by_bin[("SERIAL-ITEM", "WH-1")] = 0
    fake_frappe.stock_policy["restricted_items"].add("SERIAL-ITEM")
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )

    order = fb_order_module.FBOrder()
    order.name = "FB-ORDER-1"
    order.order_id = "ORDER-1"
    order.booth_warehouse = "WH-1"

    with pytest.raises(
        fb_order_module.frappe.ValidationError,
        match="serialised or batched",
    ):
        order.validate_stock_availability(
            [
                {
                    "resolved_components": [
                        {
                            "item": "SERIAL-ITEM",
                            "stock_qty": 1,
                            "affects_stock": 1,
                        }
                    ]
                }
            ]
        )

    assert fake_frappe.created_logs == []


def test_before_submit_still_raises_non_stock_failures(fake_frappe):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )

    order = fb_order_module.FBOrder()
    order.booth_warehouse = "WH-1"
    order.build_line_resolutions = lambda: []

    def raise_non_stock_failure(_line_resolutions):
        raise RuntimeError("resolved sale projection failed")

    order.create_resolved_sales = raise_non_stock_failure

    with pytest.raises(RuntimeError, match="resolved sale projection failed"):
        order.before_submit()


def _prepared_component() -> dict[str, object]:
    return {
        "item": "ITEM-1",
        "source_type": "Base Recipe",
        "qty": 1.0,
        "uom": "Gram",
        "stock_qty": 1.0,
        "stock_uom": "Gram",
        "warehouse": "WH-1",
        "source_reference": "component-1",
        "affects_stock": 1,
        "affects_cogs": 1,
        "remarks": None,
    }


def test_prepared_sale_reuses_only_the_frozen_resolved_snapshot(
    fake_frappe,
    monkeypatch,
):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    component = _prepared_component()
    resolved_sale = SimpleNamespace(
        name="RESOLVED-1",
        fb_order="FB-ORDER-1",
        fb_order_line="FB-ORDER-LINE-1",
        backend_line_uuid="LINE-UUID-1",
        sellable_item="ITEM-SALE-1",
        booth_warehouse="WH-1",
        recipe="RECIPE-1",
        recipe_version="1",
        qty=1,
        status="Prepared",
        selected_modifiers=[],
        resolved_components=[
            SimpleNamespace(
                **component,
                name="CHILD-METADATA-MUST-NOT-BE-HASHED",
                parent="RESOLVED-1",
            )
        ],
    )
    resolved_sale.resolution_hash = fb_order_module._resolution_hash(
        recipe="RECIPE-1",
        recipe_version="1",
        selected_modifiers=[],
        resolved_components=[component],
    )
    monkeypatch.setattr(
        fb_order_module.frappe,
        "get_doc",
        lambda doctype, name: resolved_sale,
        raising=False,
    )

    line = SimpleNamespace(
        name="FB-ORDER-LINE-1",
        backend_line_uuid="LINE-UUID-1",
        item="ITEM-SALE-1",
        recipe="RECIPE-1",
        recipe_version="1",
        qty=1,
        resolved_sale="RESOLVED-1",
        resolved_components_snapshot=json.dumps([component], sort_keys=True),
    )
    order = fb_order_module.FBOrder()
    order.name = "FB-ORDER-1"
    order.booth_warehouse = "WH-1"
    order.items = [line]

    resolutions = order.validate_prepared_resolved_sales()

    assert resolutions[0]["resolved_sale"] is resolved_sale
    assert resolutions[0]["resolved_components"] == [component]


def test_prepared_sale_rejects_a_changed_resolution_hash(fake_frappe, monkeypatch):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    component = _prepared_component()
    resolved_sale = SimpleNamespace(
        name="RESOLVED-1",
        fb_order="FB-ORDER-1",
        fb_order_line="FB-ORDER-LINE-1",
        backend_line_uuid="LINE-UUID-1",
        sellable_item="ITEM-SALE-1",
        booth_warehouse="WH-1",
        recipe="RECIPE-1",
        recipe_version="1",
        qty=1,
        status="Prepared",
        resolution_hash="tampered",
        selected_modifiers=[],
        resolved_components=[SimpleNamespace(**component)],
    )
    monkeypatch.setattr(
        fb_order_module.frappe,
        "get_doc",
        lambda doctype, name: resolved_sale,
        raising=False,
    )
    order = fb_order_module.FBOrder()
    order.name = "FB-ORDER-1"
    order.booth_warehouse = "WH-1"
    order.items = [
        SimpleNamespace(
            name="FB-ORDER-LINE-1",
            backend_line_uuid="LINE-UUID-1",
            item="ITEM-SALE-1",
            recipe="RECIPE-1",
            recipe_version="1",
            qty=1,
            resolved_sale="RESOLVED-1",
            resolved_components_snapshot=json.dumps([component], sort_keys=True),
        )
    ]

    with pytest.raises(
        fb_order_module.frappe.ValidationError,
        match="resolution hash does not match",
    ):
        order.validate_prepared_resolved_sales()


def test_prepared_before_submit_never_re_resolves_the_current_catalog(
    fake_frappe,
    monkeypatch,
):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    frozen_resolutions = [{"resolved_components": []}]
    calls: list[tuple[str, object]] = []
    order = fb_order_module.FBOrder()
    order.accepted_sale_fingerprint = "f" * 64
    order.build_line_resolutions = lambda: (_ for _ in ()).throw(
        AssertionError("prepared sale must not re-resolve the catalog")
    )
    order.validate_prepared_resolved_sales = lambda: frozen_resolutions
    order.validate_stock_availability = lambda value: calls.append(("stock", value))
    order.mark_prepared_resolved_sales_submitted = lambda value: calls.append(
        ("resolved", value)
    )
    monkeypatch.setattr(
        fb_order_module,
        "register_qr_payment_settlement",
        lambda value: calls.append(("settlement", value)),
    )

    order.before_submit()

    assert calls == [
        ("resolved", frozen_resolutions),
        ("settlement", order),
    ]


def test_prepared_sale_rejects_persisted_price_customer_or_payment_edits(
    fake_frappe,
) -> None:
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    before = SimpleNamespace(
        accepted_sale_fingerprint="f" * 64,
        customer="CUSTOMER-1",
        grand_total=Decimal("12.50"),
        items=[
            SimpleNamespace(
                line_id="LINE-1",
                item="ITEM-1",
                qty=1,
                unit_price=Decimal("12.50"),
            )
        ],
        payments=[
            SimpleNamespace(
                source_payment_id="PAY-1",
                payment_method="DuitNow QR",
                payment_channel_code="maybank",
                amount=Decimal("12.50"),
                tendered_amount=Decimal("12.50"),
                change_amount=Decimal("0"),
            )
        ],
    )
    order = fb_order_module.FBOrder()
    order.accepted_sale_fingerprint = "f" * 64
    order.customer = "CUSTOMER-2"
    order.grand_total = Decimal("12.50")
    order.items = list(before.items)
    order.payments = list(before.payments)
    order.get_doc_before_save = lambda: before

    with pytest.raises(
        fb_order_module.frappe.ValidationError,
        match=r"immutable sale snapshot cannot be changed: order\.customer",
    ):
        order.validate_prepared_sale_immutability()


def test_prepared_sale_allows_only_qr_settlement_lifecycle_updates(
    fake_frappe,
) -> None:
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    payment_before = SimpleNamespace(
        source_payment_id="PAY-1",
        payment_method="DuitNow QR",
        payment_channel_code="maybank",
        amount=Decimal("12.50"),
        tendered_amount=Decimal("12.50"),
        change_amount=Decimal("0"),
        external_transaction_id=None,
        settlement_status="awaiting_provider",
    )
    payment_after = SimpleNamespace(**vars(payment_before))
    payment_after.external_transaction_id = "MB-REF-1"
    payment_after.settlement_status = "pending_reconciliation"
    before = SimpleNamespace(
        accepted_sale_fingerprint="f" * 64,
        automatic_qr_state="provider_pending",
        items=[],
        payments=[payment_before],
    )
    order = fb_order_module.FBOrder()
    order.accepted_sale_fingerprint = "f" * 64
    order.automatic_qr_state = "manual_pending_reconciliation"
    order.items = []
    order.payments = [payment_after]
    order.get_doc_before_save = lambda: before

    order.validate_prepared_sale_immutability()


def test_prepared_sale_tax_rate_canonicalizes_omitted_float_to_zero(
    fake_frappe,
) -> None:
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    before = SimpleNamespace(
        accepted_sale_fingerprint="f" * 64,
        tax_rate=0,
        items=[],
        payments=[],
    )
    order = fb_order_module.FBOrder()
    order.accepted_sale_fingerprint = "f" * 64
    order.tax_rate = None
    order.items = []
    order.payments = []
    order.get_doc_before_save = lambda: before

    order.validate_prepared_sale_immutability()

    order.tax_rate = Decimal("0.08")
    with pytest.raises(
        fb_order_module.frappe.ValidationError,
        match=r"immutable sale snapshot cannot be changed: order\.tax_rate",
    ):
        order.validate_prepared_sale_immutability()

    before.tax_rate = Decimal("0.08")
    order.tax_rate = Decimal("0.0800005")
    with pytest.raises(
        fb_order_module.frappe.ValidationError,
        match=r"immutable sale snapshot cannot be changed: order\.tax_rate",
    ):
        order.validate_prepared_sale_immutability()


def test_unprepared_before_submit_still_validates_current_stock(
    fake_frappe,
    monkeypatch,
):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    current_resolutions = [{"resolved_components": []}]
    calls: list[tuple[str, object]] = []
    order = fb_order_module.FBOrder()
    order.accepted_sale_fingerprint = ""
    order.build_line_resolutions = lambda: current_resolutions
    order.validate_stock_availability = lambda value: calls.append(("stock", value))
    order.create_resolved_sales = lambda value: calls.append(("resolved", value))
    monkeypatch.setattr(
        fb_order_module,
        "register_qr_payment_settlement",
        lambda value: calls.append(("settlement", value)),
    )

    order.before_submit()

    assert calls == [
        ("stock", current_resolutions),
        ("resolved", current_resolutions),
        ("settlement", order),
    ]


def test_on_submit_marks_noop_stock_projection_as_terminal_success(fake_frappe, monkeypatch):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    projection_updates: list[tuple[str, str, str, str | None, str | None]] = []
    stock_service_called = {"value": False}

    def fail_stock_service(*_args, **_kwargs):
        stock_service_called["value"] = True
        raise AssertionError("stock service should not run for no-op stock projections")

    monkeypatch.setattr(fb_order_module, "create_sales_invoice", lambda order: "SINV-1")
    monkeypatch.setattr(
        fb_order_module,
        "create_ingredient_stock_entry",
        fail_stock_service,
    )
    monkeypatch.setattr(
        fb_order_module,
        "update_projection_state",
        lambda log, state, doctype, target_name, error: projection_updates.append(
            (log, state, doctype, target_name, error)
        ),
    )

    order = fb_order_module.FBOrder()
    order.name = "FB-ORDER-1"
    order.order_id = "ORDER-1"
    order.external_idempotency_key = "idem-1"
    order.status = "Draft"
    order.invoice_status = "Pending"
    order.stock_status = "Pending"
    order.shift = None
    order.items = [SimpleNamespace(resolved_sale="RESOLVED-1")]
    order.db_set_calls = []
    order.db_set = lambda fieldname, value, update_modified=False: order.db_set_calls.append(
        (fieldname, value, update_modified)
    )
    order.get_resolved_sales = lambda: [
        SimpleNamespace(
            name="RESOLVED-1",
            booth_warehouse="WH-1",
            resolved_components=[
                SimpleNamespace(
                    item="ITEM-1",
                    warehouse="WH-1",
                    stock_qty=1,
                    affects_stock=0,
                )
            ],
        )
    ]

    order.on_submit()

    assert stock_service_called["value"] is False
    assert order.stock_status == "Posted"
    assert ("stock_status", "Posted", False) in order.db_set_calls
    assert ("PROJECTION-LOG", "Succeeded", "Stock Entry", None, None) in projection_updates
