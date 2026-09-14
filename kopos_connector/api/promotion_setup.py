"""Scoped promotion editor. Documents own drafts; snapshots own till settings.

The original v1 example endpoints remain for existing clients. The full-page
editor uses real catalog prices and the versioned, process-isolated kernel.
"""
from __future__ import annotations

from decimal import Decimal
import json

import frappe
from frappe import _
from frappe.utils import cstr, get_datetime, now_datetime
from jsonschema import Draft7Validator

from kopos_connector.api.promotions import serialize_promotion


OFFER_TYPES = {
    "percentage": ("item_discount", "percentage", None),
    "amount": ("item_discount", "fixed_amount", None),
    "price": ("item_discount", "fixed_price", None),
    "free_second": ("buy_x_get_y", "free_item", "0"),
    "half_second": ("nth_item_discount", "percentage", "50"),
}
_NAMES = {
    "name": "Offer name", "kind": "Offer", "value": "Discount or price",
    "items": "Menu items", "outlets": "Outlets", "repeat": "How often",
    "activation": "How staff apply it", "start": "Start date", "end": "End date",
    "example_price": "Example price", "example_qty": "Example quantity",
}
_LINKS = {
    "type": "array", "minItems": 1, "maxItems": 50, "uniqueItems": True,
    "items": {"type": "string", "minLength": 1, "maxLength": 140},
}
_AMOUNT = {"type": "string", "pattern": r"^[0-9]{1,7}(\.[0-9]{1,2})?$"}
_DATE_WINDOW = {"type": "string", "pattern": r"^([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})?$"}
_SETUP_VALIDATOR = Draft7Validator({
    "type": "object", "additionalProperties": False,
    "required": list(_NAMES),
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 140},
        "kind": {"enum": list(OFFER_TYPES)},
        "value": _AMOUNT, "items": _LINKS, "outlets": _LINKS,
        "repeat": {"enum": ["once", "repeat"]},
        "activation": {"enum": ["automatic", "manual_selectable"]},
        "start": _DATE_WINDOW, "end": _DATE_WINDOW,
        "example_price": _AMOUNT,
        "example_qty": {"type": "integer", "minimum": 1, "maximum": 100},
    },
})


def _read_setup(payload):
    if isinstance(payload, str):
        if len(payload) > 20000:
            frappe.throw(_("This offer is too large. Choose up to 50 items and outlets."))
        try:
            payload = json.loads(payload)
        except ValueError:
            frappe.throw(_("The offer could not be read. Reopen Create an offer and try again."))
    error = next(_SETUP_VALIDATOR.iter_errors(payload), None)
    if error:
        field = next(iter(error.absolute_path), "")
        label = _NAMES.get(field, "Offer details")
        frappe.throw(_("Check {0}. Choose the listed options, enter a valid amount with at most two decimal places, and select at least one item and outlet.").format(_(label)))
    if not payload["name"].strip():
        frappe.throw(_("Give this offer a name, such as Afternoon drinks."))
    return payload


def _setup_fields(values):
    promotion_type, discount_type, fixed_value = OFFER_TYPES[values["kind"]]
    for field in ("start", "end"):
        try:
            if values[field]:
                get_datetime(values[field])
        except (ValueError, TypeError, OverflowError):
            frappe.throw(_("Choose a valid {0} using the calendar.").format(_(_NAMES[field])))
    return {
        "promotion_name": values["name"].strip(),
        "display_label": values["name"].strip(),
        "promotion_type": promotion_type, "discount_type": discount_type,
        "discount_value": fixed_value if fixed_value is not None else values["value"],
        "is_active": 0, "offline_allowed": 1, "priority": 100,
        "activation_mode": values["activation"], "stacking_policy": "exclusive",
        "outlet_scope_mode": "selected_pos_profiles", "eligible_scope_mode": "eligible_pool",
        "repeat_mode": values["repeat"], "buy_qty": 1, "discount_qty": 1,
        "discount_target": "cheaper_eligible", "comparison_basis": "base_item_only",
        "discount_basis": "base_item_only", "modifier_policy": "excluded_by_default",
        "min_qty": 0, "min_amount": 0,
        "valid_from": values["start"] or None, "valid_upto": values["end"] or None,
        "eligible_items": [{"item_code": name} for name in values["items"]],
        "eligible_item_groups": [],
        "eligible_pos_profiles": [{"pos_profile": name} for name in values["outlets"]],
    }


