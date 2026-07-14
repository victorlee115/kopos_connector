from __future__ import annotations

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.doctype.fb_modifier.fb_modifier import FBModifier
from kopos_connector.kopos.doctype.fb_modifier_group.fb_modifier_group import (
    FBModifierGroup,
)


def _modifier() -> FBModifier:
    modifier = FBModifier()
    modifier.name = "FB-MOD-OAT"
    modifier.modifier_group = "FB-GRP-MILK"
    modifier.kind = "Replace"
    modifier.target_substitution_key = "milk"
    modifier.target_item = "MILK"
    modifier.new_item = "OAT-MILK"
    modifier.qty_delta = 0
    modifier.qty_uom = "ml"
    modifier.scale_percent = 100
    modifier.affects_stock = 1
    modifier.affects_recipe = 1
    modifier.price_adjustment = "2.00"
    modifier.active = 1
    modifier.is_new = lambda: False
    return modifier


def test_used_modifier_operational_change_requires_a_new_modifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _modifier()
    current = _modifier()
    current.new_item = "ALMOND-MILK"
    current.get_doc_before_save = lambda: previous
    monkeypatch.setattr(frappe.db, "exists", lambda *args: True)

    with pytest.raises(Exception, match="create a new modifier"):
        current.validate_used_operational_definition_is_immutable()


def test_used_modifier_price_and_active_state_may_change_for_future_sales(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _modifier()
    current = _modifier()
    current.price_adjustment = "2.50"
    current.active = 0
    current.get_doc_before_save = lambda: previous
    monkeypatch.setattr(frappe.db, "exists", lambda *args: True)

    current.validate_used_operational_definition_is_immutable()


def _modifier_group() -> FBModifierGroup:
    group = FBModifierGroup()
    group.name = "FB-GRP-MILK"
    group.group_name = "Milk"
    group.selection_type = "Single"
    group.is_required = 1
    group.min_selection = 1
    group.max_selection = 1
    group.parent_modifier = None
    group.active = 1
    group.is_new = lambda: False
    return group


def test_published_modifier_group_rules_are_immutable_before_first_sale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _modifier_group()
    current = _modifier_group()
    current.max_selection = 2
    current.get_doc_before_save = lambda: previous
    monkeypatch.setattr(frappe.db, "exists", lambda *args: False)

    with pytest.raises(Exception, match="published or referenced"):
        current.validate_published_operational_definition_is_immutable()


def test_published_modifier_group_display_name_may_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _modifier_group()
    current = _modifier_group()
    current.group_name = "Milk choice"
    current.get_doc_before_save = lambda: previous
    monkeypatch.setattr(frappe.db, "exists", lambda *args: False)

    current.validate_published_operational_definition_is_immutable()
