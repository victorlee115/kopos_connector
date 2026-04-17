from __future__ import annotations

from typing import Any

import frappe


def create_projection_log(
    source_doctype: str,
    source_name: str,
    projection_type: str,
    idempotency_key: str,
    payload_hash: str,
) -> str | None:
    if not source_doctype or not source_name or not projection_type:
        return None

    existing_log = _find_existing_projection(
        source_doctype=source_doctype,
        source_name=source_name,
        projection_type=projection_type,
        idempotency_key=idempotency_key,
    )
    if existing_log:
        return existing_log

    savepoint = _make_savepoint("fb_projection_log")

    try:
        log_doc = frappe.new_doc("FB Projection Log")
        log_doc.projection_id = frappe.generate_hash(length=16)
        log_doc.source_doctype = source_doctype
        log_doc.source_name = source_name
        log_doc.source_event_type = projection_type
        log_doc.projection_type = projection_type
        log_doc.idempotency_key = idempotency_key or None
        log_doc.payload_hash = payload_hash or None
        log_doc.state = "Pending"
        log_doc.created_at = frappe.utils.now()
        log_doc.last_attempt_at = None
        log_doc.insert(ignore_permissions=True)
        return log_doc.name
    except Exception:
        _rollback_savepoint(savepoint)
        duplicate_log = _find_existing_projection(
            source_doctype=source_doctype,
            source_name=source_name,
            projection_type=projection_type,
            idempotency_key=idempotency_key,
        )
        if duplicate_log:
            return duplicate_log
        _log_error("Projection log creation failed")
        return None


def update_projection_state(
    log_name: str,
    state: str,
    target_doctype: str | None = None,
    target_name: str | None = None,
    error: Any = None,
) -> str | None:
    if not log_name or not state:
        return None

    savepoint = _make_savepoint("fb_projection_update")

    try:
        log_doc = frappe.get_doc("FB Projection Log", log_name)
        log_doc.state = state
        log_doc.target_doctype = target_doctype or None
        log_doc.target_name = target_name or None
        log_doc.last_attempt_at = frappe.utils.now()
        log_doc.last_error = _stringify_error(error)
        if state == "Failed":
            log_doc.retry_count = int(log_doc.retry_count or 0) + 1
        log_doc.save(ignore_permissions=True)
        return log_doc.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Projection log update failed")
        return None


def get_pending_projections() -> list[dict[str, Any]]:
    try:
        return frappe.get_all(
            "FB Projection Log",
            filters={"state": "Pending"},
            fields=[
                "name",
                "projection_id",
                "source_doctype",
                "source_name",
                "projection_type",
                "idempotency_key",
                "payload_hash",
                "retry_count",
                "last_attempt_at",
            ],
            order_by="created_at asc",
        )
    except Exception:
        _log_error("Fetching pending projections failed")
        return []


def retry_failed_projections() -> list[dict[str, Any]]:
    try:
        failed_logs = frappe.get_all(
            "FB Projection Log",
            filters={"state": "Failed"},
            fields=["name"],
            order_by="modified asc",
        )
    except Exception:
        _log_error("Fetching failed projections failed")
        return []

    retried: list[dict[str, Any]] = []
    for row in failed_logs:
        updated_name = update_projection_state(
            row.get("name"), "Pending", None, None, None
        )
        if updated_name:
            retried.append({"name": updated_name})
    return retried


def _find_existing_projection(
    source_doctype: str,
    source_name: str,
    projection_type: str,
    idempotency_key: str,
) -> str | None:
    filters: dict[str, Any] = {
        "source_doctype": source_doctype,
        "source_name": source_name,
        "projection_type": projection_type,
    }
    if idempotency_key:
        filters["idempotency_key"] = idempotency_key

    existing = frappe.db.get_value("FB Projection Log", filters, "name")
    return str(existing) if existing else None


def _stringify_error(error: Any) -> str | None:
    if error in (None, ""):
        return None
    if isinstance(error, Exception):
        return str(error)
    return str(error)


def _make_savepoint(prefix: str) -> str:
    name = f"{prefix}_{frappe.generate_hash(length=8)}"
    try:
        frappe.db.savepoint(name)
    except Exception:
        return ""
    return name


def _rollback_savepoint(savepoint: str) -> None:
    try:
        if savepoint:
            frappe.db.rollback(save_point=savepoint)
        else:
            frappe.db.rollback()
    except Exception:
        pass


def _log_error(title: str) -> None:
    try:
        frappe.log_error(frappe.get_traceback(), title)
    except Exception:
        pass
