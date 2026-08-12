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
            "register_qr_payment_settlement": lambda *args, **kwargs: None,
            "normalize_qr_token": lambda value: value,
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


@pytest.mark.inventory_regression
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


def test_before_submit_does_not_touch_inventory_before_accounting(fake_frappe):
    fake_frappe.stock_by_bin[("ITEM-1", "WH-1")] = 0.5
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )

    order = fb_order_module.FBOrder()
    order.name = "FB-ORDER-1"
    order.order_id = "ORDER-1"
    order.booth_warehouse = "WH-1"
    resolved_sale_calls: list[object] = []
    line_resolutions = [
        {
            "resolved_components": [
                {"item": "ITEM-1", "stock_qty": 1.0, "affects_stock": 1}
            ]
        }
    ]

    order.build_line_resolutions = lambda: line_resolutions
    order.create_resolved_sales = lambda resolutions: resolved_sale_calls.append(
        resolutions
    )

    order.before_submit()

    assert resolved_sale_calls == []
    assert fake_frappe.created_logs == []


@pytest.mark.inventory_regression
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


@pytest.mark.inventory_regression
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


def test_before_submit_does_not_invoke_failing_resolved_sale_subsystem(fake_frappe):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )

    order = fb_order_module.FBOrder()
    order.booth_warehouse = "WH-1"
    order.build_line_resolutions = lambda: []

    def raise_non_stock_failure(_line_resolutions):
        raise RuntimeError("resolved sale projection failed")

    order.create_resolved_sales = raise_non_stock_failure

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


@pytest.mark.inventory_regression
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


@pytest.mark.inventory_regression
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


def test_prepared_before_submit_uses_only_the_commercial_line_snapshot(
    fake_frappe,
    monkeypatch,
):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    calls: list[tuple[str, object]] = []
    order = fb_order_module.FBOrder()
    order.accepted_sale_fingerprint = "f" * 64
    order.build_line_resolutions = lambda: calls.append(("commercial", order)) or []
    order.validate_prepared_resolved_sales = lambda: (_ for _ in ()).throw(
        AssertionError("prepared sale must not load resolved recipe snapshots")
    )
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

    assert calls == [("commercial", order), ("settlement", order)]


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


@pytest.mark.inventory_regression
def test_prepared_sale_rejects_persisted_recipe_identity_edits(
    fake_frappe,
) -> None:
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    before = SimpleNamespace(
        accepted_sale_fingerprint="f" * 64,
        items=[
            SimpleNamespace(
                line_id="LINE-1",
                item="ITEM-1",
                qty=1,
                recipe="RECIPE-1",
                recipe_version=3,
                is_recipe_managed=1,
            )
        ],
        payments=[],
    )
    order = fb_order_module.FBOrder()
    order.accepted_sale_fingerprint = "f" * 64
    order.items = [SimpleNamespace(**vars(before.items[0]))]
    order.items[0].recipe_version = 4
    order.payments = []
    order.get_doc_before_save = lambda: before

    with pytest.raises(
        fb_order_module.frappe.ValidationError,
        match=r"immutable sale snapshot cannot be changed: items\[1\]\.recipe_version",
    ):
        order.validate_prepared_sale_immutability()