@frappe.whitelist(methods=["POST"])
def preview_offer_setup(payload):
    if not frappe.has_permission("KoPOS Promotion", "create"):
        frappe.throw(_("You do not have permission to create offers. Ask your administrator for access."), frappe.PermissionError)
    if frappe.db.get_single_value("System Settings", "time_zone") != "Asia/Kuala_Lumpur":
        frappe.throw(_("Your ERP timezone must be Malaysia before scheduling offers. Ask your administrator to correct System Settings; no offer has been saved."))
    values = _read_setup(payload)
    item_labels = {}
    for doctype, names in (("Item", values["items"]), ("POS Profile", values["outlets"])):
        for name in names:
            linked = frappe.get_doc(doctype, name)
            linked.check_permission("read")
            if linked.get("disabled") or (doctype == "Item" and not linked.get("is_sales_item")):
                frappe.throw(_("{0} is not available for this offer. Choose an enabled outlet or saleable menu item.").format(name))
            if doctype == "Item":
                item_labels[name] = cstr(linked.get("item_name") or name)
    fields = _setup_fields(values)
    promotion = frappe.get_doc({"doctype": "KoPOS Promotion", **fields})
    promotion.validate()
    promotion.name = "GUIDED-OFFER-PREVIEW"
    profile = values["outlets"][0]
    rule = serialize_promotion(promotion, profile)
    # Explicit example only. Never query live prices or pretend this synthetic
    # basket includes taxes, extras, other offers, or production acceptance.
    at_time = get_datetime(values["start"] or now_datetime())
    price_sen = int(Decimal(values["example_price"]) * 100)
    quantity = values["example_qty"]
    from kopos_connector.kopos.api.fb_orders import _calculate_expected_snapshot_promotions

    applied, _allocations = _calculate_expected_snapshot_promotions({
        "sale_datetime": at_time,
        "applied_promotions": [{"promotion_id": promotion.name}],
        "items": [{"item_code": values["items"][0], "qty": quantity, "unit_price_sen": price_sen}],
    }, [rule], profile)
    discount_sen = sum(row["amount_sen"] for row in applied)
    subtotal_sen = price_sen * quantity
    return {
        "fields": fields,
        "item_labels": item_labels,
        "example": {
            "item": values["items"][0], "qty": quantity, "price_sen": price_sen,
            "subtotal_sen": subtotal_sen, "discount_sen": discount_sen,
            "total_sen": subtotal_sen - discount_sen, "at_time": cstr(at_time),
        },
        "notice": _("Example only, using the price you entered and this offer alone. Taxes, paid extras, manual discounts and other offers are not included. Saving keeps the offer inactive; it does not send it to tablets."),
    }


@frappe.whitelist(methods=["POST"])
def review_outlet_offers(pos_profile):
    from kopos_connector.api.promotions import (
        _require_director_profile_scope, build_effective_snapshot_body,
        compute_snapshot_content_hash, require_company_director_for_promotion_publication,
        resolve_snapshot_pos_profile,
    )

    require_company_director_for_promotion_publication()
    if not isinstance(pos_profile, str) or not pos_profile.strip():
        frappe.throw(_("Choose the outlet whose offers you want to publish."))
    profile = resolve_snapshot_pos_profile(pos_profile)
    _require_director_profile_scope(profile)
    # This is the existing publication gate, including cost/second-director
    # approval. The review cannot grant an exception or change active flags.
    body = build_effective_snapshot_body(profile)
    return {
        "pos_profile": profile, "expected_hash": compute_snapshot_content_hash(body),
        "offers": [{"name": row["promotion_name"], "start": row["valid_from"], "end": row["valid_upto"]} for row in body["promotions"]],
    }


