from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from .fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()
import frappe

from kopos_connector.api.promotion_setup import preview_offer_setup
from kopos_connector.kopos.doctype.kopos_promotion.kopos_promotion import KoPOSPromotion
from kopos_connector.api import promotion_setup as editor


def setup_values(**changes):
    return {
        "name": "Afternoon drinks", "kind": "percentage", "value": "10",
        "items": ["LATTE"], "outlets": ["Cafe"], "repeat": "once",
        "activation": "automatic", "start": "2030-01-01 10:00:00", "end": "",
        "example_price": "10.00", "example_qty": 2, **changes,
    }


def editor_values(**changes):
    values = {key: value for key, value in setup_values().items() if not key.startswith("example_")}
    return {**values, "repeat": "repeat", "schedule": None, **changes}


@pytest.fixture
def editor_documents(documents, monkeypatch):
    from kopos_connector.api import promotions
    monkeypatch.setattr(promotions, "require_company_director_for_promotion_publication", lambda: None)
    monkeypatch.setattr(editor, "_check_outlets", lambda names: None)
    monkeypatch.setattr(editor, "_menu_for_profile", lambda *args: [
        {"id": "LATTE", "label": "House latte", "group": "Drinks", "price_sen": 1000}])
    monkeypatch.setattr(editor, "_published_offer_rows", lambda *args, **kwargs: ([], None))
    return documents


@pytest.mark.parametrize("kind,value,discount", [
    ("percentage", "10", 200), ("amount", "1.50", 300),
    ("price", "7.50", 500), ("free_second", "0", 1000), ("half_second", "50", 500),
])
def test_full_page_preview_uses_native_shared_runtime_and_real_catalog_prices(editor_documents, kind, value, discount):
    result = editor.preview_offer(editor_values(kind=kind, value=value), "Cafe", [{"item": "LATTE", "quantity": 2}])
    assert result["quote"]["status"] == "complete"
    assert result["quote"]["totalDiscountSen"] == discount
    assert result["quote"]["items"][0]["name"] == "House latte"
    assert result["sample_products"][0]["price_sen"] == 1000
    for doc in editor_documents[0]:
        doc.save.assert_not_called()
        doc.insert.assert_not_called()


def test_recurring_preview_shows_actual_next_runs_without_publishing(editor_documents):
    schedule = {"timeZone": "Asia/Kuala_Lumpur", "weekdays": [1, 2, 3, 4, 5, 6, 7],
                "startDate": None, "endDate": None, "startMinute": 840, "endMinute": 1020}
    result = editor.preview_offer(editor_values(start="", schedule=schedule), "Cafe", [{"item": "LATTE", "quantity": 1}])
    assert len(result["occurrences"]) == 3
    assert result["quote"]["totalDiscountSen"] == 100
    assert result["occurrences"][0]["start"].endswith("T06:00:00.000Z")


@pytest.mark.parametrize("now,ended", [
    ("2030-01-02T23:00:00+08:00", False),
    ("2030-01-03T01:59:59+08:00", False),
    ("2030-01-03T02:00:00+08:00", True),
])
def test_desk_overnight_end_status_uses_shared_schedule_boundary(now, ended):
    from datetime import datetime
    schedule = {"timeZone": "Asia/Kuala_Lumpur", "weekdays": [1, 2, 3, 4, 5, 6, 7],
                "startDate": None, "endDate": "2030-01-02", "startMinute": 1320, "endMinute": 120}
    cache = {}
    assert editor._schedule_has_ended(schedule, datetime.fromisoformat(now), cache) is ended
    assert len(cache) == 1


def test_publication_review_distinguishes_repeated_intent_with_unchanged_content(monkeypatch):
    from kopos_connector.api import promotions
    publication = []
    monkeypatch.setattr(frappe.db, "count", lambda *args, **kwargs: len(publication), raising=False)
    monkeypatch.setattr(editor, "_editable_values", lambda doc: None)
    doc = SimpleNamespace(name="OFFER", modified="2030-01-01 10:00:00")
    first = promotions._review_offer_change(doc, action="stop", profiles=[])
    publication.append("a" * 64)
    second = promotions._review_offer_change(doc, action="stop", profiles=[])
    assert first["expected_hash"] != second["expected_hash"]
    assert second["evidence"]["publication_sequence"] == 1
    assert promotions._review_offer_change(doc, action="stop", profiles=[]) == second


