from __future__ import annotations

import frappe
import pytest
from frappe.tests.utils import FrappeTestCase

pytestmark = pytest.mark.inventory_regression


class TestPromotionEconomicsContract(FrappeTestCase):
    def tearDown(self) -> None:
        frappe.db.rollback()

    def _new_inactive_promotion(self):
        return frappe.get_doc(
            {
                "doctype": "KoPOS Promotion",
                "promotion_name": f"KOPOS-ECONOMICS-{frappe.generate_hash(length=10)}",
                "promotion_type": "item_discount",
                "is_active": 0,
                "offline_allowed": 0,
                "discount_type": "percentage",
                "discount_value": 10,
            }
        )

    def test_review_fields_are_read_only_and_promotion_is_director_owned(self) -> None:
        meta = frappe.get_meta("KoPOS Promotion")
        for fieldname in (
            "economics_status",
            "economics_source_hash",
            "economics_checked_at",
            "economics_checked_by",
            "economics_block_reason",
            "economics_override_reason",
            "economics_override_by",
            "economics_override_at",
            "economics_override_hash",
        ):
            self.assertTrue(meta.get_field(fieldname).read_only, fieldname)
        roles = {permission.role for permission in meta.permissions}
        self.assertIn("Company Director", roles)
        self.assertIn("KoPOS Device API", roles)
        self.assertNotIn("Item Manager", roles)
        self.assertNotIn("POS User", roles)

    def test_review_status_cannot_be_set_directly(self) -> None:
        promotion = self._new_inactive_promotion()
        promotion.economics_status = "Ready"
        with self.assertRaisesRegex(
            frappe.ValidationError,
            "assigned by the server review flow",
        ):
            promotion.insert(ignore_permissions=True)

    def test_existing_review_status_cannot_be_changed_by_form_save(self) -> None:
        promotion = self._new_inactive_promotion()
        promotion.insert(ignore_permissions=True)
        promotion.reload()
        promotion.economics_status = "Ready"
        with self.assertRaisesRegex(
            frappe.ValidationError,
            "server-managed",
        ):
            promotion.save(ignore_permissions=True)