@frappe.whitelist(methods=["POST"])
def publish_reviewed_offers(pos_profile, expected_hash):
    from kopos_connector.api.promotions import publish_promotion_snapshot

    if not isinstance(expected_hash, str) or len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
        frappe.throw(_("Review the outlet's offers before publishing."))
    if not isinstance(pos_profile, str) or not pos_profile.strip():
        frappe.throw(_("Choose the outlet whose offers you reviewed."))
    return publish_promotion_snapshot(pos_profile=pos_profile, expected_hash=expected_hash)


def _editor_values(payload):
    if isinstance(payload, str):
        if len(payload) > 24000:
            frappe.throw(_("Choose up to 50 products and outlets."))
        try:
            payload = json.loads(payload)
        except ValueError:
            frappe.throw(_("These offer details could not be read. Your saved offer has not changed."))
    schema = _SETUP_VALIDATOR.schema
    properties = {key: value for key, value in schema["properties"].items() if not key.startswith("example_")}
    properties["schedule"] = {"type": ["object", "null"]}
    error = next(Draft7Validator({"type": "object", "required": list(properties),
                                "additionalProperties": False, "properties": properties}).iter_errors(payload), None)
    if error:
        label = _NAMES.get(next(iter(error.absolute_path), ""), "Offer details")
        frappe.throw(_("Check {0}. Complete the marked choices before continuing.").format(_(label)))
    values = dict(payload)
    if not values["name"].strip():
        frappe.throw(_("Give this offer a name."))
    values["name"] = values["name"].strip()
    values["items"] = sorted(values["items"])
    values["outlets"] = sorted(values["outlets"])
    fixed = OFFER_TYPES[values["kind"]][2]
    values["value"] = format(Decimal(fixed if fixed is not None else values["value"]).normalize(), "f")
    if frappe.db.get_single_value("System Settings", "time_zone") != "Asia/Kuala_Lumpur":
        frappe.throw(_("Scheduling needs Malaysia time in System Settings. Ask your administrator to check it; no offer has changed."))
    if values["schedule"] is not None:
        from kopos_connector.pricing.runtime import calculate

        if values["start"] or values["end"]:
            frappe.throw(_("For a repeating offer, use its first and last dates, not a second date range."))
        values["schedule"] = calculate("schedule", values["schedule"])
    return values


def _editor_fields(values):
    fields = _setup_fields(values)
    fields.update({"pricing_version": 2, "stacking_policy": "best_price",
                   "schedule_json": json.dumps(values["schedule"], separators=(",", ":")) if values["schedule"] is not None else None})
    return fields


def _permitted_outlets():
    from kopos_connector.api.director_scope import allowed_companies, director_pos_profiles, resolve_director_context

    names = set()
    for company in allowed_companies():
        names.update(director_pos_profiles(resolve_director_context(company=company)))
    if not names:
        return []
    return frappe.get_all("POS Profile", filters={"name": ["in", sorted(names)], "disabled": 0},
                          fields=["name", "company", "warehouse"], order_by="name asc", limit_page_length=501)


def _check_outlets(names):
    from kopos_connector.api.promotions import _require_director_profile_scope

    for name in names:
        _require_director_profile_scope(name)
        if frappe.db.get_value("POS Profile", name, "disabled"):
            frappe.throw(_("{0} is closed. Choose an enabled outlet.").format(name))


def _document_scope(doc):
    names = sorted({cstr(row.pos_profile) for row in doc.eligible_pos_profiles or [] if cstr(row.pos_profile)})
    if cstr(doc.outlet_scope_mode) != "selected_pos_profiles" or not names:
        frappe.throw(_("This advanced offer covers all outlets. Ask your administrator to review it without changing its scope."))
    _check_outlets(names)
    return names


