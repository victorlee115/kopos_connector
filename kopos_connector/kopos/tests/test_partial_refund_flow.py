from __future__ import annotations

from collections import defaultdict
from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api.fb_returns import list_order_refunds, process_return_payload
from kopos_connector.api.order_history import query_return_events_by_return_invoice, serialize_refund_row
from kopos_connector.kopos.api.fb_orders import submit_order_payload
from kopos_connector.kopos.api.money_contract import persisted_money_to_sen
from kopos_connector.kopos.services.accounting.refund_allocation import (
    REFUND_ALLOCATION_VERSION,
    RefundableSale,
    RefundableSaleLine,
    allocate_refund,
)
from kopos_connector.kopos.tests.frappe_test_fixtures import (
    build_sen_v1_sale_payload,
    create_open_test_shift,
)


PREFIX = "KOPOS-PART-REFUND"


def _sen(value: Any) -> int:
    return persisted_money_to_sen(value or 0, "part refund test")


class TestPartialRefundFlow(FrappeTestCase):
    """Real ERPNext credit notes, settlements and ledgers for allocated part refunds."""

    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.shift = create_open_test_shift(prefix=PREFIX, replenish_stock=False)

    def tearDown(self) -> None:
        frappe.db.rollback()

    def _sale(self, lines: list[tuple[int, ...]], *, tax_sen: int, qr: dict[str, Any] | None = None) -> tuple[Any, list[str], RefundableSale]:
        """A cash sale rounded to 5 sen, or a manually confirmed QR sale when ``qr`` evidence is given.

        Lines are (quantity, line total sen) or (quantity, line total sen, unit price sen).
        """
        payload = build_sen_v1_sale_payload(self.shift, prefix=PREFIX, quantity=lines[0][0])
        template = payload["order"]["items"][0]
        items, line_ids = [], []
        for quantity, line_total_sen, *unit in lines:
            unit_sen = unit[0] if unit else -(-line_total_sen // quantity)
            line_id = f"{PREFIX}-LINE-{frappe.generate_hash(length=10)}"
            line_ids.append(line_id)
            items.append({**template, "line_id": line_id, "qty": quantity, "unit_price_sen": unit_sen,
                          "discount_amount_sen": unit_sen * quantity - line_total_sen, "line_total_sen": line_total_sen})
        subtotal = sum(line[1] for line in lines)
        remainder = (subtotal + tax_sen) % 5
        rounding = 0 if qr else (-remainder if remainder <= 2 else 5 - remainder)
        total = subtotal + tax_sen + rounding
        payload["order"].update({"items": items, "subtotal_sen": subtotal, "tax_amount_sen": tax_sen,
                                 "rounding_adjustment_sen": rounding, "total_sen": total})
        payload["order"]["payments"][0].update({"amount_sen": total, "tendered_amount_sen": total, "change_amount_sen": 0, **(qr or {})})
        order = frappe.get_doc("FB Order", submit_order_payload(payload)["fb_order"])
        sale = RefundableSale(
            tuple(RefundableSaleLine(line_id, line[0], line[1]) for line_id, line in zip(line_ids, lines)),
            tax_sen, rounding, total, 1 if qr else 5,
        )
        return order, line_ids, sale

    def _refund(self, order: Any, return_id: str, lines: list[tuple[str, int]], amount_sen: int, method: str = "cash") -> dict[str, Any]:
        return process_return_payload(
            {
                "return_id": return_id,
                "idempotency_key": return_id,
                "device_id": order.device_id,
                "fb_order": order.name,
                "original_sales_invoice": order.sales_invoice,
                "reason_code": "Quality Issue",
                "reason_text": "Part refund fixture",
                "refund_method": method,
                "return_to_stock": 0,
                "inventory_evaluation": "included_evaluated",
                "refund_allocation_version": REFUND_ALLOCATION_VERSION,
                "amount_sen": amount_sen,
                "lines": [{"original_fb_order_line_ref": line_id, "qty_returned": quantity} for line_id, quantity in lines],
            },
            require_manager_approval=False,
        )

    def _account_nets(self, sales_invoices: list[str], journals: list[str]) -> dict[str, int]:
        nets: dict[str, int] = defaultdict(int)
        for voucher_type, names in (("Sales Invoice", sales_invoices), ("Journal Entry", journals)):
            for row in frappe.get_all("GL Entry", filters={"voucher_type": voucher_type, "voucher_no": ["in", names], "is_cancelled": 0},
                                      fields=["account", "debit", "credit"]):
                nets[row.account] += _sen(row.debit) - _sen(row.credit)
        return {account: value for account, value in nets.items() if value}

    def test_cash_part_refunds_post_exact_credit_notes_and_conserve_every_account(self) -> None:
        order, (first, second), sale = self._sale([(3, 1003), (1, 1200)], tax_sen=90)
        original = frappe.get_doc("Sales Invoice", order.sales_invoice)
        original_state = {field: str(original.get(field)) for field in ("grand_total", "write_off_amount", "outstanding_amount", "net_total", "docstatus")}
        expected_cash = _sen(frappe.db.get_value("FB Shift", self.shift.name, "expected_cash"))
        plan = [
            (f"{PREFIX}-R1-{frappe.generate_hash(length=8)}", [(first, 1)]),
            (f"{PREFIX}-R2-{frappe.generate_hash(length=8)}", [(first, 1), (second, 1)]),
            (f"{PREFIX}-R3-{frappe.generate_hash(length=8)}", [(first, 1)]),
        ]
        refunded: dict[str, int] = {}
        credit_notes, journals, total_refunded = [], [], 0
        for return_id, lines in plan:
            allocation = allocate_refund(sale, [{"line_id": k, "quantity": v} for k, v in refunded.items()],
                                         [{"line_id": line_id, "quantity": quantity} for line_id, quantity in lines])
            response = self._refund(order, return_id, lines, allocation.amount_sen)
            self.assertEqual((response["status"], response["settlement_status"]), ("ok", "Posted"))
            self.assertEqual(response["refund_allocation"], allocation.to_wire())
            credit = frappe.get_doc("Sales Invoice", response["return_sales_invoice"])
            self.assertEqual((credit.docstatus, credit.is_return, credit.return_against), (1, 1, original.name))
            self.assertEqual(-sum(_sen(item.net_amount) for item in credit.items), allocation.net_sen)
            self.assertEqual(-_sen(credit.total_taxes_and_charges), allocation.tax_sen)
            self.assertEqual(_sen(credit.write_off_amount), allocation.rounding_sen)
            self.assertEqual(-(_sen(credit.grand_total) - _sen(credit.write_off_amount)), allocation.amount_sen)
            self.assertEqual(_sen(credit.outstanding_amount), 0)
            self.assertTrue(all(float(item.rate) <= float(next(row.rate for row in original.items if row.name == item.sales_invoice_item)) for item in credit.items))
            event = frappe.get_doc("FB Return Event", response["return_event"])
            self.assertEqual((_sen(event.settlement_amount), _sen(event.refund_amount)), (allocation.amount_sen, allocation.amount_sen))
            credit_notes.append(credit.name)
            journals.append(response["settlement_document"])
            total_refunded += allocation.amount_sen
            for line_id, quantity in lines:
                refunded[line_id] = refunded.get(line_id, 0) + quantity
            self.assertEqual(_sen(frappe.db.get_value("FB Shift", self.shift.name, "expected_cash")), expected_cash - total_refunded)
        self.assertEqual(total_refunded, sale.total_sen)
        self.assertEqual(self._account_nets([original.name, *credit_notes], journals), {})
        original.reload()
        self.assertEqual({field: str(original.get(field)) for field in original_state}, original_state)

        # A replay after later refunds returns its own recorded allocation and documents.
        replay = self._refund(order, plan[0][0], plan[0][1], allocate_refund(sale, [], [{"line_id": first, "quantity": 1}]).amount_sen)
        self.assertEqual((replay["status"], replay["return_sales_invoice"]), ("duplicate", credit_notes[0]))
        with self.assertRaisesRegex(frappe.ValidationError, "already been refunded|exceeds purchased quantity"):
            self._refund(order, f"{PREFIX}-R4-{frappe.generate_hash(length=8)}", [(first, 1)], 330)

        # The tablet reads the sale's refunds back, oldest first, to record any it missed.
        listed = list_order_refunds(order.sales_invoice)["refunds"]
        self.assertEqual([row["return_id"] for row in listed], [return_id for return_id, _lines in plan])
        self.assertEqual([row["return_sales_invoice"] for row in listed], credit_notes)
        self.assertEqual([row["refund_allocation"]["isFinal"] for row in listed], [False, False, True])
        self.assertTrue(all(row["settlement_status"] == "Posted" and row["amount_sen"] == row["refund_allocation"]["amountSen"] for row in listed))
        self.assertEqual(sum(row["amount_sen"] for row in listed), sale.total_sen)
        events = query_return_events_by_return_invoice(credit_notes)
        history_row = serialize_refund_row({"name": credit_notes[2], "return_against": original.name}, items=[], payments=[], return_event=events[credit_notes[2]])
        self.assertEqual((history_row["return_id"], history_row["refund_allocation"]), (plan[2][0], listed[2]["refund_allocation"]))

    def test_stale_conflicting_and_full_after_part_requests_are_refused(self) -> None:
        order, (line,), sale = self._sale([(2, 2400)], tax_sen=0)
        return_id = f"{PREFIX}-STALE-{frappe.generate_hash(length=8)}"
        with self.assertRaisesRegex(frappe.ValidationError, "refund amount changed"):
            self._refund(order, return_id, [(line, 1)], 1199, method="cash")
        allocation = allocate_refund(sale, [], [{"line_id": line, "quantity": 1}])
        first = self._refund(order, return_id, [(line, 1)], allocation.amount_sen, method="cash")
        self.assertEqual(first["refund_allocation"]["amountSen"], 1200)
        with self.assertRaisesRegex(frappe.ValidationError, "different"):
            self._refund(order, return_id, [(line, 2)], 2400, method="cash")
        with self.assertRaisesRegex(frappe.ValidationError, "exceeds purchased quantity|Partial ERP returns are not supported"):
            process_return_payload({
                "return_id": f"{PREFIX}-FULL-{frappe.generate_hash(length=8)}", "device_id": order.device_id,
                "fb_order": order.name, "original_sales_invoice": order.sales_invoice, "reason_code": "Other",
                "refund_method": "cash", "return_to_stock": 0, "inventory_evaluation": "included_evaluated",
            }, require_manager_approval=False)

    def test_zero_value_lines_register_and_have_nothing_to_refund(self) -> None:
        # A zero-priced item and a fully discounted item inside a paid sale.
        order, (paid, zero_priced, discounted), sale = self._sale([(1, 1200), (1, 0, 0), (1, 0, 450)], tax_sen=0)
        self.assertEqual(order.invoice_status, "Posted")
        invoice = frappe.get_doc("Sales Invoice", order.sales_invoice)
        self.assertEqual((_sen(invoice.grand_total), _sen(invoice.outstanding_amount)), (1200, 0))
        rows = {row.custom_fb_order_line_ref: row for row in invoice.items}
        self.assertEqual((_sen(rows[zero_priced].amount), _sen(rows[discounted].amount), float(rows[discounted].discount_percentage)), (0, 0, 100.0))
        with self.assertRaisesRegex(frappe.ValidationError, "no paid amount left"):
            self._refund(order, f"{PREFIX}-FREE-{frappe.generate_hash(length=8)}", [(zero_priced, 1), (discounted, 1)], 5)
        allocation = allocate_refund(sale, [], [{"line_id": paid, "quantity": 1}, {"line_id": discounted, "quantity": 1}])
        response = self._refund(order, f"{PREFIX}-WITH-FREE-{frappe.generate_hash(length=8)}", [(paid, 1), (discounted, 1)], allocation.amount_sen)
        self.assertEqual((response["refund_allocation"]["amountSen"], response["refund_allocation"]["isFinal"]), (1200, True))

    def test_qr_part_refund_settles_to_the_sale_clearing_ledger(self) -> None:
        company = self.shift.company
        parent = frappe.db.get_value("Account", {"company": company, "root_type": "Asset", "is_group": 1}, "name")
        clearing = frappe.get_doc({
            "doctype": "Account", "account_name": f"Part Refund QR Clearing {frappe.generate_hash(length=8)}",
            "parent_account": parent, "company": company, "is_group": 0, "account_type": "",
            "account_currency": frappe.db.get_value("Company", company, "default_currency"),
        }).insert(ignore_permissions=True).name
        mode = frappe.get_doc("Mode of Payment", "DuitNow QR")
        next(row for row in mode.accounts if row.company == company).default_account = clearing
        mode.save(ignore_permissions=True)
        profile = frappe.get_doc("POS Profile", frappe.db.get_value("KoPOS Device", {"device_id": self.shift.device_id}, "pos_profile"))
        profile.custom_kopos_manual_qr_suspense_account = clearing
        profile.save(ignore_permissions=True)
        suffix = frappe.generate_hash(length=10)
        captured_at = frappe.utils.now_datetime().isoformat()
        evidence = {
            "payment_method": "DuitNow QR", "payment_channel_code": "static_qr",
            "reference_no": f"PART-QR-{suffix}", "external_transaction_id": f"static-{suffix}",
            "manual_confirmation_warning_acknowledged": True,
            "manual_confirmation_evidence": {
                "evidence_kind": "no_receipt_acknowledgement", "captured_at": captured_at,
                "upload_status": "not_required", "reconciliation_status": "pending_reconciliation",
                "local_confirmed_at": captured_at, "local_confirmed_by": self.shift.staff_id,
                "local_confirmation_reference": f"PART-QR-{suffix}",
                "reconciliation_idempotency_key": f"PART-QR-RECONCILIATION-{suffix}",
                "evidence_captured_device_id": self.shift.device_id,
                "no_receipt_acknowledged": True, "no_receipt_reason_code": "receipt_unavailable",
            },
        }
        order, (line,), sale = self._sale([(3, 1000)], tax_sen=0, qr=evidence)
        allocation = allocate_refund(sale, [], [{"line_id": line, "quantity": 1}])
        self.assertEqual(allocation.amount_sen, 333)
        response = self._refund(order, f"{PREFIX}-QR-{suffix}", [(line, 1)], allocation.amount_sen, method="qr")
        self.assertEqual(response["refund_allocation"], allocation.to_wire())
        journal = frappe.get_all("GL Entry", filters={"voucher_type": "Journal Entry", "voucher_no": response["settlement_document"], "is_cancelled": 0},
                                 fields=["account", "debit", "credit"])
        self.assertEqual(sum(_sen(row.credit) - _sen(row.debit) for row in journal if row.account == clearing), 333)
