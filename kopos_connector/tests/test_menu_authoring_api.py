from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.api import menu_authoring


class _Recipe:
    def __init__(self, name: str, *, valid: bool = True, fail_on_save: bool = False):
        self.name = name
        self.status = "Draft"
        self.canonical_hash = ""
        self.company = "JiJi"
        self.sellable_item = name
        self.components = []
        self.allowed_modifier_groups = []
        self._valid = valid
        self._fail_on_save = fail_on_save
        self.save_count = 0

    def freeze_stock_component_conversions(self):
        return None

    def freeze_modifier_effects(self):
        return None

    def validate(self):
        if not self._valid:
            raise ValueError("recipe validation failed")

    def save(self, **_kwargs):
        self.save_count += 1
        if self._fail_on_save:
            raise ValueError("late database validation failed")


class _Database:
    def __init__(self):
        self.savepoints: list[str] = []
        self.rollbacks: list[str] = []

    def exists(self, doctype, name):
        return doctype == "FB Recipe" and name in {"RECIPE-A", "RECIPE-B"}

    def savepoint(self, name):
        self.savepoints.append(name)

    def rollback(self, *, save_point):
        self.rollbacks.append(save_point)


class _ItemDatabase(_Database):
    def exists(self, doctype, name):
        return doctype in {"Company", "Item Group", "UOM"}


class _ItemDocument:
    name = "ITEM-DRAFT-1"
    item_code = "ITEM-DRAFT-1"

    def __init__(self, payload):
        self.__dict__.update(payload)

    def insert(self, **_kwargs):
        return None


def test_publish_validates_every_recipe_before_first_save():
    first = _Recipe("RECIPE-A")
    second = _Recipe("RECIPE-B", valid=False)
    database = _Database()

    with patch.object(menu_authoring.frappe, "db", database), patch.object(
        menu_authoring.frappe, "get_doc", side_effect=[first, second]
    ), patch.object(
        menu_authoring, "_preview_for_doc", return_value={"valid": True, "errors": []}
    ):
        # Make the second failure occur during the preflight validation pass.
        with patch.object(menu_authoring, "_prepare_recipe_for_publish", side_effect=[None, ValueError("recipe validation failed")]):
            try:
                menu_authoring.publish_menu_recipe_selection(payload={"recipes": ["RECIPE-A", "RECIPE-B"]})
            except menu_authoring.frappe.ValidationError as error:
                assert "Nothing was published" in str(error)
            else:
                raise AssertionError("expected a validation failure")

    assert first.save_count == 0
    assert database.savepoints == []


def test_publish_rolls_back_when_standard_document_save_fails_late():
    first = _Recipe("RECIPE-A")
    second = _Recipe("RECIPE-B", fail_on_save=True)
    database = _Database()

    with patch.object(menu_authoring.frappe, "db", database), patch.object(
        menu_authoring.frappe, "get_doc", side_effect=[first, second]
    ), patch.object(
        menu_authoring, "_prepare_recipe_for_publish", side_effect=[None, None]
    ):
        try:
            menu_authoring.publish_menu_recipe_selection(payload={"recipes": ["RECIPE-A", "RECIPE-B"]})
        except ValueError as error:
            assert "late database validation" in str(error)
        else:
            raise AssertionError("expected a late save failure")

    assert first.save_count == 1
    assert second.save_count == 1
    assert database.savepoints == ["kopos_menu_recipe_publish"]
    assert database.rollbacks == ["kopos_menu_recipe_publish"]


def test_child_rows_are_copied_without_frappe_parent_identity():
    row = SimpleNamespace(item="MILK", qty="0.2", uom="L", name="old-child", parent="old")

    copied = menu_authoring._child_dict(row, ("item", "qty", "uom", "name", "parent"))

    assert copied == {"item": "MILK", "qty": "0.2", "uom": "L", "name": "old-child", "parent": "old"}


def test_guided_item_flow_creates_disabled_standard_item_with_explicit_role():
    database = _ItemDatabase()
    meta = SimpleNamespace(has_field=lambda fieldname: fieldname == "custom_fb_item_role")
    with patch.object(menu_authoring.frappe, "db", database), patch.object(
        menu_authoring.frappe, "get_meta", return_value=meta
    ), patch.object(menu_authoring.frappe, "get_doc", side_effect=lambda payload: _ItemDocument(payload)):
        result = menu_authoring.create_menu_item_draft(
            payload={
                "company": "JiJi",
                "item_name": "Cold Foam",
                "item_group": "Ingredients",
                "stock_uom": "Nos",
                "item_role": "Prep Item",
            }
        )
    assert result["status"] == "ok"
    assert result["disabled"] is True
    assert result["item_role"] == "Prep Item"


def test_recipe_serialization_exposes_effective_dates_for_cutover_entry():
    serialized = menu_authoring._serialize_recipe(
        SimpleNamespace(
            name="REC-1",
            recipe_code="REC-1",
            recipe_name="Mont Blanc",
            sellable_item="MONT-BLANC",
            company="JiJi",
            recipe_type="Finished Drink",
            status="Draft",
            version_no=2,
            yield_qty=1,
            yield_uom="Nos",
            default_serving_qty=1,
            default_serving_uom="Nos",
            effective_from="2026-08-15 09:00:00",
            effective_to=None,
            components=[],
            allowed_modifier_groups=[],
        )
    )
    assert serialized["effective_from"] == "2026-08-15 09:00:00"
