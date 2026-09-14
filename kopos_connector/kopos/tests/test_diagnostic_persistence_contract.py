from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api._maybank_qr_persistence import (
    _durable_generation_release,
    _record_poll_attempt,
)
from kopos_connector.utils.diagnostics import log_sanitized_error, redacted_json


class OpaqueDiagnosticValue:
    def __json__(self):
        return {"private": "unlabelled-private-marker"}

    def __iter__(self):
        yield "unlabelled-private-marker"


class TestDiagnosticPersistenceContract(FrappeTestCase):
    """Synthetic provider evidence only; all native writes roll back."""

    def tearDown(self):
        frappe.db.rollback()

    def _transaction(self, payload, *, status="unknown"):
        frappe.set_user("Administrator")
        doc = frappe.get_doc({
            "doctype": "Maybank QR Transaction",
            "transaction_refno": "DIAGNOSTIC-" + frappe.generate_hash(length=12),
            "status": status, "sale_amount": Decimal("12.30"), "sale_amount_sen": 1230,
            "raw_response": redacted_json(payload),
        }).insert(ignore_permissions=True)
        return doc.reload()

    def test_native_encoder_hooks_cannot_add_private_data_after_redaction(self):
        doc = self._transaction({"status": "QR000", "provider_result": OpaqueDiagnosticValue()})
        self.assertNotIn("unlabelled-private-marker", doc.raw_response)
        self.assertEqual(json.loads(doc.raw_response), {"status": "QR000", "provider_result": "[unsupported]"})
        doc.save(ignore_permissions=True)
        self.assertEqual(doc.reload().sale_amount_sen, 1230)

    def test_large_nested_payload_preserves_both_audited_recovery_fences(self):
        for status, resolution, envelope_status in (
            ("unknown", "provider_transaction_absent", "generation_abandoned"),
            ("timeout", "provider_transaction_cancelled", "timeout"),
        ):
            with self.subTest(resolution=resolution):
                doc = self._transaction({}, status=status)
                values = {
                    "provider_result": {str(i): ["oversized" * 100] * 100 for i in range(100)},
                    "status": envelope_status, "resolution": resolution,
                    "resolved_by": "Administrator", "resolved_at": "2026-09-13T22:30:00+08:00",
                    "reason": "Synthetic provider absence or cancellation evidence",
                    "evidence_reference": "DIAGNOSTIC-CASE", "provider_replay_blocked": True,
                    "provider_reference": doc.transaction_refno,
                }
                doc.raw_response = redacted_json(values)
                doc.save(ignore_permissions=True)
                doc.reload()
                self.assertLess(len(doc.raw_response), 10000)
                self.assertEqual(_durable_generation_release(doc), resolution)
                self.assertEqual(json.loads(doc.raw_response)["provider_replay_blocked"], True)

    def test_unsupported_recovery_evidence_cannot_authorize_another_attempt(self):
        doc = self._transaction({"resolution": OpaqueDiagnosticValue(), "status": "generation_abandoned"})
        self.assertIsNone(_durable_generation_release(doc))

    def test_omitted_required_recovery_text_never_counts_as_evidence(self):
        values = {
            "status": "generation_abandoned", "resolution": "provider_transaction_absent",
            "resolved_by": "Administrator", "resolved_at": "2026-09-13T22:30:00+08:00",
            "reason": "Synthetic provider absence evidence", "evidence_reference": "DIAGNOSTIC-CASE",
            "provider_replay_blocked": True,
        }
        for field in ("resolved_by", "resolved_at", "reason", "evidence_reference"):
            for invalid in (OpaqueDiagnosticValue(), "x" * 501):
                with self.subTest(field=field, invalid=type(invalid).__name__):
                    doc = self._transaction({**values, field: invalid})
                    self.assertIsNone(_durable_generation_release(doc))

    def test_native_poll_diagnostics_handle_cycles_without_changing_payment_state(self):
        doc = self._transaction({"status": "QR000"}, status="pending")
        cycle = []
        cycle.append(cycle)
        for count in (1, 2):
            _record_poll_attempt(doc.name, {"status": "diagnostic-observation", "data": cycle})
            doc.reload()
            self.assertEqual(doc.poll_count, count)
            self.assertEqual(doc.status, "pending")
            self.assertEqual(doc.sale_amount_sen, 1230)
            self.assertIn("[omitted]", doc.raw_response)
            self.assertIsNone(_durable_generation_release(doc))

    def test_plain_frappe_dict_and_unicode_round_trip_without_mutation(self):
        values = frappe._dict({"status": "QR000", "data": [{"transaction_refno": "ref-1", "token": "private"}],
                              "description": "kedai 咖啡", "amount_sen": 1230})
        doc = self._transaction(values)
        persisted = json.loads(doc.raw_response)
        self.assertEqual(persisted["description"], values.description)
        self.assertEqual(persisted["amount_sen"], 1230)
        self.assertEqual(persisted["data"][0]["token"], "[redacted]")
        self.assertEqual(values.data[0]["token"], "private")


