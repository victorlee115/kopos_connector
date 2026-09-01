from __future__ import annotations

import hashlib

import frappe
from frappe.model.document import Document
from frappe.utils import cstr


class FBInventoryCommandReceipt(Document):
    """Immutable replay identity for one tablet inventory command."""

    def autoname(self) -> None:
        command_id = cstr(self.command_id).strip()
        if not command_id:
            frappe.throw("Inventory command receipt requires command_id")
        self.name = "KOPOS-CMD-" + hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:32]

    def validate(self) -> None:
        if not cstr(self.command_id).strip():
            frappe.throw("Inventory command receipt requires command_id")
        payload_hash = cstr(self.payload_hash).strip()
        if len(payload_hash) != 64 or any(character not in "0123456789abcdef" for character in payload_hash):
            frappe.throw("Inventory command receipt requires a lowercase SHA-256 payload hash")
        if cstr(self.state).strip() == "Completed":
            if cstr(self.outcome).strip() not in {"Completed", "Exception"}:
                frappe.throw("Completed inventory command receipt requires an outcome")
            if not cstr(self.result_doctype).strip() or not cstr(self.result_document).strip():
                frappe.throw("Completed inventory command receipt requires its result document")
