from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint as frappe_cint
from frappe.utils import cstr, flt as frappe_flt, get_datetime, now_datetime
from jsonschema import Draft7Validator, ValidationError as SchemaValidationError

from kopos_connector.api.catalog import get_default_pos_profile
from kopos_connector.api.devices import get_device_doc, get_session_roles, require_system_manager

SNAPSHOT_STATUS_PUBLISHED = "Published"
SNAPSHOT_STATUS_SUPERSEDED = "Superseded"
REVIEW_STATUS_NOT_REQUIRED = "not_required"
REVIEW_STATUS_PENDING = "pending_review"
REVIEW_DECISIONS = {"approved_override", "rejected"}

MINOR_RECONCILIATION_ISSUES = {
    "Applied promotion total does not match line allocations",
}

MAJOR_RECONCILIATION_ISSUES = {
    "Applied promotions missing snapshot version",
    "Applied promotion amount evidence is invalid",
    "Referenced promotion snapshot was not found",
    "Promotion snapshot hash mismatch",
    "Applied promotion ids do not match published snapshot",
    "Promotion snapshot profile mismatch",
}


def cint(value: Any) -> int:
    return int(frappe_cint(value))


def flt(value: Any) -> float:
    return float(frappe_flt(value))


def sort_string_values(values: list[str]) -> list[str]:
    return sorted(values)


