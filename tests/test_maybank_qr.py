from __future__ import annotations

import importlib
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

api_module = importlib.import_module("kopos_connector.api")
devices_module = importlib.import_module("kopos_connector.api.devices")
maybank_qr = importlib.import_module("kopos_connector.api.maybank_qr")
maybank_contract = importlib.import_module(
    "kopos_connector.api._maybank_qr_contract"
)
maybank_generation = importlib.import_module(
    "kopos_connector.api._maybank_qr_generation"
)
maybank_persistence = importlib.import_module(
    "kopos_connector.api._maybank_qr_persistence"
)
maybank_rate_limit = importlib.import_module(
    "kopos_connector.api._maybank_qr_rate_limit"
)
maybank_resolution = importlib.import_module(
    "kopos_connector.api._maybank_qr_resolution"
)
maybank_status = importlib.import_module("kopos_connector.api._maybank_qr_status")
poll_maybank = importlib.import_module("kopos_connector.tasks.poll_maybank")
diagnostics = importlib.import_module("kopos_connector.utils.diagnostics")
maybank_client = importlib.import_module("kopos_connector.services.maybank.client")


PREPARED_FB_ORDER = "FB-ORDER-PREPARED-1"
PREPARED_FB_ORDER_PAYMENT = "FBPAY-PREPARED-1"
PREPARED_SALE_FINGERPRINT = "a" * 64


def _prepared_sale() -> dict[str, str]:
    return {
        "fb_order": PREPARED_FB_ORDER,
        "fb_order_payment": PREPARED_FB_ORDER_PAYMENT,
        "accepted_sale_fingerprint": PREPARED_SALE_FINGERPRINT,
        "payment_method": "DuitNow QR",
        "company": "Test Company",
        "currency": "MYR",
    }


def _prepared_request_fingerprint(
    device_id: str,
    idempotency_key: str,
    amount_sen: int,
) -> str:
    return maybank_qr._request_fingerprint(
        device_id,
        idempotency_key,
        fb_order=PREPARED_FB_ORDER,
        fb_order_payment=PREPARED_FB_ORDER_PAYMENT,
        accepted_sale_fingerprint=PREPARED_SALE_FINGERPRINT,
        amount_sen=amount_sen,
        currency="MYR",
    )


class MaybankQrStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        suspense_account = patch.object(
            maybank_generation,
            "resolve_manual_qr_suspense_account",
            return_value="Manual QR Suspense - TC",
        )
        suspense_account.start()
        self.addCleanup(suspense_account.stop)
        verified_qr_account = patch.object(
            maybank_generation,
            "resolve_verified_qr_settlement_account",
            return_value={"account": "QR Clearing - TC", "type": "Bank"},
        )
        verified_qr_account.start()
        self.addCleanup(verified_qr_account.stop)

    def test_provider_device_identity_uses_small_samsung_tab_a11(self):
        self.assertEqual(maybank_client.DEVICE_NAME, "Samsung Galaxy Tab A11 Small")
        self.assertEqual(maybank_client.DEVICE_OS, "Android")

    def test_generate_qr_inserts_transaction(self):
        client = Mock()
        client.outlet_id = "outlet-B"
        client.generate_qr.return_value = {
            "status": "QR000",
            "data": [
                {
                    "transaction_refno": "ref-1",
                    "qr_data": "0002010102110011BR123QDSAMR01",
                    "expires_in_seconds": 120,
                }
            ],
        }

        txn_doc = Mock()

        def insert_reservation(**kwargs):
            self.assertEqual(client.generate_qr.call_count, 0)

        txn_doc.insert.side_effect = insert_reservation
        inserted_docs: list[dict] = []
        reservation = {
            "name": "MBQR-REQUEST",
            "transaction_refno": "REQUEST-PLACEHOLDER",
            "status": "creating",
            "qr_data": None,
            "sale_amount": "10.00",
            "sale_amount_sen": 1000,
            "device_id": "device-1",
            "fb_order": PREPARED_FB_ORDER,
            "fb_order_payment": PREPARED_FB_ORDER_PAYMENT,
            "request_fingerprint": "f" * 64,
            "poll_count": 0,
        }

        def capture_get_doc(doc):
            if isinstance(doc, dict):
                inserted_docs.append(doc)
            return txn_doc

        def load_reservation(request_fingerprint: str):
            reservation["request_fingerprint"] = request_fingerprint
            reservation["transaction_refno"] = maybank_qr._reservation_reference(
                request_fingerprint
            )
            return reservation

        with (
            patch.object(maybank_generation, "_check_rate_limit"),
            patch.object(
                maybank_generation,
                "_load_prepared_automatic_qr_sale",
                return_value=_prepared_sale(),
            ),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=None),
            patch.object(
                maybank_qr.frappe,
                "get_doc",
                side_effect=capture_get_doc,
            ),
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_generation,
                "_load_reserved_txn_with_order_lock",
                side_effect=load_reservation,
            ),
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
            patch.object(maybank_qr.frappe.db, "commit") as commit,
            patch.object(
                maybank_generation,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
        ):
            result = maybank_qr.generate_maybank_qr_payload(
                {
                    "amount_sen": 1000,
                    "device_id": "device-1",
                    "idempotency_key": "key-1",
                }
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["qr_data"], "0002010102110011BR123QDSAMR01")
        self.assertEqual(result["transaction_refno"], "ref-1")
        self.assertEqual(result["sale_amount_sen"], 1000)
        self.assertEqual(result["sale_amount"], "10.00")
        self.assertEqual(result["fb_order"], PREPARED_FB_ORDER)
        self.assertEqual(result["fb_order_payment"], PREPARED_FB_ORDER_PAYMENT)
        client.generate_qr.assert_called_once_with("10.00")

        self.assertEqual(len(inserted_docs), 1)
        self.assertEqual(inserted_docs[0]["provider"], "maybank_qr")
        self.assertEqual(inserted_docs[0]["outlet_id"], "outlet-B")
        self.assertEqual(inserted_docs[0]["currency"], "MYR")
        self.assertEqual(inserted_docs[0]["company"], "Test Company")
        self.assertEqual(inserted_docs[0]["business_date"], "2026-03-13")
        self.assertEqual(len(inserted_docs[0]["request_fingerprint"]), 64)
        self.assertEqual(inserted_docs[0]["status"], "creating")
        self.assertEqual(commit.call_count, 2)
        txn_doc.save.assert_not_called()
        persisted_updates = set_value.call_args.args[2]
        self.assertEqual(persisted_updates["status"], "pending")
        self.assertEqual(persisted_updates["transaction_refno"], "ref-1")
        raw_response = persisted_updates["raw_response"]
        self.assertIn("[redacted]", raw_response)
        self.assertNotIn("0002010102110011BR123QDSAMR01", raw_response)

    def test_runtime_lookup_rejects_ambiguous_device_idempotency_rows(self):
        rows = [
            {"name": "MBQR-1", "device_id": "device-1"},
            {"name": "MBQR-2", "device_id": "device-1"},
        ]
        with patch.object(
            maybank_qr.frappe.db,
            "sql",
            return_value=rows,
        ) as sql:
            with self.assertRaisesRegex(
                maybank_qr.frappe.ValidationError,
                "evidence is ambiguous",
            ):
                maybank_qr._load_existing_txn("device-1", "key-1")

        self.assertIn("LIMIT 2", sql.call_args.args[0])
        self.assertIn("FOR UPDATE", sql.call_args.args[0])
        self.assertEqual(sql.call_args.args[1], ("device-1", "key-1"))

    def test_generate_qr_rejects_excessive_amount(self):
        with self.assertRaises(maybank_qr.frappe.ValidationError):
            maybank_qr.generate_maybank_qr_payload(
                {
                    "amount_sen": 10_000_001,
                    "device_id": "device-1",
                    "idempotency_key": "key-1",
                }
            )

    def test_generate_qr_fences_unsafe_verified_qr_account_before_provider(self):
        rejection = {"status": "preflight_rejected", "provider_called": False}
        with (
            patch.object(
                maybank_generation,
                "_load_prepared_automatic_qr_sale",
                return_value=_prepared_sale(),
            ),
            patch.object(maybank_generation, "_load_existing_txn", return_value=None),
            patch.object(
                maybank_generation,
                "resolve_verified_qr_settlement_account",
                side_effect=maybank_qr.frappe.ValidationError(
                    "Verified QR settlement account cannot be Cash"
                ),
            ),
            patch.object(
                maybank_generation,
                "_register_preflight_rejection_fence",
                return_value=rejection,
            ) as register_fence,
            patch.object(maybank_qr.MaybankClient, "from_settings") as client,
        ):
            result = maybank_qr.generate_maybank_qr_payload(
                {
                    "amount_sen": 1000,
                    "device_id": "device-1",
                    "idempotency_key": "unsafe-account-key",
                    "fb_order": PREPARED_FB_ORDER,
                    "fb_order_payment": PREPARED_FB_ORDER_PAYMENT,
                    "accepted_sale_fingerprint": PREPARED_SALE_FINGERPRINT,
                }
            )

        self.assertEqual(result, rejection)
        register_fence.assert_called_once()
        client.assert_not_called()

    def test_generate_qr_rejects_provider_reference_in_static_namespace(self):
        client = Mock()
        client.generate_qr.return_value = {
            "status": "QR000",
            "data": [
                {
                    "transaction_refno": "static-provider-collision",
                    "qr_data": "QR-DATA",
                }
            ],
        }

        with self.assertRaisesRegex(
            maybank_qr.frappe.ValidationError,
            "static QR namespace",
        ):
            maybank_qr._generate_qr_payload(
                client,
                "10.00",
                datetime(2026, 3, 13, 18, 5, 0),
            )

    def test_generate_qr_rejects_whitespace_provider_reference(self):
        client = Mock()
        client.generate_qr.return_value = {
            "status": "QR000",
            "data": [
                {
                    "transaction_refno": " ref-1 ",
                    "qr_data": "QR-DATA",
                }
            ],
        }

        with self.assertRaisesRegex(
            maybank_qr.frappe.ValidationError,
            "nonempty exact value",
        ):
            maybank_qr._generate_qr_payload(
                client,
                "10.00",
                datetime(2026, 3, 13, 18, 5, 0),
            )

    def test_generate_qr_rejects_provider_amount_mismatch(self):
        client = Mock()
        client.generate_qr.return_value = {
            "status": "QR000",
            "data": [
                {
                    "transaction_refno": "ref-1",
                    "qr_data": "QR-DATA",
                    "sale_amount": "9.99",
                }
            ],
        }

        with self.assertRaisesRegex(
            maybank_qr.frappe.ValidationError,
            "does not match the prepared sale",
        ):
            maybank_qr._generate_qr_payload(
                client,
                "10.00",
                datetime(2026, 3, 13, 18, 5, 0),
            )

    def test_diagnostics_redacts_tokens_pins_qr_and_provider_payloads(self):
        payload = {
            "api_key": "api-key-secret",
            "api_secret": "api-secret-value",
            "authorization": "Bearer live-token",
            "pin_hash": "pin-hash-value",
            "data": [
                {
                    "qr_data": "0002010102110011BR123QDSAMR01",
                    "transaction_refno": "ref-1",
                }
            ],
            "nested": {"raw_response": {"token": "provider-token"}},
        }

        redacted = diagnostics.redacted_json(payload)

        self.assertIn("[redacted]", redacted)
        for secret in [
            "api-key-secret",
            "api-secret-value",
            "Bearer live-token",
            "pin-hash-value",
            "0002010102110011BR123QDSAMR01",
            "provider-token",
        ]:
            self.assertNotIn(secret, redacted)
        self.assertIn("ref-1", redacted)

    def test_generate_qr_rejects_zero_amount(self):
        with self.assertRaises(maybank_qr.frappe.ValidationError):
            maybank_qr.generate_maybank_qr_payload(
                {
                    "amount_sen": 0,
                    "device_id": "device-1",
                    "idempotency_key": "key-1",
                }
            )

    def test_generate_qr_rejects_non_numeric_amount(self):
        with self.assertRaises(maybank_qr.frappe.ValidationError) as error:
            maybank_qr.generate_maybank_qr_payload(
                {
                    "amount_sen": "abc",
                    "device_id": "device-1",
                    "idempotency_key": "key-bad-amount",
                }
            )

        self.assertIn("amount_sen must be an integer", str(error.exception))

    def test_generate_qr_preserves_expired_transaction_evidence(self):
        existing = SimpleNamespace(
            name="txn-expired",
            transaction_refno="ref-expired",
            status="timeout",
            qr_data="000201010211",
            sale_amount="10.00",
            sale_amount_sen=1000,
            expires_at=datetime(2026, 3, 13, 18, 4, 0),
            device_id="device-1",
        )

        delete_calls: list[object] = []

        def fake_sql(sql, *args, **kwargs):
            if "DELETE" in sql:
                delete_calls.append(args[0] if args else "")

        with (
            patch.object(
                maybank_status,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
            patch.object(maybank_status, "_load_existing_txn", return_value=existing),
            patch.object(maybank_qr.frappe.db, "sql", side_effect=fake_sql),
            patch.object(maybank_generation, "_check_rate_limit"),
        ):
            with self.assertRaises(maybank_qr.frappe.ValidationError) as error:
                maybank_qr._resolve_existing_txn(
                    "device-1",
                    "key-expired",
                    1000,
                    datetime(2026, 3, 13, 18, 5, 0),
                )

        self.assertEqual(str(error.exception), maybank_qr.USED_IDEMPOTENCY_MESSAGE)
        self.assertEqual(len(delete_calls), 0)

    def test_generate_qr_rejects_paid_existing_transaction_without_deleting_it(self):
        existing = SimpleNamespace(
            name="txn-paid",
            transaction_refno="ref-paid",
            status="paid",
            qr_data="000201010211",
            sale_amount="10.00",
            sale_amount_sen=1000,
            expires_at=datetime(2026, 3, 13, 18, 10, 0),
            device_id="device-1",
        )

        delete_calls: list[tuple] = []

        def fake_sql(sql, *args, **kwargs):
            if "DELETE" in sql:
                delete_calls.append(args)

        with (
            patch.object(
                maybank_status,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
            patch.object(maybank_status, "_load_existing_txn", return_value=existing),
            patch.object(maybank_qr.frappe.db, "sql", side_effect=fake_sql),
            patch.object(maybank_generation, "_check_rate_limit"),
        ):
            with self.assertRaises(maybank_qr.frappe.ValidationError) as error:
                maybank_qr._resolve_existing_txn(
                    "device-1", "key-paid", 1000, datetime(2026, 3, 13, 18, 5, 0)
                )

        self.assertEqual(str(error.exception), maybank_qr.PAID_TRANSACTION_MESSAGE)
        self.assertEqual(len(delete_calls), 0)

    def test_generate_qr_reuses_live_scanned_transaction(self):
        request_fingerprint = _prepared_request_fingerprint(
            "device-1", "key-scanned", 1000
        )
        existing = SimpleNamespace(
            name="txn-scanned",
            transaction_refno="ref-scanned",
            status="scanned",
            qr_data="000201010211SCANNED",
            sale_amount="999.99",
            sale_amount_sen=1000,
            expires_at=datetime(2026, 3, 13, 18, 10, 0),
            device_id="device-1",
            company="Test Company",
            fb_order=PREPARED_FB_ORDER,
            fb_order_payment=PREPARED_FB_ORDER_PAYMENT,
            request_fingerprint=request_fingerprint,
        )

        with (
            patch.object(
                maybank_generation,
                "_load_prepared_automatic_qr_sale",
                return_value=_prepared_sale(),
            ),
            patch.object(
                maybank_generation,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
            patch.object(maybank_generation, "_load_existing_txn", return_value=existing),
            patch.object(maybank_generation, "_check_rate_limit"),
        ):
            result = maybank_qr.generate_maybank_qr_payload(
                {
                    "amount_sen": 1000,
                    "device_id": "device-1",
                    "idempotency_key": "key-scanned",
                }
            )

        self.assertEqual(result["transaction_refno"], "ref-scanned")
        self.assertEqual(result["qr_data"], "000201010211SCANNED")
        self.assertEqual(result["sale_amount_sen"], 1000)
        self.assertEqual(result["sale_amount"], "10.00")

    def test_generate_qr_rejects_existing_amount_mismatch(self):
        existing = {
            "name": "txn-mismatch",
            "transaction_refno": "ref-mismatch",
            "status": "pending",
            "qr_data": "000201010211MISMATCH",
            "sale_amount": "10.00",
            "sale_amount_sen": 1000,
            "expires_at": datetime(2026, 3, 13, 18, 10, 0),
            "device_id": "device-1",
        }

        with patch.object(maybank_status, "_load_existing_txn", return_value=existing):
            with self.assertRaises(maybank_qr.frappe.ValidationError) as error:
                maybank_qr._resolve_existing_txn(
                    "device-1",
                    "key-mismatch",
                    1200,
                    datetime(2026, 3, 13, 18, 5, 0),
                )

        self.assertIn("amount does not match", str(error.exception))

    def test_generate_qr_raises_for_provider_error(self):
        client = Mock()
        client.outlet_id = "outlet-1"
        client.generate_qr.return_value = {
            "status": "QR999",
            "text": "Downstream error",
        }

        with (
            patch.object(maybank_generation, "_check_rate_limit"),
            patch.object(
                maybank_generation,
                "_load_prepared_automatic_qr_sale",
                return_value=_prepared_sale(),
            ),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=None),
            patch.object(maybank_qr.frappe, "get_doc", return_value=Mock()),
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_generation,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
        ):
            with self.assertRaises(maybank_qr.frappe.ValidationError) as error:
                maybank_qr.generate_maybank_qr_payload(
                    {
                        "amount_sen": 1000,
                        "device_id": "device-1",
                        "idempotency_key": "key-provider-error",
                    }
                )

        self.assertIn("Maybank QR generation failed", str(error.exception))

    def test_generate_qr_raises_for_empty_data(self):
        client = Mock()
        client.outlet_id = "outlet-1"
        client.generate_qr.return_value = {"status": "QR000", "data": []}

        with (
            patch.object(maybank_generation, "_check_rate_limit"),
            patch.object(
                maybank_generation,
                "_load_prepared_automatic_qr_sale",
                return_value=_prepared_sale(),
            ),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=None),
            patch.object(maybank_qr.frappe, "get_doc", return_value=Mock()),
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_generation,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
        ):
            with self.assertRaises(maybank_qr.frappe.ValidationError) as error:
                maybank_qr.generate_maybank_qr_payload(
                    {
                        "amount_sen": 1000,
                        "device_id": "device-1",
                        "idempotency_key": "key-empty-data",
                    }
                )

        self.assertIn("empty data", str(error.exception).lower())

    def test_generate_qr_never_calls_provider_twice_after_duplicate_insert(self):
        duplicate_error = maybank_qr.frappe.DuplicateEntryError("duplicate")
        client = Mock()
        client.outlet_id = "outlet-1"
        client.generate_qr.return_value = {
            "status": "QR000",
            "data": [{"transaction_refno": "ref-dup-1", "qr_data": "QR-1"}],
        }

        first_doc = Mock()
        first_doc.insert.side_effect = duplicate_error
        with (
            patch.object(maybank_generation, "_check_rate_limit"),
            patch.object(
                maybank_generation,
                "_load_prepared_automatic_qr_sale",
                return_value=_prepared_sale(),
            ),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=None),
            patch.object(
                maybank_generation, "_resolve_existing_txn", return_value=None
            ) as resolve_existing,
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_qr.frappe,
                "get_doc",
                return_value=first_doc,
            ),
            patch.object(
                maybank_generation,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
        ):
            with self.assertRaises(maybank_qr.frappe.DuplicateEntryError):
                maybank_qr.generate_maybank_qr_payload(
                    {
                        "amount_sen": 1000,
                        "device_id": "device-1",
                        "idempotency_key": "key-duplicate-retry",
                    }
                )

        self.assertEqual(client.generate_qr.call_count, 0)
        first_doc.save.assert_not_called()
        self.assertEqual(resolve_existing.call_count, 2)

    def test_concurrent_loser_reuses_winning_reservation_without_provider_call(self):
        duplicate_error = maybank_qr.frappe.DuplicateEntryError("duplicate")
        client = Mock()
        client.outlet_id = "outlet-1"
        reservation = Mock()
        reservation.insert.side_effect = duplicate_error
        winning_transaction = {
            "name": "MBQR-WINNER",
            "transaction_refno": "ref-winner",
            "status": "pending",
            "qr_data": "QR-WINNER",
            "sale_amount": "10.00",
            "sale_amount_sen": 1000,
            "expires_at": datetime(2026, 3, 13, 18, 6, 0),
            "device_id": "device-1",
            "fb_order": PREPARED_FB_ORDER,
            "fb_order_payment": PREPARED_FB_ORDER_PAYMENT,
        }

        with (
            patch.object(maybank_generation, "_check_rate_limit"),
            patch.object(
                maybank_generation,
                "_load_prepared_automatic_qr_sale",
                return_value=_prepared_sale(),
            ),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=None),
            patch.object(
                maybank_generation,
                "_load_reserved_txn_for_update",
                return_value=winning_transaction,
            ),
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(maybank_qr.frappe, "get_doc", return_value=reservation),
            patch.object(
                maybank_generation,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
        ):
            result = maybank_qr.generate_maybank_qr_payload(
                {
                    "amount_sen": 1000,
                    "device_id": "device-1",
                    "idempotency_key": "key-concurrent",
                }
            )

        self.assertEqual(result["transaction_refno"], "ref-winner")
        self.assertEqual(result["qr_data"], "QR-WINNER")
        self.assertEqual(result["sale_amount_sen"], 1000)
        self.assertEqual(result["sale_amount"], "10.00")
        client.generate_qr.assert_not_called()
        reservation.save.assert_not_called()

    def test_check_payment_polls_live_status_for_pending_rows(self):
        txn = Mock()
        txn.transaction_refno = "ref-1"
        txn.status = "pending"
        txn.last_polled_at = datetime(2026, 3, 13, 18, 3, 0)
        txn.created_at = datetime(2026, 3, 13, 18, 0, 0)
        txn.device_id = "device-1"
        txn.sale_amount = "10.00"
        txn.sale_amount_sen = 1000
        txn.outlet_id = "outlet-1"
        txn.currency = "MYR"
        txn.provider = "maybank_qr"
        txn.paid_at = None
        persisted_txn = SimpleNamespace(
            transaction_refno="ref-1",
            status="paid",
            device_id="device-1",
            sale_amount="999.99",
            sale_amount_sen=1000,
            currency="MYR",
            provider="maybank_qr",
            paid_at=datetime(2026, 3, 13, 18, 6, 0),
        )

        client = Mock()
        client.check_status.return_value = {
            "status": "QR000",
            "data": [
                {"status": 1, "transaction_refno": "ref-1", "sale_amount": "10.00"}
            ],
        }

        with (
            patch.object(
                maybank_qr.frappe.db,
                "get_value",
                return_value="txn-1",
            ),
            patch.object(
                maybank_qr.frappe,
                "get_doc",
                side_effect=[txn, persisted_txn],
            ),
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_status,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
            patch.object(maybank_status, "_poll_txn_status", return_value="paid") as poll,
        ):
            result = maybank_qr.check_maybank_payment_payload("ref-1", "device-1")

        poll.assert_called_once_with(txn)
        self.assertEqual(result["status"], "paid")
        self.assertEqual(result["transaction_refno"], "ref-1")
        self.assertEqual(result["sale_amount_sen"], 1000)
        self.assertEqual(result["sale_amount"], "10.00")
        self.assertIsNotNone(result["paid_at"])

    def test_check_payment_rejects_persisted_reference_drift(self):
        txn = SimpleNamespace(
            transaction_refno="ref-other",
            status="pending",
            last_polled_at=datetime(2026, 3, 13, 18, 6, 0),
            created_at=datetime(2026, 3, 13, 18, 6, 0),
            device_id="device-1",
            sale_amount="10.00",
            sale_amount_sen=1000,
            currency="MYR",
            provider="maybank_qr",
            paid_at=None,
        )
        with (
            patch.object(maybank_qr.frappe.db, "get_value", return_value="txn-1"),
            patch.object(maybank_qr.frappe, "get_doc", return_value=txn),
            patch.object(
                maybank_status,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
        ):
            with self.assertRaisesRegex(
                maybank_qr.frappe.ValidationError,
                "does not match the requested session",
            ):
                maybank_qr.check_maybank_payment_payload("ref-1", "device-1")

    def test_check_payment_rejects_fractional_persisted_sen(self):
        txn = SimpleNamespace(
            transaction_refno="ref-1",
            status="pending",
            last_polled_at=datetime(2026, 3, 13, 18, 6, 0),
            created_at=datetime(2026, 3, 13, 18, 6, 0),
            device_id="device-1",
            sale_amount="10.00",
            sale_amount_sen="1000.5",
            currency="MYR",
            provider="maybank_qr",
            paid_at=None,
        )
        with (
            patch.object(maybank_qr.frappe.db, "get_value", return_value="txn-1"),
            patch.object(maybank_qr.frappe, "get_doc", return_value=txn),
            patch.object(
                maybank_status,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
        ):
            with self.assertRaisesRegex(
                maybank_qr.frappe.ValidationError,
                "sale_amount_sen must be an integer",
            ):
                maybank_qr.check_maybank_payment_payload("ref-1", "device-1")

    def test_check_payment_endpoint_uses_authenticated_device_scope(self):
        api_module.frappe.local.response = {}
        api_module.frappe.flags = SimpleNamespace()
        device = SimpleNamespace(
            name="KOPOS-DEVICE-1",
            device_id="device-1",
            config_version=7,
        )

        with (
            patch.object(api_module, "require_kopos_api_access"),
            patch.object(
                api_module, "get_authenticated_device_doc", return_value=device
            ),
            patch.object(
                api_module,
                "require_device_operational_scope",
                return_value=(device, SimpleNamespace()),
            ) as require_scope,
            patch.object(
                api_module,
                "lock_device_for_operational_mutation",
                return_value=device,
            ) as lock_device,
            patch.object(
                maybank_qr,
                "check_maybank_payment_payload",
                return_value={"status": "pending", "transaction_refno": "ref-1"},
            ) as payload,
        ):
            api_module.check_maybank_payment(transaction_refno="ref-1")

        payload.assert_called_once_with(transaction_refno="ref-1", device_id="device-1")
        require_scope.assert_called_once_with("device-1", currency="MYR")
        lock_device.assert_called_once_with(device_id="device-1")
        self.assertTrue(api_module.frappe.flags.commit)

    def test_check_payment_endpoint_uses_explicit_device_context(self):
        api_module.frappe.local.response = {}
        device = SimpleNamespace(
            name="KOPOS-DEVICE-2",
            device_id="device-2",
            config_version=8,
        )

        with (
            patch.object(api_module, "require_kopos_api_access"),
            patch.object(
                api_module,
                "require_device_operational_scope",
                return_value=(device, SimpleNamespace()),
            ) as require_scope,
            patch.object(
                api_module,
                "lock_device_for_operational_mutation",
                return_value=device,
            ) as lock_device,
            patch.object(
                maybank_qr,
                "check_maybank_payment_payload",
                return_value={"status": "pending", "transaction_refno": "ref-2"},
            ) as payload,
        ):
            api_module.check_maybank_payment(
                transaction_refno="ref-2", device_id="device-2"
            )

        lock_device.assert_called_once_with(device_id="device-2")
        require_scope.assert_called_once_with("device-2", currency="MYR")
        payload.assert_called_once_with(transaction_refno="ref-2", device_id="device-2")

    def test_generation_revalidates_device_only_after_provider_evidence_is_committed(self):
        api_module.frappe.local.response = {}
        initial_device = SimpleNamespace(
            name="KOPOS-DEVICE-1",
            device_id="device-1",
            config_version=7,
        )
        changed_device = SimpleNamespace(
            name="KOPOS-DEVICE-1",
            device_id="device-1",
            config_version=8,
        )
        events: list[str] = []

        def provider_call(_payload: dict) -> dict:
            events.append("provider")
            return {"status": "ok", "transaction_refno": "ref-1", "qr_data": "QR"}

        def commit() -> None:
            events.append("commit")

        def device_lock(*, device_id: str):
            events.append("device-lock")
            self.assertEqual(device_id, "device-1")
            return changed_device

        with (
            patch.object(
                api_module,
                "_get_submit_payload",
                return_value={
                    "device_id": "device-1",
                    "amount_sen": 1000,
                    "idempotency_key": "key-1",
                },
            ),
            patch.object(
                api_module,
                "require_device_operational_scope",
                return_value=(initial_device, SimpleNamespace()),
            ),
            patch.object(
                maybank_qr,
                "generate_maybank_qr_payload",
                side_effect=provider_call,
            ),
            patch.object(api_module.frappe.db, "commit", side_effect=commit),
            patch.object(
                api_module,
                "lock_device_for_operational_mutation",
                side_effect=device_lock,
            ),
        ):
            api_module.generate_maybank_qr()

        self.assertEqual(events, ["provider", "commit", "device-lock"])
        self.assertEqual(api_module.frappe.local.response["status"], "error")
        self.assertIn(
            "authority changed",
            api_module.frappe.local.response["message"],
        )

    def test_ambiguous_generation_resolution_is_system_manager_only(self):
        api_module.frappe.local.response = {}
        with (
            patch.object(
                api_module,
                "require_system_manager",
                side_effect=api_module.frappe.ValidationError(
                    "Only a System Manager can perform this action"
                ),
            ),
            patch.object(
                maybank_qr,
                "resolve_maybank_qr_generation_payload",
            ) as resolve_payload,
        ):
            api_module.resolve_maybank_qr_generation(
                transaction_name="MBQR-1",
                resolution="provider_transaction_absent",
            )

        resolve_payload.assert_not_called()
        self.assertEqual(api_module.frappe.local.response["status"], "error")
        self.assertIn(
            "Only a System Manager",
            api_module.frappe.local.response["message"],
        )

    def test_ambiguous_generation_resolution_rejects_dual_role_device_session(self):
        api_module.frappe.local.response = {}
        with (
            patch.object(api_module, "require_system_manager"),
            patch.object(
                api_module,
                "get_session_roles",
                return_value={"System Manager", devices_module.KOPOS_DEVICE_API_ROLE},
            ),
            patch.object(
                maybank_qr,
                "resolve_maybank_qr_generation_payload",
            ) as resolve_payload,
        ):
            api_module.resolve_maybank_qr_generation(
                transaction_name="MBQR-1",
                resolution="provider_transaction_absent",
            )

        resolve_payload.assert_not_called()
        self.assertEqual(api_module.frappe.local.response["status"], "error")
        self.assertIn(
            "non-device System Manager",
            api_module.frappe.local.response["message"],
        )

    def test_authenticated_device_doc_resolves_and_caches_single_mapping(self):
        devices_module.frappe.session = SimpleNamespace(user="device-user@example.com")
        devices_module.frappe.flags = SimpleNamespace()

        with (
            patch.object(
                devices_module,
                "get_session_roles",
                return_value={devices_module.KOPOS_DEVICE_API_ROLE},
            ),
            patch.object(
                devices_module.frappe,
                "get_all",
                return_value=[{"name": "KOPOS-DEVICE-1"}],
            ),
            patch.object(
                devices_module,
                "get_device_doc",
                return_value=SimpleNamespace(
                    name="KOPOS-DEVICE-1",
                    device_id="device-1",
                    api_user="device-user@example.com",
                ),
            ) as get_device_doc,
        ):
            device = devices_module.get_authenticated_device_doc()

        self.assertEqual(device.device_id, "device-1")
        self.assertEqual(devices_module.frappe.flags.kopos_device.device_id, "device-1")
        get_device_doc.assert_called_once_with(name="KOPOS-DEVICE-1")

    def test_authenticated_device_doc_rejects_ambiguous_mapping(self):
        devices_module.frappe.session = SimpleNamespace(user="device-user@example.com")
        devices_module.frappe.flags = SimpleNamespace()

        with (
            patch.object(
                devices_module,
                "get_session_roles",
                return_value={devices_module.KOPOS_DEVICE_API_ROLE},
            ),
            patch.object(
                devices_module.frappe,
                "get_all",
                return_value=[
                    {"name": "KOPOS-DEVICE-1"},
                    {"name": "KOPOS-DEVICE-2"},
                ],
            ),
        ):
            with self.assertRaises(devices_module.frappe.ValidationError) as error:
                devices_module.get_authenticated_device_doc()

        self.assertIn("multiple KoPOS Devices", str(error.exception))

    def test_expired_scheduler_keeps_reconciling_provider_pending_status(self):
        client = Mock()
        client.check_status.return_value = {
            "status": "QR000",
            "data": [
                {
                    "status": 2,
                    "transaction_refno": "ref-2",
                    "sale_amount": "10.00",
                }
            ],
        }

        txn = SimpleNamespace(
            name="txn-2",
            transaction_refno="ref-2",
            status="pending",
            last_polled_at=datetime(2026, 3, 13, 18, 4, 0),
            created_at=datetime(2026, 3, 13, 18, 4, 0),
            expires_at=datetime(2026, 3, 13, 18, 5, 0),
            poll_count=2,
            sale_amount_sen=1000,
            outlet_id="outlet-1",
            currency="MYR",
            device_id="device-1",
            provider="maybank_qr",
        )

        with (
            patch.object(
                poll_maybank,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
            patch.object(
                poll_maybank, "_apply_provider_poll_result", return_value="pending"
            ) as apply_result,
            patch.object(poll_maybank.frappe.db, "sql") as sql_update,
        ):
            poll_maybank._poll_single(client, txn)

        client.check_status.assert_called_once_with("ref-2")
        apply_result.assert_called_once_with(
            "txn-2", client.check_status.return_value
        )
        sql_update.assert_not_called()

    def test_stale_loader_selects_due_rows_past_grace(self):
        with (
            patch.object(
                poll_maybank.frappe,
                "get_all",
                return_value=[
                    SimpleNamespace(
                        name="txn-stale",
                        transaction_refno="ref-stale",
                        status="pending",
                        last_polled_at=datetime(2026, 3, 13, 18, 4, 0),
                        created_at=datetime(2026, 3, 13, 18, 0, 0),
                        expires_at=datetime(2026, 3, 13, 18, 5, 0),
                        poll_count=1,
                    )
                ],
            ),
        ):
            selected = poll_maybank._load_due_stale_transactions(
                datetime(2026, 3, 13, 18, 6, 0)
            )

        self.assertEqual([row.name for row in selected], ["txn-stale"])

    def test_poll_lock_contention_skips_scheduler_work(self):
        redis_client = Mock()
        redis_client.set.return_value = False
        cache = SimpleNamespace(redis_client=lambda: redis_client)

        with (
            patch.object(poll_maybank.frappe, "cache", return_value=cache),
            patch.object(poll_maybank.frappe, "get_all") as get_all,
        ):
            poll_maybank.poll_pending_maybank_transactions()

        get_all.assert_not_called()

    def test_poll_lock_without_atomic_redis_skips_scheduler_work(self):
        cache = SimpleNamespace(redis_client=lambda: None)

        with (
            patch.object(poll_maybank.frappe, "cache", return_value=cache),
            patch.object(poll_maybank.frappe, "log_error") as log_error,
            patch.object(poll_maybank.frappe, "get_all") as get_all,
        ):
            poll_maybank.poll_pending_maybank_transactions()

        get_all.assert_not_called()
        log_error.assert_called_once()

    def test_scheduler_separates_active_qrs_from_long_tail_backlog(self):
        redis_client = Mock()
        redis_client.set.return_value = True
        redis_client.eval.return_value = 1
        cache = SimpleNamespace(redis_client=lambda: redis_client)

        with (
            patch.object(poll_maybank.frappe, "cache", return_value=cache),
            patch.object(
                poll_maybank.frappe,
                "get_all",
                side_effect=[[], [], []],
            ) as get_all,
            patch.object(
                poll_maybank,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
        ):
            poll_maybank.poll_pending_maybank_transactions()

        self.assertEqual(get_all.call_count, 3)
        scanned_call, pending_call, stale_call = get_all.call_args_list
        self.assertEqual(scanned_call.kwargs["filters"]["status"], "scanned")
        self.assertEqual(pending_call.kwargs["filters"]["status"], "pending")
        self.assertEqual(scanned_call.kwargs["filters"]["expires_at"][0], ">")
        self.assertEqual(pending_call.kwargs["filters"]["expires_at"][0], ">")
        self.assertEqual(stale_call.kwargs["filters"]["expires_at"][0], "<=")
        self.assertTrue(
            stale_call.kwargs["order_by"].startswith("last_polled_at asc")
        )
        self.assertTrue(
            scanned_call.kwargs["order_by"].startswith("last_polled_at asc")
        )
        release_calls = [
            call
            for call in redis_client.eval.call_args_list
            if call.args[0] == poll_maybank.LOCK_RELEASE_SCRIPT
        ]
        self.assertEqual(len(release_calls), 1)

    def test_poll_single_touches_row_on_provider_failure(self):
        txn = SimpleNamespace(
            name="txn-error",
            transaction_refno="ref-error",
            status="pending",
            last_polled_at=None,
            created_at=datetime(2026, 3, 13, 18, 4, 0),
            expires_at=datetime(2026, 3, 13, 18, 6, 0),
            poll_count=0,
        )
        client = Mock()
        client.check_status.side_effect = RuntimeError("network down")

        with (
            patch.object(
                poll_maybank,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
            patch.object(poll_maybank.frappe.db, "sql") as sql_update,
        ):
            with self.assertRaises(RuntimeError):
                poll_maybank._poll_single(client, txn)

        sql_statement = sql_update.call_args.args[0]
        self.assertIn("last_polled_at", sql_statement)

    def test_poll_single_does_not_create_local_timeout_from_pending_provider(self):
        txn = SimpleNamespace(
            name="txn-timeout",
            transaction_refno="ref-timeout",
            status="pending",
            last_polled_at=datetime(2026, 3, 13, 18, 5, 0),
            created_at=datetime(2026, 3, 13, 18, 4, 0),
            expires_at=datetime(2026, 3, 13, 18, 5, 1),
            poll_count=0,
        )
        client = Mock()
        client.check_status.return_value = {
            "status": "QR000",
            "data": [
                {
                    "status": 2,
                    "transaction_refno": "ref-timeout",
                    "sale_amount": "10.00",
                }
            ],
        }
        txn.sale_amount_sen = 1000
        txn.outlet_id = "outlet-1"
        txn.currency = "MYR"
        txn.device_id = "device-1"
        txn.provider = "maybank_qr"

        with (
            patch.object(
                poll_maybank,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
            patch.object(
                poll_maybank, "_apply_provider_poll_result", return_value="pending"
            ) as apply_result,
        ):
            poll_maybank._poll_single(client, txn)

        apply_result.assert_called_once_with(
            "txn-timeout", client.check_status.return_value
        )

    def test_poll_rejects_mismatched_provider_response_without_status_change(self):
        txn = SimpleNamespace(
            name="txn-mismatch",
            transaction_refno="ref-expected",
            status="pending",
            last_polled_at=None,
            created_at=datetime(2026, 3, 13, 18, 4, 0),
            expires_at=datetime(2026, 3, 13, 18, 6, 0),
            poll_count=0,
            sale_amount_sen=1000,
            outlet_id="outlet-1",
            currency="MYR",
            device_id="device-1",
            provider="maybank_qr",
        )
        client = Mock()
        client.check_status.return_value = {
            "status": "QR000",
            "data": [
                {
                    "status": 1,
                    "transaction_refno": "ref-other",
                    "sale_amount": "10.00",
                }
            ],
        }

        with patch.object(
            poll_maybank,
            "_apply_provider_poll_result",
            side_effect=poll_maybank.frappe.ValidationError("reference mismatch"),
        ) as apply_result:
            with self.assertRaises(poll_maybank.frappe.ValidationError):
                poll_maybank._poll_single(client, txn)

        apply_result.assert_called_once_with(
            "txn-mismatch", client.check_status.return_value
        )

    def test_poll_rejects_provider_amount_mismatch_without_status_change(self):
        txn = SimpleNamespace(
            name="txn-amount-mismatch",
            transaction_refno="ref-amount",
            status="pending",
            last_polled_at=None,
            created_at=datetime(2026, 3, 13, 18, 4, 0),
            expires_at=datetime(2026, 3, 13, 18, 6, 0),
            poll_count=0,
            sale_amount_sen=1000,
            outlet_id="outlet-1",
            currency="MYR",
            device_id="device-1",
            provider="maybank_qr",
        )
        client = Mock()
        client.check_status.return_value = {
            "status": "QR000",
            "data": [
                {
                    "status": 1,
                    "transaction_refno": "ref-amount",
                    "sale_amount": "9.99",
                }
            ],
        }

        with patch.object(
            poll_maybank,
            "_apply_provider_poll_result",
            side_effect=poll_maybank.frappe.ValidationError("amount mismatch"),
        ) as apply_result:
            with self.assertRaises(poll_maybank.frappe.ValidationError):
                poll_maybank._poll_single(client, txn)

        apply_result.assert_called_once_with(
            "txn-amount-mismatch", client.check_status.return_value
        )

    def test_paid_is_terminal_when_stale_pending_response_finishes_late(self):
        paid = {
            "name": "txn-paid",
            "transaction_refno": "ref-paid",
            "status": "paid",
            "poll_count": 4,
            "paid_at": datetime(2026, 3, 13, 18, 5, 0),
            "scanned_at": datetime(2026, 3, 13, 18, 4, 0),
        }
        stale_response = {"status": "QR000", "data": [{"status": 2}]}
        with (
            patch.object(maybank_status, "_record_poll_attempt") as record_attempt,
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
        ):
            status = maybank_qr._transition_txn_status_locked(
                paid,
                "pending",
                2,
                stale_response,
            )

        self.assertEqual(status, "paid")
        set_value.assert_not_called()
        record_attempt.assert_called_once_with("txn-paid", stale_response)

    def test_scanned_cannot_regress_to_pending(self):
        scanned = {
            "name": "txn-scanned",
            "transaction_refno": "ref-scanned",
            "status": "scanned",
            "poll_count": 2,
            "scanned_at": datetime(2026, 3, 13, 18, 4, 0),
        }
        with (
            patch.object(maybank_status, "_record_poll_attempt") as record_attempt,
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
        ):
            status = maybank_qr._transition_txn_status_locked(
                scanned,
                "pending",
                2,
                {"status": "QR000"},
            )

        self.assertEqual(status, "scanned")
        set_value.assert_not_called()
        record_attempt.assert_called_once()

    def test_pending_to_paid_is_locked_then_later_stale_result_is_ignored(self):
        pending = {
            "name": "txn-race",
            "transaction_refno": "ref-race",
            "status": "pending",
            "poll_count": 1,
            "paid_at": None,
            "scanned_at": None,
        }
        paid_view = {**pending, "status": "paid", "paid_at": datetime(2026, 3, 13, 18, 6, 0)}
        txn_doc = SimpleNamespace(
            transaction_refno="ref-race",
            owner="device-user@example.com",
        )
        with (
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
            patch.object(maybank_qr.frappe, "get_doc", return_value=txn_doc),
            patch.object(
                maybank_qr.frappe,
                "publish_realtime",
                create=True,
            ) as publish,
            patch.object(maybank_status, "_record_poll_attempt") as record_attempt,
            patch.object(
                maybank_status,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
        ):
            first = maybank_qr._transition_txn_status_locked(
                pending,
                "paid",
                1,
                {"status": "QR000", "data": [{"status": 1}]},
            )
            second = maybank_qr._transition_txn_status_locked(
                paid_view,
                "pending",
                2,
                {"status": "QR000", "data": [{"status": 2}]},
            )

        self.assertEqual((first, second), ("paid", "paid"))
        self.assertEqual(set_value.call_count, 1)
        self.assertEqual(set_value.call_args.args[2]["status"], "paid")
        self.assertEqual(set_value.call_args.args[2]["poll_count"], 2)
        publish.assert_called_once()
        self.assertTrue(publish.call_args.kwargs["after_commit"])
        record_attempt.assert_called_once()

    def test_provider_identity_is_checked_against_freshly_locked_row(self):
        locked = {
            "name": "txn-locked",
            "transaction_refno": "ref-correct",
            "status": "pending",
            "poll_count": 1,
            "sale_amount_sen": 1000,
            "outlet_id": "outlet-1",
            "currency": "MYR",
            "device_id": "device-1",
            "provider": "maybank_qr",
        }
        response = {
            "status": "QR000",
            "data": [
                {
                    "status": 1,
                    "transaction_refno": "ref-stale-object",
                    "sale_amount": "10.00",
                }
            ],
        }
        with (
            patch.object(maybank_status, "_load_txn_for_update", return_value=locked),
            patch.object(maybank_status, "_record_poll_attempt") as record_attempt,
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
        ):
            with self.assertRaisesRegex(
                maybank_qr.frappe.ValidationError,
                "reference does not match",
            ):
                maybank_qr._apply_provider_poll_result("txn-locked", response)

        set_value.assert_not_called()
        record_attempt.assert_called_once_with("txn-locked", response)

    def test_creating_reservation_never_replays_provider_and_escalates_when_stale(self):
        existing = {
            "name": "MBQR-AMBIGUOUS",
            "transaction_refno": "REQUEST-ABC",
            "status": "creating",
            "sale_amount_sen": 1000,
            "device_id": "device-1",
            "request_fingerprint": "a" * 64,
            "created_at": datetime(2026, 3, 13, 18, 0, 0),
        }
        active = maybank_qr._resolve_existing_txn(
            "device-1",
            "key-1",
            1000,
            datetime(2026, 3, 13, 18, 1, 0),
            existing=existing,
        )
        stale = maybank_qr._resolve_existing_txn(
            "device-1",
            "key-1",
            1000,
            datetime(2026, 3, 13, 18, 3, 0),
            existing=existing,
        )

        self.assertEqual(active["status"], "creating")
        self.assertEqual(stale["status"], "generation_ambiguous")
        self.assertTrue(active["provider_replay_blocked"])
        self.assertTrue(stale["provider_replay_blocked"])
        self.assertEqual(stale["recovery_action"], "resolve_maybank_qr_generation")

    def test_audited_abandonment_requires_elapsed_safety_window(self):
        locked = {
            "name": "MBQR-AMBIGUOUS",
            "status": "creating",
            "created_at": datetime(2026, 3, 13, 18, 0, 0),
        }
        with (
            patch.object(maybank_resolution, "_load_txn_for_update", return_value=locked),
            patch.object(
                maybank_resolution,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 16, 0),
            ),
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
            patch.object(maybank_resolution, "_audit_generation_resolution") as audit,
        ):
            result = maybank_qr._abandon_ambiguous_generation(
                "MBQR-AMBIGUOUS",
                confirmation=maybank_qr.CREATION_ABANDON_CONFIRMATION,
                reason="Provider portal confirms no transaction exists",
                evidence_reference="support-case-1234",
            )

        self.assertEqual(result["status"], "generation_abandoned")
        self.assertTrue(result["new_generation_authorized"])
        self.assertEqual(set_value.call_args.args[2]["status"], "unknown")
        audit.assert_called_once()

    def test_expired_pending_stays_reconcilable_until_audited_staff_resolution(self):
        locked = {
            "name": "MBQR-EXPIRED",
            "transaction_refno": "provider-ref-expired",
            "status": "pending",
            "expires_at": datetime(2026, 3, 12, 18, 0, 0),
        }
        with (
            patch.object(maybank_resolution, "_load_txn_for_update", return_value=locked),
            patch.object(
                maybank_resolution,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 0, 1),
            ),
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
            patch.object(maybank_resolution, "_audit_generation_resolution") as audit,
        ):
            result = maybank_qr._close_expired_reconciliation(
                "MBQR-EXPIRED",
                "provider-ref-expired",
                confirmation=maybank_qr.PENDING_RECONCILIATION_CONFIRMATION,
                reason="Provider portal confirms the QR was cancelled unpaid",
                evidence_reference="provider-export-20260313-row-42",
            )

        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["new_generation_authorized"])
        updates = set_value.call_args.args[2]
        self.assertEqual(updates["status"], "timeout")
        self.assertIsNone(updates["maybank_status"])
        audit.assert_called_once()

    def test_expired_pending_cannot_be_closed_during_late_settlement_window(self):
        locked = {
            "name": "MBQR-RECENTLY-EXPIRED",
            "transaction_refno": "provider-ref-recent",
            "status": "pending",
            "expires_at": datetime(2026, 3, 13, 17, 0, 0),
        }
        with (
            patch.object(maybank_resolution, "_load_txn_for_update", return_value=locked),
            patch.object(
                maybank_resolution,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 0, 0),
            ),
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
            patch.object(maybank_resolution, "_audit_generation_resolution") as audit,
        ):
            with self.assertRaisesRegex(
                maybank_qr.frappe.ValidationError,
                "late-settlement reconciliation window",
            ):
                maybank_qr._close_expired_reconciliation(
                    "MBQR-RECENTLY-EXPIRED",
                    "provider-ref-recent",
                    confirmation=maybank_qr.PENDING_RECONCILIATION_CONFIRMATION,
                    reason="Provider portal currently shows no payment",
                    evidence_reference="provider-export-current",
                )

        set_value.assert_not_called()
        audit.assert_not_called()

    def test_late_original_provider_result_stays_fenced_after_audited_release(self):
        abandoned = {
            "name": "MBQR-LATE",
            "transaction_refno": maybank_qr._reservation_reference("b" * 64),
            "status": "unknown",
            "sale_amount": "10.00",
            "sale_amount_sen": 1000,
            "request_fingerprint": "b" * 64,
            "fb_order": PREPARED_FB_ORDER,
            "fb_order_payment": PREPARED_FB_ORDER_PAYMENT,
        }
        with (
            patch.object(
                maybank_generation,
                "_load_reserved_txn_with_order_lock",
                return_value=abandoned,
            ),
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
            patch.object(maybank_generation, "_audit_generation_resolution") as audit,
            patch.object(maybank_qr.frappe, "log_error"),
        ):
            result = maybank_qr._finalize_reserved_generation(
                "b" * 64,
                result={"status": "QR000"},
                transaction_refno="provider-ref-late",
                qr_data="QR-LATE",
                expires_at=datetime(2026, 3, 13, 18, 20, 0),
            )

        self.assertEqual(result["status"], "late_provider_result_fenced")
        self.assertFalse(result["display_authorized"])
        self.assertEqual(result["settlement_status"], "pending_reconciliation")
        self.assertNotIn("qr_data", result)
        self.assertNotIn("status", set_value.call_args.args[2])
        self.assertNotIn("maybank_status", set_value.call_args.args[2])
        self.assertEqual(
            set_value.call_args.args[2]["transaction_refno"],
            "provider-ref-late",
        )
        audit.assert_called_once()

    def test_support_resolution_checks_provider_before_locking_and_records_paid(self):
        snapshot = {
            "name": "MBQR-SUPPORT",
            "transaction_refno": maybank_qr._reservation_reference("c" * 64),
            "status": "creating",
            "sale_amount_sen": 1000,
            "outlet_id": "outlet-1",
            "currency": "MYR",
            "device_id": "device-1",
            "provider": "maybank_qr",
            "request_fingerprint": "c" * 64,
            "poll_count": 0,
            "paid_at": None,
            "scanned_at": None,
        }
        client = Mock()
        events: list[str] = []

        def check_status(reference: str) -> dict:
            events.append("provider")
            self.assertEqual(reference, "provider-ref-support")
            return {
                "status": "QR000",
                "data": [
                    {
                        "status": 1,
                        "transaction_refno": reference,
                        "sale_amount": "10.00",
                        "outlet_id": "outlet-1",
                        "currency": "MYR",
                    }
                ],
            }

        client.check_status.side_effect = check_status

        def lock_row(_name: str):
            events.append("row-lock")
            return snapshot

        with (
            patch.object(
                maybank_resolution,
                "_load_generation_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                maybank_resolution.MaybankClient,
                "from_settings",
                return_value=client,
            ),
            patch.object(maybank_resolution, "_load_txn_for_update", side_effect=lock_row),
            patch.object(maybank_qr.frappe.db, "set_value") as set_value,
            patch.object(maybank_resolution, "_audit_generation_resolution") as audit,
            patch.object(
                maybank_resolution,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 20, 0),
            ),
        ):
            result = maybank_qr._resolve_generation_with_provider_reference(
                "MBQR-SUPPORT",
                "provider-ref-support",
                reason="Matched the transaction in the provider portal",
                evidence_reference="support-case-9876",
            )

        self.assertEqual(events, ["provider", "row-lock"])
        self.assertEqual(result["status"], "paid")
        updates = set_value.call_args.args[2]
        self.assertEqual(updates["transaction_refno"], "provider-ref-support")
        self.assertEqual(updates["status"], "paid")
        self.assertIsNotNone(updates["paid_at"])
        audit.assert_called_once()

    def test_qr_rate_limit_is_atomic_per_device_and_provider_outlet(self):
        calls: list[tuple] = []

        class AtomicCache:
            @staticmethod
            def make_key(key: str) -> str:
                return key

            @staticmethod
            def eval(*args):
                calls.append(args)
                return [11, 1]

        with (
            patch.object(maybank_rate_limit.frappe, "cache", return_value=AtomicCache()),
            patch.object(
                maybank_rate_limit.frappe,
                "conf",
                {"maybank_qr_per_device_per_minute": 10},
                create=True,
            ),
        ):
            with self.assertRaisesRegex(
                maybank_qr.frappe.ValidationError,
                "request limit exceeded",
            ):
                maybank_rate_limit._check_rate_limit("device-1", "outlet-1")

        self.assertEqual(calls[0][1], 2)
        self.assertNotIn("device-1", calls[0][2])
        self.assertNotIn("outlet-1", calls[0][3])
        self.assertEqual(calls[0][5:], (10, 120))

    def test_device_limited_qr_calls_do_not_increment_outlet_counter(self):
        script = maybank_rate_limit.QR_RATE_LIMIT_SCRIPT
        device_guard = script.index("if device_count >= device_limit then")
        device_rejection = script.index(
            "return {device_limit + 1, outlet_count}",
            device_guard,
        )
        outlet_increment = script.index("redis.call('INCR', KEYS[2])")

        self.assertLess(device_guard, device_rejection)
        self.assertLess(device_rejection, outlet_increment)

    def test_qr_rate_limit_fails_closed_without_atomic_redis(self):
        with patch.object(
            maybank_rate_limit.frappe,
            "cache",
            return_value=SimpleNamespace(),
        ):
            with self.assertRaisesRegex(
                maybank_qr.frappe.ValidationError,
                "temporarily unavailable",
            ):
                maybank_rate_limit._check_rate_limit("device-1", "outlet-1")

    def test_expired_polling_uses_bounded_long_tail_intervals(self):
        txn = SimpleNamespace(
            status="pending",
            expires_at=datetime(2026, 3, 13, 18, 0, 0),
            poll_count=100,
        )
        self.assertEqual(
            poll_maybank._minimum_poll_interval_seconds(
                txn,
                datetime(2026, 3, 13, 18, 30, 0),
            ),
            poll_maybank.EXPIRED_POLL_INTERVAL_SECONDS,
        )
        self.assertEqual(
            poll_maybank._minimum_poll_interval_seconds(
                txn,
                datetime(2026, 3, 13, 20, 0, 0),
            ),
            poll_maybank.EXPIRED_LONG_TAIL_INTERVAL_SECONDS,
        )
        self.assertEqual(
            poll_maybank._minimum_poll_interval_seconds(
                txn,
                datetime(2026, 3, 15, 18, 0, 0),
            ),
            poll_maybank.EXPIRED_ARCHIVE_INTERVAL_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
