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
poll_maybank = importlib.import_module("kopos_connector.tasks.poll_maybank")


class MaybankQrStatusTests(unittest.TestCase):
    def test_generate_qr_inserts_transaction(self):
        client = Mock()
        client.outlet_id = "outlet-1"
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
        txn_doc.insert.return_value = None

        with (
            patch.object(maybank_qr, "_check_rate_limit"),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=None),
            patch.object(
                maybank_qr.frappe,
                "get_doc",
                return_value=txn_doc,
            ),
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_qr,
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
        client.generate_qr.assert_called_once_with("10.00")

    def test_generate_qr_rejects_excessive_amount(self):
        with self.assertRaises(maybank_qr.frappe.ValidationError):
            maybank_qr.generate_maybank_qr_payload(
                {
                    "amount_sen": 10_000_001,
                    "device_id": "device-1",
                    "idempotency_key": "key-1",
                }
            )

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

        self.assertIn("amount_sen must be between", str(error.exception))

    def test_generate_qr_deletes_expired_transaction(self):
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
                maybank_qr,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=existing),
            patch.object(maybank_qr.frappe.db, "sql", side_effect=fake_sql),
            patch.object(maybank_qr, "_check_rate_limit"),
        ):
            result = maybank_qr._resolve_existing_txn(
                "device-1", "key-expired", 1000, datetime(2026, 3, 13, 18, 5, 0)
            )

        self.assertIsNone(result)
        self.assertEqual(len(delete_calls), 1)

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
                maybank_qr,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=existing),
            patch.object(maybank_qr.frappe.db, "sql", side_effect=fake_sql),
            patch.object(maybank_qr, "_check_rate_limit"),
        ):
            with self.assertRaises(maybank_qr.frappe.ValidationError) as error:
                maybank_qr._resolve_existing_txn(
                    "device-1", "key-paid", 1000, datetime(2026, 3, 13, 18, 5, 0)
                )

        self.assertEqual(str(error.exception), maybank_qr.PAID_TRANSACTION_MESSAGE)
        self.assertEqual(len(delete_calls), 0)

    def test_generate_qr_reuses_live_scanned_transaction(self):
        existing = SimpleNamespace(
            name="txn-scanned",
            transaction_refno="ref-scanned",
            status="scanned",
            qr_data="000201010211SCANNED",
            sale_amount="10.00",
            expires_at=datetime(2026, 3, 13, 18, 10, 0),
        )

        with (
            patch.object(
                maybank_qr,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=existing),
            patch.object(maybank_qr, "_check_rate_limit"),
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

        with patch.object(maybank_qr, "_load_existing_txn", return_value=existing):
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
        client.generate_qr.return_value = {
            "status": "QR999",
            "text": "Downstream error",
        }

        with (
            patch.object(maybank_qr, "_check_rate_limit"),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=None),
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_qr,
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
        client.generate_qr.return_value = {"status": "QR000", "data": []}

        with (
            patch.object(maybank_qr, "_check_rate_limit"),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=None),
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_qr,
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

    def test_generate_qr_retries_after_duplicate_insert_with_new_reference(self):
        duplicate_error = maybank_qr.frappe.DuplicateEntryError("duplicate")
        client = Mock()
        client.outlet_id = "outlet-1"
        client.generate_qr.side_effect = [
            {
                "status": "QR000",
                "data": [{"transaction_refno": "ref-dup-1", "qr_data": "QR-1"}],
            },
            {
                "status": "QR000",
                "data": [{"transaction_refno": "ref-dup-2", "qr_data": "QR-2"}],
            },
        ]

        first_doc = Mock()
        first_doc.insert.side_effect = duplicate_error
        second_doc = Mock()
        second_doc.insert.return_value = None

        with (
            patch.object(maybank_qr, "_check_rate_limit"),
            patch.object(maybank_qr.frappe.db, "get_value", return_value=None),
            patch.object(maybank_qr.frappe.db, "rollback") as rollback,
            patch.object(
                maybank_qr, "_resolve_existing_txn", return_value=None
            ) as resolve_existing,
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_qr.frappe,
                "get_doc",
                side_effect=[first_doc, second_doc],
            ),
            patch.object(
                maybank_qr,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
        ):
            result = maybank_qr.generate_maybank_qr_payload(
                {
                    "amount_sen": 1000,
                    "device_id": "device-1",
                    "idempotency_key": "key-duplicate-retry",
                }
            )

        self.assertEqual(result["transaction_refno"], "ref-dup-2")
        self.assertEqual(result["qr_data"], "QR-2")
        self.assertEqual(client.generate_qr.call_count, 2)
        rollback.assert_called_once()
        self.assertEqual(resolve_existing.call_count, 2)

    def test_check_payment_polls_live_status_for_pending_rows(self):
        txn = Mock()
        txn.transaction_refno = "ref-1"
        txn.status = "pending"
        txn.last_polled_at = datetime(2026, 3, 13, 18, 3, 0)
        txn.created_at = datetime(2026, 3, 13, 18, 0, 0)
        txn.device_id = "device-1"
        txn.sale_amount = "10.00"
        txn.paid_at = None

        client = Mock()
        client.check_status.return_value = {
            "status": "QR000",
            "data": [
                {"status": 1, "transaction_refno": "ref-1", "sale_amount": "10.00"}
            ],
        }

        with (
            patch.object(maybank_qr.frappe.db, "get_value", return_value="txn-1"),
            patch.object(maybank_qr.frappe, "get_doc", return_value=txn),
            patch.object(
                maybank_qr.MaybankClient, "from_settings", return_value=client
            ),
            patch.object(
                maybank_qr,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
            patch.object(maybank_qr, "_update_txn_status") as update_status,
        ):
            result = maybank_qr.check_maybank_payment_payload("ref-1", "device-1")

        update_status.assert_called_once()
        self.assertEqual(result["status"], "paid")
        self.assertIsNotNone(result["paid_at"])

    def test_check_payment_endpoint_uses_authenticated_device_scope(self):
        api_module.frappe.local.response = {}
        device = SimpleNamespace(device_id="device-1")

        with (
            patch.object(api_module, "require_kopos_api_access"),
            patch.object(
                api_module, "get_authenticated_device_doc", return_value=device
            ),
            patch.object(
                maybank_qr,
                "check_maybank_payment_payload",
                return_value={"status": "pending", "transaction_refno": "ref-1"},
            ) as payload,
        ):
            api_module.check_maybank_payment(transaction_refno="ref-1")

        payload.assert_called_once_with(transaction_refno="ref-1", device_id="device-1")

    def test_check_payment_endpoint_uses_explicit_device_context(self):
        api_module.frappe.local.response = {}
        device = SimpleNamespace(device_id="device-2")

        with (
            patch.object(api_module, "require_kopos_api_access"),
            patch.object(
                api_module, "require_device_context", return_value=device
            ) as require_context,
            patch.object(
                maybank_qr,
                "check_maybank_payment_payload",
                return_value={"status": "pending", "transaction_refno": "ref-2"},
            ) as payload,
        ):
            api_module.check_maybank_payment(
                transaction_refno="ref-2", device_id="device-2"
            )

        require_context.assert_called_once_with(device_id="device-2")
        payload.assert_called_once_with(transaction_refno="ref-2", device_id="device-2")

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

    def test_expired_scheduler_marks_timeout_after_final_pending_poll(self):
        client = Mock()
        client.check_status.return_value = {
            "status": "QR000",
            "data": [{"status": 2}],
        }

        txn = SimpleNamespace(
            name="txn-2",
            transaction_refno="ref-2",
            status="pending",
            last_polled_at=datetime(2026, 3, 13, 18, 4, 0),
            created_at=datetime(2026, 3, 13, 18, 4, 0),
            expires_at=datetime(2026, 3, 13, 18, 5, 0),
            poll_count=2,
        )

        with (
            patch.object(
                poll_maybank,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
            patch.object(poll_maybank, "_update_txn_status") as update_status,
            patch.object(poll_maybank.frappe.db, "sql") as sql_update,
            patch.object(poll_maybank.frappe.db, "commit"),
        ):
            poll_maybank._poll_single(client, txn)

        client.check_status.assert_called_once_with("ref-2")
        update_status.assert_called_once_with(
            "txn-2", "timeout", 2, client.check_status.return_value
        )
        sql_update.assert_not_called()

    def test_stale_sweep_times_out_rows_past_grace(self):
        client = Mock()
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
            patch.object(poll_maybank, "_poll_single") as poll_single,
        ):
            processed = poll_maybank._sweep_stale_pending_transactions(
                client, datetime(2026, 3, 13, 18, 6, 0)
            )

        self.assertEqual(processed, {"txn-stale"})
        poll_single.assert_called_once()

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
                poll_maybank.frappe,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 5, 0),
            ),
            patch.object(poll_maybank.frappe.db, "sql") as sql_update,
            patch.object(poll_maybank.frappe.db, "commit") as commit,
        ):
            with self.assertRaises(RuntimeError):
                poll_maybank._poll_single(client, txn)

        sql_statement = sql_update.call_args.args[0]
        self.assertIn("last_polled_at", sql_statement)
        commit.assert_called_once()

    def test_poll_single_times_out_via_shared_status_update(self):
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
            "data": [{"status": 2}],
        }

        with (
            patch.object(
                poll_maybank,
                "now_datetime",
                return_value=datetime(2026, 3, 13, 18, 6, 0),
            ),
            patch.object(poll_maybank, "_update_txn_status") as update_status,
            patch.object(poll_maybank.frappe.db, "commit") as commit,
        ):
            poll_maybank._poll_single(client, txn)

        update_status.assert_called_once_with(
            "txn-timeout", "timeout", 2, client.check_status.return_value
        )
        commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
