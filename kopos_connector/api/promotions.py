from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, get_datetime, now_datetime

from kopos_connector.api.catalog import get_default_pos_profile
from kopos_connector.api.devices import get_device_doc, require_system_manager

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
    "Referenced promotion snapshot was not found",
    "Promotion snapshot hash mismatch",
    "Applied promotion ids do not match published snapshot",
}


def get_promotion_snapshot_payload(
    pos_profile: str | None = None,
    current_version: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Return the latest published promotion snapshot for a POS profile."""
    profile_name = resolve_snapshot_pos_profile(pos_profile, device_id=device_id)
    latest = get_latest_published_snapshot(profile_name)

    if latest:
        payload = frappe.parse_json(latest.snapshot_payload or "{}")
        if not isinstance(payload, dict):
            payload = {}
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

    payload = build_snapshot_payload(profile_name)
    payload["source"] = "live"
    payload["is_current"] = cstr(current_version) == cstr(payload["snapshot_version"])
    return payload


def publish_promotion_snapshot(
    pos_profile: str | None = None, device_id: str | None = None
) -> dict[str, Any]:
    """Publish a deterministic promotion snapshot for a POS profile."""
    require_system_manager()
    profile_name = resolve_snapshot_pos_profile(pos_profile, device_id=device_id)
    payload = build_snapshot_payload(profile_name)
    latest = get_latest_published_snapshot(profile_name)
    if latest and cstr(latest.snapshot_hash) == cstr(payload["snapshot_hash"]):
        return {
            "status": "unchanged",
            "snapshot_version": latest.snapshot_version,
            "snapshot_hash": latest.snapshot_hash,
            "pos_profile": latest.pos_profile,
            "promotion_count": latest.promotion_count,
        }

    if latest:
        latest.status = SNAPSHOT_STATUS_SUPERSEDED
        latest.save(ignore_permissions=True)

    snapshot = frappe.new_doc("KoPOS Promotion Snapshot")
    snapshot.snapshot_version = payload["snapshot_version"]
    snapshot.status = SNAPSHOT_STATUS_PUBLISHED
    snapshot.pos_profile = profile_name
    snapshot.published_at = payload["published_at"]
    snapshot.effective_from = payload["effective_from"]
    snapshot.snapshot_hash = payload["snapshot_hash"]
    snapshot.promotion_count = len(payload["promotions"])
    snapshot.snapshot_payload = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    snapshot.insert(ignore_permissions=True)
    add_audit_comment(
        snapshot,
        "KoPOS promotion snapshot published for {0} with {1} promotions".format(
            profile_name, snapshot.promotion_count
        ),
    )
    frappe.db.commit()

    return {
        "status": "published",
        "snapshot_version": snapshot.snapshot_version,
        "snapshot_hash": snapshot.snapshot_hash,
        "pos_profile": snapshot.pos_profile,
        "promotion_count": snapshot.promotion_count,
    }


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
    if not snapshot_version:
        return build_reconciliation_result(
            "review_required", "Applied promotions missing snapshot version"
        )

    snapshot = get_snapshot_by_version(pos_profile, snapshot_version)
    if not snapshot:
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

    applied_total = sum(
        flt(promotion.get("amount")) for promotion in applied_promotions
    )
    line_total = 0.0
    for item in (payload.get("order") or {}).get("items", []):
        if not isinstance(item, dict):
            continue
        for allocation in item.get("promotion_allocations") or []:
            if isinstance(allocation, dict):
                line_total += flt(allocation.get("amount"))

    if abs(applied_total - line_total) > 0.01:
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
    frappe.db.commit()

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


def build_snapshot_payload(pos_profile: str) -> dict[str, Any]:
    generated_at = now_datetime()
    effective_from = generated_at.isoformat()
    promotions = get_active_promotions(pos_profile, generated_at)
    normalized = [serialize_promotion(doc, pos_profile) for doc in promotions]
    body = {
        "pos_profile": pos_profile,
        "effective_from": effective_from,
        "promotions": normalized,
    }
    snapshot_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **body,
        "published_at": generated_at.isoformat(),
        "snapshot_hash": snapshot_hash,
        "snapshot_version": build_snapshot_version(generated_at, snapshot_hash),
    }


def get_active_promotions(pos_profile: str, at_time=None) -> list[Any]:
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
        if not promotion_is_active(doc, pos_profile, at_time):
            continue
        out.append(doc)
    return out


def promotion_is_active(doc, pos_profile: str, at_time) -> bool:
    if cstr(doc.outlet_scope_mode or "all_pos_profiles") == "selected_pos_profiles":
        allowed_profiles = {
            cstr(row.pos_profile)
            for row in (doc.eligible_pos_profiles or [])
            if cstr(row.pos_profile)
        }
        if pos_profile not in allowed_profiles:
            return False

    if doc.valid_from and get_datetime(doc.valid_from) > at_time:
        return False
    if doc.valid_upto and get_datetime(doc.valid_upto) < at_time:
        return False
    return True


def serialize_promotion(doc, pos_profile: str) -> dict[str, Any]:
    return {
        "promotion_id": doc.name,
        "promotion_name": doc.promotion_name,
        "display_label": cstr(doc.display_label or doc.promotion_name),
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
        "valid_from": doc.valid_from,
        "valid_upto": doc.valid_upto,
        "pos_profile": pos_profile,
        "eligible_items": [
            cstr(row.item_code)
            for row in (doc.eligible_items or [])
            if cstr(row.item_code)
        ],
        "min_qty": cint(doc.min_qty or 0),
        "min_amount": flt(doc.min_amount or 0),
        "eligible_item_groups": [
            cstr(row.item_group)
            for row in (doc.eligible_item_groups or [])
            if cstr(row.item_group)
        ],
        "selected_pos_profiles": [
            cstr(row.pos_profile)
            for row in (doc.eligible_pos_profiles or [])
            if cstr(row.pos_profile)
        ],
    }


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
    return "KOPOS-PROMO-{0}-{1}".format(
        timestamp.strftime("%Y%m%d%H%M%S"), snapshot_hash[:8].upper()
    )


def cint(value: Any) -> int:
    return int(frappe.utils.cint(value))


def flt(value: Any) -> float:
    return float(frappe.utils.flt(value))


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
