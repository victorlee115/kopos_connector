from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import patch

import frappe
import pytest
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from kopos_connector.kopos.services.inventory_autopilot import document_coordinator


pytestmark = pytest.mark.inventory_regression


class TestInventoryDraftPurchaseOrderContract(FrappeTestCase):
    """Prove the Draft-PO boundary with real standard ERPNext documents."""

    def tearDown(self) -> None:
        frappe.db.rollback()

    def test_created_purchase_order_stays_draft_without_outbound_records(self) -> None:
        company = frappe.get_all(
            "Company",
            fields=["name", "default_currency"],
            order_by="name asc",
            limit_page_length=1,
        )[0]
        item_group = frappe.get_all(
            "Item Group",
            filters={"is_group": 0},
            pluck="name",
            order_by="name asc",
            limit_page_length=1,
        )[0]
        supplier_group = frappe.get_all(
            "Supplier Group",
            pluck="name",
            order_by="is_group asc, name asc",
            limit_page_length=1,
        )[0]
        suffix = frappe.generate_hash(length=10).upper()
        item_code = f"INV-PO-{suffix}"

        frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": item_code,
                "item_name": item_code,
                "item_group": item_group,
                "stock_uom": "Nos",
                "is_stock_item": 0,
                "is_purchase_item": 1,
                "is_sales_item": 0,
            }
        ).insert(ignore_permissions=True)
        supplier = frappe.get_doc(
            {
                "doctype": "Supplier",
                "supplier_name": f"INV-PO-SUPPLIER-{suffix}",
                "supplier_group": supplier_group,
                "supplier_type": "Company",
            }
        ).insert(ignore_permissions=True)

        required_date = add_days(nowdate(), 7)
        material_request = frappe.new_doc("Material Request")
        material_request.company = company.name
        material_request.material_request_type = "Purchase"
        material_request.append(
            "items",
            {
                "item_code": item_code,
                "qty": 1,
                "uom": "Nos",
                "conversion_factor": 1,
                "schedule_date": required_date,
            },
        )
        material_request.insert(ignore_permissions=True)
        material_request.submit()

        quotation = frappe.new_doc("Supplier Quotation")
        quotation.company = company.name
        quotation.supplier = supplier.name
        quotation.currency = company.default_currency
        quotation.transaction_date = nowdate()
        quotation.valid_till = add_days(nowdate(), 14)
        quotation.append(
            "items",
            {
                "item_code": item_code,
                "qty": 1,
                "rate": 1,
                "uom": "Nos",
                "conversion_factor": 1,
                "schedule_date": required_date,
                "material_request": material_request.name,
                "material_request_item": material_request.items[0].name,
            },
        )
        quotation.insert(ignore_permissions=True)
        quotation.submit()

        plan = {
            "name": f"INV-PO-PLAN-{suffix}",
            "warehouse": "",
            "created_documents": (
                '[{"doctype":"Material Request","name":"'
                + material_request.name
                + '"}]'
            ),
        }
        quotation_hash = document_coordinator.quotation_snapshot_hash(quotation)
        before_outbound = {
            doctype: frappe.db.count(doctype)
            for doctype in ("Email Queue", "Communication")
        }

        with patch.object(
            document_coordinator, "outbound_configuration_safe", return_value=(True, "")
        ), patch.object(
            document_coordinator, "_execution_plan", return_value=(plan, ())
        ), patch.object(
            document_coordinator, "_has_inventory_fingerprint_field", return_value=True
        ), patch.object(
            document_coordinator, "_find_by_fingerprint", return_value=None
        ), patch.object(
            document_coordinator, "_current_matching_quotations", return_value=[quotation.name]
        ), patch.object(
            document_coordinator, "purchase_review_owner", return_value="Administrator"
        ), patch.object(
            document_coordinator, "inventory_automation_identity", return_value=nullcontext("inventory-test")
        ), patch.object(
            document_coordinator, "_ensure_purchase_order_todo"
        ), patch.object(
            document_coordinator, "_record_plan_document"
        ), patch.object(
            frappe,
            "sendmail",
            side_effect=AssertionError("Draft PO creation must not send email"),
        ):
            result = document_coordinator.create_draft_purchase_order(
                company=company.name,
                material_request=material_request.name,
                quotation=quotation.name,
                plan_hash="plan-hash",
                policy_hash="policy-hash",
                quotation_hash=quotation_hash,
            )

        self.assertEqual(result["status"], "created_draft")
        self.assertEqual(result["docstatus"], 0)
        purchase_order = frappe.get_doc("Purchase Order", result["purchase_order"])
        self.assertEqual(purchase_order.docstatus, 0)
        self.assertEqual(purchase_order.supplier, supplier.name)
        self.assertEqual(purchase_order.items[0].item_code, item_code)
        self.assertEqual(purchase_order.items[0].material_request, material_request.name)
        self.assertEqual(
            {doctype: frappe.db.count(doctype) for doctype in before_outbound},
            before_outbound,
        )
