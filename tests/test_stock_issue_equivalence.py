from __future__ import annotations

import importlib
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from .fake_frappe import install_fake_frappe_modules

pytestmark = pytest.mark.inventory_regression


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()

import frappe


@pytest.fixture
def stock_issue_service():
    return importlib.import_module(
        "kopos_connector.kopos.services.inventory.stock_issue_service"
    )


def _resolved_sale() -> SimpleNamespace:
    return SimpleNamespace(
        name="RESOLVED-1",
        company="JiJi Cafe",
        booth_warehouse="Ingredients - JC",
        resolved_components=[
            SimpleNamespace(
                affects_stock=1,
                warehouse="Ingredients - JC",
                item="MILK",
                stock_qty=Decimal("0.10"),
                stock_uom="Litre",
                source_type="Recipe",
                source_reference="RECIPE-1",
                remarks=None,
            ),
            SimpleNamespace(
                affects_stock=1,
                warehouse="Ingredients - JC",
                item="MILK",
                stock_qty=Decimal("0.20"),
                stock_uom="Litre",
                source_type="Modifier",
                source_reference="MOD-1",
                remarks=None,
            ),
        ],
    )


def test_grouped_stock_issue_quantities_use_exact_decimal_arithmetic(
    stock_issue_service,
) -> None:
    rows = stock_issue_service._build_grouped_issue_items([_resolved_sale()])

    assert len(rows) == 1
    assert rows[0]["qty"] == Decimal("0.30")
    assert isinstance(rows[0]["qty"], Decimal)
    assert rows[0]["s_warehouse"] == "Ingredients - JC"
    assert rows[0]["t_warehouse"] is None


def test_submitted_material_issue_must_exactly_match_resolved_components(
    stock_issue_service,
) -> None:
    order = SimpleNamespace(
        name="FB-ORDER-1",
        company="JiJi Cafe",
        shift="FB-SHIFT-1",
    )
    stock_entry = SimpleNamespace(
        name="STE-1",
        docstatus=1,
        stock_entry_type="Material Issue",
        purpose="Material Issue",
        company="JiJi Cafe",
        custom_fb_order="FB-ORDER-1",
        custom_fb_shift="FB-SHIFT-1",
        items=[
            SimpleNamespace(
                item_code="MILK",
                s_warehouse="Ingredients - JC",
                t_warehouse=None,
                qty=Decimal("0.30"),
                transfer_qty=Decimal("0.30"),
                uom="Litre",
                stock_uom="Litre",
                conversion_factor=Decimal("1"),
            )
        ],
    )

    stock_issue_service._validate_stock_entry_equivalence(
        order,
        [_resolved_sale()],
        stock_entry,
    )

    stock_entry.items[0].t_warehouse = "Finished Goods - JC"
    with pytest.raises(
        frappe.ValidationError,
        match="target warehouse",
    ):
        stock_issue_service._validate_stock_entry_equivalence(
            order,
            [_resolved_sale()],
            stock_entry,
        )


def test_stock_issue_rejects_non_finite_component_quantity(stock_issue_service) -> None:
    resolved_sale = _resolved_sale()
    resolved_sale.resolved_components[0].stock_qty = "NaN"

    with pytest.raises(ValueError, match="finite positive decimal"):
        stock_issue_service._build_grouped_issue_items([resolved_sale])


def test_stock_issue_projection_identity_is_stable_and_bounded(
    stock_issue_service,
) -> None:
    order = SimpleNamespace(name="FB-ORDER-1")
    assert (
        stock_issue_service._stock_issue_projection_id(order)
        == "fb-order:FB-ORDER-1:stock-issue"
    )
    long_id = stock_issue_service._stock_issue_projection_id(
        SimpleNamespace(name="X" * 500)
    )
    assert long_id.startswith("fb-order:")
    assert long_id.endswith(":stock-issue")
    assert len(long_id) <= 140


def test_recovered_stock_issue_rejects_another_projection_identity(
    stock_issue_service,
) -> None:
    order = SimpleNamespace(
        name="FB-ORDER-1",
        company="JiJi Cafe",
        shift="FB-SHIFT-1",
    )
    stock_entry = SimpleNamespace(
        name="STE-1",
        docstatus=1,
        stock_entry_type="Material Issue",
        purpose="Material Issue",
        company="JiJi Cafe",
        custom_fb_order="FB-ORDER-1",
        custom_fb_shift="FB-SHIFT-1",
        custom_fb_projection_id="fb-order:FB-ORDER-2:stock-issue",
        items=[],
    )

    with pytest.raises(frappe.ValidationError, match="projection identity"):
        stock_issue_service._validate_stock_entry_equivalence(
            order,
            [_resolved_sale()],
            stock_entry,
        )


def test_stock_entry_projection_custom_field_is_database_unique() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (
        root / "kopos_connector/kopos/install/fb_custom_fields.py"
    ).read_text()
    assert '"fieldname": "custom_fb_projection_id"' in source
    projection_field_block = source.split(
        '"fieldname": "custom_fb_projection_id"', 1
    )[1].split("},", 1)[0]
    assert '"unique": 1' in projection_field_block
