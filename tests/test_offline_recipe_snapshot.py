from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from .fake_frappe import install_fake_frappe_modules

pytestmark = pytest.mark.inventory_regression


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.doctype.fb_order.fb_order import FBOrder


def _offline_order() -> FBOrder:
    order = FBOrder()
    order.name = "FB-ORDER-1"
    order.company = "JiJi Cafe"
    order.sale_datetime = datetime(2026, 7, 11, 12, 0, 0)
    order.request_fingerprint = "f" * 64
    return order


def _historical_recipe(version: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        name="RECIPE-LATTE-V1",
        version_no=version,
        status="Inactive",
        sellable_item="LATTE",
        company="JiJi Cafe",
        effective_from=datetime(2026, 7, 1, 0, 0, 0),
        effective_to=datetime(2026, 7, 11, 23, 59, 59),
        components=[SimpleNamespace(item="MILK")],
        recipe_name="Latte v1",
    )


def test_offline_sale_resolves_the_exact_historical_recipe_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _offline_order()
    recipe = _historical_recipe()
    line = SimpleNamespace(
        line_id="LINE-1",
        item="LATTE",
        recipe=recipe.name,
        recipe_version=1,
        is_recipe_managed=1,
        item_name_snapshot="Latte",
    )
    monkeypatch.setattr(
        frappe,
        "get_cached_doc",
        lambda doctype, name: recipe,
    )
    monkeypatch.setattr(frappe.db, "get_value", lambda *args, **kwargs: 0)

    resolved = order.resolve_recipe_for_line(1, line)

    assert resolved is recipe
    assert line.recipe_version == 1


def test_offline_sale_rejects_recipe_identity_with_a_changed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _offline_order()
    recipe = _historical_recipe(version=2)
    line = SimpleNamespace(
        line_id="LINE-1",
        item="LATTE",
        recipe=recipe.name,
        recipe_version=1,
        is_recipe_managed=1,
        item_name_snapshot="Latte",
    )
    monkeypatch.setattr(frappe, "get_cached_doc", lambda *args: recipe)

    with pytest.raises(Exception, match="version changed"):
        order.resolve_recipe_for_line(1, line)
