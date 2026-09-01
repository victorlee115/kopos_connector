from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "kopos_connector" / "demo" / "matcha_cafe.py").read_text(encoding="utf-8")
CATALOG_SOURCE = (
    ROOT / "kopos_connector" / "demo" / "moonleaf_catalog.py"
).read_text(encoding="utf-8")
RESTORED_SOURCE = (
    ROOT / "kopos_connector" / "acceptance" / "restored_commissioning.py"
).read_text(encoding="utf-8")


def test_demo_entrypoints_match_the_smoke_harness() -> None:
    assert "def setup_matcha_cafe_demo_json(" in SOURCE
    assert "def reset_matcha_cafe_demo_json(" in SOURCE
    assert "confirm_offline_orders_cleared" in SOURCE


def test_demo_is_test_site_only_and_never_sends_supplier_orders() -> None:
    assert "_require_safe_demo_site()" in SOURCE
    assert "The sample café can only be started on a training or test site" in SOURCE
    assert '"supplier_send": "outside_jiji_only"' in SOURCE
    assert '"purpose": "Opening Stock"' in SOURCE
    assert '"stock_entry_type": "Material Receipt"' in SOURCE
    assert '"Manufacture"' in SOURCE
    assert "The sample café opening stock did not finish" in SOURCE


def test_demo_covers_coffee_matcha_recipes_and_guided_evidence() -> None:
    for expected in (
        '"name": "Matcha Latte"',
        '"name": "Strawberry Matcha Latte"',
        '"name": "Iced Americano"',
        '"name": "Vanilla Oat Latte"',
        '"name": "Brown Sugar Cloud Coffee"',
        '"name": "Matcha Cloud"',
        '"menu_group": "Coffee"',
        '"menu_group": "Matcha"',
        "Extra matcha",
        "Oat milk",
        "Moonleaf Vanilla Cold Foam",
        "Moonleaf Matcha Concentrate",
        "success_and_failure_update_examples_exist",
        "supplier_order_is_draft",
        "count_assignment_exists",
        "staff_candidates_exist",
        "supplier_has_a_friendly_name",
    ):
        assert expected in CATALOG_SOURCE or expected in SOURCE


def test_demo_and_restored_runner_share_one_moonleaf_catalogue() -> None:
    for exported_name in (
        "MOONLEAF_INGREDIENTS",
        "MOONLEAF_OPENING_PACKS",
        "MOONLEAF_SELLABLES",
        "MOONLEAF_SUPPLIER",
    ):
        assert exported_name in SOURCE
    assert "from kopos_connector.demo.moonleaf_catalog import" in RESTORED_SOURCE
    assert "from kopos_connector.acceptance.restored_commissioning import" not in SOURCE
    assert "Matcha & Bean" not in SOURCE


def test_active_moonleaf_catalogue_has_no_unmeasured_size_choices() -> None:
    assert '"name": "Regular"' not in CATALOG_SOURCE
    assert '"name": "Large"' not in CATALOG_SOURCE
    assert '"qty_delta": Decimal("2")' in CATALOG_SOURCE
    assert '"target_substitution_key": "milk"' in CATALOG_SOURCE


def test_demo_verifies_recipes_choices_prices_and_preparation() -> None:
    for assertion in (
        "customer_choices_are_recipe_bound",
        "every_drink_has_a_price",
        "menu_items_use_counter_groups",
        "preparation_recipes_exist",
        "prepared_batches_exist",
        "made_item_waste_used_the_recipe",
        "counter_is_ready_for_business_pages",
        "director_can_open_the_cafe",
    ):
        assert assertion in SOURCE


def test_demo_commissions_one_human_facing_counter_and_scoped_director() -> None:
    assert 'DEMO_POS_PROFILE = "Moonleaf Counter Tablet"' in SOURCE
    assert "def _ensure_pos_profile(" in SOURCE
    assert 'document.set("payments", [{"mode_of_payment": cash_mode, "default": 1}])' in SOURCE
    assert '_ensure_user_permission(DEMO_DIRECTOR, "Company", DEMO_COMPANY)' in SOURCE
    assert "ensure_jiji_business_module_profile()" in SOURCE
    assert "def _ensure_staff_candidates(" in SOURCE
    assert 'SUPPLIER_NAME = "Moonleaf Provisions"' in CATALOG_SOURCE
    assert "def _supplier_id()" in SOURCE
    assert 'document.supplier = supplier' in SOURCE


def test_opening_reconciliation_contains_only_the_outlet_warehouse() -> None:
    opening = SOURCE.split("def _ensure_opening_stock", 1)[1].split(
        "def _ensure_storeroom_stock", 1
    )[0]
    assert '"warehouse": main_warehouse' in opening
    assert "source_warehouse" not in opening
    assert '"stock_entry_type": "Material Receipt"' in SOURCE


def test_demo_failure_example_uses_cafe_language() -> None:
    assert "JiJi could not finish this stock update. The saved information is safe." in SOURCE
    for internal_term in ("Test worker unavailable", "scheduler", "safe automatic retry"):
        assert internal_term not in SOURCE


def test_demo_reset_is_idempotent_by_business_identity() -> None:
    assert '{"input_hash": _plan_hash()}' in SOURCE
    assert 'schedule_key = f"{DEMO_PREFIX}:weekly-count"' in SOURCE


def test_demo_reset_removes_only_bounded_unpublished_moonleaf_recipe_edits() -> None:
    setup = SOURCE.split("def _setup(*, reset: bool)", 1)[1].split(
        "def _require_safe_demo_site", 1
    )[0]
    cleanup = SOURCE.split("def _reset_recipe_drafts()", 1)[1].split(
        "def _require_safe_demo_site", 1
    )[0]

    assert "_reset_recipe_drafts() if reset else 0" in setup
    assert '"company": DEMO_COMPANY' in cleanup
    assert '"sellable_item": ["in", menu_item_codes]' in cleanup
    assert '"status": "Draft"' in cleanup
    assert "MAX_RESET_RECIPE_DRAFTS + 1" in cleanup
    assert 'frappe.delete_doc("FB Recipe", name, ignore_permissions=True)' in cleanup