@lru_cache(maxsize=1)
def _canonical_promotion_validator() -> Draft7Validator:
    # Packaged bytes are generated from POS's versioned contract, never loaded
    # from a sibling checkout in a running Bench. Historical payloads stay intact.
    schema = json.loads(
        files("kopos_connector").joinpath("contracts/promotion.v1.schema.json").read_text()
    )
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def amount_to_sen(value: Any) -> int | None:
    raw_value = cstr(value).strip()
    if not raw_value:
        return None
    try:
        decimal_value = Decimal(raw_value)
    except InvalidOperation:
        return None
    return int(
        (decimal_value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def build_effective_snapshot_body(
    pos_profile: str,
    at_time: Any | None = None,
) -> dict[str, Any]:
    evaluation_time = get_datetime(at_time or now_datetime())
    # Publish scheduled offers ahead of time so offline tablets can enforce
    # their existing valid_from guard without an operator publishing at opening.
    promotions = get_active_promotions(pos_profile, evaluation_time, include_scheduled=True)
    from kopos_connector.api.inventory import validate_promotion_economics_for_publication

    for promotion in promotions:
        try:
            validate_promotion_economics_for_publication(
                promotion,
                pos_profile=pos_profile,
            )
        except (ValueError, frappe.ValidationError) as error:
            frappe.throw(
                _("Promotion {0} cannot be published: {1}").format(
                    cstr(getattr(promotion, "name", "")),
                    cstr(error),
                ),
                frappe.ValidationError,
            )
    normalized = [serialize_promotion(doc, pos_profile) for doc in promotions]
    normalized.sort(
        key=lambda promo: (promo.get("priority", 100), promo.get("promotion_id", ""))
    )
    return {
        "pos_profile": pos_profile,
        "promotions": normalized,
    }


def compute_snapshot_content_hash(body: dict[str, Any]) -> str:
    content_string = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content_string.encode("utf-8")).hexdigest()


def build_snapshot_version_from_hash(snapshot_hash: str) -> str:
    return "KOPOS-PROMO-{0}".format(snapshot_hash[:16].upper())


def build_snapshot_payload_for_persistence(
    pos_profile: str,
    at_time: Any | None = None,
) -> dict[str, Any]:
    evaluation_time = get_datetime(at_time or now_datetime())
    effective_body = build_effective_snapshot_body(pos_profile, evaluation_time)
    snapshot_hash = compute_snapshot_content_hash(effective_body)
    snapshot_version = build_snapshot_version_from_hash(snapshot_hash)
    return {
        **effective_body,
        "effective_from": evaluation_time.isoformat(),
        "published_at": now_datetime().isoformat(),
        "snapshot_hash": snapshot_hash,
        "snapshot_version": snapshot_version,
    }


def get_snapshot_by_hash(pos_profile: str, snapshot_hash: str):
    if not snapshot_hash:
        return None
    name = frappe.db.get_value(
        "KoPOS Promotion Snapshot",
        {"pos_profile": pos_profile, "snapshot_hash": snapshot_hash},
        "name",
    )
    if not name:
        return None
    return frappe.get_doc("KoPOS Promotion Snapshot", name)


def get_snapshot_by_version_any_profile(snapshot_version: str):
    if not snapshot_version:
        return None
    name = frappe.db.get_value(
        "KoPOS Promotion Snapshot",
        {"snapshot_version": snapshot_version},
        "name",
    )
    if not name:
        return None
    return frappe.get_doc("KoPOS Promotion Snapshot", name)


def get_snapshot_by_hash_any_profile(snapshot_hash: str):
    if not snapshot_hash:
        return None
    name = frappe.db.get_value(
        "KoPOS Promotion Snapshot",
        {"snapshot_hash": snapshot_hash},
        "name",
    )
    if not name:
        return None
    return frappe.get_doc("KoPOS Promotion Snapshot", name)


def ensure_persisted_snapshot(
    pos_profile: str,
    at_time: Any | None = None,
    payload: dict[str, Any] | None = None,
    atomic: bool = False,
) -> tuple[Any, bool]:
    resolved_payload = payload or build_snapshot_payload_for_persistence(
        pos_profile, at_time
    )
    existing = get_snapshot_by_hash(pos_profile, resolved_payload["snapshot_hash"])
    if existing:
        return existing, False
    try:
        snapshot = frappe.new_doc("KoPOS Promotion Snapshot")
        snapshot.snapshot_version = resolved_payload["snapshot_version"]
        snapshot.status = SNAPSHOT_STATUS_PUBLISHED
        snapshot.pos_profile = pos_profile
        snapshot.published_at = resolved_payload["published_at"]
        snapshot.effective_from = resolved_payload["effective_from"]
        snapshot.snapshot_hash = resolved_payload["snapshot_hash"]
        snapshot.promotion_count = len(resolved_payload["promotions"])
        snapshot.snapshot_payload = json.dumps(
            resolved_payload, sort_keys=True, separators=(",", ":")
        )
        snapshot.insert(ignore_permissions=True)
        add_audit_comment(
            snapshot,
            "KoPOS promotion snapshot auto-persisted for {0} with {1} promotions".format(
                pos_profile, snapshot.promotion_count
            ),
        )
        return snapshot, True
    except frappe.UniqueValidationError:
        if atomic:
            # The multi-outlet publication transaction must remain intact.
            # Frappe rolls back the whole request; a retry checks its receipt.
            raise
        if hasattr(frappe.db, "rollback"):
            frappe.db.rollback()
        existing = get_snapshot_by_hash(pos_profile, resolved_payload["snapshot_hash"])
        if existing:
            return existing, False
        raise


def get_promotion_snapshot_payload(
    pos_profile: str | None = None,
    current_version: str | None = None,
    device_id: str | None = None,
    supported_kernel_hash: str | None = None,
) -> dict[str, Any] | None:
    profile_name = resolve_snapshot_pos_profile(pos_profile, device_id=device_id)
    latest = get_latest_published_snapshot(profile_name)

    if latest:
        payload = frappe.parse_json(latest.snapshot_payload or "{}")
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("schema_version") == 2 and supported_kernel_hash != payload.get("kernel_hash"):
            frappe.throw(_("This tablet needs a pricing update before it can receive these offers."), frappe.ValidationError)
        payload.update(
            {
                "snapshot_version": latest.snapshot_version,
                "snapshot_hash": latest.snapshot_hash,
                "published_at": latest.published_at,
                "effective_from": latest.effective_from,
                "pos_profile": latest.pos_profile,
                "source": "published",
                "is_current": cstr(current_version) == cstr(latest.snapshot_version),
            }
        )
        return payload

    # Promotion snapshots are the immutable authority consumed by POS.  A
    # live calculation here would let an unpublished or stale promotion leak
    # into checkout, so callers must handle the explicit unavailable state.
    return None


@frappe.whitelist(methods=["POST"])
def report_device_promotion_state(device_id, kernel_hash, snapshot_hash=None, snapshot_version=None):
    from kopos_connector.api.devices import require_device_context, lock_device_for_operational_mutation
    from kopos_connector.pricing.adapter import runtime_hash

    device = require_device_context(device_id=device_id)
    if device is None or not cint(device.enabled):
        frappe.throw(_("Choose an enabled tablet."), frappe.ValidationError)
    if kernel_hash != runtime_hash():
        frappe.throw(_("This tablet reports a different pricing version. Update ERP and the tablet together."), frappe.ValidationError)
    if bool(snapshot_hash) != bool(snapshot_version):
        frappe.throw(_("The exact saved offer version and hash are both required."), frappe.ValidationError)
    desired = {"promotion_kernel_hash": kernel_hash}
    status = "capable"
    if snapshot_hash:
        latest = get_latest_published_snapshot(cstr(device.pos_profile))
        if not latest or latest.snapshot_hash != snapshot_hash or latest.snapshot_version != snapshot_version:
            return {"status": "not_current"}
        desired.update({"promotion_snapshot_hash": snapshot_hash, "promotion_snapshot_version": snapshot_version})
        status = "received"
    if any(cstr(getattr(device, field, None)) != value for field, value in desired.items()):
        device = lock_device_for_operational_mutation(device_id=device_id)
        if snapshot_hash:
            # Recheck after waiting for the device lock. A later publication is
            # still safe: the desk compares this exact hash with its latest one.
            latest = get_latest_published_snapshot(cstr(device.pos_profile))
            if not latest or latest.snapshot_hash != snapshot_hash or latest.snapshot_version != snapshot_version:
                return {"status": "not_current"}
        if any(cstr(getattr(device, field, None)) != value for field, value in desired.items()):
            if snapshot_hash:
                desired["promotion_received_at"] = now_datetime()
            frappe.db.set_value("KoPOS Device", device.name, desired, update_modified=False)
    return {"status": status, "snapshot_hash": snapshot_hash, "kernel_hash": kernel_hash}


def publish_promotion_snapshot(
    pos_profile: str | None = None, device_id: str | None = None,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    require_company_director_for_promotion_publication()
    profile_name = resolve_snapshot_pos_profile(pos_profile, device_id=device_id)
    _require_director_profile_scope(profile_name)
    lock_promotion_profiles([profile_name])
    latest = get_latest_published_snapshot(profile_name)
    if latest and (frappe.parse_json(latest.snapshot_payload or "{}").get("schema_version") == 2):
        frappe.throw(_("Use Offers to review, activate or stop an offer. The legacy publisher cannot replace guided offers."))
    payload = build_snapshot_payload_for_persistence(profile_name)
    if expected_hash is not None and expected_hash != payload["snapshot_hash"]:
        frappe.throw(
            _("These offers changed after your review. Review the outlet's offers again before publishing."),
            frappe.ValidationError,
        )
    result = persist_reviewed_snapshot(profile_name, payload)
    if hasattr(frappe.db, "commit"):
        frappe.db.commit()
    return result


def lock_promotion_profiles(profiles):
    for profile in sorted(set(profiles)):
        frappe.db.sql("SELECT name FROM `tabPOS Profile` WHERE name = %s FOR UPDATE", (profile,))


def _offer_affected_profiles(promotion):
    """Include previously published outlets even after a draft removes one."""
    from kopos_connector.api.promotion_setup import _document_scope, _check_outlets

    profiles = set(_document_scope(promotion))
    snapshots = frappe.get_all("KoPOS Promotion Snapshot", filters={"status": SNAPSHOT_STATUS_PUBLISHED},
                               fields=["pos_profile", "snapshot_payload"], limit_page_length=501)
    if len(snapshots) > 500:
        frappe.throw(_("This site has too many outlet snapshots for a single offer review."))
    for snapshot in snapshots:
        body = frappe.parse_json(snapshot.snapshot_payload or "{}")
        if any(row.get("promotion_id") == promotion.name for row in body.get("promotions", [])):
            profiles.add(cstr(snapshot.pos_profile))
    _check_outlets(sorted(profiles))
    return sorted(profiles)


def _guided_rollout_issues(profile):
    """Admission is artifact-bound, not a checkbox a manager can bypass."""
    manifest = json.loads(files("kopos_connector.pricing").joinpath("runtime.json").read_text())
    if manifest.get("status") != "checkout_admitted":
        return [{"title": _("Pricing rollout is not finished"),
                 "message": _("Drafts and sample receipts are available. Activation stays off until this pricing version has passed its checkout checks."),
                 "route": [], "action": ""}]
    meta = frappe.get_meta("KoPOS Device")
    if not meta.has_field("promotion_kernel_hash"):
        return [{"title": _("Tablet update needed"), "message": _("The tablet delivery checks have not been installed yet."), "route": ["jiji_staff"]}]
    devices = frappe.get_all("KoPOS Device", filters={"enabled": 1, "pos_profile": profile},
                             fields=["device_name", "promotion_kernel_hash"], limit_page_length=501)
    if not devices or len(devices) > 500 or any(row.promotion_kernel_hash != manifest["bundleSha256"] for row in devices):
        return [{"title": _("Update the tills at {0}").format(profile),
                 "message": _("Every enabled till must confirm it understands this pricing version before this offer can be published."),
                 "route": ["jiji_staff"], "action": _("Check tablets")}]
    return []


def _review_offer_change(promotion, *, action, profiles):
    from kopos_connector.api.inventory import review_promotion_economics
    from kopos_connector.api.promotion_setup import _menu_for_profile, _editable_values, _check_items
    from kopos_connector.pricing.adapter import snapshot_body, time_iso, runtime_hash
    from kopos_connector.pricing.runtime import calculate

    if action not in {"activate", "stop"}:
        frappe.throw(_("Choose Activate or Stop."))
    values = _editable_values(promotion)
    if action == "activate" and (values is None or cint(getattr(promotion, "pricing_version", 1) or 1) != 2):
        frappe.throw(_("Save this offer in the guided editor before reviewing activation."))
    if action == "activate":
        _check_items(values)
    selected = set(values["outlets"]) if values else set()
    issues, targets, economics = [], {}, {}
    for profile in profiles:
        latest = get_latest_published_snapshot(profile)
        previous = frappe.parse_json(latest.snapshot_payload) if latest else {"pos_profile": profile, "promotions": []}
        rows = [dict(row) for row in previous.get("promotions", []) if row["promotion_id"] != promotion.name]
        if action == "activate" and profile in selected:
            if previous.get("schema_version") == 2 and previous.get("kernel_hash") != runtime_hash():
                frappe.throw(_("{0} has offers from a different pricing release. Ask support to review the upgrade; its published prices have not changed.").format(profile))
            if previous.get("schema_version", 1) != 2 and rows:
                issues.append({"title": _("Review older offers at {0}").format(profile),
                               "message": _("Older offers cannot be silently moved to best-price calculation. Stop and replace them in a planned change before activating this offer. Until then their published prices stay unchanged."),
                               "route": ["List", "KoPOS Promotion"], "action": _("Review advanced offers")})
            issues.extend(_guided_rollout_issues(profile))
            economics[profile] = review_promotion_economics(promotion, profile)
            if not economics[profile]["ready"]:
                issues.append({"title": _("Check costs at {0}").format(profile),
                               "message": economics[profile]["reason"] + ". " + _("Correct the recipe or price, or ask a second director to approve an exception."),
                               "route": ["jiji_menu_recipes"], "action": _("Check menu & recipes")})
            rows.append(serialize_promotion(promotion, profile, kernel_version=2))
            for row in rows:
                row.setdefault("schedule", None)
                row["valid_from"] = time_iso(row["valid_from"])
                row["valid_upto"] = time_iso(row["valid_upto"])
            menu = _menu_for_profile(profile)
            frozen = {row["item_code"]: row for row in previous.get("frozen_items", [])}
            # Keep group membership of previously published items immutable.
            for item in menu:
                frozen.setdefault(item["id"], {"item_code": item["id"], "item_group": item["group"]})
            body = snapshot_body(profile, rows, list(frozen.values()))
        elif previous.get("schema_version") == 2:
            body = {key: previous[key] for key in ("schema_version", "kernel_version", "kernel_hash", "pos_profile", "frozen_items")}
            body["promotions"] = rows
        else:
            body = {"pos_profile": profile, "promotions": rows}
        snapshot_hash = calculate("identity", body)["hash"] if body.get("schema_version") == 2 else compute_snapshot_content_hash(body)
        targets[profile] = {"body": body, "hash": snapshot_hash, "base_hash": cstr(latest.snapshot_hash) if latest else None}
    # Content-addressed snapshots may recur after Activate -> Stop -> Activate.
    # Bind new intent to the append-only receipt count, not wall-clock order.
    # Old retries return their original receipt before reaching this review.
    publication_count = frappe.db.count("KoPOS Promotion Publication", {"promotion": promotion.name})
    review = {"action": action, "promotion": promotion.name, "revision": cstr(promotion.modified),
              "publication_sequence": publication_count,
              "targets": {profile: {"base": value["base_hash"], "next": value["hash"]} for profile, value in targets.items()},
              "economics": economics}
    return {"expected_hash": calculate("identity", review)["hash"], "issues": issues,
            "targets": targets, "economics": economics, "revision": cstr(promotion.modified), "evidence": review}


def review_offer_change(name, *, action, revision=None):
    from kopos_connector.api.promotion_setup import _read_editor_document

    require_company_director_for_promotion_publication()
    promotion = _read_editor_document(name)
    if action == "activate" and cstr(promotion.modified) != cstr(revision):
        frappe.throw(_("This draft has changed. Save and review it again before activating."))
    review = _review_offer_change(promotion, action=action, profiles=_offer_affected_profiles(promotion))
    return {key: review[key] for key in ("expected_hash", "issues", "revision")}


def publish_reviewed_offer_change(name, expected_hash, *, action, revision=None):
    from kopos_connector.api.promotion_setup import _read_editor_document
    from kopos_connector.api.inventory import _record_promotion_economics_check

    require_company_director_for_promotion_publication()
    if not isinstance(expected_hash, str) or len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
        frappe.throw(_("Review this offer before publishing a change."))
    # Offer, then outlet locks: every multi-outlet change uses this order.
    frappe.db.sql("SELECT name FROM `tabKoPOS Promotion` WHERE name = %s FOR UPDATE", (name,))
    promotion = _read_editor_document(name)
    if frappe.db.exists("KoPOS Promotion Publication", expected_hash):
        receipt = frappe.get_doc("KoPOS Promotion Publication", expected_hash)
        if receipt.promotion != name or receipt.action != action:
            frappe.throw(_("This review receipt belongs to a different action."))
        results = frappe.parse_json(receipt.outlet_results)
        for result in results:
            _require_director_profile_scope(result["pos_profile"])
        return {"status": "already_published", "outlets": results}
    profiles = _offer_affected_profiles(promotion)
    lock_promotion_profiles(profiles)
    if action == "activate" and cstr(promotion.modified) != cstr(revision):
        frappe.throw(_("This draft changed after review. Reopen and review the new version."))
    review = _review_offer_change(promotion, action=action, profiles=profiles)
    if review["expected_hash"] != expected_hash:
        frappe.throw(_("The offer, prices, costs or published offers changed after review. Review again; nothing has been sent."))
    if review["issues"]:
        frappe.throw(_("Resolve the checks shown on Review before activating this offer."))
    for economics in review["economics"].values():
        if not economics["override"]:
            _record_promotion_economics_check(promotion, source_hash=economics["source_hash"], status="Ready", reason="", commit=False)
    results = []
    for profile, target in review["targets"].items():
        payload = {**target["body"], "snapshot_hash": target["hash"],
                   "snapshot_version": build_snapshot_version_from_hash(target["hash"]),
                   "effective_from": now_datetime().isoformat(), "published_at": now_datetime().isoformat()}
        results.append(persist_reviewed_snapshot(profile, payload, atomic=True))
    if action == "stop" and cint(promotion.is_active):
        # Otherwise an old outlet-wide publisher could reintroduce a stopped
        # legacy offer from its still-active source document.
        promotion.is_active = 0
        promotion.save(ignore_permissions=True)
    frappe.get_doc({"doctype": "KoPOS Promotion Publication", "request_hash": expected_hash,
                    "promotion": name, "action": action, "reviewed_revision": review["revision"],
                    "review_evidence": json.dumps(review["evidence"], separators=(",", ":")),
                    "outlet_results": json.dumps(results, separators=(",", ":"))}).insert(ignore_permissions=True)
    # Frappe's POST transaction commits the document evidence, all snapshots and
    # the immutable receipt together. A failure in any outlet rolls everything back.
    return {"status": "published", "outlets": results}


def persist_reviewed_snapshot(profile_name, payload, *, atomic=False):
    """One persistence owner. Caller owns profile locks and transaction commit."""
    latest = get_latest_published_snapshot(profile_name)
    if latest and cstr(latest.snapshot_hash) == cstr(payload["snapshot_hash"]):
        return {
            "status": "unchanged",
            "snapshot_version": latest.snapshot_version,
            "snapshot_hash": latest.snapshot_hash,
            "pos_profile": latest.pos_profile,
            "promotion_count": latest.promotion_count,
        }
    snapshot, created = ensure_persisted_snapshot(profile_name, payload=payload, atomic=atomic)
    if latest and cstr(latest.name) != cstr(getattr(snapshot, "name", None)):
        latest.status = SNAPSHOT_STATUS_SUPERSEDED
        latest.save(ignore_permissions=True)
    if cstr(getattr(snapshot, "status", None)) != SNAPSHOT_STATUS_PUBLISHED:
        snapshot.status = SNAPSHOT_STATUS_PUBLISHED
        snapshot.save(ignore_permissions=True)
    if created:
        return {
            "status": "published",
            "snapshot_version": snapshot.snapshot_version,
            "snapshot_hash": snapshot.snapshot_hash,
            "pos_profile": snapshot.pos_profile,
            "promotion_count": snapshot.promotion_count,
        }
    return {
        "status": "unchanged",
        "snapshot_version": snapshot.snapshot_version,
        "snapshot_hash": snapshot.snapshot_hash,
        "pos_profile": snapshot.pos_profile,
        "promotion_count": snapshot.promotion_count,
    }


def require_company_director_for_promotion_publication() -> None:
    roles = get_session_roles()
    if not roles.intersection({"Company Director", "System Manager"}):
        frappe.throw(
            _("Promotion publication requires Company Director permission"),
            frappe.PermissionError,
        )


def _require_director_profile_scope(pos_profile: str) -> None:
    from kopos_connector.api.director_scope import (
        director_pos_profiles,
        resolve_director_context,
    )

    profile = frappe.db.get_value(
        "POS Profile",
        cstr(pos_profile).strip(),
        ["company", "warehouse"],
        as_dict=True,
    )
    if not profile:
        frappe.throw(_("Choose an outlet that exists"), frappe.ValidationError)
    context = resolve_director_context(
        company=cstr(profile.get("company")).strip(),
        warehouse=cstr(profile.get("warehouse")).strip(),
    )
    if cstr(pos_profile).strip() not in director_pos_profiles(context):
        frappe.throw(_("You do not have access to this outlet."), frappe.PermissionError)


def reconcile_promotion_payload(
    pos_profile: str, payload: dict[str, Any]
) -> dict[str, Any]:
    pricing_context = payload.get("pricing_context") or {}
    applied_promotions = payload.get("applied_promotions") or []
    if not applied_promotions:
        return build_reconciliation_result(
            "not_applicable", "No applied promotions present"
        )

    snapshot_version = cstr(pricing_context.get("snapshot_version"))
    snapshot_hash = cstr(pricing_context.get("snapshot_hash"))
    snapshot = None
    if snapshot_version:
        snapshot = get_snapshot_by_version(pos_profile, snapshot_version)
    if not snapshot and snapshot_hash:
        snapshot = get_snapshot_by_hash(pos_profile, snapshot_hash)

    if not snapshot:
        snapshot_any_profile = None
        if snapshot_version:
            snapshot_any_profile = get_snapshot_by_version_any_profile(snapshot_version)
        if not snapshot_any_profile and snapshot_hash:
            snapshot_any_profile = get_snapshot_by_hash_any_profile(snapshot_hash)
        if snapshot_any_profile:
            return build_reconciliation_result(
                "review_required", "Promotion snapshot profile mismatch"
            )
        if not snapshot_version and not snapshot_hash:
            return build_reconciliation_result(
                "review_required", "Applied promotions missing snapshot version"
            )
        return build_reconciliation_result(
            "review_required", "Referenced promotion snapshot was not found"
        )

    if snapshot_hash and cstr(snapshot.snapshot_hash) != snapshot_hash:
        return build_reconciliation_result(
            "review_required", "Promotion snapshot hash mismatch"
        )

    snapshot_payload = frappe.parse_json(snapshot.snapshot_payload or "{}")
    snapshot_promotions = {
        cstr(promotion.get("promotion_id"))
        for promotion in (snapshot_payload.get("promotions") or [])
        if isinstance(promotion, dict)
    }
    applied_ids = {
        cstr(promotion.get("promotion_id"))
        for promotion in applied_promotions
        if isinstance(promotion, dict)
    }
    if not applied_ids.issubset(snapshot_promotions):
        return build_reconciliation_result(
            "review_required",
            "Applied promotion ids do not match published snapshot",
        )

    applied_total_sen = 0
    for promotion in applied_promotions:
        promotion_amount_sen = amount_to_sen(promotion.get("amount"))
        if promotion_amount_sen is None:
            return build_reconciliation_result(
                "review_required", "Applied promotion amount evidence is invalid"
            )
        applied_total_sen += promotion_amount_sen
    line_total_sen = 0
    for item in (payload.get("order") or {}).get("items", []):
        if not isinstance(item, dict):
            continue
        for allocation in item.get("promotion_allocations") or []:
            if isinstance(allocation, dict):
                allocation_amount_sen = amount_to_sen(allocation.get("amount"))
                if allocation_amount_sen is None:
                    return build_reconciliation_result(
                        "review_required",
                        "Applied promotion amount evidence is invalid",
                    )
                line_total_sen += allocation_amount_sen

    if applied_total_sen != line_total_sen:
        return build_reconciliation_result(
            "review_required",
            "Applied promotion total does not match line allocations",
        )

    return build_reconciliation_result(
        "matched",
        "Promotion payload matched published snapshot",
        snapshot_version=snapshot.snapshot_version,
        snapshot_hash=snapshot.snapshot_hash,
    )


def get_promotion_review_queue(limit: int = 20) -> list[dict[str, Any]]:
    require_system_manager()
    filters: dict[str, Any] = {
        "custom_kopos_promotion_reconciliation_status": "review_required"
    }
    fields = [
        "name",
        "posting_date",
        "customer",
        "custom_kopos_pricing_mode",
        "custom_kopos_promotion_snapshot_version",
        "custom_kopos_promotion_payload",
    ]
    has_review_status_field = frappe.db.has_column(
        "POS Invoice", "custom_kopos_promotion_review_status"
    )
    if has_review_status_field:
        fields.append("custom_kopos_promotion_review_status")
    if frappe.db.has_column("POS Invoice", "custom_kopos_promotion_review_decision"):
        fields.append("custom_kopos_promotion_review_decision")

    invoices = frappe.get_all(
        "POS Invoice",
        filters=filters,
        fields=fields,
        order_by="modified desc",
        limit=limit,
    )
    out = []
    for invoice in invoices:
        review_status_value = cstr(invoice.get("custom_kopos_promotion_review_status"))
        if has_review_status_field and review_status_value not in (
            "",
            REVIEW_STATUS_PENDING,
        ):
            continue
        payload = frappe.parse_json(
            invoice.get("custom_kopos_promotion_payload") or "{}"
        )
        reconciliation = (
            payload.get("reconciliation") if isinstance(payload, dict) else {}
        )
        severity = None
        review_route = None
        if isinstance(reconciliation, dict):
            severity = reconciliation.get(
                "severity"
            ) or classify_reconciliation_severity(
                cstr(reconciliation.get("status") or "review_required"),
                cstr(reconciliation.get("message")),
            )
            review_route = reconciliation.get("review_route") or (
                "manager_review" if severity == "major" else "ops_review"
            )
        out.append(
            {
                "pos_invoice": invoice.get("name"),
                "posting_date": invoice.get("posting_date"),
                "customer": invoice.get("customer"),
                "pricing_mode": invoice.get("custom_kopos_pricing_mode"),
                "snapshot_version": invoice.get(
                    "custom_kopos_promotion_snapshot_version"
                ),
                "severity": severity,
                "review_route": review_route,
                "review_status": review_status_value or REVIEW_STATUS_PENDING,
                "review_decision": invoice.get(
                    "custom_kopos_promotion_review_decision"
                ),
                "review_reason": reconciliation.get("message")
                if isinstance(reconciliation, dict)
                else None,
            }
        )
    return out


def review_promotion_reconciliation(
    pos_invoice: str,
    decision: str,
    notes: str | None = None,
    reviewed_by: str | None = None,
) -> dict[str, Any]:
    require_system_manager()
    decision_value = cstr(decision)
    if decision_value not in REVIEW_DECISIONS:
        frappe.throw(
            _("decision must be one of: {0}").format(
                ", ".join(sorted(REVIEW_DECISIONS))
            ),
            frappe.ValidationError,
        )

    if not frappe.db.exists("POS Invoice", pos_invoice):
        frappe.throw(
            _("POS Invoice {0} was not found").format(pos_invoice),
            frappe.ValidationError,
        )

    invoice = frappe.get_doc("POS Invoice", pos_invoice)
    if cstr(getattr(invoice, "custom_kopos_promotion_reconciliation_status", None)) != (
        "review_required"
    ):
        frappe.throw(
            _("POS Invoice {0} does not require promotion review").format(pos_invoice),
            frappe.ValidationError,
        )

    current_review_status = cstr(
        getattr(invoice, "custom_kopos_promotion_review_status", None)
        or REVIEW_STATUS_PENDING
    )
    if current_review_status != REVIEW_STATUS_PENDING:
        frappe.throw(
            _("POS Invoice {0} has already been reviewed").format(pos_invoice),
            frappe.ValidationError,
        )

    metadata = get_invoice_promotion_metadata(invoice)
    reconciliation = metadata.get("reconciliation") or {}
    resolved_by = cstr(reviewed_by) or cstr(getattr(frappe.session, "user", None))
    resolved_at = now_datetime().isoformat()
    review_payload = {
        "decision": decision_value,
        "notes": cstr(notes),
        "reviewed_by": resolved_by,
        "reviewed_at": resolved_at,
        "status": "completed",
    }
    metadata["review"] = review_payload
    append_promotion_audit_event(
        metadata,
        "review.completed",
        {
            "decision": decision_value,
            "notes": cstr(notes),
            "reviewed_by": resolved_by,
            "severity": cstr(reconciliation.get("severity")),
            "review_route": cstr(reconciliation.get("review_route")),
        },
        event_at=resolved_at,
        actor=resolved_by,
    )

    update_fields = {}
    if hasattr(invoice, "custom_kopos_promotion_payload"):
        update_fields["custom_kopos_promotion_payload"] = serialize_json_compact(
            metadata
        )
    if hasattr(invoice, "custom_kopos_promotion_review_status"):
        update_fields["custom_kopos_promotion_review_status"] = decision_value
    if hasattr(invoice, "custom_kopos_promotion_review_decision"):
        update_fields["custom_kopos_promotion_review_decision"] = decision_value
    if hasattr(invoice, "custom_kopos_promotion_reviewed_by"):
        update_fields["custom_kopos_promotion_reviewed_by"] = resolved_by
    if hasattr(invoice, "custom_kopos_promotion_reviewed_at"):
        update_fields["custom_kopos_promotion_reviewed_at"] = resolved_at
    if hasattr(invoice, "custom_kopos_promotion_review_notes"):
        update_fields["custom_kopos_promotion_review_notes"] = cstr(notes)
    if update_fields:
        frappe.db.set_value(
            "POS Invoice", invoice.name, update_fields, update_modified=True
        )
        for key, value in update_fields.items():
            setattr(invoice, key, value)
    add_audit_comment(
        invoice,
        "KoPOS promotion review {0} by {1}: {2}".format(
            decision_value,
            resolved_by or "Unknown",
            cstr(notes) or "No notes provided",
        ),
    )
    return {
        "status": "ok",
        "pos_invoice": invoice.name,
        "decision": decision_value,
        "review_status": getattr(
            invoice, "custom_kopos_promotion_review_status", decision_value
        ),
        "reviewed_by": resolved_by,
        "reviewed_at": resolved_at,
    }


def repair_promotion_reconciliation_invoices(
    limit: int | None = None,
) -> dict[str, Any]:
    require_system_manager()
    filters: dict[str, Any] = {
        "custom_kopos_promotion_reconciliation_status": "review_required"
    }
    if frappe.db.has_column("POS Invoice", "custom_kopos_promotion_review_status"):
        filters["custom_kopos_promotion_review_status"] = [
            "in",
            ["", REVIEW_STATUS_PENDING],
        ]
    invoices = frappe.get_all(
        "POS Invoice",
        filters=filters,
        fields=[
            "name",
            "pos_profile",
            "custom_kopos_promotion_payload",
            "custom_kopos_promotion_review_status",
        ],
        order_by="modified asc",
        limit_page_length=limit,
    )
    scanned = 0
    repaired = 0
    still_pending = 0
    for invoice_row in invoices:
        scanned += 1
        invoice_name = cstr(invoice_row.get("name"))
        try:
            pos_profile = cstr(invoice_row.get("pos_profile"))
            review_status = cstr(
                invoice_row.get("custom_kopos_promotion_review_status")
            )
            if review_status not in ("", REVIEW_STATUS_PENDING):
                still_pending += 1
                continue
            payload_str = invoice_row.get("custom_kopos_promotion_payload")
            if not payload_str:
                still_pending += 1
                continue
            payload = frappe.parse_json(payload_str)
            if not isinstance(payload, dict):
                still_pending += 1
                continue
            pricing_context = payload.get("pricing_context") or {}
            snapshot_version = cstr(pricing_context.get("snapshot_version"))
            snapshot_hash = cstr(pricing_context.get("snapshot_hash"))
            applied_promotions = payload.get("applied_promotions") or []
            if not applied_promotions or (not snapshot_version and not snapshot_hash):
                still_pending += 1
                continue
            snapshot = None
            repair_path = "version"
            if snapshot_version:
                snapshot = get_snapshot_by_version(pos_profile, snapshot_version)
            if not snapshot and snapshot_hash:
                snapshot = get_snapshot_by_hash(pos_profile, snapshot_hash)
                if snapshot:
                    repair_path = "hash"
            if not snapshot:
                snapshot_any = None
                if snapshot_version:
                    snapshot_any = get_snapshot_by_version_any_profile(snapshot_version)
                if not snapshot_any and snapshot_hash:
                    snapshot_any = get_snapshot_by_hash_any_profile(snapshot_hash)
                if snapshot_any:
                    still_pending += 1
                    continue
                still_pending += 1
                continue
            new_reconciliation = reconcile_promotion_payload(pos_profile, payload)
            if new_reconciliation.get("status") != "matched":
                still_pending += 1
                continue
            payload["reconciliation"] = new_reconciliation
            append_promotion_audit_event(
                payload,
                "repair.matched",
                {
                    "message": "Legacy invoice repaired via persisted snapshot evidence",
                    "repair_path": repair_path,
                    "snapshot_version": new_reconciliation.get("snapshot_version"),
                    "snapshot_hash": new_reconciliation.get("snapshot_hash"),
                },
            )
            invoice_doc = frappe.get_doc("POS Invoice", invoice_name)
            update_fields = {}
            if hasattr(invoice_doc, "custom_kopos_promotion_payload"):
                update_fields["custom_kopos_promotion_payload"] = (
                    serialize_json_compact(payload)
                )
            if hasattr(invoice_doc, "custom_kopos_promotion_reconciliation_status"):
                update_fields["custom_kopos_promotion_reconciliation_status"] = (
                    "matched"
                )
            if hasattr(invoice_doc, "custom_kopos_promotion_review_status"):
                update_fields["custom_kopos_promotion_review_status"] = (
                    REVIEW_STATUS_NOT_REQUIRED
                )
            if hasattr(invoice_doc, "custom_kopos_promotion_snapshot_version"):
                update_fields["custom_kopos_promotion_snapshot_version"] = cstr(
                    new_reconciliation.get("snapshot_version")
                )
            if update_fields:
                frappe.db.set_value(
                    "POS Invoice", invoice_name, update_fields, update_modified=True
                )
            add_audit_comment(
                invoice_doc,
                "KoPOS promotion reconciliation repaired automatically via {0} evidence".format(
                    repair_path
                ),
            )
            repaired += 1
        except Exception:
            if hasattr(frappe, "log_error"):
                frappe.log_error(
                    frappe.get_traceback(),
                    "KoPOS promotion reconciliation repair failed for {0}".format(
                        invoice_name or "unknown invoice"
                    ),
                )
            still_pending += 1
    return {
        "scanned": scanned,
        "repaired": repaired,
        "still_pending": still_pending,
    }


def build_snapshot_payload(pos_profile: str) -> dict[str, Any]:
    return build_snapshot_payload_for_persistence(pos_profile)


def get_active_promotions(pos_profile: str, at_time=None, *, include_scheduled: bool = False) -> list[Any]:
    at_time = get_datetime(at_time or now_datetime())
    promotions = frappe.get_all(
        "KoPOS Promotion",
        filters={"is_active": 1, "offline_allowed": 1},
        fields=["name"],
        order_by="priority asc, modified asc, name asc",
    )
    out = []
    for row in promotions:
        doc = frappe.get_doc("KoPOS Promotion", row.name)
        if not promotion_is_active(doc, pos_profile, at_time, include_scheduled=include_scheduled):
            continue
        out.append(doc)
    return out


def promotion_is_active(doc, pos_profile: str, at_time, *, include_scheduled: bool = False) -> bool:
    if cstr(doc.outlet_scope_mode or "all_pos_profiles") == "selected_pos_profiles":
        allowed_profiles = {
            cstr(row.pos_profile)
            for row in (doc.eligible_pos_profiles or [])
            if cstr(row.pos_profile)
        }
        if pos_profile not in allowed_profiles:
            return False

    if not include_scheduled and doc.valid_from and get_datetime(doc.valid_from) > at_time:
        return False
    if doc.valid_upto and get_datetime(doc.valid_upto) < at_time:
        return False
    return True


def serialize_promotion(doc, pos_profile: str, *, kernel_version: int = 1) -> dict[str, Any]:
    if cint(getattr(doc, "pricing_version", 1) or 1) != 1 and kernel_version != 2:
        raise ValueError("This guided offer requires the reviewed, version-2 publication flow")
    display_label = cstr(doc.display_label or doc.promotion_name)
    guided = kernel_version == 2 and cint(getattr(doc, "pricing_version", 1) or 1) == 2
    payload = {
        "promotion_id": doc.name,
        "promotion_name": display_label if guided else doc.promotion_name,
        "display_label": display_label,
        "customer_message": cstr(doc.customer_message or ""),
        "promotion_type": doc.promotion_type,
        "activation_mode": cstr(doc.activation_mode or "automatic"),
        "offline_allowed": bool(doc.offline_allowed),
        "priority": cint(doc.priority),
        "stacking_policy": cstr(doc.stacking_policy or "exclusive"),
        "discount_target": cstr(doc.discount_target or "cheaper_eligible"),
        "discount_type": cstr(doc.discount_type or "percentage"),
        "discount_value": flt(doc.discount_value),
        "buy_qty": cint(doc.buy_qty or 0),
        "discount_qty": cint(doc.discount_qty or 0),
        "repeat_mode": cstr(doc.repeat_mode or "once"),
        "eligible_scope_mode": cstr(doc.eligible_scope_mode or "eligible_pool"),
        "comparison_basis": cstr(doc.comparison_basis or "base_item_only"),
        "discount_basis": cstr(doc.discount_basis or "base_item_only"),
        "modifier_policy": cstr(doc.modifier_policy or "excluded_by_default"),
        # MariaDB reloads Datetime fields as datetime objects. Use Frappe's
        # wire representation before hashing; preserve legacy strings and nulls.
        "valid_from": cstr(doc.valid_from) if doc.valid_from is not None else None,
        "valid_upto": cstr(doc.valid_upto) if doc.valid_upto is not None else None,
        "pos_profile": pos_profile,
        "eligible_items": sort_string_values(
            [
                cstr(row.item_code)
                for row in (doc.eligible_items or [])
                if cstr(row.item_code)
            ]
        ),
        "min_qty": cint(doc.min_qty or 0),
        "min_amount": flt(doc.min_amount or 0),
        "eligible_item_groups": sort_string_values(
            [
                cstr(row.item_group)
                for row in (doc.eligible_item_groups or [])
                if cstr(row.item_group)
            ]
        ),
        "selected_pos_profiles": sort_string_values(
            [
                cstr(row.pos_profile)
                for row in (doc.eligible_pos_profiles or [])
                if cstr(row.pos_profile)
            ]
        ),
    }
    try:
        # JSON Schema's numeric model excludes NaN/Infinity; Python's default
        # JSON encoder does not. Reject them before structural validation.
        json.dumps(payload, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise ValueError(f"Promotion {doc.name} contains a non-JSON snapshot value") from error
    try:
        _canonical_promotion_validator().validate(payload)
    except SchemaValidationError as error:
        field = "/".join(str(part) for part in error.absolute_path) or "row"
        raise ValueError(
            f"Promotion {doc.name} has invalid snapshot field {field}: {error.message}"
        ) from error
    if kernel_version == 2:
        from kopos_connector.pricing.adapter import time_iso
        from kopos_connector.pricing.runtime import calculate

        schedule = frappe.parse_json(getattr(doc, "schedule_json", None) or "null")
        payload["schedule"] = calculate("schedule", schedule) if schedule is not None else None
        payload["valid_from"] = time_iso(doc.valid_from)
        payload["valid_upto"] = time_iso(doc.valid_upto)
    return payload


def resolve_snapshot_pos_profile(
    pos_profile: str | None, device_id: str | None = None
) -> str:
    if cstr(device_id).strip():
        device_doc = get_device_doc(device_id=device_id)
        return cstr(device_doc.pos_profile)

    profile_name = cstr(pos_profile)
    if profile_name:
        if not frappe.db.exists("POS Profile", profile_name):
            frappe.throw(
                _("POS Profile {0} was not found").format(profile_name),
                frappe.ValidationError,
            )
        return profile_name

    default_profile = get_default_pos_profile(None)
    if not default_profile:
        frappe.throw(_("No enabled POS Profile was found"), frappe.ValidationError)
    return cstr(default_profile["name"])


def get_latest_published_snapshot(pos_profile: str):
    names = frappe.get_all(
        "KoPOS Promotion Snapshot",
        filters={"pos_profile": pos_profile, "status": SNAPSHOT_STATUS_PUBLISHED},
        fields=["name"],
        order_by="published_at desc, creation desc",
        limit=1,
    )
    if not names:
        return None
    return frappe.get_doc("KoPOS Promotion Snapshot", names[0].name)


def get_snapshot_by_version(pos_profile: str, snapshot_version: str):
    name = frappe.db.get_value(
        "KoPOS Promotion Snapshot",
        {"pos_profile": pos_profile, "snapshot_version": snapshot_version},
        "name",
    )
    if not name:
        return None
    return frappe.get_doc("KoPOS Promotion Snapshot", name)


def build_snapshot_version(timestamp, snapshot_hash: str) -> str:
    return build_snapshot_version_from_hash(snapshot_hash)


def build_reconciliation_result(
    status: str,
    message: str,
    snapshot_version: str | None = None,
    snapshot_hash: str | None = None,
) -> dict[str, Any]:
    severity = classify_reconciliation_severity(status, message)
    review_route = "manager_review" if severity == "major" else "ops_review"
    result = {
        "status": status,
        "message": message,
        "severity": severity,
        "review_route": review_route if status == "review_required" else None,
    }
    if snapshot_version:
        result["snapshot_version"] = snapshot_version
    if snapshot_hash:
        result["snapshot_hash"] = snapshot_hash
    return result


def classify_reconciliation_severity(status: str, message: str | None) -> str:
    if status != "review_required":
        return "none"
    if cstr(message) in MAJOR_RECONCILIATION_ISSUES:
        return "major"
    if cstr(message) in MINOR_RECONCILIATION_ISSUES:
        return "minor"
    return "minor"


def derive_review_status(reconciliation_status: str) -> str:
    if cstr(reconciliation_status) == "review_required":
        return REVIEW_STATUS_PENDING
    return REVIEW_STATUS_NOT_REQUIRED


def get_invoice_promotion_metadata(invoice: Any) -> dict[str, Any]:
    payload = frappe.parse_json(
        getattr(invoice, "custom_kopos_promotion_payload", None) or "{}"
    )
    return payload if isinstance(payload, dict) else {}


def append_promotion_audit_event(
    metadata: dict[str, Any],
    event: str,
    details: dict[str, Any] | None = None,
    event_at: str | None = None,
    actor: str | None = None,
) -> None:
    audit_events = metadata.get("audit_events")
    if not isinstance(audit_events, list):
        audit_events = []
    audit_events.append(
        {
            "event": cstr(event),
            "at": cstr(event_at) or now_datetime().isoformat(),
            "actor": cstr(actor),
            "details": details or {},
        }
    )
    metadata["audit_events"] = audit_events


def add_audit_comment(doc: Any, message: str) -> None:
    if not cstr(message):
        return
    if hasattr(doc, "add_comment"):
        doc.add_comment("Comment", text=cstr(message))


def serialize_json_compact(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
