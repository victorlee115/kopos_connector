from __future__ import annotations

from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.inventory_autopilot import tracking_scope


def test_recipe_scope_recurses_into_prepared_stock_and_keeps_operational_residue() -> None:
    def recipe_items(*, item: str, company: str, at_time: object) -> set[str]:
        assert company == "Moonleaf"
        return {
            "LATTE": {"MILK", "CUP", "COLD-FOAM"},
            "COLD-FOAM": {"CREAM"},
        }.get(item, set())

    def physical_items(items: object) -> set[str]:
        return {str(item) for item in items if str(item) != "LATTE"}

    with (
        patch.object(tracking_scope, "_current_recipe_products", return_value={"LATTE", "COLD-FOAM"}),
        patch.object(tracking_scope, "active_recipe_tracking_items", side_effect=recipe_items),
        patch.object(tracking_scope, "_stock_items", side_effect=physical_items),
        patch.object(tracking_scope, "_residual_bin_items", return_value={"OLD-SYRUP"}),
        patch.object(tracking_scope, "_availability_rule_items", return_value={"FILTER"}),
        patch.object(tracking_scope, "_open_count_items", return_value=set()),
        patch.object(tracking_scope, "_open_plan_items", return_value=set()),
    ):
        scope = tracking_scope.resolve_tracking_scope(
            company="Moonleaf",
            warehouse="Moonleaf Bar",
            catalog_item_ids={"LATTE", "COOKIE"},
        )

    assert scope.item_codes == frozenset(
        {"COOKIE", "MILK", "CUP", "COLD-FOAM", "CREAM", "OLD-SYRUP", "FILTER"}
    )
    assert "LATTE" not in scope.item_codes
    assert scope.recipe_items == frozenset({"MILK", "CUP", "COLD-FOAM", "CREAM"})
    assert scope.residual_items == frozenset({"OLD-SYRUP"})


def test_explicit_empty_outlet_menu_does_not_inherit_another_outlets_recipes() -> None:
    with (
        patch.object(tracking_scope, "_current_recipe_products", return_value={"OTHER-OUTLET-DRINK"}),
        patch.object(tracking_scope, "active_recipe_tracking_items") as recipe_items,
        patch.object(tracking_scope, "_stock_items", side_effect=lambda items: set(items)),
        patch.object(tracking_scope, "_residual_bin_items", return_value={"LEFTOVER-MILK"}),
        patch.object(tracking_scope, "_availability_rule_items", return_value=set()),
        patch.object(tracking_scope, "_open_count_items", return_value=set()),
        patch.object(tracking_scope, "_open_plan_items", return_value=set()),
    ):
        scope = tracking_scope.resolve_tracking_scope(
            company="Moonleaf",
            warehouse="Moonleaf Bar",
            catalog_item_ids=set(),
        )

    recipe_items.assert_not_called()
    assert scope.item_codes == frozenset({"LEFTOVER-MILK"})


def test_tracking_source_merge_is_stable_and_ignores_blank_values() -> None:
    assert tracking_scope.merge_tracking_sources(
        ["MILK", " CUP "], ["", "MILK"], ["MATCHA"]
    ) == frozenset({"MILK", "CUP", "MATCHA"})


def test_open_count_scope_reads_the_count_lines_actual_item_field() -> None:
    def get_all(doctype: str, **kwargs: object) -> list[object]:
        if doctype == "FB Inventory Count Task":
            assert kwargs["pluck"] == "name"
            return ["COUNT-1"]
        assert doctype == "FB Inventory Count Task Line"
        assert kwargs["fields"] == ["item_id"]
        return [{"item_id": "MATCHA"}]

    with patch.object(
        tracking_scope.frappe.db,
        "exists",
        return_value=True,
    ), patch.object(tracking_scope.frappe, "get_all", side_effect=get_all):
        assert tracking_scope._open_count_items("Moonleaf Bar") == {"MATCHA"}
