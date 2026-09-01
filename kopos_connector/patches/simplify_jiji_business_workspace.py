# pyright: reportMissingImports=false

"""Keep business navigation simple on sites that already have JiJi installed.

Workspace fixtures do not replace an installed layout. This patch removes only
the cards and links JiJi itself previously shipped, so an administrator's
unrelated additions remain untouched. Running it more than once is safe.
"""

from __future__ import annotations

import json
from typing import Any

import frappe


OLD_HEADER = "Your café"
NEW_HEADER = "Run your café"
OLD_PARAGRAPH = (
    "Start at Home. It shows the one thing that needs you and where the rest is waiting."
)
NEW_PARAGRAPH = "Choose what you want to do. Start at Home if you are not sure."
REMOVED_CONTENT_TEXT = {
    "Advanced",
    "Where every number above came from. If you never open this, you can still run your café.",
}
REMOVED_CARD_NAMES = {"Reports", "Where the numbers come from", "Support tools"}
REMOVED_LINK_TARGETS = {
    "KoPOS Shift Reconciliation",
    "KoPOS Device Health",
    "Sales Invoice",
    "KoPOS Staff Access",
    "Purchase Order",
    "FB Shift",
    "FB Order",
    "Stock Entry",
    "KoPOS Device",
    "kopos_provisioning",
}
REMOVED_CARD_LABELS = {"Reports", "Where the numbers come from", "Support tools"}


def simplify_content(raw_content: str | None) -> tuple[str, bool]:
    try:
        blocks = json.loads(raw_content or "[]")
    except (TypeError, ValueError):
        return raw_content or "", False
    if not isinstance(blocks, list):
        return raw_content or "", False

    kept: list[Any] = []
    changed = False
    for block in blocks:
        if not isinstance(block, dict):
            kept.append(block)
            continue
        data = block.get("data")
        if not isinstance(data, dict):
            kept.append(block)
            continue
        text = data.get("text")
        card_name = data.get("card_name")
        if text in REMOVED_CONTENT_TEXT or card_name in REMOVED_CARD_NAMES:
            changed = True
            continue
        if text == OLD_HEADER:
            data = {**data, "text": NEW_HEADER}
            block = {**block, "data": data}
            changed = True
        elif text == OLD_PARAGRAPH:
            data = {**data, "text": NEW_PARAGRAPH}
            block = {**block, "data": data}
            changed = True
        kept.append(block)

    if not changed:
        return raw_content or "", False
    return json.dumps(kept, separators=(",", ":")), True


def _row_value(row: Any, fieldname: str) -> str:
    if isinstance(row, dict):
        return str(row.get(fieldname) or "")
    return str(getattr(row, fieldname, "") or "")


def execute() -> None:
    if frappe.db.exists("Workspace", "JiJi"):
        workspace = frappe.get_doc("Workspace", "JiJi")
        workspace.content, content_changed = simplify_content(
            getattr(workspace, "content", None)
        )
        original_links = list(getattr(workspace, "links", ()) or ())
        kept_links = [
            row
            for row in original_links
            if _row_value(row, "link_to") not in REMOVED_LINK_TARGETS
            and _row_value(row, "label") not in REMOVED_CARD_LABELS
        ]
        links_changed = len(kept_links) != len(original_links)
        if links_changed:
            workspace.set("links", kept_links)
        if content_changed or links_changed:
            workspace.save(ignore_permissions=True)

    if frappe.db.exists("Workspace", "KoPOS Support"):
        support = frappe.get_doc("Workspace", "KoPOS Support")
        roles = {_row_value(row, "role") for row in (getattr(support, "roles", ()) or ())}
        if roles != {"System Manager"}:
            support.set("roles", [{"role": "System Manager"}])
            support.save(ignore_permissions=True)
