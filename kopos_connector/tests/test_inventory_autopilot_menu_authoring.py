from kopos_connector.kopos.services.inventory_autopilot.menu_authoring import (
    CSV_HEADERS,
    MAX_GUIDED_COMPONENTS,
    build_guided_recipe_preview,
    csv_template,
    draft_recipe_code,
    summarize_menu_authoring,
    validate_recipe_csv,
)


def test_template_has_one_stable_header_row():
    assert csv_template() == ",".join(CSV_HEADERS) + "\n"


def test_draft_recipe_code_keeps_the_item_company_and_version_readable():
    assert draft_recipe_code(
        sellable_item="MONT-BLANC", company_abbr="JJI", version_no=3
    ) == "MONT-BLANC-JJI-v3"


def test_draft_recipe_code_stays_inside_the_standard_data_limit():
    code = draft_recipe_code(
        sellable_item="X" * 200, company_abbr="JJI", version_no=12
    )

    assert len(code) == 140
    assert code.endswith("-JJI-v12")


def test_recipe_csv_groups_component_rows_and_does_not_require_writes():
    content = ",".join(CSV_HEADERS) + "\n" + ",".join([
        "mont-blanc-v1", "Mont Blanc", "MONT-BLANC", "JiJi", "Finished Drink",
        "1", "Nos", "1", "Nos", "Cold Foam", "Prep Item", "0.08", "kg", "0.08", "kg", "1", "1",
    ]) + "\n" + ",".join([
        "mont-blanc-v1", "Mont Blanc", "MONT-BLANC", "JiJi", "Finished Drink",
        "1", "Nos", "1", "Nos", "Orange Juice", "Prep Item", "0.12", "L", "0.12", "L", "1", "1",
    ]) + "\n"

    result = validate_recipe_csv(content)

    assert result["valid"] is True
    assert len(result["recipes"]) == 1
    assert len(result["recipes"][0]["components"]) == 2
    assert result["recipes"][0]["recipe_code"] == "mont-blanc-v1"


def test_recipe_csv_reports_row_level_mismatch_and_invalid_numbers():
    content = ",".join(CSV_HEADERS) + "\n" + ",".join([
        "recipe", "Name", "ITEM", "Company", "Finished Drink",
        "bad", "Nos", "1", "Nos", "Sugar", "Ingredient", "0", "kg", "", "", "1", "1",
    ]) + "\n" + ",".join([
        "recipe", "Different Name", "ITEM", "Company", "Finished Drink",
        "1", "Nos", "1", "Nos", "Water", "Ingredient", "1", "L", "", "", "1", "1",
    ]) + "\n"

    result = validate_recipe_csv(content)

    assert result["valid"] is False
    assert {error["row"] for error in result["errors"]} == {2, 3}
    assert any("yield_qty" in error["message"] for error in result["errors"])
    assert any("recipe-level fields" in error["message"] for error in result["errors"])


def test_menu_checklist_counts_only_saleable_items_and_requires_explicit_exclusions():
    result = summarize_menu_authoring(
        item_rows=[
            {
                "name": "MONT-BLANC",
                "item_name": "Mont Blanc",
                "is_sales_item": 1,
                "disabled": 0,
                "custom_fb_item_role": "Sellable Drink",
                "custom_fb_inventory_excluded": 0,
            },
            {
                "name": "GIFT-CARD",
                "item_name": "Gift card",
                "is_sales_item": 1,
                "disabled": 0,
                "custom_fb_item_role": "Sellable Drink",
                "custom_fb_inventory_excluded": 1,
                "custom_fb_inventory_exclusion_reason": "No physical ingredient consumption",
            },
            {
                "name": "MILK",
                "item_name": "Milk",
                "is_sales_item": 0,
                "is_stock_item": 1,
                "disabled": 0,
                "custom_fb_item_role": "Ingredient",
                "custom_fb_inventory_excluded": 0,
            },
        ],
        recipe_rows=[
            {
                "name": "MONT-BLANC-V1",
                "status": "Active",
                "sellable_item": "MONT-BLANC",
                "company": "JiJi",
                "canonical_hash": "a" * 64,
            },
            {
                "name": "LEGACY-STUB",
                "status": "Active",
                "sellable_item": "GIFT-CARD",
                "company": "JiJi",
                "canonical_hash": "",
            },
        ],
        bom_count=1,
        modifier_count=2,
        promotion_count=0,
        company="JiJi",
        company_selection_required=False,
        item_fields_ready=True,
        recipe_schema_ready=True,
    )

    assert result["saleable_items"] == 2
    assert result["items_ready"] == 1
    assert result["items_missing_recipe"] == 0
    assert result["approved_exclusions"] == 1
    assert result["unclassified_items"] == 0
    assert result["ready"] is True


