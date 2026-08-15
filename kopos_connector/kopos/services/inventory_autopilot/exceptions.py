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
) -> str:
    identity = {
        "company": cstr(company).strip() or None,
        "item": cstr(item).strip() or None,
        "reason_code": cstr(reason_code).strip(),
        "source_doctype": cstr(source_doctype).strip() or None,
        "source_name": cstr(source_name).strip() or None,
        "warehouse": cstr(warehouse).strip() or None,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
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
        _ensure_todo(existing, values)
        return cstr(existing)
    values["first_seen"] = now
    values["occurrence_count"] = 1
    document = frappe.get_doc({"doctype": "FB Inventory Exception", **values})
    document.insert(ignore_permissions=True)
    _ensure_todo(document.name, values)
    return cstr(document.name)


def _ensure_todo(exception_name: str, values: dict[str, Any]) -> None:
    if not frappe.db.exists("DocType", "ToDo"):
        return
    if frappe.db.exists(
        "ToDo",
        {"reference_type": "FB Inventory Exception", "reference_name": exception_name, "status": "Open"},
    ):
        return
    todo = frappe.get_doc({
        "doctype": "ToDo",
        "description": f"Inventory exception: {values['summary']} — {values['next_action']}",
        "reference_type": "FB Inventory Exception",
        "reference_name": exception_name,
        "owner": cstr(getattr(frappe.session, "user", None)).strip() or "Administrator",
        "status": "Open",
    })
    todo.insert(ignore_permissions=True)
