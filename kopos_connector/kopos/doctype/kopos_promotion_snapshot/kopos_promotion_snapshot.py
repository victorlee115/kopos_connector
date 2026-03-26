# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class KoPOSPromotionSnapshot(Document):
    def on_trash(self) -> None:
        referenced_by_version = frappe.db.exists(
            "POS Invoice",
            {"custom_kopos_promotion_snapshot_version": self.snapshot_version},
        )
        if referenced_by_version:
            frappe.throw(
                _("Cannot delete snapshot {0}: Referenced by POS Invoice {1}").format(
                    self.snapshot_version, referenced_by_version
                ),
                frappe.ValidationError,
            )

        if not self.snapshot_hash:
            return

        hash_pattern = '%"snapshot_hash":"{0}"%'.format(
            frappe.db.escape(self.snapshot_hash)
        )
        result = frappe.db.sql(
            """
            SELECT name FROM `tabPOS Invoice`
            WHERE custom_kopos_promotion_payload LIKE %s
            LIMIT 1
            """,
            (hash_pattern,),
        )
        if result and len(result) > 0 and len(result[0]) > 0:
            frappe.throw(
                _(
                    "Cannot delete snapshot {0}: Referenced by POS Invoice {1} via hash"
                ).format(self.snapshot_version, result[0][0]),
                frappe.ValidationError,
            )
