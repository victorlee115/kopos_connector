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


class _ExistingMenuItem:
    name = "MATCHA-LATTE"
    disabled = 0
    is_sales_item = 1

    def __init__(self, *, is_stock_item: int):
        self.is_stock_item = is_stock_item
        self.custom_fb_item_role = ""
        self.save_count = 0

    def save(self):
        self.save_count += 1


def test_publish_validates_every_recipe_before_first_save():
    first = _Recipe("RECIPE-A")
    second = _Recipe("RECIPE-B", valid=False)
    database = _Database()

    with patch.object(menu_authoring, "resolve_director_context"), patch.object(
        menu_authoring.frappe, "db", database
    ), patch.object(
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

    with patch.object(menu_authoring, "resolve_director_context"), patch.object(
        menu_authoring.frappe, "db", database
    ), patch.object(
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
    created = []

    def build_item(payload):
        created.append(payload)
        return _ItemDocument(payload)

    with patch.object(menu_authoring, "resolve_director_context"), patch.object(
        menu_authoring.frappe, "db", database
    ), patch.object(
        menu_authoring.frappe, "get_meta", return_value=meta
    ), patch.object(menu_authoring.frappe, "get_doc", side_effect=build_item):
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
    assert created[0]["item_code"].startswith("JIJI-PRODUCT-COLD-FOAM-")
    assert "item_code" not in result


def test_plain_language_item_roles_set_truthful_standard_item_flags():
    assert menu_authoring._item_role_flags("Sellable Drink") == {
        "is_sales_item": 1,
        "is_purchase_item": 0,
        "is_stock_item": 0,
    }
    assert menu_authoring._item_role_flags("Packaged Finished Good") == {
        "is_sales_item": 1,
        "is_purchase_item": 1,
        "is_stock_item": 1,
    }
    assert menu_authoring._item_role_flags("Non-Stock Service") == {
        "is_sales_item": 1,
        "is_purchase_item": 0,
        "is_stock_item": 0,
    }
    assert menu_authoring._item_role_flags("Ingredient") == {
        "is_sales_item": 0,
        "is_purchase_item": 1,
        "is_stock_item": 1,
    }


def test_guided_existing_item_role_update_stays_inside_directors_price_lists():
    item = _ExistingMenuItem(is_stock_item=0)
    database = SimpleNamespace(
        exists=lambda doctype, filters: doctype == "Item Price"
        and filters
        == {
            "item_code": "MATCHA-LATTE",
            "price_list": ["in", ["Moonleaf Selling"]],
        }
    )
    context = SimpleNamespace(
        company="Moonleaf Café",
        allowed_price_lists=("Moonleaf Selling",),
    )
    meta = SimpleNamespace(has_field=lambda fieldname: fieldname == "custom_fb_item_role")
    with patch.object(menu_authoring, "resolve_director_context", return_value=context), patch.object(
        menu_authoring.frappe, "db", database
    ), patch.object(
        menu_authoring.frappe, "get_doc", return_value=item
    ), patch.object(
        menu_authoring.frappe, "get_meta", return_value=meta
    ):
        result = menu_authoring.set_menu_item_role(
            company="Moonleaf Café",
            item="MATCHA-LATTE",
            item_role="Sellable Drink",
        )

    assert result == {
        "status": "ok",
        "item": "MATCHA-LATTE",
        "item_role": "Sellable Drink",
    }
    assert item.custom_fb_item_role == "Sellable Drink"
    assert item.save_count == 1


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


def test_guided_stock_item_owns_technical_flags_and_outlet_default():
    context = SimpleNamespace(
        company="Moonleaf Café",
        warehouse="Moonleaf Bar - MC",
        allowed_warehouses=("Moonleaf Bar - MC",),
    )
    database = SimpleNamespace(
        exists=lambda doctype, name: doctype == "Item Group" and name == "Raw Material"
    )
    meta = SimpleNamespace(has_field=lambda fieldname: fieldname == "custom_fb_item_role")
    created = []

    def build_item(payload):
        created.append(payload)
        return _ItemDocument(payload)

    with patch.object(menu_authoring, "resolve_director_context", return_value=context), patch.object(
        menu_authoring.frappe, "db", database
    ), patch.object(
        menu_authoring.frappe, "get_meta", return_value=meta
    ), patch.object(
        menu_authoring, "_friendly_uom_choices", return_value=[{"value": "Gram", "label": "gram / grams"}]
    ), patch.object(menu_authoring.frappe, "get_doc", side_effect=build_item):
        result = menu_authoring.create_guided_stock_item(
            payload={
                "company": "Moonleaf Café",
                "name": "Ceremonial Matcha",
                "kind": "Ingredient",
                "unit": "Gram",
            }
        )

    payload = created[0]
    assert payload["item_code"].startswith("JIJI-STOCK-CEREMONIAL-MATCHA-")
    assert payload["is_stock_item"] == 1
    assert payload["is_sales_item"] == 0
    assert payload["custom_fb_item_role"] == "Ingredient"
    assert payload["item_defaults"] == [
        {"company": "Moonleaf Café", "default_warehouse": "Moonleaf Bar - MC"}
    ]
    assert result["item"]["item_name"] == "Ceremonial Matcha"


def test_guided_recipe_rejects_an_ingredient_from_another_cafe():
    context = SimpleNamespace(
        company="Moonleaf Café",
        allowed_warehouses=("Moonleaf Bar - MC",),
    )
    with patch.object(menu_authoring, "resolve_director_context", return_value=context), patch.object(
        menu_authoring,
        "_allowed_component_item_names",
        return_value={"MATCHA"},
    ):
        try:
            menu_authoring._validate_guided_component_scope(
                {"company": "Moonleaf Café", "recipe": ""},
                [{"item": "OTHER-CAFE-MILK"}],
            )
        except menu_authoring.frappe.PermissionError as error:
            assert "this café only" in str(error)
        else:
            raise AssertionError("expected an out-of-scope ingredient to be rejected")


def test_guided_editor_preserves_hidden_stock_behavior_for_an_unchanged_ingredient():
    source = SimpleNamespace(
        components=[
            SimpleNamespace(
                item="MATCHA",
                component_type="Ingredient",
                qty="4",
                uom="Gram",
                is_optional=0,
                is_substitutable=1,
                substitution_key="matcha",
                affects_stock=1,
                affects_cogs=1,
                loss_factor_pct="2",
                sort_order=1,
                remarks="Keep this behind the simple editor",
            )
        ]
    )

    result = menu_authoring._guided_components(
        [
            {
                "item": "MATCHA",
                "component_type": "Ingredient",
                "qty": "5",
                "uom": "Gram",
                "affects_stock": 1,
                "affects_cogs": 1,
                "loss_factor_pct": "0",
            }
        ],
        {
            "MATCHA": {
                "is_stock_item": 1,
                "item_role": "Ingredient",
            }
        },
        source_recipe=source,
    )

    assert result[0]["qty"] == "5"
    assert result[0]["loss_factor_pct"] == "2"
    assert result[0]["is_substitutable"] == 1
    assert result[0]["substitution_key"] == "matcha"
    assert result[0]["remarks"] == "Keep this behind the simple editor"


def test_public_recipe_amount_is_what_the_director_entered_not_hidden_loss() -> None:
    public = menu_authoring._public_preview(
        {
            "valid": True,
            "errors": [],
            "warnings": [],
            "components": [
                {
                    "item": "MATCHA",
                    "item_name": "Matcha powder",
                    "component_type": "Ingredient",
                    "entered_qty_per_serving": "4",
                    "entered_uom": "Gram",
                    "stock_qty_per_serving": "4.08",
                    "stock_uom": "Gram",
                    "affects_stock": 1,
                    "valuation_rate": "0.1",
                    "cost_per_batch": "0.408",
                    "cost_per_serving": "0.408",
                }
            ],
            "cost_per_batch": "0.408",
            "cost_per_serving": "0.408",
        }
    )

    assert public["tracked_items"] == [
        {"name": "Matcha powder", "amount": "4 g", "kind": "Ingredient"}
    ]


def test_guided_amount_choice_uses_the_saved_ingredient_amount():
    recipe = SimpleNamespace(
        components=[SimpleNamespace(item="MATCHA", stock_qty="4", stock_uom="Gram")]
    )
    effect = menu_authoring._guided_choice_effect(
        {
            "name": "Extra matcha",
            "change_type": "change_amount",
            "ingredient": "MATCHA",
            "amount": "6",
            "unit": "Gram",
        },
        recipe=recipe,
        allowed_items={"MATCHA"},
        item_details={
            "MATCHA": {
                "is_stock_item": 1,
                "stock_uom": "Gram",
                "conversion_factors": {"Gram": "1"},
            }
        },
    )

    assert effect == {
        "kind": "Scale",
        "target_item": "MATCHA",
        "scale_percent": "150",
        "affects_stock": 1,
        "affects_recipe": 1,
    }


def test_menu_page_choices_return_friendly_labels_inside_scope():
    context = SimpleNamespace(
        status="ready",
        company="Moonleaf Café",
        allowed_companies=("Moonleaf Café",),
        allowed_price_lists=("Moonleaf Selling",),
    )

    def get_all(doctype, **_kwargs):
        if doctype == "Item Price":
            return ["MATCHA-16"]
        if doctype == "Item":
            return [{
                "name": "MATCHA-16",
                "item_name": "Iced Matcha Latte",
                "item_group": "Drinks",
            }]
        if doctype == "Item Group":
            return [{"name": "Drinks", "item_group_name": "Drinks"}]
        if doctype == "POS Profile":
            return [{"name": "MOONLEAF-POS", "warehouse": "Moonleaf Bar - MC"}]
        if doctype == "Warehouse":
            return [{"name": "Moonleaf Bar - MC", "warehouse_name": "Moonleaf Bar"}]
        raise AssertionError(f"unexpected lookup: {doctype}")

    with patch.object(menu_authoring, "resolve_director_context", return_value=context), patch.object(
        menu_authoring, "director_pos_profiles", return_value=("MOONLEAF-POS",)
    ), patch.object(menu_authoring.frappe, "get_all", side_effect=get_all):
        result = menu_authoring.get_menu_page_choices(company="Moonleaf Café")

    assert result["cafes"] == [{"value": "Moonleaf Café", "label": "Moonleaf Café"}]
    assert result["products"] == [{"value": "MATCHA-16", "label": "Iced Matcha Latte"}]
    assert result["menu_groups"] == [{"value": "Drinks", "label": "Drinks"}]
    assert result["outlets"] == [{"value": "MOONLEAF-POS", "label": "Moonleaf Bar"}]


def test_public_preview_never_returns_internal_validation_language():
    public = menu_authoring._public_preview(
        {
            "valid": False,
            "errors": [{"field": "yield_uom", "message": "No ERP conversion for Nos"}],
            "warnings": [{"field": "cost", "message": "Current valuation is missing for ITEM-001"}],
            "components": [],
            "cost_per_batch": None,
            "cost_per_serving": None,
        }
    )

    rendered = str(public)
    assert "ERP" not in rendered
    assert "UOM" not in rendered
    assert "valuation" not in rendered
    assert public["errors"] == [
        {"message": "Choose a measuring unit that matches how the ingredient is counted."}
    ]


def test_unstarted_stock_tracking_does_not_claim_a_published_product_is_incomplete():
    assert menu_authoring._human_menu_message(
        "Stock tracking has not started for this outlet"
    ) == "Start stock tracking to see what today's stock can make."