def _menu_for_profile(profile_name, item_codes=None):
    from kopos_connector.api.catalog import get_allowed_item_groups, get_saleable_item_rows, get_item_price_amounts_map, money_to_sen

    profile = frappe.get_cached_doc("POS Profile", profile_name).as_dict()
    filters = {"disabled": 0, "is_sales_item": 1}
    groups = get_allowed_item_groups(profile)
    if groups:
        filters["item_group"] = ["in", sorted(groups)]
    rows = get_saleable_item_rows(filters, item_codes=set(item_codes) if item_codes is not None else None)
    if len(rows) > 10000:
        frappe.throw(_("This menu is too large to review in one offer."))
    price_list = cstr(profile.get("selling_price_list"))
    amounts = get_item_price_amounts_map([row["id"] for row in rows], price_list,
                                        {row["id"]: cstr(row.get("stock_uom")) for row in rows})
    return [{"id": row["id"], "label": row["name"], "group": cstr(row.get("category_id")),
             "price_sen": money_to_sen(amounts[row["id"]] if row["id"] in amounts else row.get("price", 0))}
            for row in rows if not price_list or row["id"] in amounts]


def _check_items(values):
    for profile in values["outlets"]:
        available = {row["id"] for row in _menu_for_profile(profile, values["items"])}
        missing = set(values["items"]) - available
        if missing:
            frappe.throw(_("{0} is not on the current menu at {1}. Check Menu & prices or remove it from this offer.").format(", ".join(sorted(missing)), profile))


def _editable_values(doc):
    """No lossy import: unsupported rules stay on their existing expert form."""
    guided = int(getattr(doc, "pricing_version", 1) or 1) == 2
    kind = next((key for key, (offer_type, discount_type, fixed) in OFFER_TYPES.items()
                 if doc.promotion_type == offer_type and doc.discount_type == discount_type
                 and (fixed is None or Decimal(str(doc.discount_value or 0)) == Decimal(fixed))), None)
    pair = kind in {"free_second", "half_second"}
    if (not kind or not doc.eligible_items or doc.eligible_item_groups
            or cstr(doc.outlet_scope_mode) != "selected_pos_profiles"
            or cstr(doc.stacking_policy or "exclusive") not in {"exclusive", "best_price"}
            or cstr(doc.eligible_scope_mode or "eligible_pool") != "eligible_pool"
            or cstr(doc.modifier_policy or "excluded_by_default") != "excluded_by_default"
            or int(doc.min_qty or 0) or Decimal(str(doc.min_amount or 0))
            or not int(doc.offline_allowed or 0) or doc.priority is None or int(doc.priority) != 100
            or cstr(doc.comparison_basis or "base_item_only") != "base_item_only"
            or cstr(doc.discount_basis or "base_item_only") != "base_item_only"
            or cstr(doc.discount_target or "cheaper_eligible") != "cheaper_eligible"
            or (not guided and cstr(doc.display_label or doc.promotion_name) != doc.promotion_name)
            or cstr(doc.customer_message)
            or (pair and (int(doc.buy_qty or 0) != 1 or int(doc.discount_qty or 0) != 1))):
        return None
    return {"name": cstr(doc.display_label or doc.promotion_name) if guided else doc.promotion_name,
            "kind": kind, "value": format(Decimal(str(doc.discount_value or 0)).normalize(), "f"),
            "items": sorted(cstr(row.item_code) for row in doc.eligible_items),
            "outlets": sorted(cstr(row.pos_profile) for row in doc.eligible_pos_profiles),
            "repeat": cstr(doc.repeat_mode or "repeat"), "activation": cstr(doc.activation_mode or "automatic"),
            "start": cstr(doc.valid_from or ""), "end": cstr(doc.valid_upto or ""),
            "schedule": frappe.parse_json(getattr(doc, "schedule_json", None) or "null")}


