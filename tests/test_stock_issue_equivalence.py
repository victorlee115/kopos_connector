from __future__ import annotations

import importlib
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()

import frappe


stock_issue_service = importlib.import_module(
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


def test_grouped_stock_issue_quantities_use_exact_decimal_arithmetic() -> None:
    rows = stock_issue_service._build_grouped_issue_items([_resolved_sale()])

    assert len(rows) == 1
    assert rows[0]["qty"] == Decimal("0.30")
    assert isinstance(rows[0]["qty"], Decimal)
    assert rows[0]["s_warehouse"] == "Ingredients - JC"
    assert rows[0]["t_warehouse"] is None


def test_submitted_material_issue_must_exactly_match_resolved_components() -> None:
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


def test_stock_issue_rejects_non_finite_component_quantity() -> None:
    resolved_sale = _resolved_sale()
    resolved_sale.resolved_components[0].stock_qty = "NaN"

    with pytest.raises(ValueError, match="finite positive decimal"):
        stock_issue_service._build_grouped_issue_items([resolved_sale])