def test_native_cost_batch_checks_every_same_and_mixed_pair_at_product_limit(editor_documents):
    from kopos_connector.pricing.adapter import promotion_cost_cases
    items = [{"item": f"ITEM-{index:02}", "item_group": "Drinks", "baseline_price_sen": 500 + index,
              "cogs_sen": 200, "components": []} for index in range(50)]
    doc = frappe.get_doc({"doctype": "KoPOS Promotion", **editor._editor_fields(
        editor_values(kind="free_second", value="0", items=[row["item"] for row in items]))})
    doc.name = "PAIR-COST-TEST"
    doc.validate()
    assert editor._editable_values(doc) is not None
    cases = promotion_cost_cases(doc, "Cafe", items)
    assert len(cases) == 1275
    assert cases[0]["net_revenue_sen"] == 500
    assert cases[1]["net_revenue_sen"] == 501
    assert all(row["cogs_sen"] == 400 for row in cases)


@pytest.mark.parametrize("change", [{"priority": 0}, {"modifier_policy": "include_paid_modifiers"},
                                    {"customer_message": "Keep this"},
                                    {"comparison_basis": "another_basis"}])
def test_guided_import_never_silently_overwrites_advanced_fields(editor_documents, change):
    doc = frappe.get_doc({"doctype": "KoPOS Promotion", **editor._editor_fields(editor_values())})
    doc.validate()
    assert editor._editable_values(doc) is not None
    for key, value in change.items():
        setattr(doc, key, value)
    assert editor._editable_values(doc) is None


def test_guided_offer_uses_editable_label_without_changing_frappe_identity(editor_documents):
    from kopos_connector.api.promotions import serialize_promotion
    doc = frappe.get_doc({"doctype": "KoPOS Promotion", **editor._editor_fields(editor_values())})
    doc.name = doc.promotion_name
    original = doc.name
    doc.display_label = "Friday night drinks"
    doc.validate()
    assert editor._editable_values(doc)["name"] == "Friday night drinks"
    wire = serialize_promotion(doc, "Cafe", kernel_version=2)
    assert wire["promotion_id"] == original
    assert wire["promotion_name"] == "Friday night drinks"
    assert doc.promotion_name == original


def test_legacy_distinct_label_remains_an_advanced_setting(editor_documents):
    from kopos_connector.api.promotions import serialize_promotion
    doc = frappe.get_doc({"doctype": "KoPOS Promotion", **editor._setup_fields(editor_values())})
    doc.name = doc.promotion_name
    doc.display_label = "Keep the legacy customer label"
    doc.validate()
    assert editor._editable_values(doc) is None
    assert serialize_promotion(doc, "Cafe")["promotion_name"] == doc.promotion_name


def test_guided_preview_cannot_use_another_outlet_or_unavailable_product(editor_documents):
    with pytest.raises(frappe.ValidationError, match="offer's outlets"):
        editor.preview_offer(editor_values(), "Other cafe", [{"item": "LATTE", "quantity": 1}])
    with pytest.raises(frappe.ValidationError, match="not on the current menu"):
        editor.preview_offer(editor_values(items=["MISSING"]), "Cafe", [{"item": "MISSING", "quantity": 1}])


def test_guided_permissions_run_before_parsing_or_lookup(monkeypatch):
    from kopos_connector.api import promotions
    def denied():
        raise frappe.PermissionError("No director scope")
    monkeypatch.setattr(promotions, "require_company_director_for_promotion_publication", denied)
    monkeypatch.setattr(editor, "_editor_values", Mock(side_effect=AssertionError("Not authorized")))
    with pytest.raises(frappe.PermissionError):
        editor.save_offer_draft("{")


@pytest.fixture
def documents(monkeypatch):
    defaults = json.loads((Path(__file__).parents[1] / "kopos_connector/kopos/doctype/kopos_promotion/kopos_promotion.json").read_text())
    created = []
    linked = SimpleNamespace(get=lambda field: 1 if field == "is_sales_item" else 0, check_permission=Mock())

    def get_doc(doctype, name=None):
        if isinstance(doctype, str):
            assert doctype in {"Item", "POS Profile"}
            return linked
        doc = KoPOSPromotion()
        for field in defaults["fields"]:
            setattr(doc, field["fieldname"], field.get("default"))
        for field, value in doctype.items():
            setattr(doc, field, [SimpleNamespace(**row) for row in value] if isinstance(value, list) else value)
        doc.get_doc_before_save = lambda: None
        doc.save = doc.insert = Mock(side_effect=AssertionError("Preview must not save"))
        created.append(doc)
        return doc

    monkeypatch.setattr(frappe, "get_doc", get_doc)
    monkeypatch.setattr(frappe, "has_permission", lambda *args: True)
    return created, linked