def _read_editor_document(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 140:
        frappe.throw(_("Choose a saved offer."))
    doc = frappe.get_doc("KoPOS Promotion", name)
    _document_scope(doc)
    return doc


@frappe.whitelist(methods=["GET"])
def get_offer_editor(name):
    from kopos_connector.api.promotions import require_company_director_for_promotion_publication

    require_company_director_for_promotion_publication()
    doc = _read_editor_document(name)
    values = _editable_values(doc)
    return {"id": doc.name, "revision": cstr(doc.modified), "values": values,
            "editable": values is not None,
            "message": "" if values else _("This offer uses advanced settings. Its rules have not been changed. Ask an administrator to review it.")}


@frappe.whitelist(methods=["GET"])
def get_offer_products(pos_profile):
    from kopos_connector.api.promotions import require_company_director_for_promotion_publication

    require_company_director_for_promotion_publication()
    _check_outlets([pos_profile])
    return {"items": _menu_for_profile(pos_profile), "pos_profile": pos_profile}


@frappe.whitelist(methods=["POST"])
def save_offer_draft(payload, name=None, revision=None):
    from kopos_connector.api.promotions import require_company_director_for_promotion_publication

    require_company_director_for_promotion_publication()
    values = _editor_values(payload)
    _check_outlets(values["outlets"])
    _check_items(values)
    fields = _editor_fields(values)
    if name:
        frappe.db.sql("SELECT name FROM `tabKoPOS Promotion` WHERE name = %s FOR UPDATE", (name,))
        doc = _read_editor_document(name)
        if _editable_values(doc) is None:
            frappe.throw(_("This advanced offer cannot be changed in the guided editor."))
        if cstr(doc.modified) != cstr(revision):
            if _editable_values(doc) == values and int(getattr(doc, "pricing_version", 1) or 1) == 2:
                return {"id": doc.name, "revision": cstr(doc.modified), "status": "draft_saved"}
            frappe.throw(_("Someone changed this offer while you were editing. Reopen it to review the latest version."))
        # Check-and-save also uses Frappe's optimistic modified timestamp check.
        # Frappe's field autoname keeps promotion_name tied to the document ID.
        # The existing display_label owns editable v2 names; never rename IDs
        # referenced by published snapshots or delayed historical sales.
        fields["promotion_name"] = doc.promotion_name
        doc.update(fields)
        doc.save(ignore_permissions=True)
    else:
        if frappe.db.exists("KoPOS Promotion", values["name"].strip()):
            existing = _read_editor_document(values["name"].strip())
            if _editable_values(existing) == values and int(getattr(existing, "pricing_version", 1) or 1) == 2:
                return {"id": existing.name, "revision": cstr(existing.modified), "status": "draft_saved"}
            frappe.throw(_("An offer already has this name. Choose another name or edit that offer."))
        doc = frappe.get_doc({"doctype": "KoPOS Promotion", **fields})
        doc.insert(ignore_permissions=True)
    return {"id": doc.name, "revision": cstr(doc.modified), "status": "draft_saved"}


def _published_offer_rows(profile, exclude=None):
    from kopos_connector.api.promotions import get_latest_published_snapshot

    latest = get_latest_published_snapshot(profile)
    if not latest:
        return [], None
    payload = frappe.parse_json(latest.snapshot_payload)
    return [dict(row, schedule=row.get("schedule")) for row in payload.get("promotions", []) if row["promotion_id"] != exclude], latest


@frappe.whitelist(methods=["POST"])
def preview_offer(payload, pos_profile, basket, at_time=None, name=None):
    from kopos_connector.api.promotions import require_company_director_for_promotion_publication
    from kopos_connector.pricing.adapter import quote_snapshot, snapshot_body, time_iso
    from kopos_connector.pricing.runtime import calculate

    require_company_director_for_promotion_publication()
    values = _editor_values(payload)
    _check_outlets(values["outlets"])
    if pos_profile not in values["outlets"]:
        frappe.throw(_("Choose one of this offer's outlets for the example."))
    if name:
        _read_editor_document(name)
    _check_items(values)
    doc = frappe.get_doc({"doctype": "KoPOS Promotion", **_editor_fields(values)})
    doc.validate()
    doc.name = name or "UNSAVED-OFFER"
    rule = serialize_promotion(doc, pos_profile, kernel_version=2)
    rows, _latest = _published_offer_rows(pos_profile, exclude=name)
    if any(row["promotion_id"] == doc.name for row in rows):
        frappe.throw(_("Choose a different offer name."))
    rows.append(rule)
    menu = _menu_for_profile(pos_profile)
    by_id = {row["id"]: row for row in menu}
    if isinstance(basket, str):
        if len(basket) > 24000:
            frappe.throw(_("Choose up to 50 basket lines."))
        try:
            basket = json.loads(basket)
        except ValueError:
            frappe.throw(_("Check the sample basket."))
    if not isinstance(basket, list) or not 1 <= len(basket) <= 50:
        frappe.throw(_("Add between one and 50 products to the sample basket."))
    cart = []
    for index, line in enumerate(basket):
        if (not isinstance(line, dict) or set(line) != {"item", "quantity"}
                or line["item"] not in by_id or type(line["quantity"]) is not int
                or not 1 <= line["quantity"] <= 100):
            frappe.throw(_("Choose a menu product and a whole quantity from 1 to 100."))
        item = by_id[line["item"]]
        cart.append({"id": f"EXAMPLE-{index:03}", "menuItemId": item["id"], "name": item["label"],
                     "quantity": line["quantity"], "unitPriceSen": item["price_sen"], "modifierTotalSen": 0,
                     "modifiers": [], "discountAmountSen": 0, "lineTotalSen": item["price_sen"] * line["quantity"]})
    body = snapshot_body(pos_profile, rows, [{"item_code": row["id"], "item_group": row["group"]} for row in menu])
    when = time_iso(at_time or values["start"] or now_datetime())
    occurrences = calculate("occurrences", {"schedule": values["schedule"], "now": time_iso(now_datetime())}) if values["schedule"] else []
    if not at_time and occurrences:
        from datetime import datetime
        when = max(time_iso(now_datetime()), occurrences[0]["start"], key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))
    quote = quote_snapshot(body, cart, when, selected=[doc.name] if values["activation"] == "manual_selectable" else [])
    return {"quote": quote, "at_time": when, "occurrences": occurrences,
            "pos_profile": pos_profile, "offer_id": doc.name,
            "sample_products": [row for row in menu if row["id"] in values["items"]],
            "notice": _("Current menu prices, before tax and paid extras. Includes this draft and the outlet's published offers. Nothing has been sent to tablets.")}


