from kopos_connector.kopos.services.inventory_autopilot.menu_authoring import (
    CSV_HEADERS,
    csv_template,
    validate_recipe_csv,
)


def test_template_has_one_stable_header_row():
    assert csv_template() == ",".join(CSV_HEADERS) + "\n"


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
