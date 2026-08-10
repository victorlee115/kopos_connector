from __future__ import annotations

from types import SimpleNamespace

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.doctype.fb_recipe.fb_recipe import FBRecipe


def _group(
    *,
    selection_type: str = "Multiple",
    is_required: object = 0,
    min_selection: object = 0,
    max_selection: object = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        name="ADDITIONAL_ESPRESSO_SHOT",
        selection_type=selection_type,
        is_required=is_required,
        min_selection=min_selection,
        max_selection=max_selection,
    )


def _recipe_row(
    *,
    required: object = 0,
    override_min_selection: object = 0,
    override_max_selection: object = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        modifier_group="ADDITIONAL_ESPRESSO_SHOT",
        required=required,
        override_min_selection=override_min_selection,
        override_max_selection=override_max_selection,
        default_modifier=None,
    )


def _recipe(row: SimpleNamespace) -> FBRecipe:
    recipe = FBRecipe()
    recipe.name = "AMERICANO_COFFEE_RECIPE"
    recipe.allowed_modifier_groups = [row]
    recipe.get = lambda fieldname: getattr(recipe, fieldname, None)
    return recipe


def _install_group_lookup(
    monkeypatch: pytest.MonkeyPatch,
    group: SimpleNamespace,
) -> None:
    def get_cached_doc(doctype: str, name: str) -> SimpleNamespace:
        assert doctype == "FB Modifier Group"
        assert name == group.name
        return group

    monkeypatch.setattr(frappe, "get_cached_doc", get_cached_doc)


@pytest.mark.parametrize(
    ("override_min_selection", "override_max_selection"),
    [(None, None), ("", ""), (0, 0), ("0", "0")],
)
def test_blank_or_zero_recipe_overrides_keep_the_shared_rule(
    monkeypatch: pytest.MonkeyPatch,
    override_min_selection: object,
    override_max_selection: object,
) -> None:
    group = _group()
    _install_group_lookup(monkeypatch, group)
    recipe = _recipe(
        _recipe_row(
            override_min_selection=override_min_selection,
            override_max_selection=override_max_selection,
        )
    )

    recipe.validate_modifier_groups()


def test_recipe_required_flag_cannot_change_an_optional_shared_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = _group()
    _install_group_lookup(monkeypatch, group)
    recipe = _recipe(_recipe_row(required=1))

    with pytest.raises(
        frappe.ValidationError,
        match=(
            "Recipe AMERICANO_COFFEE_RECIPE changes the selection rules for "
            "modifier group ADDITIONAL_ESPRESSO_SHOT.*separate modifier group"
        ),
    ):
        recipe.validate_modifier_groups()


def test_nonzero_recipe_override_cannot_change_the_shared_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = _group()
    _install_group_lookup(monkeypatch, group)
    recipe = _recipe(_recipe_row(override_max_selection=2))

    with pytest.raises(
        frappe.ValidationError,
        match=(
            "Recipe AMERICANO_COFFEE_RECIPE changes the selection rules for "
            "modifier group ADDITIONAL_ESPRESSO_SHOT.*separate modifier group"
        ),
    ):
        recipe.validate_modifier_groups()


def test_nonzero_recipe_override_equal_to_shared_rule_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = _group(is_required=1, min_selection=1, max_selection=3)
    _install_group_lookup(monkeypatch, group)
    recipe = _recipe(
        _recipe_row(
            required=1,
            override_min_selection=1,
            override_max_selection=3,
        )
    )

    recipe.validate_modifier_groups()


def test_invalid_recipe_override_explains_how_to_fix_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = _group(selection_type="Single", max_selection=1)
    _install_group_lookup(monkeypatch, group)
    recipe = _recipe(_recipe_row(override_max_selection=2))

    with pytest.raises(
        frappe.ValidationError,
        match="invalid selection rules.*Use a separate modifier group",
    ):
        recipe.validate_modifier_groups()


def test_only_an_unchanged_active_recipe_can_bypass_rules_while_retiring() -> None:
    previous = SimpleNamespace(
        name="AMERICANO_COFFEE_RECIPE",
        recipe_code="AMERICANO_COFFEE_RECIPE",
        recipe_name="Americano",
        sellable_item="AMERICANO",
        company="Test Company",
        version_no=1,
        recipe_type="Finished Drink",
        yield_qty=1,
        yield_uom="Nos",
        default_serving_qty=1,
        default_serving_uom="Nos",
        station="Coffee",
        effective_from=None,
        effective_to=None,
        notes=None,
        status="Active",
        components=[],
        allowed_modifier_groups=[_recipe_row(required=1)],
    )
    recipe = _recipe(_recipe_row(required=1))
    for fieldname, value in vars(previous).items():
        setattr(recipe, fieldname, value)
    recipe.status = "Retired"
    recipe.get_doc_before_save = lambda: previous

    assert recipe.is_status_only_retirement() is True

    recipe.recipe_name = "Changed while retiring"
    assert recipe.is_status_only_retirement() is False


def test_component_remarks_cannot_change_while_retiring_legacy_recipe() -> None:
    component = SimpleNamespace(
        item="KOPOS-NONSTOCK-COMPONENT",
        component_type="Ingredient",
        qty=1,
        uom="Nos",
        stock_qty=1,
        stock_uom="Nos",
        is_optional=0,
        is_substitutable=0,
        substitution_key=None,
        affects_stock=0,
        affects_cogs=0,
        loss_factor_pct=0,
        sort_order=1,
        remarks="Original note",
    )
    previous = SimpleNamespace(
        name="AMERICANO_COFFEE_RECIPE",
        recipe_code="AMERICANO_COFFEE_RECIPE",
        recipe_name="Americano",
        sellable_item="AMERICANO",
        company="Test Company",
        version_no=1,
        recipe_type="Finished Drink",
        yield_qty=1,
        yield_uom="Nos",
        default_serving_qty=1,
        default_serving_uom="Nos",
        station="Coffee",
        effective_from=None,
        effective_to=None,
        notes=None,
        status="Active",
        components=[component],
        allowed_modifier_groups=[_recipe_row(required=1)],
    )
    recipe = _recipe(_recipe_row(required=1))
    for fieldname, value in vars(previous).items():
        setattr(recipe, fieldname, value)
    recipe.components = [SimpleNamespace(**vars(component))]
    recipe.status = "Retired"
    recipe.get_doc_before_save = lambda: previous

    assert recipe.is_status_only_retirement() is True

    recipe.components[0].remarks = "Changed while retiring"
    assert recipe.is_status_only_retirement() is False
