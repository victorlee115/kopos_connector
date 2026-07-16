# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class KoPOSPromotionSnapshot(Document):
    def on_trash(self) -> None:
        snapshot_version = str(getattr(self, "snapshot_version", None) or "").strip()
        snapshot_hash = str(getattr(self, "snapshot_hash", None) or "").strip()

        for doctype, version_field, hash_field in (
            (
                "FB Order",
                "promotion_snapshot_version",
                "promotion_snapshot_hash",
            ),
            (
                "Sales Invoice",
                "custom_kopos_promotion_snapshot_version",
                "custom_kopos_promotion_snapshot_hash",
            ),
        ):
            if snapshot_version:
                referenced_by_version = frappe.db.exists(
                    doctype,
                    {version_field: snapshot_version},
                )
                if referenced_by_version:
                    frappe.throw(
                        _(
                            "Cannot delete promotion snapshot {0}: "
                            "referenced by {1} {2} via version"
                        ).format(
                            snapshot_version,
                            doctype,
                            referenced_by_version,
                        ),
                        frappe.ValidationError,
                    )

            if snapshot_hash:
                referenced_by_hash = frappe.db.exists(
                    doctype,
                    {hash_field: snapshot_hash},
                )
                if referenced_by_hash:
                    frappe.throw(
                        _(
                            "Cannot delete promotion snapshot {0}: "
                            "referenced by {1} {2} via hash"
                        ).format(
                            snapshot_version,
                            doctype,
                            referenced_by_hash,
                        ),
                        frappe.ValidationError,
                    )
