from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY_MARK = "pytest.mark.inventory_regression"


def _tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def _marked_test_names(relative: str) -> set[str]:
    return {
        node.name
        for node in ast.walk(_tree(relative))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(ast.unparse(decorator) == INVENTORY_MARK for decorator in node.decorator_list)
    }


def _has_module_inventory_mark(relative: str) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets)
        and ast.unparse(node.value) == INVENTORY_MARK
        for node in _tree(relative).body
    )


def test_whole_optional_modules_are_marked_and_defer_optional_imports() -> None:
    optional_modules = (
        "tests/test_modifier_bounds_persistence_contract.py",
        "tests/test_recipe_modifier_authoring_contract.py",
        "tests/test_recipe_version_immutability.py",
        "tests/test_stock_issue_equivalence.py",
        "kopos_connector/kopos/tests/test_catalog_persistence_contract.py",
        "kopos_connector/kopos/tests/test_recipe_resolver.py",
        "kopos_connector/kopos/tests/test_e2e_return_remake_flow.py",
    )
    forbidden_prefixes = (
        "kopos_connector.api.fb_remakes",
        "kopos_connector.kopos.doctype.fb_recipe",
        "kopos_connector.kopos.services.inventory",
        "kopos_connector.kopos.services.recipe",
        "kopos_connector.smoke",
    )

    for relative in optional_modules:
        assert _has_module_inventory_mark(relative), relative
        top_level_optional_imports = [
            node.module
            for node in _tree(relative).body
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith(forbidden_prefixes)
        ]
        assert top_level_optional_imports == [], relative


def test_mixed_suites_keep_only_optional_behavior_behind_inventory_marker() -> None:
    expected_marked = {
        "kopos_connector/tests/test_fb_schema_contract.py": {
            "test_fb_order_inventory_schema",
            "test_prepared_resolved_sale_schema",
            "test_fb_stock_override_log_schema",
            "test_fb_order_line_inventory_schema",
            "test_fb_recipe_schema",
            "test_fb_modifier_schema",
            "test_fb_modifier_group_schema",
            "test_fb_modifier_group_authoring_script_exists",
            "test_fb_modifier_dependency_authoring_doc_exists",
            "test_fb_resolved_sale_schema",
            "test_fb_projection_log_inventory_schema",
            "test_fb_return_event_inventory_schema",
            "test_fb_return_event_line_inventory_schema",
            "test_fb_remake_event_schema",
            "test_fb_waste_event_schema",
            "test_fb_refill_schema",
            "test_inventory_child_tables_exist_and_are_tables",
        },
        "kopos_connector/tests/test_smoke_reliability_seed_contract.py": {
            "test_existing_recipe_is_repointed_to_reliability_item_code",
            "test_smoke_recipe_uses_company_specific_code_instead_of_mutating_published_recipe",
            "test_smoke_recipe_keeps_base_code_for_same_company",
            "test_smoke_recipe_components_pin_stock_units_before_frappe_defaults",
            "test_existing_smoke_recipe_repairs_component_stock_quantities_and_units",
            "test_existing_smoke_recipe_components_are_idempotent_when_correct",
        },
        "kopos_connector/tests/test_fb_order_stock_policy.py": {
            "test_detect_and_log_stock_shortfall",
            "test_before_submit_rejects_shortfall_when_negative_stock_policy_is_disabled",
            "test_before_submit_rejects_serialised_shortfall_even_when_negative_stock_is_enabled",
            "test_prepared_sale_reuses_only_the_frozen_resolved_snapshot",
            "test_prepared_sale_rejects_a_changed_resolution_hash",
            "test_prepared_sale_rejects_persisted_recipe_identity_edits",
        },
        "kopos_connector/tests/test_fb_service_contracts.py": {
            "test_ingredient_stock_service_uses_fb_order_sale_datetime",
            "test_return_service_updates_resolved_sale_status",
            "test_return_quantity_guard_locks_resolved_sales_before_validation",
            "test_transfer_service_uses_resolved_basic_rate",
        },
        "tests/test_task6_shift_lifecycle.py": {
            "test_shift_stock_requirement_is_bulk_bounded_for_two_thousand_orders",
        },
    }

    for relative, expected in expected_marked.items():
        assert expected.issubset(_marked_test_names(relative)), relative


def test_commercial_failure_boundaries_remain_in_default_selection() -> None:
    relative = "kopos_connector/tests/test_fb_order_stock_policy.py"
    marked = _marked_test_names(relative)
    assert {
        "test_before_submit_does_not_touch_inventory_before_accounting",
        "test_before_submit_does_not_invoke_failing_resolved_sale_subsystem",
        "test_prepared_before_submit_uses_only_the_commercial_line_snapshot",
        "test_commercial_line_snapshot_never_loads_recipe_or_inventory",
        "test_on_submit_never_invokes_optional_inventory",
        "test_on_submit_keeps_accounting_when_inventory_hooks_would_fail",
    }.isdisjoint(marked)

    api_source = (
        ROOT / "kopos_connector/kopos/tests/test_api.py"
    ).read_text(encoding="utf-8")
    assert "replenish_stock=True" not in api_source
