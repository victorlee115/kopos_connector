# pyright: reportMissingImports=false

"""Replace only the original technical KoPOS Support introduction.

The standard Workspace JSON does not overwrite an already-installed Workspace.
Match the two shipped text blocks exactly so an administrator's customized
layout or replacement copy remains untouched.
"""

from __future__ import annotations

import json

import frappe


OLD_HEADER = "KoPOS support cockpit"
NEW_HEADER = "Need help?"
OLD_PARAGRAPH = (
    "Minimum admin/support entry point for device health, active FB Shifts, "
    "FB Orders, Sales Invoices, projection_status review, reconciliation, "
    "and smoke/support reports. Provisioning remains System Manager-only."
)
NEW_PARAGRAPH = (
    "Use this page when a tablet, sale, or shift needs help. Choose the matching "
    "check below. Tablet setup is only for System Managers."
)


def execute() -> None:
    if not frappe.db.exists("Workspace", "KoPOS Support"):
        return
    content = frappe.db.get_value("Workspace", "KoPOS Support", "content")
    try:
        blocks = json.loads(content or "[]")
    except (TypeError, ValueError):
        return
    if not isinstance(blocks, list):
        return

    changed = False
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("data"), dict):
            continue
        text = block["data"].get("text")
        if text == OLD_HEADER:
            block["data"]["text"] = NEW_HEADER
            changed = True
        elif text == OLD_PARAGRAPH:
            block["data"]["text"] = NEW_PARAGRAPH
            changed = True

    if changed:
        frappe.db.set_value(
            "Workspace",
            "KoPOS Support",
            "content",
            json.dumps(blocks, separators=(",", ":")),
            update_modified=False,
        )
