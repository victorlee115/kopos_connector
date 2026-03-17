# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class KoPOSPromotionSnapshot(Document):
    def on_trash(self):
        referenced = frappe.db.exists(
            "POS Invoice",
            {"custom_kopos_promotion_snapshot_version": self.snapshot_version},
        )
        if referenced:
            frappe.throw(
                _("Cannot delete snapshot {0}: Referenced by POS Invoice {1}").format(
                    self.snapshot_version, referenced
                ),
                frappe.ValidationError,
            )