@pytest.mark.parametrize("kind,value,qty,repeat,discount", [
    ("percentage", "10", 2, "once", 200),
    ("percentage", "12.55", 3, "once", 378),
    ("amount", "1.50", 2, "once", 300),
    ("amount", "50", 2, "once", 2000),
    ("price", "7.50", 2, "once", 500),
    ("price", "15", 2, "once", 0),
    ("free_second", "0", 1, "once", 0),
    ("free_second", "0", 2, "once", 1000),
    ("free_second", "0", 4, "once", 1000),
    ("free_second", "0", 4, "repeat", 2000),
    ("half_second", "0", 2, "once", 500),
    ("half_second", "0", 4, "repeat", 1000),
])
def test_templates_use_existing_pricing_without_saving(documents, kind, value, qty, repeat, discount):
    values = setup_values(kind=kind, value=value, example_qty=qty, repeat=repeat)
    before = copy.deepcopy(values)
    result = preview_offer_setup(values)
    assert values == before
    assert result["example"]["discount_sen"] == discount
    assert result["example"]["total_sen"] == qty * 1000 - discount
    assert result["fields"]["is_active"] == 0
    assert result["fields"]["stacking_policy"] == "exclusive"
    assert result["fields"]["modifier_policy"] == "excluded_by_default"
    assert result["fields"]["eligible_items"] == [{"item_code": "LATTE"}]
    assert result["fields"]["outlet_scope_mode"] == "selected_pos_profiles"
    documents[0][0].save.assert_not_called()
    assert documents[1].check_permission.call_args_list


@pytest.mark.parametrize("change", [
    {"kind": "order_discount"}, {"value": "nan"}, {"value": "1.001"},
    {"value": "-1"}, {"value": "1e5"}, {"value": True},
    {"value": "101"}, {"value": "0"}, {"items": []}, {"outlets": []},
    {"items": ["LATTE", "LATTE"]}, {"items": ["LATTE"] * 51},
    {"example_qty": 101}, {"example_qty": 1.5}, {"example_qty": True},
    {"example_price": "Infinity"}, {"name": " "}, {"name": "x" * 141},
    {"start": "not a date"}, {"end": "2029-01-01 00:00:00"},
    {"start": "2030-01-01T10:00:00Z"}, {"start": "2030-13-01 10:00:00"},
    {"is_active": 1}, {"economics_status": "Ready"}, {"repeat": "maybe"},
])
def test_invalid_or_unreviewed_settings_are_rejected(documents, change):
    with pytest.raises(frappe.ValidationError):
        preview_offer_setup(setup_values(**change))
    for doc in documents[0]:
        doc.save.assert_not_called()


@pytest.mark.parametrize("payload", ["{", "x" * 20001, "[]", "null", "{}"])
def test_bad_request_is_explained_without_echoing_its_contents(documents, payload):
    with pytest.raises(frappe.ValidationError):
        preview_offer_setup(payload)


def test_permission_checked_before_payload_or_business_lookups(monkeypatch):
    monkeypatch.setattr(frappe, "has_permission", lambda *args: False)
    lookup = Mock(side_effect=AssertionError("No business lookup before permission"))
    monkeypatch.setattr(frappe, "get_doc", lookup)
    with pytest.raises(frappe.PermissionError):
        preview_offer_setup("{")
    lookup.assert_not_called()


def test_linked_item_and_outlet_permission_is_not_bypassed(documents):
    documents[1].check_permission.side_effect = frappe.PermissionError("Denied")
    with pytest.raises(frappe.PermissionError):
        preview_offer_setup(setup_values())
    assert documents[0] == []


def test_disabled_or_non_saleable_choices_are_rejected(documents):
    documents[1].get = lambda _field: 0
    with pytest.raises(frappe.ValidationError, match="not available"):
        preview_offer_setup(setup_values())


def test_wrong_system_timezone_cannot_silently_shift_an_offer(documents, monkeypatch):
    monkeypatch.setattr(frappe.db, "get_single_value", lambda *args: "Asia/Kolkata")
    with pytest.raises(frappe.ValidationError, match="timezone must be Malaysia"):
        preview_offer_setup(setup_values())