class TestSanitizedErrorLogContract(FrappeTestCase):
    """Synthetic Error Logs are deliberately retained: native MyISAM ignores rollback."""

    def tearDown(self):
        frappe.db.rollback()

    def _title(self):
        frappe.set_user("Administrator")
        return "KOPOS-DIAG-" + frappe.generate_hash(length=16)

    def _stored(self, title):
        rows = frappe.get_all("Error Log", filters={"method": title},
                              fields=["name", "method", "error", "metadata", "fingerprint", "trace_id"])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Read through SQL, not Desk onload (which marks seen and commits).
        for field in ("metadata", "fingerprint", "trace_id"):
            self.assertFalse(row.get(field), field)
        return row

    def test_native_error_log_retains_summary_and_locations_without_source_or_locals(self):
        title = self._title()
        try:
            raise ValueError("password=SYNTHETIC-SENSITIVE-TEXT")  # SYNTHETIC-SOURCE-TEXT
        except ValueError as error:
            private_local = "SYNTHETIC-LOCAL-TEXT"
            log_sanitized_error(title, error)
            self.assertTrue(private_local)
        row = self._stored(title)
        self.assertTrue(row.error.startswith("ValueError: [redacted]\n"))
        self.assertIn("test_diagnostic_persistence_contract.py:", row.error)
        self.assertNotIn("SYNTHETIC-", row.error)
        self.assertNotIn("/home/", row.error)

    def test_native_sdk_does_not_capture_unrelated_active_exception_or_request(self):
        from sentry_sdk import Client, Hub
        from sentry_sdk.transport import Transport
        from werkzeug.wrappers import Request
        from frappe.utils.sentry import capture_exception

        class MemoryTransport(Transport):
            def __init__(self):
                super().__init__()
                self.events = []

            def capture_event(self, event):
                self.events.append(event)

            def capture_envelope(self, envelope):
                self.events.append(envelope.get_event())

        transport = MemoryTransport()
        # This installed SDK cannot copy Python 3.14 FrameLocalsProxy. Disable
        # locals only on this test client; request/active-exception capture is real.
        client = Client(dsn="http://public@127.0.0.1/1", transport=transport,
                        default_integrations=False, include_local_variables=False)
        self.addCleanup(client.close)
        title = self._title()
        native_settings = frappe.get_system_settings
        request = Request.from_values(method="POST", path="/api/method/diagnostic_test",
                                      json={"private": "SYNTHETIC-REQUEST-TEXT"})
        with Hub(client), patch.object(frappe.local, "request", request, create=True), \
                patch.object(frappe.local, "form_dict", frappe._dict(cmd="diagnostic_test"), create=True), \
                patch.object(frappe, "logger") as failure_logger, \
                patch.object(frappe, "get_system_settings", side_effect=lambda key: (
                    1 if key == "enable_telemetry" else native_settings(key))):
            failure_logger.return_value.error.side_effect = RuntimeError("SDK capture fixture failed")
            try:
                raise RuntimeError("SYNTHETIC-ACTIVE-TEXT")
            except RuntimeError:
                # Positive control proves native SDK capture is active; the
                # explicit memory transport has no network implementation.
                capture_exception(message="safe control")
                self.assertEqual(len(transport.events), 1)
                encoded = json.dumps(transport.events, default=str)
                self.assertIn("SYNTHETIC-ACTIVE-TEXT", encoded)
                self.assertIn("SYNTHETIC-REQUEST-TEXT", encoded)
                transport.events.clear()
                log_sanitized_error(title, ValueError("boom"))
                self.assertEqual(len(transport.events), 0)
        row = self._stored(title)
        self.assertEqual(row.error, "ValueError: boom")