def test_menu_checklist_blocks_missing_recipe_and_unexplained_exclusion():
    result = summarize_menu_authoring(
        item_rows=[
            {
                "name": "LATTE",
                "item_name": "Latte",
                "is_sales_item": 1,
                "disabled": 0,
                "custom_fb_item_role": "Sellable Drink",
                "custom_fb_inventory_excluded": 0,
            },
            {
                "name": "SERVICE",
                "item_name": "Service",
                "is_sales_item": 1,
                "disabled": 0,
                "custom_fb_item_role": "",
                "custom_fb_inventory_excluded": 1,
                "custom_fb_inventory_exclusion_reason": "",
            },
        ],
        recipe_rows=[],
        bom_count=0,
        modifier_count=0,
        promotion_count=0,
        company="JiJi",
        company_selection_required=False,
        item_fields_ready=True,
        recipe_schema_ready=True,
    )

    assert result["items_missing_recipe"] == 1
    assert result["invalid_exclusions"] == 1
    assert result["unclassified_items"] == 1
    assert result["missing_recipe_items"] == ["Latte"]
    assert result["ready"] is False


def test_guided_preview_uses_decimal_uom_conversion_and_serving_ratio():
    result = build_guided_recipe_preview(
        yield_qty="10",
        yield_uom="Nos",
        default_serving_qty="1",
        default_serving_uom="Nos",
        components=[
            {
                "item": "MILK",
                "component_type": "Ingredient",
                "qty": "0.2",
                "uom": "L",
                "loss_factor_pct": "5",
                "affects_stock": 1,
                "affects_cogs": 1,
            }
        ],
        item_details={
            "MILK": {
                "stock_uom": "ml",
                "conversion_factors": {"L": "1000"},
                "valuation_rate": "0.01",
            }
        },
    )

    assert result["valid"] is True
    row = result["components"][0]
    assert row["conversion_factor"] == "1000"
    assert row["stock_qty_per_batch"] == "210"
    assert row["stock_qty_per_serving"] == "21"
    assert result["cost_per_batch"] == "2.1"
    assert result["cost_per_serving"] == "0.21"


def test_guided_preview_blocks_missing_prepared_bom_and_reports_missing_cost():
    result = build_guided_recipe_preview(
        yield_qty="1",
        yield_uom="Nos",
        default_serving_qty="1",
        default_serving_uom="Nos",
        components=[
            {
                "item": "COLD-FOAM",
                "component_type": "Prep Item",
                "qty": "0.1",
                "uom": "L",
                "affects_stock": 1,
                "affects_cogs": 1,
            }
        ],
        item_details={
            "COLD-FOAM": {
                "stock_uom": "L",
                "conversion_factors": {},
                "valuation_rate": None,
            }
        },
        prepared_components={},
    )

    assert result["valid"] is False
    assert result["cost_status"] == "missing"
    assert any("BOM" in error["message"] for error in result["errors"])
    assert any("valuation" in warning["message"] for warning in result["warnings"])


def test_guided_preview_bounds_component_rows():
    result = build_guided_recipe_preview(
        yield_qty="1",
        yield_uom="Nos",
        default_serving_qty="1",
        default_serving_uom="Nos",
        components=[{} for _ in range(MAX_GUIDED_COMPONENTS + 1)],
        item_details={},
    )

    assert result["valid"] is False
    assert any("at most" in error["message"] for error in result["errors"])
