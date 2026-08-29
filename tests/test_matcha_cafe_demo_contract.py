from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "kopos_connector" / "demo" / "matcha_cafe.py").read_text(encoding="utf-8")


def test_demo_entrypoints_match_the_smoke_harness() -> None:
    assert "def setup_matcha_cafe_demo_json(" in SOURCE
    assert "def reset_matcha_cafe_demo_json(" in SOURCE
    assert "confirm_offline_orders_cleared" in SOURCE


def test_demo_is_test_site_only_and_never_sends_supplier_orders() -> None:
    assert "_require_safe_demo_site()" in SOURCE
    assert "The sample café can only be started on a training or test site" in SOURCE
    assert '"supplier_send": "outside_jiji_only"' in SOURCE
    assert SOURCE.count("document.submit()") == 1
    assert "The sample café opening stock did not finish" in SOURCE


def test_demo_covers_coffee_matcha_recipes_and_guided_evidence() -> None:
    for expected in (
        "Classic Matcha Latte",
        "Strawberry Matcha Latte",
        "Double-Shot Flat White",
        "Iced Oat Latte",
        "Vanilla Cold Brew",
        "success_and_failure_update_examples_exist",
        "supplier_order_is_draft",
        "count_assignment_exists",
    ):
        assert expected in SOURCE


def test_demo_failure_example_uses_cafe_language() -> None:
    assert "JiJi could not finish this stock update. The saved information is safe." in SOURCE
    for internal_term in ("Test worker unavailable", "scheduler", "safe automatic retry"):
        assert internal_term not in SOURCE


def test_demo_reset_is_idempotent_by_business_identity() -> None:
    assert '{"input_hash": _plan_hash()}' in SOURCE
    assert 'schedule_key = f"{DEMO_PREFIX}:weekly-count"' in SOURCE