def _schedule_has_ended(schedule, now, cache):
    """The shared schedule owner decides when the final overnight run ends."""
    if not schedule or not schedule.get("endDate") or schedule["endDate"] > now.date().isoformat():
        return False
    from kopos_connector.pricing.runtime import calculate

    key = json.dumps(schedule, sort_keys=True)
    if key not in cache:
        cache[key] = not calculate("occurrences", {"schedule": schedule, "now": now.isoformat()})
    return cache[key]


@frappe.whitelist(methods=["GET"])
def get_promotion_desk():
    from kopos_connector.api.promotions import require_company_director_for_promotion_publication
    from kopos_connector.pricing.adapter import time_iso
    from datetime import datetime

    require_company_director_for_promotion_publication()
    outlets = _permitted_outlets()
    allowed = {row["name"] for row in outlets}
    names = frappe.get_all("KoPOS Promotion POS Profile", filters={"pos_profile": ["in", sorted(allowed)]},
                           pluck="parent", limit_page_length=5001) if allowed else []
    if len(names) > 5000:
        frappe.throw(_("There are too many offers to show at once. Ask support to archive old drafts."))
    published, latest_by_profile = {}, {}
    for profile in sorted(allowed):
        rows, snapshot = _published_offer_rows(profile)
        latest_by_profile[profile] = snapshot
        for row in rows:
            published.setdefault(row["promotion_id"], []).append({"outlet": profile, "rule": row,
                                                                  "snapshot_hash": snapshot.snapshot_hash})
    devices = frappe.get_all("KoPOS Device", filters={"enabled": 1, "pos_profile": ["in", sorted(allowed)]},
                             fields=["device_name", "pos_profile", "promotion_snapshot_hash", "promotion_snapshot_version", "promotion_received_at"],
                             limit_page_length=5001) if allowed else []
    if len(devices) > 5000:
        frappe.throw(_("There are too many tablets to show a reliable delivery count."))
    offers, schedule_end_cache = [], {}
    now = datetime.fromisoformat(time_iso(now_datetime()))
    for name in sorted(set(names)):
        doc = frappe.get_doc("KoPOS Promotion", name)
        scope = {cstr(row.pos_profile) for row in doc.eligible_pos_profiles or []}
        if not scope or not scope.issubset(allowed) or cstr(doc.outlet_scope_mode) != "selected_pos_profiles":
            continue
        values = _editable_values(doc)
        live = published.get(name, [])
        state = "Draft"
        last = frappe.get_all("KoPOS Promotion Publication", filters={"promotion": name},
                              fields=["action", "outlet_results"], order_by="creation desc", limit_page_length=1)
        delivery_scope = set(scope) | {entry["outlet"] for entry in live}
        if not live and last and last[0].action == "stop":
            state = "Stopped"
            delivery_scope.update(row["pos_profile"] for row in frappe.parse_json(last[0].outlet_results))
        if live:
            state = "Published"
            rules = [entry["rule"] for entry in live]
            if all((row.get("valid_upto") and datetime.fromisoformat(time_iso(row["valid_upto"])) < now)
                   or _schedule_has_ended(row.get("schedule"), now, schedule_end_cache) for row in rules):
                state = "Ended"
            elif any(row.get("schedule") or (row.get("valid_from") and datetime.fromisoformat(time_iso(row["valid_from"])) > now) for row in rules):
                state = "Scheduled"
        delivery_rows = []
        if live or state == "Stopped":
            for device in devices:
                if device.pos_profile not in delivery_scope:
                    continue
                snapshot = latest_by_profile.get(device.pos_profile)
                received = bool(snapshot and device.promotion_snapshot_hash == snapshot.snapshot_hash
                                and device.promotion_snapshot_version == snapshot.snapshot_version)
                delivery_rows.append({"name": device.device_name, "outlet": device.pos_profile,
                                      "received": received, "received_at": cstr(device.promotion_received_at) if received else None})
        received = sum(row["received"] for row in delivery_rows)
        delivery = (_("{0} of {1} tills confirmed · {2} waiting to sync").format(received, len(delivery_rows), len(delivery_rows) - received)
                    if delivery_rows else _("No enabled tills to confirm") if live or state == "Stopped" else _("Draft only · not sent to tills"))
        offers.append({"id": name, "name": values["name"] if values else doc.promotion_name,
                       "state": state, "editable": values is not None,
                       "outlets": sorted(scope), "published_outlets": [row["outlet"] for row in live],
                       "has_draft_changes": bool(live) and any(
                           serialize_promotion(doc, row["outlet"], kernel_version=2) != row["rule"] for row in live),
                       "delivery": delivery, "tablets": delivery_rows})
    return {"outlets": outlets, "offers": offers, "time_zone": "Asia/Kuala_Lumpur", "server_time": cstr(now_datetime())}


@frappe.whitelist(methods=["POST"])
def review_saved_offer(name, revision):
    from kopos_connector.api.promotions import review_offer_change
    return review_offer_change(name, action="activate", revision=revision)


@frappe.whitelist(methods=["POST"])
def activate_reviewed_offer(name, revision, expected_hash):
    from kopos_connector.api.promotions import publish_reviewed_offer_change
    return publish_reviewed_offer_change(name, expected_hash, action="activate", revision=revision)


@frappe.whitelist(methods=["POST"])
def review_offer_stop(name):
    from kopos_connector.api.promotions import review_offer_change
    return review_offer_change(name, action="stop")


@frappe.whitelist(methods=["POST"])
def stop_reviewed_offer(name, expected_hash):
    from kopos_connector.api.promotions import publish_reviewed_offer_change
    return publish_reviewed_offer_change(name, expected_hash, action="stop")
