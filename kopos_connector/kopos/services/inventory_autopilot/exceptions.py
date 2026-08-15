from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, cstr, now_datetime


def upsert_inventory_exception(
    *,
    reason_code: str,
    summary: str,
    next_action: str,
    severity: str = "Warning",
    company: str | None = None,
    warehouse: str | None = None,
    item: str | None = None,
    source_doctype: str | None = None,
    source_name: str | None = None,
    responsible_owner: str | None = None,
) -> str:
    identity = _exception_identity(
        reason_code=reason_code,
        company=company,
        warehouse=warehouse,
        item=item,
        source_doctype=source_doctype,
        source_name=source_name,
    )
    fingerprint = _exception_fingerprint(identity)
    now = now_datetime()
    existing = frappe.db.get_value("FB Inventory Exception", {"fingerprint": fingerprint}, "name")
    values: dict[str, Any] = {
        "fingerprint": fingerprint,
        "severity": severity,
        "status": "Open",
        "summary": cstr(summary).strip(),
        "reason_code": identity["reason_code"],
        "next_action": cstr(next_action).strip(),
        "company": identity["company"],
        "warehouse": identity["warehouse"],
        "item": identity["item"],
        "source_doctype": identity["source_doctype"],
        "source_name": identity["source_name"],
        "last_seen": now,
    }
    if existing:
        count = cint(frappe.db.get_value("FB Inventory Exception", existing, "occurrence_count"))
        values["occurrence_count"] = max(1, count) + 1
        frappe.db.set_value("FB Inventory Exception", existing, values, update_modified=False)
        _ensure_todo(existing, values, responsible_owner=responsible_owner)
        return cstr(existing)
    values["first_seen"] = now
    values["occurrence_count"] = 1
    document = frappe.get_doc({"doctype": "FB Inventory Exception", **values})
    document.insert(ignore_permissions=True)
    _ensure_todo(document.name, values, responsible_owner=responsible_owner)
    return cstr(document.name)


def resolve_inventory_exception(
    *,
    reason_code: str,
    company: str | None = None,
    warehouse: str | None = None,
    item: str | None = None,
    source_doctype: str | None = None,
    source_name: str | None = None,
) -> str | None:
    """Resolve the exact recurring condition and its open notification."""

    identity = _exception_identity(
        reason_code=reason_code,
        company=company,
        warehouse=warehouse,
        item=item,
        source_doctype=source_doctype,
        source_name=source_name,
    )
    existing = frappe.db.get_value(
        "FB Inventory Exception",
        {"fingerprint": _exception_fingerprint(identity)},
        "name",
    )
    if not existing:
        return None
    frappe.db.set_value(
        "FB Inventory Exception",
        existing,
        {"status": "Resolved", "last_seen": now_datetime()},
        update_modified=False,
    )
    if frappe.db.exists("DocType", "ToDo"):
        todos = frappe.get_all(
            "ToDo",
            filters={
                "reference_type": "FB Inventory Exception",
                "reference_name": existing,
                "status": "Open",
            },
            pluck="name",
            limit_page_length=100,
        )
        for todo in todos:
            frappe.db.set_value("ToDo", todo, "status", "Closed", update_modified=False)
    return cstr(existing)


def _exception_identity(
    *,
    reason_code: str,
    company: str | None,
    warehouse: str | None,
    item: str | None,
    source_doctype: str | None,
    source_name: str | None,
) -> dict[str, str | None]:
    return {
        "company": cstr(company).strip() or None,
        "item": cstr(item).strip() or None,
        "reason_code": cstr(reason_code).strip(),
        "source_doctype": cstr(source_doctype).strip() or None,
        "source_name": cstr(source_name).strip() or None,
        "warehouse": cstr(warehouse).strip() or None,
    }


def _exception_fingerprint(identity: dict[str, str | None]) -> str:
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _ensure_todo(
    exception_name: str,
    values: dict[str, Any],
    *,
    responsible_owner: str | None = None,
) -> None:
    if not frappe.db.exists("DocType", "ToDo"):
        return
    if frappe.db.exists(
        "ToDo",
        {"reference_type": "FB Inventory Exception", "reference_name": exception_name, "status": "Open"},
    ):
        return
    owner = _resolve_todo_owner(values, responsible_owner=responsible_owner)
    todo = frappe.get_doc({
        "doctype": "ToDo",
        "description": f"Inventory exception: {values['summary']} — {values['next_action']}",
        "reference_type": "FB Inventory Exception",
        "reference_name": exception_name,
        "owner": owner,
        "status": "Open",
    })
    todo.insert(ignore_permissions=True)


def _resolve_todo_owner(values: dict[str, Any], *, responsible_owner: str | None) -> str:
    """Use explicit or source-document ownership before the session fallback.

    Workers often run without a meaningful interactive session.  Callers that
    know the accountable person can provide ``responsible_owner``; otherwise
    an existing source document's real owner is the only safe configured
    authority.  The session user remains a compatibility fallback for manual
    actions, and Administrator is used only when Frappe supplies no session
    identity at all.
    """

    explicit_owner = cstr(responsible_owner).strip()
    if explicit_owner:
        return explicit_owner
    source_doctype = cstr(values.get("source_doctype")).strip()
    source_name = cstr(values.get("source_name")).strip()
    if source_doctype and source_name:
        try:
            source_owner = cstr(frappe.db.get_value(source_doctype, source_name, "owner")).strip()
        except Exception:
            source_owner = ""
        if source_owner:
            return source_owner
    return cstr(getattr(frappe.session, "user", None)).strip() or "Administrator"
