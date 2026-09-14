from __future__ import annotations

import json
from datetime import datetime

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api.promotions import (
    compute_snapshot_content_hash,
    serialize_promotion,
)


class TestPromotionSnapshotPersistenceContract(FrappeTestCase):
    """Snapshot hashing must accept the real MariaDB Datetime field shape."""

    def tearDown(self) -> None:
        frappe.db.rollback()

    def _editor_payload(self):
        from kopos_connector.api.promotion_setup import _menu_for_profile
        profiles = frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name", limit=1)
        self.assertTrue(profiles, "Seed the isolated fixture first")
        menu = _menu_for_profile(profiles[0])
        self.assertTrue(menu, "Fixture needs a current priced menu item")
        return {"name": f"GUIDED-V2-{frappe.generate_hash(length=10)}", "kind": "half_second", "value": "50",
                "items": [menu[0]["id"]], "outlets": profiles, "repeat": "repeat", "activation": "automatic",
                "start": "", "end": "", "schedule": None}

    def test_full_page_draft_retry_copy_and_native_preview_round_trip(self):
        from kopos_connector.api.promotion_setup import save_offer_draft, get_offer_editor, preview_offer
        values = self._editor_payload()
        before = frappe.db.count("KoPOS Promotion Snapshot")
        saved = save_offer_draft(json.dumps(values))
        loaded = get_offer_editor(saved["id"])
        self.assertTrue(loaded["editable"])
        self.assertEqual(loaded["values"], values)
        self.assertEqual(save_offer_draft(values), saved)
        self.assertEqual(frappe.db.get_value("KoPOS Promotion", saved["id"], "is_active"), 0)
        edited_values = {**values, "value": "10", "kind": "percentage"}
        edited = save_offer_draft(edited_values, saved["id"], saved["revision"])
        self.assertEqual(save_offer_draft(edited_values, saved["id"], saved["revision"]), edited)
        with self.assertRaises(frappe.ValidationError):
            save_offer_draft({**edited_values, "value": "20"}, saved["id"], saved["revision"])
        preview = preview_offer(edited_values, values["outlets"][0], [{"item": values["items"][0], "quantity": 2}], name=saved["id"])
        self.assertEqual(preview["quote"]["status"], "complete")
        copy = save_offer_draft({**edited_values, "name": values["name"] + " copy"})
        self.assertNotEqual(copy["id"], saved["id"])
        self.assertEqual(frappe.db.count("KoPOS Promotion Snapshot"), before)

    def test_multi_outlet_publication_does_not_include_drafts_or_reactivate_on_retry(self):
        """Real MariaDB lifecycle; admission/economics intentionally stubbed.

        This proves persistence, not commissioned costs or tablet acceptance.
        """
        from unittest.mock import patch
        from kopos_connector.api import promotions
        from kopos_connector.api.promotion_setup import save_offer_draft
        values = self._editor_payload()
        outlets = []
        for index in range(2):
            profile = frappe.copy_doc(frappe.get_doc("POS Profile", values["outlets"][0]))
            profile.name = f"{values['name']}-outlet-{index}"
            profile.insert(ignore_permissions=True)
            outlets.append(profile.name)
        values["outlets"] = outlets
        saved = save_offer_draft(values)
        other = save_offer_draft({**values, "name": values["name"] + " unrelated draft"})
        # Approved exceptions do not rewrite the draft's modified timestamp.
        # That is the adversarial case for repeated identical publication intent.
        ready = {"source_hash": "a" * 64, "ready": True, "override": True, "reason": ""}
        with patch.object(promotions, "_guided_rollout_issues", return_value=[]), patch(
            "kopos_connector.api.inventory.review_promotion_economics", return_value=ready
        ):
            initial = promotions.review_offer_change(saved["id"], action="activate", revision=saved["revision"])
            original_persist = promotions.persist_reviewed_snapshot
            attempts = []

            def fail_second_outlet(profile, payload, **kwargs):
                attempts.append(profile)
                if len(attempts) == 2:
                    raise RuntimeError("Injected second-outlet failure")
                return original_persist(profile, payload, **kwargs)

            # The API's transaction owner rolls back on failure. Exercise that
            # boundary against MariaDB, without discarding the fixture setup.
            frappe.db.savepoint("promotion_atomic_publication")
            with patch.object(promotions, "persist_reviewed_snapshot", side_effect=fail_second_outlet):
                with self.assertRaisesRegex(RuntimeError, "second-outlet"):
                    promotions.publish_reviewed_offer_change(saved["id"], initial["expected_hash"], action="activate", revision=saved["revision"])
            frappe.db.rollback(save_point="promotion_atomic_publication")
            self.assertFalse(frappe.db.exists("KoPOS Promotion Publication", initial["expected_hash"]))
            for profile in outlets:
                self.assertIsNone(promotions.get_latest_published_snapshot(profile))
            requests = set()
            for cycle in range(3):
                review = promotions.review_offer_change(saved["id"], action="activate", revision=saved["revision"])
                self.assertNotIn(review["expected_hash"], requests, f"Cycle {cycle} needs new intent")
                requests.add(review["expected_hash"])
                result = promotions.publish_reviewed_offer_change(saved["id"], review["expected_hash"], action="activate", revision=saved["revision"])
                self.assertEqual(result["status"], "published")
                self.assertEqual(len(result["outlets"]), 2)
                for profile in outlets:
                    snapshot = promotions.get_latest_published_snapshot(profile)
                    rows = json.loads(snapshot.snapshot_payload)["promotions"]
                    self.assertEqual([row["promotion_id"] for row in rows], [saved["id"]])
                    self.assertNotIn(other["id"], snapshot.snapshot_payload)
                stop = promotions.review_offer_change(saved["id"], action="stop")
                self.assertNotIn(stop["expected_hash"], requests)
                requests.add(stop["expected_hash"])
                promotions.publish_reviewed_offer_change(saved["id"], stop["expected_hash"], action="stop")
                for request_hash in requests:
                    receipt = frappe.get_doc("KoPOS Promotion Publication", request_hash)
                    retried = promotions.publish_reviewed_offer_change(saved["id"], request_hash, action=receipt.action, revision=saved["revision"])
                    self.assertEqual(retried["status"], "already_published")
                for profile in outlets:
                    self.assertEqual(json.loads(promotions.get_latest_published_snapshot(profile).snapshot_payload)["promotions"], [])

    def test_renamed_guided_templates_keep_identity_history_and_editability(self):
        from kopos_connector.api.promotions import get_latest_published_snapshot
        from kopos_connector.api.promotion_setup import (
            get_offer_editor, get_promotion_desk, preview_offer, review_saved_offer, save_offer_draft,
        )

        device, body, payload, item = self._persist_test_snapshot()
        identity = body["promotions"][0]["promotion_id"]
        original = frappe.get_doc("KoPOS Promotion", identity)
        snapshot = get_latest_published_snapshot(device.pos_profile)
        retained_bytes = snapshot.snapshot_payload
        before_count = frappe.db.count("KoPOS Promotion Snapshot")
        current = get_offer_editor(identity)
        for kind, value in (("percentage", "10"), ("amount", "1"), ("price", "5"),
                            ("free_second", "0"), ("half_second", "50")):
            with self.subTest(kind=kind):
                values = {**current["values"], "name": f"{identity} renamed {kind}", "kind": kind, "value": value}
                saved = save_offer_draft(values, identity, current["revision"])
                self.assertEqual(save_offer_draft(values, identity, current["revision"]), saved)
                current = get_offer_editor(identity)
                self.assertTrue(current["editable"])
                self.assertEqual(current["values"], values)
                stored = frappe.get_doc("KoPOS Promotion", identity)
                self.assertEqual(stored.name, identity)
                self.assertEqual(stored.promotion_name, original.promotion_name)
                self.assertEqual(stored.display_label, values["name"])
                self.assertEqual(stored.is_active, 0)
                row = next(row for row in get_promotion_desk()["offers"] if row["id"] == identity)
                self.assertEqual(row["name"], values["name"])
                self.assertTrue(row["editable"])
                self.assertTrue(row["has_draft_changes"])
                preview = preview_offer(values, device.pos_profile, [{"item": item["id"], "quantity": 2}], name=identity)
                self.assertEqual(preview["quote"]["status"], "complete")
                for credit in preview["quote"]["appliedPromotions"]:
                    self.assertEqual(credit["promotionId"], identity)
                    self.assertEqual(credit["promotionName"], values["name"])
                # Real readiness still reports the rollout/cost gates; rename
                # must not throw the contradictory "save in guided editor" error.
                self.assertIn("issues", review_saved_offer(identity, saved["revision"]))
                snapshot.reload()
                self.assertEqual(snapshot.snapshot_payload, retained_bytes)
                self.assertEqual(frappe.db.count("KoPOS Promotion Snapshot"), before_count)
        copied = save_offer_draft({**current["values"], "name": f"{identity} renamed copy"})
        self.assertNotEqual(copied["id"], identity)
        self.assertEqual(frappe.db.get_value("KoPOS Promotion", copied["id"], "is_active"), 0)

    def _persist_test_snapshot(self, *, current=False, kind="half_second", value="50"):
        """Test fixture only: internal persistence, not rollout approval."""
        from kopos_connector.api import promotions
        from kopos_connector.api.promotion_setup import save_offer_draft, _menu_for_profile
        from kopos_connector.pricing.adapter import snapshot_body
        from kopos_connector.pricing.runtime import calculate

        device_names = frappe.get_all("KoPOS Device", filters={"enabled": 1}, pluck="name", limit=1)
        self.assertTrue(device_names, "Seed an enabled test device first")
        device = frappe.get_doc("KoPOS Device", device_names[0])
        menu = _menu_for_profile(device.pos_profile)
        values = {**self._editor_payload(), "kind": kind, "value": value,
                  "items": [menu[0]["id"]], "outlets": [device.pos_profile],
                  "schedule": None if current else {"timeZone": "Asia/Kuala_Lumpur", "weekdays": [1, 2, 3, 4, 5, 6, 7],
                               "startDate": None, "endDate": None, "startMinute": 1320, "endMinute": 120}}
        saved = save_offer_draft(values)
        doc = frappe.get_doc("KoPOS Promotion", saved["id"])
        body = snapshot_body(device.pos_profile, [serialize_promotion(doc, device.pos_profile, kernel_version=2)],
                             [{"item_code": item["id"], "item_group": item["group"]} for item in menu])
        identity = calculate("identity", body)
        published_at = frappe.utils.now_datetime().isoformat() if current else "2030-01-01 00:00:00"
        payload = {**body, "snapshot_hash": identity["hash"], "snapshot_version": identity["version"],
                   "effective_from": published_at, "published_at": published_at}
        promotions.persist_reviewed_snapshot(device.pos_profile, payload, atomic=True)
        return device, body, payload, menu[0]

    def test_v2_templates_post_and_fully_refund_exact_native_accounting(self):
        """Real invoice/GL/settlement, not rollout or manager-authorization proof.

        Uses only transaction-local fixtures on the existing seeded device. No
        base-data seeder, stock replenishment or committed shop-data changes.
        Partial refunds remain an explicit unsupported product boundary.
        """
        from copy import deepcopy
        from decimal import Decimal
        from unittest.mock import patch
        from kopos_connector.api.fb_returns import process_return_payload
        from kopos_connector.kopos.api.fb_orders import submit_order_payload
        from kopos_connector.kopos.api.money_contract import persisted_money_to_sen
        from kopos_connector.kopos.tests.frappe_test_fixtures import build_sen_v1_sale_payload
        from kopos_connector.pricing.adapter import quote_snapshot

        for kind, value in (("percentage", "12.5"), ("amount", "1.01"),
                            ("price", "1.01"), ("free_second", "0"), ("half_second", "50")):
            with self.subTest(kind=kind):
                device, body, snapshot, item = self._persist_test_snapshot(current=True, kind=kind, value=value)
                profile = frappe.get_doc("POS Profile", device.pos_profile)
                shift = frappe.get_doc({
                    "doctype": "FB Shift", "shift_code": f"PROMO-GL-{frappe.generate_hash(length=10)}",
                    "device_id": device.device_id, "staff_id": frappe.session.user,
                    "warehouse": profile.warehouse, "company": profile.company,
                    "opening_float": 0, "status": "Open",
                }).insert(ignore_permissions=True)
                payload = build_sen_v1_sale_payload(shift, prefix="PROMO-GL")
                when = payload["order"]["created_at"]
                cart = [{"id": f"CART-{index}", "menuItemId": item["id"], "name": item["label"],
                         "quantity": qty, "unitPriceSen": item["price_sen"], "modifierTotalSen": 37,
                         "discountAmountSen": 5, "modifiers": []} for index, qty in enumerate((3, 2))]
                quote = quote_snapshot(body, cart, when)
                self.assertTrue(quote["appliedPromotions"])
                wire_items = [{
                    "line_id": f"SALE-{index}", "item_code": line["menuItemId"], "item_name": item["label"],
                    "qty": line["quantity"], "unit_price_sen": line["unitPriceSen"],
                    "modifier_total_sen": line["modifierTotalSen"], "discount_amount_sen": line["discountAmountSen"],
                    "line_total_sen": (line["unitPriceSen"] + line["modifierTotalSen"]) * line["quantity"] - line["discountAmountSen"],
                    "modifiers": [{"modifier_group": "Synthetic extras", "modifier": "Extra", "price_adjustment_sen": 37}],
                    "promotion_allocations": [{"promotion_id": credit["promotionId"], "amount_sen": credit["amountSen"],
                                               "quantity": credit["quantity"], "scope": credit["scope"]}
                                              for credit in line["promotionAllocations"]],
                } for index, line in enumerate(quote["items"])]
                total = sum(line["line_total_sen"] for line in wire_items)
                payload["order"].update({"items": wire_items, "subtotal_sen": total, "total_sen": total})
                payload["order"]["payments"][0].update({"amount_sen": total, "tendered_amount_sen": total, "change_amount_sen": 0})
                payload["offline_priced"] = True
                payload["pricing_context"] = {
                    "snapshot_hash": snapshot["snapshot_hash"], "snapshot_version": snapshot["snapshot_version"],
                    "pricing_mode": "offline_snapshot", "priced_at": when,
                    "offline_applied_promotion_ids": [row["promotionId"] for row in quote["appliedPromotions"]],
                    "promotion_expiry_by_id": {row["promotionId"]: None for row in quote["appliedPromotions"]},
                }
                payload["applied_promotions"] = [{
                    "promotion_id": row["promotionId"], "promotion_name": row["promotionName"],
                    "promotion_type": row["promotionType"], "scope": "order", "source": "snapshot",
                    "amount_sen": row["amountSen"], "offline_applied": True,
                    "snapshot_hash": snapshot["snapshot_hash"], "snapshot_version": snapshot["snapshot_version"],
                } for row in quote["appliedPromotions"]]
                from kopos_connector.kopos.services.accounting import sales_invoice_service
                with patch.object(sales_invoice_service, "_log_error", wraps=sales_invoice_service._log_error) as errors:
                    result = submit_order_payload(payload)
                self.assertEqual(result["status"], "ok", [(str(call.args[1])) for call in errors.call_args_list])
                self.assertEqual(submit_order_payload(deepcopy(payload))["sales_invoice"], result["sales_invoice"])
                invoice = frappe.get_doc("Sales Invoice", result["sales_invoice"])
                invoice.reload()
                self.assertEqual(invoice.docstatus, 1)
                self.assertEqual(invoice.update_stock, 0)
                self.assertEqual(persisted_money_to_sen(invoice.grand_total, "invoice total"), total)
                self.assertEqual(persisted_money_to_sen(invoice.outstanding_amount, "outstanding"), 0)
                self.assertEqual(invoice.custom_kopos_promotion_snapshot_hash, snapshot["snapshot_hash"])
                self.assertEqual(invoice.custom_kopos_promotion_reconciliation_status, "matched")
                self.assertEqual([p.custom_fb_source_payment_id for p in invoice.payments],
                                 [payload["order"]["payments"][0]["payment_id"]])
                self.assertEqual(frappe.db.count("Sales Invoice", {"custom_fb_idempotency_key": payload["idempotency_key"]}), 1)
                original_bytes = invoice.custom_kopos_promotion_payload
                original_lines = {row.name: (row.custom_fb_order_line_ref, row.custom_kopos_promotion_allocation,
                                            row.custom_kopos_modifiers) for row in invoice.items}
                for line in invoice.items:
                    wire = next(row for row in wire_items if row["line_id"] == line.custom_fb_order_line_ref)
                    self.assertEqual(persisted_money_to_sen(line.amount, "line amount"), wire["line_total_sen"])
                    self.assertEqual(json.loads(line.custom_kopos_promotion_allocation), wire["promotion_allocations"])
                return_id = f"PROMO-GL-RETURN-{frappe.generate_hash(length=12)}"
                refund = {"return_id": return_id, "idempotency_key": return_id, "device_id": device.device_id,
                          "fb_order": result["fb_order"], "original_sales_invoice": invoice.name,
                          "reason_code": "Quality Issue", "reason_text": "Synthetic promotion accounting check",
                          "refund_method": "cash", "return_to_stock": False}
                partial = {**refund, "lines": [{"original_sales_invoice_item": invoice.items[0].name, "qty_returned": 1}]}
                with self.assertRaisesRegex(frappe.ValidationError, "Partial ERP returns are not supported"):
                    process_return_payload(partial, require_manager_approval=False)
                self.assertFalse(frappe.db.exists("FB Return Event", {"return_id": return_id}))
                returned = process_return_payload(refund, require_manager_approval=False)
                self.assertEqual(returned["status"], "ok")
                self.assertEqual(returned["settlement_status"], "Posted")
                self.assertEqual(returned["reversal_stock_entries"], [])
                duplicate = process_return_payload(deepcopy(refund), require_manager_approval=False)
                self.assertEqual(duplicate["status"], "duplicate")
                for key in ("return_event", "return_sales_invoice", "settlement_doctype", "settlement_document"):
                    self.assertEqual(duplicate[key], returned[key])
                credit = frappe.get_doc("Sales Invoice", returned["return_sales_invoice"])
                credit.reload()
                self.assertEqual(credit.docstatus, 1)
                self.assertEqual(credit.is_return, 1)
                self.assertEqual(credit.return_against, invoice.name)
                self.assertEqual(credit.update_stock, 0)
                self.assertEqual(persisted_money_to_sen(credit.grand_total, "return total"), -total)
                self.assertEqual(persisted_money_to_sen(credit.outstanding_amount, "return outstanding"), 0)
                self.assertEqual(credit.custom_kopos_promotion_payload, original_bytes)
                for line in credit.items:
                    self.assertEqual((line.custom_fb_order_line_ref, line.custom_kopos_promotion_allocation,
                                      line.custom_kopos_modifiers), original_lines[line.sales_invoice_item])
                settlement = frappe.get_doc(returned["settlement_doctype"], returned["settlement_document"])
                self.assertEqual(settlement.docstatus, 1)
                self.assertEqual(settlement.custom_fb_return_event, returned["return_event"])
                for doctype, name in (("Sales Invoice", invoice.name), ("Sales Invoice", credit.name),
                                      (returned["settlement_doctype"], returned["settlement_document"])):
                    entries = frappe.get_all("GL Entry", filters={"voucher_type": doctype, "voucher_no": name, "is_cancelled": 0},
                                             fields=["debit", "credit", "debit_in_account_currency", "credit_in_account_currency"])
                    self.assertTrue(entries, f"Missing posted GL for {doctype} {name}")
                    for debit, credit_field in (("debit", "credit"), ("debit_in_account_currency", "credit_in_account_currency")):
                        self.assertEqual(sum(Decimal(str(row[debit])) - Decimal(str(row[credit_field])) for row in entries), Decimal(0))
                invoice.reload()
                self.assertEqual(invoice.custom_kopos_promotion_payload, original_bytes)

    def test_rounded_cash_full_refunds_reverse_native_gl_and_shift_cash(self):
        """Synthetic direct services; not manager/provider or rollout acceptance."""
        from copy import deepcopy
        from decimal import Decimal
        from unittest.mock import patch
        from kopos_connector.api.fb_returns import process_return_payload
        from kopos_connector.kopos.api.fb_orders import submit_order_payload
        from kopos_connector.kopos.api.money_contract import persisted_money_to_sen
        from kopos_connector.kopos.services.accounting import sales_invoice_service
        from kopos_connector.kopos.services.accounting.return_invoice_service import calculate_fb_shift_cash
        from kopos_connector.kopos.tests.frappe_test_fixtures import build_sen_v1_sale_payload

        from kopos_connector.install.install import ensure_native_change_ledger_entries

        ensure_native_change_ledger_entries()
        settings = frappe.get_single("POS Settings")
        self.assertEqual(settings.post_change_gl_entries, 1)
        modified = settings.modified
        ensure_native_change_ledger_entries()
        self.assertEqual(frappe.get_single("POS Settings").modified, modified)
        device = frappe.get_doc("KoPOS Device", frappe.get_all("KoPOS Device", filters={"enabled": 1}, pluck="name", limit=1)[0])
        profile = frappe.get_doc("POS Profile", device.pos_profile)
        shift = frappe.get_doc({"doctype": "FB Shift", "shift_code": "ROUNDING-" + frappe.generate_hash(length=10),
            "device_id": device.device_id, "staff_id": frappe.session.user, "warehouse": profile.warehouse,
            "company": profile.company, "opening_float": 0, "status": "Open"}).insert(ignore_permissions=True)
        total_sales = 0
        for rounding in (-2, -1, 0, 1, 2):
            for tender in ("exact", "change", "split_cash"):
                with self.subTest(rounding=rounding, tender=tender):
                    total = 3843 + rounding
                    paid = total if tender == "exact" else 5000
                    payload = build_sen_v1_sale_payload(shift, prefix="ROUNDING", quantity=3)
                    payload["order"]["items"][0].update({"discount_amount_sen": 7, "line_total_sen": 3593})
                    payload["order"].update({"subtotal_sen": 3593, "tax_amount_sen": 250,
                                             "rounding_adjustment_sen": rounding, "total_sen": total})
                    payments = payload["order"]["payments"]
                    payments[0].update({"amount_sen": total, "tendered_amount_sen": paid, "change_amount_sen": paid - total})
                    if tender == "split_cash":
                        payments.append({**payments[0], "payment_id": payments[0]["payment_id"] + "-SPLIT",
                                         "amount_sen": total - 1000, "tendered_amount_sen": paid - 1000})
                        payments[0].update({"amount_sen": 1000, "tendered_amount_sen": 1000, "change_amount_sen": 0})
                    with patch.object(sales_invoice_service, "_log_error", wraps=sales_invoice_service._log_error) as errors:
                        posted = submit_order_payload(payload)
                    self.assertEqual(posted["status"], "ok", [str(call.args[1]) for call in errors.call_args_list])
                    self.assertEqual(submit_order_payload(deepcopy(payload))["sales_invoice"], posted["sales_invoice"])
                    invoice = frappe.get_doc("Sales Invoice", posted["sales_invoice"])
                    self.assertEqual(invoice.docstatus, 1)
                    self.assertEqual(persisted_money_to_sen(invoice.grand_total, "grand total"), 3843)
                    self.assertEqual(persisted_money_to_sen(invoice.write_off_amount, "write off"), -rounding)
                    self.assertEqual(persisted_money_to_sen(invoice.paid_amount, "paid"), paid)
                    self.assertEqual(persisted_money_to_sen(invoice.change_amount, "change"), paid - total)
                    self.assertEqual(persisted_money_to_sen(invoice.outstanding_amount, "outstanding"), 0)
                    self.assertEqual([row.custom_fb_source_payment_id for row in invoice.payments],
                                     [row["payment_id"] for row in payments])
                    cash_account = invoice.payments[0].account
                    self.assertEqual(frappe.db.get_value("Account", cash_account, "account_type"), "Cash")
                    original_money = {field: invoice.get(field) for field in (
                        "grand_total", "write_off_amount", "paid_amount", "change_amount", "total_taxes_and_charges")}
                    total_sales += total
                    refund_id = "ROUNDING-RETURN-" + frappe.generate_hash(length=12)
                    refund = {"return_id": refund_id, "idempotency_key": refund_id, "device_id": device.device_id,
                        "fb_order": posted["fb_order"], "original_sales_invoice": invoice.name, "reason_code": "Quality Issue",
                        "reason_text": "Synthetic rounded cash refund", "refund_method": "cash", "return_to_stock": False}
                    returned = process_return_payload(refund, require_manager_approval=False)
                    self.assertEqual(returned["status"], "ok")
                    self.assertEqual(returned["settlement_status"], "Posted")
                    repeated = process_return_payload(deepcopy(refund), require_manager_approval=False)
                    self.assertEqual(repeated["status"], "duplicate")
                    for key in ("return_event", "return_sales_invoice", "settlement_document"):
                        self.assertEqual(repeated[key], returned[key])
                    credit = frappe.get_doc("Sales Invoice", returned["return_sales_invoice"])
                    self.assertEqual(credit.payments, [])
                    self.assertEqual(credit.custom_fb_order, invoice.custom_fb_order)
                    self.assertEqual(credit.update_outstanding_for_self, 1)
                    if rounding:
                        self.assertEqual(credit.write_off_account, invoice.write_off_account)
                        self.assertEqual(credit.write_off_cost_center, invoice.write_off_cost_center)
                    self.assertEqual(persisted_money_to_sen(credit.write_off_amount, "credit write off"), rounding)
                    self.assertEqual(persisted_money_to_sen(credit.grand_total, "credit total"), -3843)
                    self.assertEqual(persisted_money_to_sen(credit.outstanding_amount, "credit outstanding"), 0)
                    account_net = {}
                    for doctype, name, expected_cash in (("Sales Invoice", invoice.name, total),
                        ("Sales Invoice", credit.name, 0), (returned["settlement_doctype"], returned["settlement_document"], -total)):
                        doc = frappe.get_doc(doctype, name)
                        self.assertEqual(doc.docstatus, 1)
                        rows = frappe.get_all("GL Entry", filters={"voucher_type": doctype, "voucher_no": name, "is_cancelled": 0},
                            fields=["account", "debit", "credit", "debit_in_account_currency", "credit_in_account_currency"])
                        self.assertTrue(rows)
                        for debit, credit_field in (("debit", "credit"), ("debit_in_account_currency", "credit_in_account_currency")):
                            balances = [Decimal(str(row[debit])) - Decimal(str(row[credit_field])) for row in rows]
                            self.assertEqual(sum(balances), Decimal(0))
                            self.assertEqual(sum(value for row, value in zip(rows, balances) if row.account == cash_account),
                                             Decimal(expected_cash) / 100)
                            for row, balance in zip(rows, balances):
                                key = (row.account, debit)
                                account_net[key] = account_net.get(key, Decimal(0)) + balance
                    self.assertTrue(all(balance == 0 for balance in account_net.values()), account_net)
                    cash = calculate_fb_shift_cash(shift.name)
                    self.assertEqual(cash["cash_sales"], Decimal(total_sales) / 100)
                    self.assertEqual(cash["cash_refunds"], Decimal(total_sales) / 100)
                    self.assertEqual(cash["net_cash"], Decimal(0))
                    invoice.reload()
                    self.assertEqual({field: invoice.get(field) for field in original_money}, original_money)
                    self.assertEqual(persisted_money_to_sen(invoice.outstanding_amount, "original outstanding after refund"), 0)

    def test_native_invoice_rate_precision_installation_is_idempotent(self):
        from kopos_connector.install.install import ensure_sales_invoice_rate_precision

        def setters():
            return frappe.get_all("Property Setter", filters={"doc_type": "Sales Invoice Item"},
                                  fields=["name", "field_name", "property", "value", "modified"], order_by="name")

        amount_precision = {field: frappe.get_precision("Sales Invoice Item", field)
                            for field in ("amount", "net_amount", "base_amount", "base_net_amount")}
        ensure_sales_invoice_rate_precision()
        first = setters()
        ensure_sales_invoice_rate_precision()
        self.assertEqual(setters(), first)
        for field in ("rate", "net_rate", "base_rate", "base_net_rate"):
            self.assertGreaterEqual(frappe.get_precision("Sales Invoice Item", field), 6)
        self.assertEqual({field: frappe.get_precision("Sales Invoice Item", field) for field in amount_precision},
                         amount_precision)

    def test_native_fractional_rates_preserve_sen_for_supported_quantities(self):
        from decimal import Decimal
        from kopos_connector.kopos.api.money_contract import persisted_money_to_sen

        profile = frappe.get_doc("POS Profile", self._editor_payload()["outlets"][0])
        currency = frappe.db.get_value("Company", profile.company, "default_currency")
        for quantity in range(1, 101):
            for total_sen in (1, 1001, 3256):
                with self.subTest(quantity=quantity, total_sen=total_sen):
                    invoice = frappe.new_doc("Sales Invoice")
                    invoice.update({"company": profile.company, "currency": currency, "conversion_rate": 1,
                                    "ignore_pricing_rule": 1})
                    row = invoice.append("items", {"qty": quantity,
                        "rate": Decimal(total_sen) / Decimal(100) / Decimal(quantity)})
                    invoice.calculate_taxes_and_totals()
                    for field in ("amount", "net_amount", "base_amount", "base_net_amount"):
                        self.assertEqual(persisted_money_to_sen(row.get(field), field), total_sen)

    def test_exact_device_ack_is_durable_coalesced_and_permission_checked(self):
        from unittest.mock import patch
        from kopos_connector.api import promotions, devices

        device, body, payload, _ = self._persist_test_snapshot()
        args = {"device_id": device.device_id, "kernel_hash": body["kernel_hash"],
                "snapshot_hash": payload["snapshot_hash"], "snapshot_version": payload["snapshot_version"]}
        self.assertEqual(promotions.report_device_promotion_state(**args)["status"], "received")
        device.reload()
        self.assertEqual(device.promotion_snapshot_hash, payload["snapshot_hash"])
        self.assertEqual(device.promotion_snapshot_version, payload["snapshot_version"])
        timestamp = device.promotion_received_at
        self.assertIsNotNone(timestamp)
        with patch.object(devices, "lock_device_for_operational_mutation", side_effect=AssertionError("Unchanged ack must not lock or write")):
            self.assertEqual(promotions.report_device_promotion_state(**args)["status"], "received")
            self.assertEqual(promotions.report_device_promotion_state(**{**args, "snapshot_hash": "b" * 64})["status"], "not_current")
        device.reload()
        self.assertEqual(device.promotion_received_at, timestamp)
        with self.assertRaises(frappe.ValidationError):
            promotions.report_device_promotion_state(**{**args, "snapshot_version": None})
        with self.assertRaises(frappe.ValidationError):
            promotions.get_promotion_snapshot_payload(device_id=device.device_id)
        self.assertEqual(promotions.get_promotion_snapshot_payload(device_id=device.device_id, supported_kernel_hash=body["kernel_hash"])["snapshot_hash"], payload["snapshot_hash"])
        previous = frappe.session.user
        try:
            frappe.set_user("Guest")
            with self.assertRaises(frappe.ValidationError):
                promotions.report_device_promotion_state(**args)
        finally:
            frappe.set_user(previous)

    def test_retained_v2_snapshot_replays_sale_line_ids_and_rejects_tampering(self):
        from copy import deepcopy
        from kopos_connector.pricing.adapter import quote_snapshot
        from kopos_connector.kopos.api.fb_orders import _validate_published_promotion_snapshot

        device, body, payload, item = self._persist_test_snapshot()
        when = "2030-01-02T01:00:00+08:00"
        cart = [{"id": line_id, "menuItemId": item["id"], "name": item["label"], "quantity": qty,
                 "unitPriceSen": item["price_sen"], "modifierTotalSen": 37, "modifiers": [],
                 "discountAmountSen": 5} for line_id, qty in (("CART-Z", 3), ("CART-A", 2))]
        quote = quote_snapshot(body, cart, when)
        self.assertTrue(quote["appliedPromotions"])
        normalized = {"device_id": device.device_id, "sale_datetime": when, "offline_priced": True,
                      "promotion_reconciliation_status": "matched", "pricing_context": {
                          "snapshot_hash": payload["snapshot_hash"], "snapshot_version": payload["snapshot_version"],
                          "pricing_mode": "offline_snapshot", "priced_at": when,
                          "promotion_expiry_by_id": {row["promotionId"]: None for row in quote["appliedPromotions"]}},
                      "applied_promotions": [{"promotion_id": row["promotionId"], "promotion_name": row["promotionName"],
                          "promotion_type": row["promotionType"], "scope": "order", "amount_sen": row["amountSen"],
                          "offline_applied": True} for row in quote["appliedPromotions"]],
                      "items": [{"line_id": f"SALE-{index}", "item_code": line["menuItemId"], "qty": line["quantity"],
                          "unit_price_sen": line["unitPriceSen"], "modifier_total_sen": line["modifierTotalSen"],
                          "discount_amount_sen": line["discountAmountSen"], "promotion_allocations": [
                              {"promotion_id": credit["promotionId"], "amount_sen": credit["amountSen"],
                               "quantity": credit["quantity"], "scope": credit["scope"]}
                              for credit in line["promotionAllocations"]]} for index, line in enumerate(quote["items"])]}
        evidence = _validate_published_promotion_snapshot(normalized)
        self.assertEqual(evidence["reconciliation"]["status"], "matched")
        tampered = deepcopy(normalized)
        tampered["applied_promotions"][0]["amount_sen"] += 1
        with self.assertRaises(frappe.ValidationError):
            _validate_published_promotion_snapshot(tampered)
        ended = deepcopy(normalized)
        ended["sale_datetime"] = ended["pricing_context"]["priced_at"] = "2030-01-02T02:00:00+08:00"
        with self.assertRaises(frappe.ValidationError):
            _validate_published_promotion_snapshot(ended)

    def test_reloaded_dates_hash_like_frappe_wire_values(self) -> None:
        for microsecond in (None, 0, 123456):
            with self.subTest(microsecond=microsecond):
                promotion = frappe.get_doc(
                    {
                        "doctype": "KoPOS Promotion",
                        "promotion_name": f"KOPOS-DATE-{frappe.generate_hash(length=10)}",
                        "promotion_type": "item_discount",
                        "is_active": 0,
                        "offline_allowed": 0,
                        "discount_type": "percentage",
                        "discount_value": 10,
                        "valid_from": None if microsecond is None else datetime(
                            2026, 9, 8, microsecond=microsecond
                        ),
                        "valid_upto": None if microsecond is None else datetime(
                            2026, 9, 9, microsecond=microsecond
                        ),
                    }
                ).insert(ignore_permissions=True)
                promotion.reload()
                if microsecond is not None:
                    self.assertIsInstance(promotion.valid_from, datetime)
                    self.assertIsInstance(promotion.valid_upto, datetime)
                    self.assertEqual(promotion.valid_from.microsecond, microsecond)
                    self.assertEqual(promotion.valid_upto.microsecond, microsecond)

                row = serialize_promotion(promotion, "KOPOS-DATE-TEST")
                wire_dates = json.loads(frappe.as_json({
                    "valid_from": promotion.valid_from,
                    "valid_upto": promotion.valid_upto,
                }))
                self.assertEqual(row["valid_from"], wire_dates["valid_from"])
                self.assertEqual(row["valid_upto"], wire_dates["valid_upto"])
                body = {"pos_profile": "KOPOS-DATE-TEST", "promotions": [row]}
                self.assertEqual(
                    compute_snapshot_content_hash(body),
                    compute_snapshot_content_hash(json.loads(frappe.as_json(body))),
                )

    def test_guided_offer_preview_and_inactive_save_round_trip(self) -> None:
        from kopos_connector.api.promotion_setup import preview_offer_setup

        items = frappe.get_all("Item", filters={"disabled": 0, "is_sales_item": 1}, pluck="name", limit=1)
        profiles = frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name", limit=1)
        self.assertTrue(items, "Seed a saleable item before running the real-service promotion test")
        self.assertTrue(profiles, "Seed an outlet before running the real-service promotion test")
        before_promotions = frappe.db.count("KoPOS Promotion")
        before_snapshots = frappe.db.count("KoPOS Promotion Snapshot")
        payload = {
            "name": f"GUIDED-TEST-{frappe.generate_hash(length=10)}",
            "kind": "half_second", "value": "0", "items": items, "outlets": profiles,
            "repeat": "repeat", "activation": "automatic",
            "start": "2030-01-01 10:00:00", "end": "2030-01-02 18:00:00",
            "example_price": "10.00", "example_qty": 4,
        }
        preview = preview_offer_setup(json.dumps(payload))
        self.assertEqual(preview["item_labels"][items[0]], frappe.db.get_value("Item", items[0], "item_name") or items[0])
        self.assertEqual(frappe.get_meta("KoPOS Promotion").title_field, "promotion_name")
        self.assertEqual(preview["example"]["discount_sen"], 1000)
        self.assertEqual(preview["example"]["total_sen"], 3000)
        self.assertEqual(frappe.db.count("KoPOS Promotion"), before_promotions)
        self.assertEqual(frappe.db.count("KoPOS Promotion Snapshot"), before_snapshots)
        # Ordinary document permissions and validation, not a privileged insert.
        promotion = frappe.get_doc({"doctype": "KoPOS Promotion", **preview["fields"]}).insert()
        promotion.reload()
        self.assertEqual(promotion.is_active, 0)
        self.assertEqual(promotion.eligible_items[0].item_code, items[0])
        self.assertEqual(promotion.eligible_pos_profiles[0].pos_profile, profiles[0])
        self.assertEqual(promotion.discount_value, 50)
        self.assertEqual(promotion.repeat_mode, "repeat")
        self.assertEqual(promotion.economics_status, "Not checked")
        self.assertIsInstance(promotion.valid_from, datetime)
        self.assertEqual(frappe.db.count("KoPOS Promotion Snapshot"), before_snapshots)
        self.assertEqual(frappe.new_doc("KoPOS Promotion").is_active, 0)

        # An explicitly enabled future offer is publishable now, but its
        # existing start-date eligibility guard must still reject it now.
        from kopos_connector.api.promotions import get_active_promotions

        promotion.is_active = 1
        promotion.save()
        before_start = datetime(2029, 12, 31)
        self.assertNotIn(promotion.name, [row.name for row in get_active_promotions(profiles[0], before_start)])
        self.assertIn(promotion.name, [row.name for row in get_active_promotions(profiles[0], before_start, include_scheduled=True)])
        self.assertNotIn(promotion.name, [row.name for row in get_active_promotions(profiles[0], datetime(2030, 1, 3), include_scheduled=True)])

    def test_guided_preview_rejects_guest(self) -> None:
        from kopos_connector.api.promotion_setup import preview_offer_setup

        previous_user = frappe.session.user
        try:
            frappe.set_user("Guest")
            with self.assertRaises(frappe.PermissionError):
                preview_offer_setup("{}")
        finally:
            frappe.set_user(previous_user)