def test_manual_offer_is_explicitly_selected_for_the_example(documents):
    result = preview_offer_setup(json.dumps(setup_values(activation="manual_selectable")))
    assert result["fields"]["activation_mode"] == "manual_selectable"
    assert result["example"]["discount_sen"] == 200


def test_preview_uses_readable_item_name_without_changing_identity_or_price(documents):
    documents[1].get = lambda field: {"is_sales_item": 1, "item_name": "Strawberry Matcha Latte"}.get(field, 0)
    result = preview_offer_setup(setup_values())
    assert result["item_labels"] == {"LATTE": "Strawberry Matcha Latte"}
    assert result["fields"]["eligible_items"] == [{"item_code": "LATTE"}]
    assert result["example"]["item"] == "LATTE"
    assert result["example"]["total_sen"] == 1800


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "Infinity", "-Infinity", "101", "invalid"])
def test_raw_form_cannot_bypass_finite_percentage_validation(value):
    doc = KoPOSPromotion()
    doc.discount_type = "percentage"
    doc.discount_value = value
    with pytest.raises(frappe.ValidationError):
        doc.validate_discount_rule()


def test_publication_review_uses_existing_cost_and_outlet_permission_gates(monkeypatch):
    from kopos_connector.api import promotions
    from kopos_connector.api.promotion_setup import review_outlet_offers

    role = Mock()
    scope = Mock()
    body = {"pos_profile": "Cafe", "promotions": [{"promotion_name": "Tea", "valid_from": None, "valid_upto": None}]}
    build = Mock(return_value=body)
    monkeypatch.setattr(promotions, "require_company_director_for_promotion_publication", role)
    monkeypatch.setattr(promotions, "resolve_snapshot_pos_profile", lambda value: value)
    monkeypatch.setattr(promotions, "_require_director_profile_scope", scope)
    monkeypatch.setattr(promotions, "build_effective_snapshot_body", build)
    result = review_outlet_offers("Cafe")
    role.assert_called_once_with()
    scope.assert_called_once_with("Cafe")
    build.assert_called_once_with("Cafe")
    assert result["expected_hash"] == promotions.compute_snapshot_content_hash(body)
    build.side_effect = frappe.ValidationError("Cost review required")
    with pytest.raises(frappe.ValidationError, match="Cost review required"):
        review_outlet_offers("Cafe")


def test_changed_review_is_rejected_before_any_snapshot_write(monkeypatch):
    from kopos_connector.api import promotions
    from kopos_connector.api.promotion_setup import publish_reviewed_offers

    monkeypatch.setattr(promotions, "require_company_director_for_promotion_publication", lambda: None)
    monkeypatch.setattr(promotions, "resolve_snapshot_pos_profile", lambda value, **kwargs: value)
    monkeypatch.setattr(promotions, "_require_director_profile_scope", lambda value: None)
    monkeypatch.setattr(promotions, "build_snapshot_payload_for_persistence", lambda profile: {"snapshot_hash": "b" * 64})
    persistence = Mock(side_effect=AssertionError("Stale review must not save"))
    monkeypatch.setattr(promotions, "ensure_persisted_snapshot", persistence)
    with pytest.raises(frappe.ValidationError, match="changed after your review"):
        publish_reviewed_offers("Cafe", "a" * 64)
    persistence.assert_not_called()


@pytest.mark.parametrize("receipt", [None, "", "a" * 63, "a" * 65, "z" * 64, 123])
def test_publication_requires_a_review_receipt(receipt):
    from kopos_connector.api.promotion_setup import publish_reviewed_offers
    with pytest.raises(frappe.ValidationError, match="Review the outlet"):
        publish_reviewed_offers("Cafe", receipt)


def test_scheduled_rules_can_be_published_ahead_without_becoming_eligible_early():
    from datetime import datetime
    from kopos_connector.api.promotions import promotion_is_active

    rule = SimpleNamespace(outlet_scope_mode="selected_pos_profiles", eligible_pos_profiles=[SimpleNamespace(pos_profile="Cafe")], valid_from="2030-01-01 10:00:00", valid_upto="2030-01-02 18:00:00")
    before_start = datetime(2030, 1, 1, 9)
    assert not promotion_is_active(rule, "Cafe", before_start)
    assert promotion_is_active(rule, "Cafe", before_start, include_scheduled=True)
    assert not promotion_is_active(rule, "Other Cafe", before_start, include_scheduled=True)
    assert not promotion_is_active(rule, "Cafe", datetime(2030, 1, 3), include_scheduled=True)
