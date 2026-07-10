# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any

import frappe

from kopos_connector.utils.diagnostics import (
    log_sanitized_error,
    make_savepoint,
    rollback_to_savepoint,
)


class ProjectionLogError(RuntimeError):
    """Raised when projection log state cannot be persisted or read."""


def create_projection_log(
    source_doctype: str,
    source_name: str,
    projection_type: str,
    idempotency_key: str,
    payload_hash: str,
) -> str:
    if not source_doctype or not source_name or not projection_type:
        raise ValueError(
            "source_doctype, source_name, and projection_type are required to create a projection log"
        )

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
    except Exception as error:
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
        raise ProjectionLogError(
            f"Projection log creation failed for {source_doctype} {source_name} {projection_type}: {error}"
        ) from error


def update_projection_state(
    log_name: str,
    state: str,
    target_doctype: str | None = None,
    target_name: str | None = None,
    error: Any = None,
) -> str:
    if not log_name or not state:
        raise ValueError("log_name and state are required to update a projection log")

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
    except Exception as error:
        _rollback_savepoint(savepoint)
        _log_error("Projection log update failed")
        raise ProjectionLogError(
            f"Projection log update failed for {log_name} to {state}: {error}"
        ) from error


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
    except Exception as error:
        _log_error("Fetching pending projections failed")
        raise ProjectionLogError(
            f"Fetching pending projections failed: {error}"
        ) from error


def retry_failed_projections() -> list[dict[str, Any]]:
    try:
        failed_logs = frappe.get_all(
            "FB Projection Log",
            filters={"state": "Failed"},
            fields=["name"],
            order_by="modified asc",
        )
    except Exception as error:
        _log_error("Fetching failed projections failed")
        raise ProjectionLogError(
            f"Fetching failed projections failed: {error}"
        ) from error

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
    return make_savepoint(prefix)


def _rollback_savepoint(savepoint: str) -> None:
    rollback_to_savepoint(savepoint, title="Projection log rollback failed")


def _log_error(title: str) -> None:
    log_sanitized_error(title)
