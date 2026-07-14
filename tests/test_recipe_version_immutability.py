from __future__ import annotations

from types import SimpleNamespace

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.doctype.fb_recipe.fb_recipe import FBRecipe


def _recipe(component_qty: str) -> FBRecipe:
    recipe = FBRecipe()
    recipe.name = "RECIPE-LATTE-V1"
    recipe.sellable_item = "LATTE"
    recipe.company = "JiJi Cafe"
    recipe.version_no = 1
    recipe.recipe_type = "Beverage"
    recipe.yield_qty = "1"
    recipe.yield_uom = "Nos"
    recipe.default_serving_qty = "1"
    recipe.default_serving_uom = "Nos"
    recipe.status = "Active"
    recipe.effective_from = None
    recipe.effective_to = None
    recipe.components = [
        SimpleNamespace(
            item="MILK",
            component_type="Ingredient",
            qty=component_qty,
            uom="ml",
            stock_qty=component_qty,
            stock_uom="ml",
            is_optional=0,
            is_substitutable=0,
            substitution_key=None,
            affects_stock=1,
            affects_cogs=1,
            loss_factor_pct=0,
            sort_order=1,
        )
    ]
    recipe.allowed_modifier_groups = []
    recipe.is_new = lambda: False
    return recipe


def test_used_recipe_definition_requires_a_new_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _recipe("200")
    current = _recipe("220")
    current.get_doc_before_save = lambda: previous
    monkeypatch.setattr(
        frappe.db,
        "exists",
        lambda doctype, filters: doctype == "FB Order Line",
    )

    with pytest.raises(Exception, match="create a new recipe version"):
        current.validate_used_version_is_immutable()


def test_used_recipe_serving_scale_requires_a_new_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _recipe("200")
    current = _recipe("200")
    current.default_serving_qty = "2"
    current.get_doc_before_save = lambda: previous
    monkeypatch.setattr(frappe.db, "exists", lambda *args: True)

    with pytest.raises(Exception, match="create a new recipe version"):
        current.validate_used_version_is_immutable()


def test_used_recipe_may_be_retired_without_mutating_its_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _recipe("200")
    current = _recipe("200")
    current.status = "Inactive"
    current.effective_to = "2026-07-12 00:00:00"
    current.get_doc_before_save = lambda: previous
    monkeypatch.setattr(frappe.db, "exists", lambda *args: True)

    current.validate_used_version_is_immutable()


def test_unused_recipe_definition_remains_editable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _recipe("200")
    current = _recipe("220")
    previous.status = "Draft"
    current.status = "Draft"
    current.get_doc_before_save = lambda: previous
    monkeypatch.setattr(frappe.db, "exists", lambda *args: False)

    current.validate_used_version_is_immutable()


def test_published_recipe_definition_is_immutable_before_first_sale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _recipe("200")
    current = _recipe("220")
    current.get_doc_before_save = lambda: previous
    monkeypatch.setattr(frappe.db, "exists", lambda *args: False)

    with pytest.raises(Exception, match="published or used"):
        current.validate_used_version_is_immutable()


def test_active_recipe_rejects_nonpositive_serving_or_component_quantities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _recipe("-200")
    recipe.default_serving_qty = "0"
    recipe.get = lambda fieldname: getattr(recipe, fieldname, None)
    monkeypatch.setattr(frappe.db, "get_value", lambda *args, **kwargs: None)
    monkeypatch.setattr(frappe, "get_all", lambda *args, **kwargs: [])

    with pytest.raises(Exception, match="Default Serving Qty"):
        recipe.validate_version()

    recipe.default_serving_qty = "1"
    with pytest.raises(Exception, match="component 1 Qty"):
        recipe.validate_version()


def test_active_stock_component_requires_item_stock_uom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _recipe("200")
    recipe.components[0].stock_uom = "Litre"
    recipe.get = lambda fieldname: getattr(recipe, fieldname, None)

    def get_value(doctype, name_or_filters, fields, **kwargs):
        if doctype == "FB Recipe":
            return None
        if doctype == "Item":
            return {"stock_uom": "ml", "is_stock_item": 1}
        raise AssertionError(f"unexpected lookup {doctype}")

    monkeypatch.setattr(frappe.db, "get_value", get_value)
    monkeypatch.setattr(frappe, "get_all", lambda *args, **kwargs: [])

    with pytest.raises(Exception, match="must match Item stock UOM ml"):
        recipe.validate_version()