def test_prepared_sale_rejects_commercial_modifier_snapshot_edits(
    fake_frappe,
) -> None:
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    original_snapshot = json.dumps(
        [
            {
                "modifier_group": "MILK",
                "modifier": "OAT",
                "price_adjustment_sen": 200,
            }
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    before = SimpleNamespace(
        accepted_sale_fingerprint="f" * 64,
        items=[
            SimpleNamespace(
                line_id="LINE-1",
                item="ITEM-1",
                qty=1,
                commercial_modifier_snapshot_json=original_snapshot,
            )
        ],
        payments=[],
    )
    order = fb_order_module.FBOrder()
    order.accepted_sale_fingerprint = "f" * 64
    order.items = [SimpleNamespace(**vars(before.items[0]))]
    order.items[0].commercial_modifier_snapshot_json = "[]"
    order.payments = []
    order.get_doc_before_save = lambda: before

    with pytest.raises(
        fb_order_module.frappe.ValidationError,
        match=(
            r"immutable sale snapshot cannot be changed: "
            r"items\[1\]\.commercial_modifier_snapshot_json"
        ),
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


def test_prepared_sale_allows_only_evidence_bound_static_winner_channel_transition(
    fake_frappe,
) -> None:
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    payment_before = SimpleNamespace(
        name="FBPAY-1",
        source_payment_id="PAY-1",
        payment_method="DuitNow QR",
        payment_channel_code="maybank",
        amount=Decimal("12.50"),
        tendered_amount=Decimal("12.50"),
        change_amount=Decimal("0"),
        is_manual_confirmation=0,
        manual_confirmation_evidence_json=None,
        reconciliation_idempotency_key=None,
        external_transaction_id=None,
    )
    payment_after = SimpleNamespace(**vars(payment_before))
    payment_after.payment_channel_code = "static_qr"
    payment_after.is_manual_confirmation = 1
    payment_after.manual_confirmation_evidence_json = '{"evidence_kind":"no_receipt_acknowledgement"}'
    payment_after.reconciliation_idempotency_key = "RECONCILE-STATIC-1"
    payment_after.external_transaction_id = "static-payment-1"
    before = SimpleNamespace(
        accepted_sale_fingerprint="f" * 64,
        automatic_qr_state="provider_ambiguous",
        automatic_qr_winner_channel=None,
        automatic_qr_payment="FBPAY-1",
        items=[],
        payments=[payment_before],
    )
    order = fb_order_module.FBOrder()
    order.accepted_sale_fingerprint = "f" * 64
    order.automatic_qr_state = "manual_pending_reconciliation"
    order.automatic_qr_winner_channel = "static_qr"
    order.automatic_qr_payment = "FBPAY-1"
    order.items = []
    order.payments = [payment_after]
    order.get_doc_before_save = lambda: before

    order.validate_prepared_sale_immutability()

    order.automatic_qr_winner_channel = None
    with pytest.raises(
        fb_order_module.frappe.ValidationError,
        match=r"immutable sale snapshot cannot be changed: payments\[1\]\.payment_channel_code",
    ):
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


def test_unprepared_before_submit_does_not_run_inventory_before_accounting(
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

    assert calls == [("settlement", order)]


def test_commercial_line_snapshot_never_loads_recipe_or_inventory(fake_frappe):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    modifier = SimpleNamespace(
        modifier_group="EXTRAS",
        modifier="EXTRA-SHOT",
        price_adjustment=Decimal("2.00"),
        instruction_text=None,
        sort_order=1,
        affects_stock=0,
        affects_recipe=0,
    )
    line = SimpleNamespace(
        recipe="MISSING-RECIPE",
        recipe_version=7,
        is_recipe_managed=1,
        resolved_sale="STALE-RESOLVED-SALE",
        resolved_components_snapshot="not-a-current-inventory-snapshot",
    )
    order = fb_order_module.FBOrder()
    order.items = [line]
    order.get_selected_modifier_rows = lambda _line: [modifier]
    order.resolve_recipe_for_line = lambda *_args: (_ for _ in ()).throw(
        AssertionError("recipe lookup must not run")
    )
    order.resolve_components_for_line = lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(AssertionError("ingredient expansion must not run"))
    order.validate_stock_availability = lambda *_args: (_ for _ in ()).throw(
        AssertionError("stock validation must not run")
    )

    resolutions = order.build_line_resolutions()

    assert resolutions == [
        {
            "line": line,
            "line_index": 1,
            "recipe_doc": None,
            "selected_modifiers": [{"row": modifier}],
            "resolved_components": [],
        }
    ]
    assert line.resolved_sale is None
    assert line.resolved_components_snapshot == "[]"


def test_on_submit_never_invokes_optional_inventory(fake_frappe, monkeypatch):
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
    order.create_projection_entry = lambda projection_type: {
        "Sales Invoice": "INV-LOG",
        "FB Shift": "SHIFT-LOG",
    }[projection_type]
    order.db_set_calls = []
    order.db_set = lambda fieldname, value, update_modified=False: order.db_set_calls.append(
        (fieldname, value, update_modified)
    )
    order.get_resolved_sales = lambda: (_ for _ in ()).throw(
        AssertionError("inventory snapshots must not be loaded during submit")
    )
    order.update_shift_expected_cash = lambda: None

    order.on_submit()

    assert stock_service_called["value"] is False
    assert order.stock_status == "Pending"
    assert ("stock_status", "Pending", False) in order.db_set_calls
    assert all(update[2] != "Stock Entry" for update in projection_updates)


def test_on_submit_keeps_accounting_when_inventory_hooks_would_fail(
    fake_frappe,
    monkeypatch,
):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )
    projection_updates: list[tuple[str, str, str, str | None, str | None]] = []

    monkeypatch.setattr(fb_order_module, "create_sales_invoice", lambda order: "SINV-1")
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
    order.create_projection_entry = lambda projection_type: {
        "Sales Invoice": "INV-LOG",
        "FB Shift": "SHIFT-LOG",
    }[projection_type]
    order.get_resolved_sales = lambda: (_ for _ in ()).throw(
        ModuleNotFoundError("optional inventory adapter is not installed")
    )
    order.requires_stock_projection = lambda _resolved: (_ for _ in ()).throw(
        ModuleNotFoundError("optional inventory adapter is not installed")
    )
    order.update_shift_expected_cash = lambda: None
    order.db_set_calls = []
    order.db_set = lambda fieldname, value, update_modified=False: order.db_set_calls.append(
        (fieldname, value, update_modified)
    )

    order.on_submit()

    assert order.status == "Submitted"
    assert order.invoice_status == "Posted"
    assert order.sales_invoice == "SINV-1"
    assert order.stock_status == "Pending"
    assert ("status", "Submitted", False) in order.db_set_calls
    assert ("invoice_status", "Posted", False) in order.db_set_calls
    assert ("sales_invoice", "SINV-1", False) in order.db_set_calls
    assert all(update[2] != "Stock Entry" for update in projection_updates)
