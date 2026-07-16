from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
public_api = importlib.import_module("kopos_connector.api")
auth = importlib.import_module("kopos_connector.auth")
provisioning = importlib.import_module("kopos_connector.api.provisioning")
safe_reset = importlib.import_module("kopos_connector.api.device_safe_reset")
safe_reset_doctype = importlib.import_module(
    "kopos_connector.kopos.doctype.kopos_device_safe_reset.kopos_device_safe_reset"
)


ZERO_QUEUE = {
    "pending_count": 0,
    "failed_count": 0,
    "syncing_count": 0,
    "dead_letter_count": 0,
}
ZERO_MIGRATION_RECOVERY = {
    "migration_recovery_point_count": 0,
    "migration_recovery_valid_point_count": 0,
    "migration_recovery_invalid_point_count": 0,
    "migration_recovery_captured_pending_total": 0,
    "migration_recovery_review_required": False,
}
PENDING_MIGRATION_RECOVERY = {
    "migration_recovery_point_count": 2,
    "migration_recovery_valid_point_count": 1,
    "migration_recovery_invalid_point_count": 1,
    "migration_recovery_captured_pending_total": 3,
    "migration_recovery_review_required": True,
}
COMPLETED_HISTORY_MIGRATION_RECOVERY = {
    "migration_recovery_point_count": 1,
    "migration_recovery_valid_point_count": 1,
    "migration_recovery_invalid_point_count": 0,
    "migration_recovery_captured_pending_total": 0,
    "migration_recovery_review_required": True,
}
EXPORT_SHA256 = "a" * 64
EXPORT_CONTENT_SHA256 = "b" * 64
EXPORT_BYTE_LENGTH = 4096
SAFE_RESET_PROTOCOL_VERSION = 2
RESET_PROOF_NONCE = "0f" * 32
RESET_PROOF_SHA256 = hashlib.sha256(RESET_PROOF_NONCE.encode()).hexdigest()
REQUEST_ID = "reset-request-001"
RESET_ID = "KSR-reset-001"
EVIDENCE_FINGERPRINT = "e" * 64
REQUEST_FINGERPRINT = "f" * 64
APPROVAL_CHALLENGE_ID = "KSAC-" + "c" * 64
APPROVAL_TOKEN = "t" * 43
REDEMPTION_IDEMPOTENCY_KEY = "i" * 43
COMPLETION_IDEMPOTENCY_KEY = "k" * 43
CANCELLATION_IDEMPOTENCY_KEY = "x" * 43
CANCELLATION_MANAGER_IDEMPOTENCY_KEY = "m" * 43
ABANDONMENT_IDEMPOTENCY_KEY = "q" * 43
CANCELLATION_REASON = "Operator cancelled before applying the approved reset"
MIGRATION_RECOVERY_ACK_REASON = (
    "Finance reviewed the archived recovery points and accepted reconciliation."
)
MIGRATION_RECOVERY_ACK_CONFIRMATION = (
    f"ACK RECOVERY {RESET_ID} {EXPORT_SHA256}"
)


class _FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.eval_calls: list[tuple[object, ...]] = []

    @staticmethod
    def _key(value: object) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    def make_key(self, key: str) -> bytes:
        return key.encode()

    def get(self, key: object) -> bytes | None:
        return self.values.get(self._key(key))

    def eval(self, *args: object) -> bytes | None:
        self.eval_calls.append(args)
        key = self._key(args[-1])
        value = self.values.pop(key, None)
        if value is not None:
            self.deleted.append(key)
        return value


class _AuditDoc:
    def __init__(self, values: dict[str, object]) -> None:
        self.__dict__.update(values)
        self.name = str(values.get("reset_id") or RESET_ID)
        self.inserted = False
        self.save_count = 0

    def insert(self, *, ignore_permissions: bool) -> None:
        if not ignore_permissions:
            raise AssertionError("audit insert must be privileged")
        self.inserted = True

    def save(self, *, ignore_permissions: bool) -> None:
        if not ignore_permissions:
            raise AssertionError("audit transition must be privileged")
        self.save_count += 1


def _device(*, config_version: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        name="KOPOS-DEVICE-001",
        device_id="tab-a-001",
        device_name="Counter Tablet",
        enabled=1,
        api_user="device-001@kopos.local",
        config_version=config_version,
        pos_profile="Counter 1",
    )


def _scope() -> dict[str, str]:
    return {
        "erp_base_url": "https://erp.example.com/tenant-a",
        "company": "JiJi",
        "currency": "MYR",
        "pos_profile": "Counter 1",
        "warehouse": "Main Warehouse",
    }


def _authorized_reset(
    *,
    request_origin: str = "device_authenticated",
) -> _AuditDoc:
    reset_doc = _AuditDoc(
        {
            "reset_id": RESET_ID,
            "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
            "status": "authorized",
            "request_expires_at": datetime(2026, 3, 14, 18, 5),
            "device": "KOPOS-DEVICE-001",
            "device_id": "tab-a-001",
            "api_user": "device-001@kopos.local",
            "reason": "Tablet safe reset requested after local archive export",
            "request_origin": request_origin,
            "registered_by_system_manager": None,
            "credential_recovery_confirmed_at": None,
            "stale_export_override": 0,
            "stale_export_override_reason": "",
            "request_id": REQUEST_ID,
            "requested_by_api_user": "device-001@kopos.local",
            "requested_at": datetime(2026, 3, 13, 18, 0),
            "previous_config_version": 2,
            "new_config_version": 0,
            "export_sha256": EXPORT_SHA256,
            "export_content_sha256": EXPORT_CONTENT_SHA256,
            "export_byte_length": EXPORT_BYTE_LENGTH,
            "exported_at": datetime(2026, 3, 13, 18, 0),
            "drained_row_count": 12,
            "reset_proof_sha256": RESET_PROOF_SHA256,
            "evidence_fingerprint": EVIDENCE_FINGERPRINT,
            "request_fingerprint": REQUEST_FINGERPRINT,
            "queue_pending_count": 0,
            "queue_failed_count": 0,
            "queue_syncing_count": 0,
            "queue_dead_letter_count": 0,
            **ZERO_MIGRATION_RECOVERY,
            "authorized_by": "Administrator",
            "authorized_at": datetime(2026, 3, 13, 18, 5),
            "authorization_count": 1,
            "approval_challenge_id": APPROVAL_CHALLENGE_ID,
            "approval_token_sha256": hashlib.sha256(
                APPROVAL_TOKEN.encode()
            ).hexdigest(),
            "approval_generation": 1,
            "approval_issued_by": "Administrator",
            "approval_issued_at": datetime(2026, 3, 13, 18, 5),
            "approval_expires_at": datetime(2026, 3, 13, 18, 20),
            "approval_erpnext_url": "https://erp.example.com/tenant-a",
            "redemption_count": 0,
            **_scope(),
        }
    )
    reset_doc.approval_fingerprint = safe_reset._approval_challenge_fingerprint(
        reset_doc,
        approval_challenge_id=APPROVAL_CHALLENGE_ID,
        approval_generation=1,
        approval_expires_at=reset_doc.approval_expires_at,
        approval_token_sha256=reset_doc.approval_token_sha256,
        approval_erpnext_url=reset_doc.approval_erpnext_url,
    )
    return reset_doc


def _production_safe_reset_doc(
    *,
    status: str = "redeemed",
    overrides: dict[str, object] | None = None,
) -> object:
    values: dict[str, object] = {
        "doctype": safe_reset.SAFE_RESET_DOCTYPE,
        "name": RESET_ID,
        "reset_id": RESET_ID,
        "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
        "request_id": REQUEST_ID,
        "status": status,
        "device": "KOPOS-DEVICE-001",
        "device_id": "tab-a-001",
        "api_user": "device-001@kopos.local",
        "reason": "Tablet safe reset requested after local archive export",
        "request_origin": "device_authenticated",
        "registered_by_system_manager": "",
        "credential_recovery_confirmed_at": None,
        "stale_export_override": 0,
        "stale_export_override_reason": "",
        "export_sha256": EXPORT_SHA256,
        "export_content_sha256": EXPORT_CONTENT_SHA256,
        "export_byte_length": EXPORT_BYTE_LENGTH,
        "exported_at": datetime(2026, 3, 13, 17, 55),
        "drained_row_count": 12,
        "queue_pending_count": 0,
        "queue_failed_count": 0,
        "queue_syncing_count": 0,
        "queue_dead_letter_count": 0,
        **ZERO_MIGRATION_RECOVERY,
        "previous_config_version": 2,
        "reset_proof_sha256": RESET_PROOF_SHA256,
        "evidence_fingerprint": EVIDENCE_FINGERPRINT,
        "request_fingerprint": REQUEST_FINGERPRINT,
        "requested_by_api_user": "device-001@kopos.local",
        "requested_at": datetime(2026, 3, 13, 18, 0),
        "request_expires_at": datetime(2026, 3, 14, 18, 0),
        **_scope(),
        "authorization_count": 0,
        "approval_generation": 0,
        "redemption_count": 0,
        "new_config_version": 0,
    }
    if status in {"authorized", "redeemed", "completed", "expired"}:
        values.update(
            {
                "authorized_by": "Administrator",
                "authorized_at": datetime(2026, 3, 13, 18, 5),
                "authorization_count": 1,
                "approval_challenge_id": APPROVAL_CHALLENGE_ID,
                "approval_token_sha256": hashlib.sha256(
                    APPROVAL_TOKEN.encode()
                ).hexdigest(),
                "approval_generation": 1,
                "approval_issued_by": "Administrator",
                "approval_issued_at": datetime(2026, 3, 13, 18, 5),
                "approval_expires_at": datetime(2026, 3, 13, 18, 20),
                "approval_fingerprint": "1" * 64,
                "approval_erpnext_url": "https://erp.example.com/tenant-a",
            }
        )
    if status in {"redeemed", "completed"}:
        redemption_digest = hashlib.sha256(
            REDEMPTION_IDEMPOTENCY_KEY.encode()
        ).hexdigest()
        redemption_result = "6" * 64
        values.update(
            {
                "credential_rotated_at": datetime(2026, 3, 13, 18, 10),
                "new_config_version": 3,
                "previous_credential_state": "complete",
                "revoked_api_key_sha256": "2" * 64,
                "issued_api_key_sha256": "3" * 64,
                "issued_api_secret_sha256": "4" * 64,
                "redeemed_approval_challenge_id": APPROVAL_CHALLENGE_ID,
                "redeemed_approval_generation": 1,
                "redeemed_approval_token_sha256": hashlib.sha256(
                    APPROVAL_TOKEN.encode()
                ).hexdigest(),
                "redeemed_approval_fingerprint": "1" * 64,
                "redeemed_approval_expires_at": datetime(2026, 3, 13, 18, 20),
                "redemption_idempotency_sha256": redemption_digest,
                "redemption_export_sha256": EXPORT_SHA256,
                "redemption_export_content_sha256": EXPORT_CONTENT_SHA256,
                "redemption_export_byte_length": EXPORT_BYTE_LENGTH,
                "redemption_setup_snapshot": "encrypted-placeholder",
                "redemption_setup_sha256": "5" * 64,
                "redemption_result_fingerprint": redemption_result,
                "redemption_issued_at": datetime(2026, 3, 13, 18, 10),
                "redeemed_at": datetime(2026, 3, 13, 18, 10),
                "last_redeemed_at": datetime(2026, 3, 13, 18, 10),
                "redeemed_recovery_expires_at": datetime(2026, 3, 14, 18, 10),
                "redemption_count": 1,
                "current_redemption_idempotency_sha256": redemption_digest,
                "current_redemption_result_fingerprint": redemption_result,
            }
        )
    if status == "completed":
        values.update(
            {
                "completion_idempotency_sha256": hashlib.sha256(
                    COMPLETION_IDEMPOTENCY_KEY.encode()
                ).hexdigest(),
                "completion_export_sha256": EXPORT_SHA256,
                "completion_export_content_sha256": EXPORT_CONTENT_SHA256,
                "completion_export_byte_length": EXPORT_BYTE_LENGTH,
                "completion_result_fingerprint": "7" * 64,
                "completed_by_api_user": "device-001@kopos.local",
                "completed_at": datetime(2026, 3, 13, 18, 15),
            }
        )
    if status == "cancelled":
        values.update(
            {
                "cancellation_idempotency_sha256": hashlib.sha256(
                    CANCELLATION_IDEMPOTENCY_KEY.encode()
                ).hexdigest(),
                "cancellation_reason": CANCELLATION_REASON,
                "cancellation_origin": "device_authenticated",
                "cancelled_by_user": "device-001@kopos.local",
                "cancelled_by_api_user": "device-001@kopos.local",
                "cancelled_at": datetime(2026, 3, 13, 18, 8),
                "cancellation_result_fingerprint": "8" * 64,
            }
        )
    if overrides:
        values.update(overrides)
    doc = safe_reset_doctype.KoPOSDeviceSafeReset()
    doc.__dict__.update(values)
    doc.is_new = lambda: False
    return doc


def _redemption_kwargs() -> dict[str, object]:
    return {
        "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
        "token": APPROVAL_TOKEN,
        "reset_id": RESET_ID,
        "request_id": REQUEST_ID,
        "approval_challenge_id": APPROVAL_CHALLENGE_ID,
        "approval_generation": 1,
        "reset_proof_nonce": RESET_PROOF_NONCE,
        "redemption_idempotency_key": REDEMPTION_IDEMPOTENCY_KEY,
        "export_sha256": EXPORT_SHA256,
        "export_content_sha256": EXPORT_CONTENT_SHA256,
        "export_byte_length": EXPORT_BYTE_LENGTH,
    }


def _cancellation_kwargs() -> dict[str, object]:
    return {
        "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
        "confirmation": f"CANCEL SAFE RESET {RESET_ID}",
        "request_id": REQUEST_ID,
        "reset_id": RESET_ID,
        "device_id": "tab-a-001",
        "reason": CANCELLATION_REASON,
        "idempotency_key": CANCELLATION_IDEMPOTENCY_KEY,
        "previous_config_version": 2,
        "reset_proof_sha256": RESET_PROOF_SHA256,
    }


def _manager_cancellation_kwargs() -> dict[str, object]:
    return {
        "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
        "confirmation": f"CANCEL SAFE RESET {RESET_ID}",
        "reset_id": RESET_ID,
        "reason": CANCELLATION_REASON,
        "idempotency_key": CANCELLATION_MANAGER_IDEMPOTENCY_KEY,
    }


def _registration_kwargs() -> dict[str, object]:
    return {
        "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
        "device_doc": _device(),
        "api_user_override": "device-001@kopos.local",
        "request_origin": safe_reset.REQUEST_ORIGIN_CREDENTIAL_RECOVERY,
        "registered_by_system_manager": "Administrator",
        "credential_recovery_confirmed_at": datetime(2026, 3, 13, 18, 5),
        "request_id": REQUEST_ID,
        "device_id": "tab-a-001",
        "reason": "Tablet credential was lost during OS recovery",
        "export_sha256": EXPORT_SHA256,
        "export_content_sha256": EXPORT_CONTENT_SHA256,
        "export_byte_length": EXPORT_BYTE_LENGTH,
        "exported_at": "2026-03-13T10:00:00Z",
        "drained_row_count": 12,
        "queue_evidence": ZERO_QUEUE,
        **ZERO_MIGRATION_RECOVERY,
        "previous_config_version": 2,
        "reset_proof_sha256": RESET_PROOF_SHA256,
        "erp_base_url": "https://erp.example.com/tenant-a",
        "company": "JiJi",
        "currency": "MYR",
        "pos_profile": "Counter 1",
        "warehouse": "Main Warehouse",
        "allow_stale_export": False,
        "stale_export_override_reason": None,
        "validate_business_state": True,
    }


def _resolution_kwargs() -> dict[str, object]:
    return {
        "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
        "device_id": "tab-a-001",
        "request_id": REQUEST_ID,
        "reset_proof_sha256": RESET_PROOF_SHA256,
        "export_sha256": EXPORT_SHA256,
        "export_content_sha256": EXPORT_CONTENT_SHA256,
        "export_byte_length": EXPORT_BYTE_LENGTH,
        "previous_config_version": 2,
    }


def _abandonment_kwargs() -> dict[str, object]:
    return {
        "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
        "device_id": "tab-a-001",
        "request_id": REQUEST_ID,
        "reason": "Tablet safe reset requested after local archive export",
        "export_sha256": EXPORT_SHA256,
        "export_content_sha256": EXPORT_CONTENT_SHA256,
        "export_byte_length": EXPORT_BYTE_LENGTH,
        "exported_at": "2026-03-13T10:00:00Z",
        "drained_row_count": 12,
        "queue_evidence": ZERO_QUEUE,
        **ZERO_MIGRATION_RECOVERY,
        "previous_config_version": 2,
        "reset_proof_sha256": RESET_PROOF_SHA256,
        **_scope(),
        "cancellation_idempotency_key": ABANDONMENT_IDEMPOTENCY_KEY,
    }


def _device_registration_kwargs() -> dict[str, object]:
    payload = _abandonment_kwargs()
    payload.pop("cancellation_idempotency_key")
    return {
        "device_doc": _device(),
        "api_user_override": None,
        "request_origin": safe_reset.REQUEST_ORIGIN_DEVICE,
        "registered_by_system_manager": None,
        "credential_recovery_confirmed_at": None,
        **payload,
        "allow_stale_export": False,
        "stale_export_override_reason": None,
        "validate_business_state": True,
    }


class DeviceSafeResetTests(unittest.TestCase):
    def setUp(self) -> None:
        safe_reset.frappe.session.user = "Administrator"
        public_api.frappe.local.response = {}

    def test_audit_schema_is_read_only_and_never_stores_raw_proof_or_credentials(self) -> None:
        schema_path = Path(
            "kopos_connector/kopos/doctype/kopos_device_safe_reset/"
            "kopos_device_safe_reset.json"
        )
        schema = json.loads(schema_path.read_text())
        fieldnames = {field["fieldname"] for field in schema["fields"]}
        fields_by_name = {field["fieldname"]: field for field in schema["fields"]}
        self.assertTrue(
            {
                "request_origin",
                "safe_reset_protocol_version",
                "export_sha256",
                "export_content_sha256",
                "export_byte_length",
                "exported_at",
                "evidence_fingerprint",
                "reset_proof_sha256",
                "approval_generation",
                "approval_token_sha256",
                "redeemed_approval_expires_at",
                "redemption_idempotency_sha256",
                "completion_idempotency_sha256",
                "cancellation_idempotency_sha256",
                "cancellation_result_fingerprint",
                "cancellation_origin",
                "cancelled_by_user",
                "previous_credential_state",
                *ZERO_MIGRATION_RECOVERY.keys(),
                "migration_recovery_acknowledged_by",
                "migration_recovery_acknowledged_at",
                "migration_recovery_acknowledgement_reason",
                "migration_recovery_ack_fingerprint",
            }.issubset(fieldnames)
        )
        self.assertTrue(
            {
                "reset_proof_nonce",
                "approval_token",
                "redemption_idempotency_key",
                "completion_idempotency_key",
                "cancellation_idempotency_key",
                "api_key",
                "api_secret",
            }.isdisjoint(fieldnames)
        )
        self.assertEqual(
            fields_by_name["redemption_setup_snapshot"]["fieldtype"],
            "Password",
        )
        self.assertEqual(fields_by_name["redemption_setup_snapshot"]["hidden"], 1)
        self.assertEqual(fields_by_name["reset_proof_sha256"]["search_index"], 1)
        self.assertEqual(fields_by_name["reset_proof_sha256"]["unique"], 1)
        for digest_field in (
            "approval_token_sha256",
            "redemption_idempotency_sha256",
            "completion_idempotency_sha256",
            "issued_api_key_sha256",
            "issued_api_secret_sha256",
        ):
            self.assertEqual(fields_by_name[digest_field]["hidden"], 1)
            self.assertEqual(fields_by_name[digest_field]["read_only"], 1)
        self.assertEqual(schema["permissions"], [
            {
                "email": 1,
                "export": 1,
                "print": 1,
                "read": 1,
                "report": 1,
                "role": "System Manager",
            }
        ])
        self.assertEqual(schema["track_changes"], 1)
        self.assertEqual(schema["allow_import"], 0)
        self.assertTrue(
            set(ZERO_MIGRATION_RECOVERY).issubset(
                safe_reset_doctype.IMMUTABLE_REQUEST_FIELDS
            )
        )
        install_source = Path("kopos_connector/install/install.py").read_text()
        for index_name in (
            "idx_kopos_safe_reset_device_status",
            "idx_kopos_maybank_device_status",
            "idx_kopos_maybank_device_reconciliation",
            "idx_kopos_manual_qr_device_status",
            "idx_kopos_fb_order_shift_status",
            "idx_kopos_projection_source_state",
            "idx_kopos_projection_retry_due",
            "idx_kopos_maybank_poll_due",
            "idx_kopos_maybank_device_created",
            "idx_kopos_resolved_sale_order",
        ):
            self.assertIn(index_name, install_source)

    def test_device_routes_are_exported_and_restricted_to_post(self) -> None:
        for route in (
            "/api/method/kopos_connector.api.request_device_safe_reset",
            "/api/method/kopos_connector.api.abandon_unregistered_device_safe_reset_request",
            "/api/method/kopos_connector.api.resolve_device_safe_reset_request",
            "/api/method/kopos_connector.api.cancel_device_safe_reset",
            "/api/method/kopos_connector.api.complete_device_safe_reset",
        ):
            self.assertIn(route, auth.ALLOWED_DEVICE_API_PATHS)
            self.assertEqual(auth.DEVICE_API_HTTP_METHODS[route], frozenset({"POST"}))
        for method_name in (
            "abandon_unregistered_device_safe_reset_request",
            "request_device_safe_reset",
            "cancel_device_safe_reset",
            "cancel_device_safe_reset_as_system_manager",
            "register_device_credential_recovery",
            "authorize_device_safe_reset",
            "complete_device_safe_reset",
            "resolve_device_safe_reset_request",
        ):
            self.assertIn(method_name, public_api.__all__)

    def test_device_auth_allows_only_post_for_abandonment_fence(self) -> None:
        path = (
            "/api/method/kopos_connector.api."
            "abandon_unregistered_device_safe_reset_request"
        )
        auth.frappe.session.user = "device-001@kopos.local"
        with (
            patch.object(
                auth,
                "get_session_roles",
                return_value=[auth.KOPOS_DEVICE_API_ROLE],
            ),
            patch.object(
                auth.frappe.local,
                "request",
                SimpleNamespace(
                    path=path,
                    method="POST",
                    content_length=1024,
                ),
            ),
        ):
            auth.enforce_device_api_restrictions()

        with (
            patch.object(
                auth,
                "get_session_roles",
                return_value=[auth.KOPOS_DEVICE_API_ROLE],
            ),
            patch.object(
                auth.frappe.local,
                "request",
                SimpleNamespace(
                    path=path,
                    method="GET",
                    content_length=0,
                ),
            ),
            self.assertRaisesRegex(
                auth.frappe.ValidationError,
                "approved KoPOS device endpoints",
            ),
        ):
            auth.enforce_device_api_restrictions()

    def test_system_manager_desk_workflow_imports_v2_code_and_issues_approval_qr(self) -> None:
        script = Path(
            "kopos_connector/page/kopos_provisioning/kopos_provisioning.js"
        ).read_text()
        self.assertIn("KOPOS-ERP-CREDENTIAL-RECOVERY-V2.", script)
        self.assertNotIn("KOPOS-ERP-CREDENTIAL-RECOVERY-V1.", script)
        self.assertIn(
            'method: "kopos_connector.api.register_device_credential_recovery"',
            script,
        )
        self.assertIn(
            'method: "kopos_connector.api.authorize_device_safe_reset"',
            script,
        )
        self.assertIn(
            'method: "kopos_connector.api.cancel_device_safe_reset_as_system_manager"',
            script,
        )
        self.assertIn("CANCEL SAFE RESET", script)
        self.assertIn("window.crypto.getRandomValues", script)
        self.assertIn("window.sessionStorage", script)
        self.assertNotIn("Math.random", script)
        self.assertIn("JSON.stringify(payload) !== decoded", script)
        self.assertIn("provisioning_mode !== \"safe_reset_approval\"", script)
        self.assertIn("approval_qr_svg", script)
        self.assertIn("approval_link", script)
        self.assertIn("safe_reset_protocol_version", script)
        self.assertIn("export_byte_length", script)
        self.assertIn("Approval does not restore the API user", script)
        self.assertIn("rawValue.length > 8192", script)
        self.assertIn("migration_recovery_review_required", script)
        self.assertIn("ACK RECOVERY", script)
        self.assertIn("kopos-authorization-load", script)
        self.assertIn("load_safe_reset", script)

    def test_public_wrappers_forward_exact_request_recovery_and_redeem_fields(self) -> None:
        request_payload = {
            "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
            "request_id": REQUEST_ID,
            "device_id": "tab-a-001",
            "reason": "support reset",
            "erp_base_url": "https://erp.example.com/tenant-a",
            "company": "JiJi",
            "currency": "MYR",
            "pos_profile": "Counter 1",
            "warehouse": "Main Warehouse",
            "export_sha256": EXPORT_SHA256,
            "export_content_sha256": EXPORT_CONTENT_SHA256,
            "export_byte_length": EXPORT_BYTE_LENGTH,
            "exported_at": "2026-03-13T10:00:00Z",
            "drained_row_count": 12,
            "queue_evidence": ZERO_QUEUE,
            **ZERO_MIGRATION_RECOVERY,
            "previous_config_version": 2,
            "reset_proof_sha256": RESET_PROOF_SHA256,
        }
        with (
            patch.object(public_api, "_get_submit_payload", return_value=request_payload),
            patch.object(
                public_api,
                "request_device_safe_reset_payload",
                return_value={"status": "requested"},
            ) as request_mock,
        ):
            public_api.request_device_safe_reset()
        self.assertEqual(request_mock.call_args.kwargs["exported_at"], request_payload["exported_at"])
        self.assertEqual(request_mock.call_args.kwargs["warehouse"], "Main Warehouse")
        self.assertEqual(request_mock.call_args.kwargs["erp_base_url"], request_payload["erp_base_url"])
        self.assertEqual(
            request_mock.call_args.kwargs["migration_recovery_review_required"],
            False,
        )

        recovery_payload = {
            **request_payload,
            "confirmation": "RECOVER tab-a-001",
            "allow_stale_export": True,
            "stale_export_override_reason": "Approved after verified support handoff delay",
        }
        public_api.frappe.local.response = {}
        with (
            patch.object(public_api, "_get_submit_payload", return_value=recovery_payload),
            patch.object(
                public_api,
                "register_device_credential_recovery_payload",
                return_value={"status": "requested"},
            ) as recovery_mock,
        ):
            public_api.register_device_credential_recovery()
        self.assertTrue(recovery_mock.call_args.kwargs["allow_stale_export"])
        self.assertEqual(
            recovery_mock.call_args.kwargs["stale_export_override_reason"],
            recovery_payload["stale_export_override_reason"],
        )

        public_api.frappe.local.response = {}
        with (
            patch.object(
                public_api,
                "_get_submit_payload",
                return_value={
                    "reset_id": RESET_ID,
                    "migration_recovery_confirmation": MIGRATION_RECOVERY_ACK_CONFIRMATION,
                    "migration_recovery_acknowledgement_reason": MIGRATION_RECOVERY_ACK_REASON,
                },
            ),
            patch.object(
                public_api,
                "authorize_device_safe_reset_payload",
                return_value={"status": "ok"},
            ) as authorize_mock,
        ):
            public_api.authorize_device_safe_reset()
        self.assertEqual(
            authorize_mock.call_args.kwargs["migration_recovery_confirmation"],
            MIGRATION_RECOVERY_ACK_CONFIRMATION,
        )
        self.assertEqual(
            authorize_mock.call_args.kwargs[
                "migration_recovery_acknowledgement_reason"
            ],
            MIGRATION_RECOVERY_ACK_REASON,
        )

        public_api.frappe.local.response = {}
        with (
            patch.object(
                public_api,
                "_get_submit_payload",
                return_value={
                    "token": "one-time-token",
                    "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
                    "reset_id": RESET_ID,
                    "request_id": REQUEST_ID,
                    "approval_challenge_id": APPROVAL_CHALLENGE_ID,
                    "approval_generation": 1,
                    "reset_proof_nonce": RESET_PROOF_NONCE,
                    "redemption_idempotency_key": REDEMPTION_IDEMPOTENCY_KEY,
                    "export_sha256": EXPORT_SHA256,
                    "export_content_sha256": EXPORT_CONTENT_SHA256,
                    "export_byte_length": EXPORT_BYTE_LENGTH,
                },
            ),
            patch.object(
                public_api,
                "redeem_pos_provisioning_payload",
                return_value={"status": "ok"},
            ) as redeem_mock,
        ):
            public_api.redeem_pos_provisioning()
        redeem_mock.assert_called_once_with(
            token="one-time-token",
            safe_reset_protocol_version=SAFE_RESET_PROTOCOL_VERSION,
            reset_id=RESET_ID,
            request_id=REQUEST_ID,
            approval_challenge_id=APPROVAL_CHALLENGE_ID,
            approval_generation=1,
            reset_proof_nonce=RESET_PROOF_NONCE,
            redemption_idempotency_key=REDEMPTION_IDEMPOTENCY_KEY,
            export_sha256=EXPORT_SHA256,
            export_content_sha256=EXPORT_CONTENT_SHA256,
            export_byte_length=EXPORT_BYTE_LENGTH,
        )
        self.assertIn(
            "no-store",
            public_api.frappe.local.response["headers"]["Cache-Control"],
        )
        self.assertEqual(
            public_api.frappe.local.response["headers"]["Pragma"],
            "no-cache",
        )

        completion_payload = {
            "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
            "device_id": "tab-a-001",
            "reset_id": RESET_ID,
            "new_config_version": 3,
            "export_sha256": EXPORT_SHA256,
            "export_content_sha256": EXPORT_CONTENT_SHA256,
            "export_byte_length": EXPORT_BYTE_LENGTH,
            "completion_idempotency_key": COMPLETION_IDEMPOTENCY_KEY,
        }
        public_api.frappe.local.response = {}
        with (
            patch.object(
                public_api,
                "_get_submit_payload",
                return_value=completion_payload,
            ),
            patch.object(
                public_api,
                "complete_device_safe_reset_payload",
                return_value={"status": "completed"},
            ) as completion_mock,
        ):
            public_api.complete_device_safe_reset()
        completion_mock.assert_called_once_with(**completion_payload)

    def test_resolve_wrapper_forwards_exact_bound_identity(self) -> None:
        payload = _resolution_kwargs()
        public_api.frappe.local.response = {}
        with (
            patch.object(
                public_api,
                "_get_submit_payload",
                return_value=payload,
            ),
            patch.object(
                public_api,
                "resolve_device_safe_reset_request_payload",
                return_value={"status": "not_registered"},
            ) as resolve_mock,
        ):
            public_api.resolve_device_safe_reset_request()

        resolve_mock.assert_called_once_with(**payload)

    def test_abandonment_wrapper_forwards_full_immutable_request(self) -> None:
        payload = _abandonment_kwargs()
        public_api.frappe.local.response = {}
        with (
            patch.object(
                public_api,
                "_get_submit_payload",
                return_value=payload,
            ),
            patch.object(
                public_api,
                "abandon_unregistered_device_safe_reset_request_payload",
                return_value={
                    "status": "cancelled",
                    "abandonment_status": "fenced",
                },
            ) as abandon_mock,
        ):
            public_api.abandon_unregistered_device_safe_reset_request()

        abandon_mock.assert_called_once_with(**payload)
        self.assertIn(
            "no-store",
            public_api.frappe.local.response["headers"]["Cache-Control"],
        )

    def test_request_validation_rejection_is_safe_only_after_bound_not_found_lookup(
        self,
    ) -> None:
        payload = _resolution_kwargs()
        rejection = safe_reset.frappe.ValidationError("queue is not fully drained")
        events: list[str] = []

        def rollback() -> None:
            events.append("rollback")

        def classify(**kwargs: object) -> dict[str, str]:
            self.assertEqual(events, ["rollback"])
            self.assertEqual(kwargs, payload)
            events.append("lookup")
            return {
                "request_registration_status": "not_found",
                "checked_at": "2026-03-13T10:05:00Z",
            }

        public_api.frappe.local.response = {}
        with (
            patch.object(
                public_api,
                "_get_submit_payload",
                return_value=payload,
            ),
            patch.object(
                public_api,
                "request_device_safe_reset_payload",
                side_effect=rejection,
            ),
            patch.object(
                public_api.frappe.db,
                "rollback",
                side_effect=rollback,
            ),
            patch.object(
                public_api,
                "classify_safe_reset_request_registration_payload",
                side_effect=classify,
            ),
        ):
            public_api.request_device_safe_reset()

        self.assertEqual(events, ["rollback", "lookup"])
        self.assertEqual(
            public_api.frappe.local.response,
            {
                "status": "rejected",
                "error_code": "SAFE_RESET_REQUEST_REJECTED_NO_COMMIT",
                "message": "queue is not fully drained",
                "request_attempt_committed": False,
                "request_registration_status": "not_found",
                "local_release_authorized": False,
                "recovery_action": (
                    "abandon_unregistered_device_safe_reset_request"
                ),
                **payload,
                "checked_at": "2026-03-13T10:05:00Z",
                "http_status_code": 400,
            },
        )
        self.assertNotIn(
            "request_committed",
            public_api.frappe.local.response,
        )

    def test_request_rejection_conflicts_remain_lookup_required(self) -> None:
        payload = _resolution_kwargs()
        rejection = safe_reset.frappe.ValidationError(
            "safe reset request evidence conflicts"
        )

        for lookup_reason in (
            "matching_request_exists",
            "active_reset_conflict",
        ):
            with self.subTest(lookup_reason=lookup_reason):
                public_api.frappe.local.response = {}
                with (
                    patch.object(
                        public_api,
                        "_get_submit_payload",
                        return_value=payload,
                    ),
                    patch.object(
                        public_api,
                        "request_device_safe_reset_payload",
                        side_effect=rejection,
                    ),
                    patch.object(public_api.frappe.db, "rollback"),
                    patch.object(
                        public_api,
                        "classify_safe_reset_request_registration_payload",
                        return_value={
                            "request_registration_status": lookup_reason
                        },
                    ),
                    patch.object(
                        public_api,
                        "_utc_server_time",
                        return_value="2026-03-13T10:05:00.000Z",
                    ),
                ):
                    public_api.request_device_safe_reset()

                response = public_api.frappe.local.response
                self.assertEqual(response["status"], "lookup_required")
                self.assertEqual(
                    response["error_code"],
                    "SAFE_RESET_REQUEST_LOOKUP_REQUIRED",
                )
                self.assertEqual(
                    response["request_registration_status"],
                    "lookup_required",
                )
                self.assertEqual(response["lookup_reason"], lookup_reason)
                self.assertFalse(response["local_release_authorized"])
                self.assertNotIn("request_committed", response)
                self.assertNotIn("request_attempt_committed", response)

    def test_malformed_or_unverifiable_rejection_never_claims_not_found(self) -> None:
        payload = {**_resolution_kwargs(), "request_id": "bad id"}
        rejection = safe_reset.frappe.ValidationError("request_id is invalid")
        public_api.frappe.local.response = {}
        with (
            patch.object(
                public_api,
                "_get_submit_payload",
                return_value=payload,
            ),
            patch.object(
                public_api,
                "request_device_safe_reset_payload",
                side_effect=rejection,
            ),
            patch.object(public_api.frappe.db, "rollback"),
            patch.object(
                public_api,
                "classify_safe_reset_request_registration_payload",
                side_effect=safe_reset.frappe.ValidationError(
                    "Valid safe reset request_id is required"
                ),
            ),
        ):
            public_api.request_device_safe_reset()

        response = public_api.frappe.local.response
        self.assertEqual(response["status"], "lookup_required")
        self.assertEqual(response["lookup_reason"], "verification_failed")
        self.assertNotEqual(
            response["request_registration_status"],
            "not_found",
        )
        self.assertNotIn("request_committed", response)
        self.assertNotIn("request_attempt_committed", response)

    def test_cancel_wrapper_forwards_exact_authenticated_audit_fields(self) -> None:
        payload = _cancellation_kwargs()
        public_api.frappe.local.response = {}
        with (
            patch.object(public_api, "_get_submit_payload", return_value=payload),
            patch.object(
                public_api,
                "cancel_device_safe_reset_payload",
                return_value={"status": "cancelled"},
            ) as cancel_mock,
        ):
            public_api.cancel_device_safe_reset()
        cancel_mock.assert_called_once_with(**payload)
        self.assertIn(
            "no-store",
            public_api.frappe.local.response["headers"]["Cache-Control"],
        )

    def test_manager_cancel_wrapper_forwards_only_manager_audit_fields(self) -> None:
        payload = _manager_cancellation_kwargs()
        public_api.frappe.local.response = {}
        with (
            patch.object(public_api, "_get_submit_payload", return_value=payload),
            patch.object(
                public_api,
                "cancel_safe_reset_as_manager_payload",
                return_value={"status": "cancelled"},
            ) as cancel_mock,
        ):
            public_api.cancel_device_safe_reset_as_system_manager()
        cancel_mock.assert_called_once_with(**payload)
        self.assertIn(
            "no-store",
            public_api.frappe.local.response["headers"]["Cache-Control"],
        )

    def test_request_rejects_nonzero_queue_evidence(self) -> None:
        kwargs = _registration_kwargs()
        kwargs["queue_evidence"] = {**ZERO_QUEUE, "failed_count": 1}
        with self.assertRaisesRegex(
            safe_reset.frappe.ValidationError,
            "fully drained queue",
        ):
            safe_reset._register_safe_reset_request(**kwargs)

    def test_reset_proof_reuse_lookup_is_independent_of_archive_digest(self) -> None:
        existing = _authorized_reset()
        with (
            patch.object(
                safe_reset.frappe.db,
                "sql",
                return_value=[{"name": RESET_ID}],
            ) as sql,
            patch.object(safe_reset.frappe, "get_doc", return_value=existing),
        ):
            result = safe_reset._find_matching_reset_for_update(
                device_id="tab-a-001",
                request_id="new-request-002",
                reset_proof_sha256=RESET_PROOF_SHA256,
            )
        self.assertIs(result, existing)
        query = sql.call_args.args[0]
        params = sql.call_args.args[1]
        self.assertIn("reset_proof_sha256 = %s", query)
        self.assertNotIn("export_sha256", query)
        self.assertEqual(
            params,
            ("tab-a-001", "new-request-002", RESET_PROOF_SHA256),
        )

    def test_resolution_not_found_is_bound_read_only_and_lock_ordered(self) -> None:
        lock_events: list[str] = []

        def lock_device(device_name: str) -> None:
            self.assertEqual(device_name, "KOPOS-DEVICE-001")
            lock_events.append("device")

        def find_matching(**kwargs: object) -> None:
            self.assertEqual(lock_events, ["device"])
            self.assertEqual(
                kwargs,
                {
                    "device_id": "tab-a-001",
                    "request_id": REQUEST_ID,
                    "reset_proof_sha256": RESET_PROOF_SHA256,
                },
            )
            lock_events.append("request")
            return None

        commit = MagicMock()
        safe_reset.frappe.session.user = "device-001@kopos.local"
        with (
            patch.object(
                safe_reset,
                "require_device_context",
                return_value=_device(),
            ),
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(
                safe_reset,
                "_lock_device_for_update",
                side_effect=lock_device,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                side_effect=find_matching,
            ),
            patch.object(safe_reset.frappe.db, "commit", commit),
        ):
            response = safe_reset.resolve_device_safe_reset_request(
                **_resolution_kwargs()
            )

        self.assertEqual(lock_events, ["device", "request"])
        self.assertEqual(
            response,
            {
                "status": "not_registered",
                "request_registration_status": "not_found",
                "request_committed": False,
                "local_release_authorized": False,
                "recovery_action": (
                    "abandon_unregistered_device_safe_reset_request"
                ),
                **_resolution_kwargs(),
                "checked_at": "2026-03-13T10:05:00Z",
            },
        )
        commit.assert_not_called()

    def test_resolution_returns_full_existing_ack_for_every_lifecycle(self) -> None:
        safe_reset.frappe.session.user = "device-001@kopos.local"
        for lifecycle_status in (
            "requested",
            "authorized",
            "redeemed",
            "completed",
            "cancelled",
            "expired",
        ):
            with self.subTest(lifecycle_status=lifecycle_status):
                existing = _authorized_reset()
                existing.status = lifecycle_status
                commit = MagicMock()
                with (
                    patch.object(
                        safe_reset,
                        "require_device_context",
                        return_value=_device(),
                    ),
                    patch.object(safe_reset, "ensure_unique_device_api_user"),
                    patch.object(safe_reset, "_lock_device_for_update"),
                    patch.object(
                        safe_reset,
                        "get_device_doc",
                        return_value=_device(),
                    ),
                    patch.object(
                        safe_reset,
                        "_find_matching_reset_for_update",
                        return_value=existing,
                    ),
                    patch.object(safe_reset.frappe.db, "commit", commit),
                ):
                    response = safe_reset.resolve_device_safe_reset_request(
                        **_resolution_kwargs()
                    )

                self.assertEqual(response, safe_reset._request_ack(existing))
                self.assertEqual(response["lifecycle_status"], lifecycle_status)
                self.assertEqual(response["request_id"], REQUEST_ID)
                self.assertEqual(
                    response["reset_proof_sha256"],
                    RESET_PROOF_SHA256,
                )
                self.assertEqual(existing.save_count, 0)
                commit.assert_not_called()

    def test_resolution_rejects_every_evidence_and_origin_mismatch(self) -> None:
        mismatch_cases = {
            "protocol": ("safe_reset_protocol_version", 1),
            "request": ("request_id", "different-request-001"),
            "device": ("device_id", "tab-a-002"),
            "proof": ("reset_proof_sha256", "1" * 64),
            "archive": ("export_sha256", "2" * 64),
            "content": ("export_content_sha256", "3" * 64),
            "length": ("export_byte_length", EXPORT_BYTE_LENGTH + 1),
            "version": ("previous_config_version", 3),
            "origin": ("request_origin", "credential_recovery"),
            "requester": (
                "requested_by_api_user",
                "other-device@kopos.local",
            ),
            "api_user": ("api_user", "other-device@kopos.local"),
        }
        safe_reset.frappe.session.user = "device-001@kopos.local"
        for label, (fieldname, value) in mismatch_cases.items():
            with self.subTest(mismatch=label):
                existing = _authorized_reset()
                setattr(existing, fieldname, value)
                commit = MagicMock()
                with (
                    patch.object(
                        safe_reset,
                        "require_device_context",
                        return_value=_device(),
                    ),
                    patch.object(safe_reset, "ensure_unique_device_api_user"),
                    patch.object(safe_reset, "_lock_device_for_update"),
                    patch.object(
                        safe_reset,
                        "get_device_doc",
                        return_value=_device(),
                    ),
                    patch.object(
                        safe_reset,
                        "_find_matching_reset_for_update",
                        return_value=existing,
                    ),
                    patch.object(safe_reset.frappe.db, "commit", commit),
                    self.assertRaises(safe_reset.frappe.ValidationError),
                ):
                    safe_reset.resolve_device_safe_reset_request(
                        **_resolution_kwargs()
                    )
                commit.assert_not_called()

    def test_resolution_and_abandonment_require_dedicated_device_binding(self) -> None:
        safe_reset.frappe.session.user = "other-user@kopos.local"
        for operation, kwargs in (
            (
                safe_reset.resolve_device_safe_reset_request,
                _resolution_kwargs(),
            ),
            (
                safe_reset.abandon_unregistered_device_safe_reset_request,
                _abandonment_kwargs(),
            ),
        ):
            with self.subTest(operation=operation.__name__):
                lock_device = MagicMock()
                with (
                    patch.object(
                        safe_reset,
                        "require_device_context",
                        return_value=_device(),
                    ),
                    patch.object(
                        safe_reset,
                        "_lock_device_for_update",
                        lock_device,
                    ),
                    self.assertRaisesRegex(
                        safe_reset.frappe.ValidationError,
                        "dedicated API user",
                    ),
                ):
                    operation(**kwargs)
                lock_device.assert_not_called()

    def test_rejection_classifier_withholds_not_found_for_active_conflict(self) -> None:
        with (
            patch.object(
                safe_reset,
                "resolve_device_safe_reset_request",
                return_value={
                    "status": "not_registered",
                    "request_registration_status": "not_found",
                    "checked_at": "2026-03-13T10:05:00Z",
                },
            ),
            patch.object(
                safe_reset,
                "_find_active_reset_for_update",
                return_value=_authorized_reset(),
            ),
        ):
            response = (
                safe_reset.classify_device_safe_reset_request_registration(
                    **_resolution_kwargs()
                )
            )
        self.assertEqual(
            response,
            {"request_registration_status": "active_reset_conflict"},
        )

    def test_migration_recovery_evidence_is_strict_bounded_and_self_consistent(self) -> None:
        self.assertEqual(
            safe_reset._normalize_migration_recovery_evidence(
                **ZERO_MIGRATION_RECOVERY
            ),
            ZERO_MIGRATION_RECOVERY,
        )
        self.assertEqual(
            safe_reset._normalize_migration_recovery_evidence(
                **PENDING_MIGRATION_RECOVERY
            ),
            PENDING_MIGRATION_RECOVERY,
        )
        self.assertEqual(
            safe_reset._normalize_migration_recovery_evidence(
                **COMPLETED_HISTORY_MIGRATION_RECOVERY
            ),
            COMPLETED_HISTORY_MIGRATION_RECOVERY,
        )
        invalid_cases = (
            {
                **ZERO_MIGRATION_RECOVERY,
                "migration_recovery_review_required": None,
            },
            {
                **ZERO_MIGRATION_RECOVERY,
                "migration_recovery_point_count": True,
            },
            {
                **ZERO_MIGRATION_RECOVERY,
                "migration_recovery_point_count": 2_147_483_648,
            },
            {
                **PENDING_MIGRATION_RECOVERY,
                "migration_recovery_invalid_point_count": 0,
            },
            {
                **PENDING_MIGRATION_RECOVERY,
                "migration_recovery_review_required": False,
            },
            {
                **ZERO_MIGRATION_RECOVERY,
                "migration_recovery_captured_pending_total": 1,
            },
        )
        for evidence in invalid_cases:
            with self.subTest(evidence=evidence), self.assertRaises(
                safe_reset.frappe.ValidationError
            ):
                safe_reset._normalize_migration_recovery_evidence(**evidence)

        strict_invalid_cases = (
            {
                **ZERO_MIGRATION_RECOVERY,
                "migration_recovery_point_count": "0",
            },
            {
                **ZERO_MIGRATION_RECOVERY,
                "migration_recovery_valid_point_count": False,
            },
            {
                **ZERO_MIGRATION_RECOVERY,
                "migration_recovery_review_required": 0,
            },
        )
        for evidence in strict_invalid_cases:
            with self.subTest(strict_evidence=evidence), self.assertRaises(
                safe_reset.frappe.ValidationError
            ):
                safe_reset._normalize_strict_migration_recovery_evidence(
                    **evidence
                )

    def test_doctype_rejects_pending_total_without_recovery_points(self) -> None:
        audit = safe_reset_doctype.KoPOSDeviceSafeReset()
        audit.__dict__.update(
            {
                **ZERO_MIGRATION_RECOVERY,
                "migration_recovery_captured_pending_total": 1,
            }
        )
        with self.assertRaisesRegex(
            safe_reset.frappe.ValidationError,
            "pending total must be zero",
        ):
            audit._validate_migration_recovery_evidence()

    def test_production_doctype_allows_reissued_current_challenge_recovery_binding(
        self,
    ) -> None:
        previous = _production_safe_reset_doc(status="redeemed")
        reissued = _production_safe_reset_doc(
            status="redeemed",
            overrides={
                "approval_challenge_id": "KSAC-" + "d" * 64,
                "approval_token_sha256": hashlib.sha256(
                    ("v" * 43).encode()
                ).hexdigest(),
                "approval_generation": 2,
                "approval_issued_at": datetime(2026, 3, 13, 18, 12),
                "approval_expires_at": datetime(2026, 3, 13, 18, 27),
                "approval_fingerprint": "8" * 64,
                "authorization_count": 2,
                "current_redemption_idempotency_sha256": "",
                "current_redemption_result_fingerprint": "",
            },
        )
        with patch.object(
            safe_reset_doctype.frappe,
            "get_doc",
            return_value=previous,
        ):
            reissued.validate()

        rebound = _production_safe_reset_doc(
            status="redeemed",
            overrides={
                "approval_challenge_id": "KSAC-" + "d" * 64,
                "approval_token_sha256": hashlib.sha256(
                    ("v" * 43).encode()
                ).hexdigest(),
                "approval_generation": 2,
                "approval_issued_at": datetime(2026, 3, 13, 18, 12),
                "approval_expires_at": datetime(2026, 3, 13, 18, 27),
                "approval_fingerprint": "8" * 64,
                "authorization_count": 2,
                "current_redemption_idempotency_sha256": hashlib.sha256(
                    REDEMPTION_IDEMPOTENCY_KEY.encode()
                ).hexdigest(),
                "current_redemption_result_fingerprint": "6" * 64,
                "last_redeemed_at": datetime(2026, 3, 13, 18, 13),
                "redemption_count": 2,
            },
        )
        with patch.object(
            safe_reset_doctype.frappe,
            "get_doc",
            return_value=reissued,
        ):
            rebound.validate()

    def test_production_doctype_keeps_request_digests_immutable(self) -> None:
        previous = _production_safe_reset_doc(status="authorized")
        for fieldname, digest in (
            ("evidence_fingerprint", "9" * 64),
            ("request_fingerprint", "a" * 64),
        ):
            current = _production_safe_reset_doc(
                status="authorized",
                overrides={fieldname: digest},
            )
            with (
                self.subTest(fieldname=fieldname),
                patch.object(
                    safe_reset_doctype.frappe,
                    "get_doc",
                    return_value=previous,
                ),
                self.assertRaisesRegex(
                    safe_reset.frappe.ValidationError,
                    f"immutable: {fieldname}",
                ),
            ):
                current.validate()

    def test_production_doctype_allows_only_pre_rotation_expiry(self) -> None:
        authorized = _production_safe_reset_doc(status="authorized")
        expired = _production_safe_reset_doc(status="expired")
        with patch.object(
            safe_reset_doctype.frappe,
            "get_doc",
            return_value=authorized,
        ):
            expired.validate()

        redeemed = _production_safe_reset_doc(status="redeemed")
        redeemed.status = "expired"
        with (
            patch.object(
                safe_reset_doctype.frappe,
                "get_doc",
                return_value=_production_safe_reset_doc(status="redeemed"),
            ),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "Cancelled or expired safe reset cannot contain credential rotation",
            ),
        ):
            redeemed.validate()

    def test_production_doctype_accepts_only_immutable_pre_rotation_cancellation(
        self,
    ) -> None:
        requested = _production_safe_reset_doc(status="requested")
        cancelled = _production_safe_reset_doc(status="cancelled")
        with patch.object(
            safe_reset_doctype.frappe,
            "get_doc",
            return_value=requested,
        ):
            cancelled.validate()

        manager_cancelled = _production_safe_reset_doc(
            status="cancelled",
            overrides={
                "cancellation_origin": "system_manager",
                "cancelled_by_user": "Administrator",
                "cancelled_by_api_user": "",
            },
        )
        with patch.object(
            safe_reset_doctype.frappe,
            "get_doc",
            return_value=requested,
        ):
            manager_cancelled.validate()

        invalid_manager_actor = _production_safe_reset_doc(
            status="cancelled",
            overrides={
                "cancellation_origin": "system_manager",
                "cancelled_by_user": "Administrator",
                "cancelled_by_api_user": "device-001@kopos.local",
            },
        )
        with (
            patch.object(
                safe_reset_doctype.frappe,
                "get_doc",
                return_value=requested,
            ),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "identity or reason is invalid",
            ),
        ):
            invalid_manager_actor.validate()

        mutated = _production_safe_reset_doc(
            status="cancelled",
            overrides={
                "cancellation_reason": (
                    "A different cancellation reason must never replace the audit"
                )
            },
        )
        with (
            patch.object(
                safe_reset_doctype.frappe,
                "get_doc",
                return_value=cancelled,
            ),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "cancellation result is immutable",
            ),
        ):
            mutated.validate()

    def test_zero_recovery_evidence_rejects_an_acknowledgement(self) -> None:
        reset_doc = _AuditDoc(
            {
                "reset_id": RESET_ID,
                "export_sha256": EXPORT_SHA256,
                "export_content_sha256": EXPORT_CONTENT_SHA256,
                **ZERO_MIGRATION_RECOVERY,
            }
        )
        with self.assertRaisesRegex(
            safe_reset.frappe.ValidationError,
            "only valid when recovery points exist",
        ):
            safe_reset._migration_recovery_authorization_updates(
                reset_doc,
                first_authorization=True,
                confirmation=MIGRATION_RECOVERY_ACK_CONFIRMATION,
                acknowledgement_reason=MIGRATION_RECOVERY_ACK_REASON,
            )

    def test_ack_fingerprint_matches_doctype_validation_contract(self) -> None:
        reset_doc = _AuditDoc(
            {
                "reset_id": RESET_ID,
                "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
                "export_sha256": EXPORT_SHA256,
                "export_content_sha256": EXPORT_CONTENT_SHA256,
                "export_byte_length": EXPORT_BYTE_LENGTH,
                **PENDING_MIGRATION_RECOVERY,
            }
        )
        updates = safe_reset._migration_recovery_authorization_updates(
            reset_doc,
            first_authorization=True,
            confirmation=MIGRATION_RECOVERY_ACK_CONFIRMATION,
            acknowledgement_reason=MIGRATION_RECOVERY_ACK_REASON,
        )
        reset_doc.__dict__.update(updates)

        doctype_doc = safe_reset_doctype.KoPOSDeviceSafeReset()
        doctype_doc.__dict__.update(reset_doc.__dict__)
        self.assertEqual(
            doctype_doc._migration_recovery_ack_fingerprint(),
            updates["migration_recovery_ack_fingerprint"],
        )
        doctype_doc.migration_recovery_captured_pending_total = 4
        self.assertNotEqual(
            doctype_doc._migration_recovery_ack_fingerprint(),
            updates["migration_recovery_ack_fingerprint"],
        )

    def test_recovery_registration_persists_immutable_evidence_and_business_gate(self) -> None:
        captured: list[_AuditDoc] = []

        def make_doc(values: dict[str, object]) -> _AuditDoc:
            doc = _AuditDoc(values)
            captured.append(doc)
            return doc

        business_gate = MagicMock()
        with (
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(safe_reset, "_find_matching_reset_for_update", return_value=None),
            patch.object(safe_reset, "_find_active_reset_for_update", return_value=None),
            patch.object(safe_reset, "_assert_no_open_shift_or_unresolved_projection", business_gate),
            patch.object(safe_reset.frappe, "get_doc", side_effect=make_doc),
            patch.object(
                safe_reset,
                "privileged_device_api_operation",
                return_value=nullcontext(),
            ),
            patch.object(safe_reset.frappe.db, "commit"),
        ):
            response = safe_reset._register_safe_reset_request(**_registration_kwargs())

        self.assertEqual(response["status"], "requested")
        self.assertEqual(response["request_origin"], "credential_recovery")
        business_gate.assert_called_once_with("tab-a-001")
        audit = captured[0]
        self.assertTrue(audit.inserted)
        self.assertEqual(audit.export_content_sha256, EXPORT_CONTENT_SHA256)
        self.assertEqual(audit.warehouse, "Main Warehouse")
        self.assertEqual(audit.registered_by_system_manager, "Administrator")
        self.assertEqual(audit.migration_recovery_point_count, 0)
        self.assertFalse(audit.migration_recovery_review_required)
        self.assertEqual(response["migration_recovery_point_count"], 0)
        self.assertFalse(response["migration_recovery_review_required"])
        self.assertFalse(hasattr(audit, "reset_proof_nonce"))
        self.assertFalse(hasattr(audit, "api_secret"))

    def test_registration_ack_construction_failure_never_commits(self) -> None:
        captured: list[_AuditDoc] = []

        def make_doc(values: dict[str, object]) -> _AuditDoc:
            doc = _AuditDoc(values)
            captured.append(doc)
            return doc

        commit = MagicMock()
        with (
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=None,
            ),
            patch.object(
                safe_reset,
                "_find_active_reset_for_update",
                return_value=None,
            ),
            patch.object(
                safe_reset,
                "_assert_no_open_shift_or_unresolved_projection",
            ),
            patch.object(safe_reset.frappe, "get_doc", side_effect=make_doc),
            patch.object(
                safe_reset,
                "privileged_device_api_operation",
                return_value=nullcontext(),
            ),
            patch.object(
                safe_reset,
                "_request_ack",
                side_effect=safe_reset.frappe.ValidationError(
                    "ACK construction failed"
                ),
            ),
            patch.object(safe_reset.frappe.db, "commit", commit),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "ACK construction failed",
            ),
        ):
            safe_reset._register_safe_reset_request(**_registration_kwargs())

        self.assertTrue(captured[0].inserted)
        commit.assert_not_called()

    def test_abandonment_persists_terminal_fence_before_acknowledging(self) -> None:
        captured: list[_AuditDoc] = []
        lock_events: list[str] = []

        def lock_device(device_name: str) -> None:
            self.assertEqual(device_name, "KOPOS-DEVICE-001")
            lock_events.append("device")

        def find_matching(**kwargs: object) -> None:
            self.assertEqual(lock_events, ["device"])
            self.assertEqual(
                kwargs,
                {
                    "device_id": "tab-a-001",
                    "request_id": REQUEST_ID,
                    "reset_proof_sha256": RESET_PROOF_SHA256,
                },
            )
            lock_events.append("request")
            return None

        def find_cancellation(**kwargs: object) -> None:
            self.assertEqual(lock_events, ["device", "request"])
            self.assertEqual(kwargs["device_id"], "tab-a-001")
            lock_events.append("cancellation")
            return None

        def make_doc(values: dict[str, object]) -> _AuditDoc:
            doc = _AuditDoc(values)
            captured.append(doc)
            return doc

        def commit() -> None:
            lock_events.append("commit")

        safe_reset.frappe.session.user = "device-001@kopos.local"
        with (
            patch.object(
                safe_reset,
                "require_device_context",
                return_value=_device(),
            ),
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(
                safe_reset,
                "_lock_device_for_update",
                side_effect=lock_device,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                side_effect=find_matching,
            ),
            patch.object(
                safe_reset,
                "_find_cancellation_idempotency_for_update",
                side_effect=find_cancellation,
            ),
            patch.object(safe_reset.frappe, "get_doc", side_effect=make_doc),
            patch.object(
                safe_reset,
                "privileged_device_api_operation",
                return_value=nullcontext(),
            ),
            patch.object(safe_reset.frappe.db, "commit", side_effect=commit),
        ):
            response = safe_reset.abandon_unregistered_device_safe_reset_request(
                **_abandonment_kwargs()
            )

        self.assertEqual(
            lock_events,
            ["device", "request", "cancellation", "commit"],
        )
        audit = captured[0]
        self.assertTrue(audit.inserted)
        self.assertEqual(audit.status, "cancelled")
        self.assertEqual(audit.request_origin, "device_authenticated")
        self.assertEqual(audit.request_id, REQUEST_ID)
        self.assertEqual(audit.reset_proof_sha256, RESET_PROOF_SHA256)
        self.assertEqual(audit.export_sha256, EXPORT_SHA256)
        self.assertEqual(audit.export_content_sha256, EXPORT_CONTENT_SHA256)
        self.assertEqual(audit.export_byte_length, EXPORT_BYTE_LENGTH)
        self.assertEqual(audit.drained_row_count, 12)
        self.assertEqual(audit.previous_config_version, 2)
        self.assertEqual(audit.requested_by_api_user, "device-001@kopos.local")
        self.assertEqual(
            audit.cancellation_idempotency_sha256,
            hashlib.sha256(ABANDONMENT_IDEMPOTENCY_KEY.encode()).hexdigest(),
        )
        self.assertEqual(
            audit.cancellation_reason,
            safe_reset.ABANDONMENT_CANCELLATION_REASON,
        )
        safe_reset._validate_stored_cancellation_result(audit)

        schema_doc = safe_reset_doctype.KoPOSDeviceSafeReset()
        schema_doc.__dict__.update(audit.__dict__)
        schema_doc.is_new = lambda: True
        schema_doc.validate()

        self.assertEqual(response["status"], "cancelled")
        self.assertEqual(response["lifecycle_status"], "cancelled")
        self.assertEqual(response["abandonment_status"], "fenced")
        self.assertTrue(response["local_release_authorized"])
        self.assertEqual(response["request_id"], REQUEST_ID)
        self.assertEqual(response["device_id"], "tab-a-001")
        self.assertEqual(response["reset_proof_sha256"], RESET_PROOF_SHA256)
        self.assertEqual(response["export_sha256"], EXPORT_SHA256)
        self.assertEqual(
            response["export_content_sha256"],
            EXPORT_CONTENT_SHA256,
        )
        self.assertEqual(response["export_byte_length"], EXPORT_BYTE_LENGTH)
        self.assertEqual(response["cancelled_at"], "2026-03-13T10:05:00Z")

    def test_abandonment_ack_failure_never_commits_tombstone(self) -> None:
        captured: list[_AuditDoc] = []

        def make_doc(values: dict[str, object]) -> _AuditDoc:
            doc = _AuditDoc(values)
            captured.append(doc)
            return doc

        commit = MagicMock()
        safe_reset.frappe.session.user = "device-001@kopos.local"
        with (
            patch.object(
                safe_reset,
                "require_device_context",
                return_value=_device(),
            ),
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=None,
            ),
            patch.object(
                safe_reset,
                "_find_cancellation_idempotency_for_update",
                return_value=None,
            ),
            patch.object(safe_reset.frappe, "get_doc", side_effect=make_doc),
            patch.object(
                safe_reset,
                "privileged_device_api_operation",
                return_value=nullcontext(),
            ),
            patch.object(
                safe_reset,
                "_abandonment_fence_ack",
                side_effect=RuntimeError("fence ACK construction failed"),
            ),
            patch.object(safe_reset.frappe.db, "commit", commit),
            self.assertRaisesRegex(RuntimeError, "fence ACK construction failed"),
        ):
            safe_reset.abandon_unregistered_device_safe_reset_request(
                **_abandonment_kwargs()
            )

        self.assertTrue(captured[0].inserted)
        commit.assert_not_called()

    def test_abandonment_returns_exact_existing_ack_without_mutation(self) -> None:
        existing = _authorized_reset()
        existing.status = "requested"
        existing.drained_row_count = 12
        safe_reset.frappe.session.user = "device-001@kopos.local"

        with patch.object(
            safe_reset,
            "_validate_reset_scope",
            return_value=_scope(),
        ):
            prepared = safe_reset._prepare_safe_reset_request_evidence(
                **{
                    key: value
                    for key, value in _device_registration_kwargs().items()
                    if key
                    not in {
                        "registered_by_system_manager",
                        "credential_recovery_confirmed_at",
                        "validate_business_state",
                    }
                }
            )
        existing.evidence_fingerprint = prepared["evidence_fingerprint"]
        existing.request_fingerprint = prepared["request_fingerprint"]
        commit = MagicMock()
        make_doc = MagicMock()
        cancellation_lookup = MagicMock()
        with (
            patch.object(
                safe_reset,
                "require_device_context",
                return_value=_device(),
            ),
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=existing,
            ),
            patch.object(
                safe_reset,
                "_find_cancellation_idempotency_for_update",
                cancellation_lookup,
            ),
            patch.object(safe_reset.frappe, "get_doc", make_doc),
            patch.object(safe_reset.frappe.db, "commit", commit),
        ):
            response = safe_reset.abandon_unregistered_device_safe_reset_request(
                **_abandonment_kwargs()
            )

        self.assertEqual(response, safe_reset._request_ack(existing))
        self.assertEqual(existing.save_count, 0)
        cancellation_lookup.assert_not_called()
        make_doc.assert_not_called()
        commit.assert_not_called()

    def test_abandonment_replays_lost_fence_ack_only_with_exact_key(self) -> None:
        existing = _authorized_reset()
        existing.status = "cancelled"
        existing.drained_row_count = 12
        safe_reset.frappe.session.user = "device-001@kopos.local"
        with patch.object(
            safe_reset,
            "_validate_reset_scope",
            return_value=_scope(),
        ):
            prepared = safe_reset._prepare_safe_reset_request_evidence(
                **{
                    key: value
                    for key, value in _device_registration_kwargs().items()
                    if key
                    not in {
                        "registered_by_system_manager",
                        "credential_recovery_confirmed_at",
                        "validate_business_state",
                    }
                }
            )
        existing.evidence_fingerprint = prepared["evidence_fingerprint"]
        existing.request_fingerprint = prepared["request_fingerprint"]
        existing.cancellation_idempotency_sha256 = hashlib.sha256(
            ABANDONMENT_IDEMPOTENCY_KEY.encode()
        ).hexdigest()
        existing.cancellation_reason = safe_reset.ABANDONMENT_CANCELLATION_REASON
        existing.cancellation_origin = safe_reset.CANCELLATION_ORIGIN_DEVICE
        existing.cancelled_by_user = "device-001@kopos.local"
        existing.cancelled_by_api_user = "device-001@kopos.local"
        existing.cancelled_at = datetime(2026, 3, 13, 18, 5)
        existing.cancellation_result_fingerprint = (
            safe_reset._cancellation_result_fingerprint(
                existing,
                cancellation_idempotency_sha256=(
                    existing.cancellation_idempotency_sha256
                ),
                cancellation_reason=existing.cancellation_reason,
                cancellation_origin=existing.cancellation_origin,
                cancelled_by_user=existing.cancelled_by_user,
                cancelled_by_api_user=existing.cancelled_by_api_user,
                cancelled_at=existing.cancelled_at,
            )
        )
        commit = MagicMock()
        common = {
            "require_device_context": patch.object(
                safe_reset,
                "require_device_context",
                return_value=_device(),
            ),
            "unique": patch.object(
                safe_reset,
                "ensure_unique_device_api_user",
            ),
            "scope": patch.object(
                safe_reset,
                "_validate_reset_scope",
                return_value=_scope(),
            ),
            "lock": patch.object(safe_reset, "_lock_device_for_update"),
            "device": patch.object(
                safe_reset,
                "get_device_doc",
                return_value=_device(),
            ),
            "find": patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=existing,
            ),
            "commit": patch.object(safe_reset.frappe.db, "commit", commit),
        }
        with ExitStack() as stack:
            for patcher in common.values():
                stack.enter_context(patcher)
            replay = safe_reset.abandon_unregistered_device_safe_reset_request(
                **_abandonment_kwargs()
            )
        self.assertEqual(replay, safe_reset._abandonment_fence_ack(existing))
        self.assertEqual(replay["abandonment_status"], "fenced")
        self.assertEqual(existing.save_count, 0)
        commit.assert_not_called()

        wrong_key_payload = {
            **_abandonment_kwargs(),
            "cancellation_idempotency_key": "r" * 43,
        }
        common = {
            "require_device_context": patch.object(
                safe_reset,
                "require_device_context",
                return_value=_device(),
            ),
            "unique": patch.object(
                safe_reset,
                "ensure_unique_device_api_user",
            ),
            "scope": patch.object(
                safe_reset,
                "_validate_reset_scope",
                return_value=_scope(),
            ),
            "lock": patch.object(safe_reset, "_lock_device_for_update"),
            "device": patch.object(
                safe_reset,
                "get_device_doc",
                return_value=_device(),
            ),
            "find": patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=existing,
            ),
        }
        with ExitStack() as stack:
            for patcher in common.values():
                stack.enter_context(patcher)
            stack.enter_context(
                self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "idempotency key was already used differently",
                )
            )
            safe_reset.abandon_unregistered_device_safe_reset_request(
                **wrong_key_payload
            )

    def test_abandonment_existing_match_requires_every_immutable_field(self) -> None:
        safe_reset.frappe.session.user = "device-001@kopos.local"
        with patch.object(
            safe_reset,
            "_validate_reset_scope",
            return_value=_scope(),
        ):
            prepared = safe_reset._prepare_safe_reset_request_evidence(
                **{
                    key: value
                    for key, value in _device_registration_kwargs().items()
                    if key
                    not in {
                        "registered_by_system_manager",
                        "credential_recovery_confirmed_at",
                        "validate_business_state",
                    }
                }
            )

        for fieldname, value in (
            ("reason", "Different retained evidence"),
            ("exported_at", datetime(2026, 3, 13, 18, 1)),
            ("drained_row_count", 11),
            ("queue_failed_count", 1),
            ("warehouse", "Different Warehouse"),
            ("migration_recovery_point_count", 1),
            ("registered_by_system_manager", "Administrator"),
        ):
            with self.subTest(fieldname=fieldname):
                existing = _authorized_reset()
                existing.evidence_fingerprint = prepared["evidence_fingerprint"]
                existing.request_fingerprint = prepared["request_fingerprint"]
                setattr(existing, fieldname, value)
                commit = MagicMock()
                with (
                    patch.object(
                        safe_reset,
                        "require_device_context",
                        return_value=_device(),
                    ),
                    patch.object(safe_reset, "ensure_unique_device_api_user"),
                    patch.object(
                        safe_reset,
                        "_validate_reset_scope",
                        return_value=_scope(),
                    ),
                    patch.object(safe_reset, "_lock_device_for_update"),
                    patch.object(
                        safe_reset,
                        "get_device_doc",
                        return_value=_device(),
                    ),
                    patch.object(
                        safe_reset,
                        "_find_matching_reset_for_update",
                        return_value=existing,
                    ),
                    patch.object(safe_reset.frappe.db, "commit", commit),
                    self.assertRaises(safe_reset.frappe.ValidationError),
                ):
                    safe_reset.abandon_unregistered_device_safe_reset_request(
                        **_abandonment_kwargs()
                    )
                commit.assert_not_called()

    def test_abandonment_rejects_existing_evidence_mismatch_and_key_reuse(self) -> None:
        safe_reset.frappe.session.user = "device-001@kopos.local"
        existing = _authorized_reset()
        existing.request_fingerprint = "0" * 64
        commit = MagicMock()
        with (
            patch.object(
                safe_reset,
                "require_device_context",
                return_value=_device(),
            ),
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=existing,
            ),
            patch.object(safe_reset.frappe.db, "commit", commit),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "does not match the existing request",
            ),
        ):
            safe_reset.abandon_unregistered_device_safe_reset_request(
                **_abandonment_kwargs()
            )
        commit.assert_not_called()

        with (
            patch.object(
                safe_reset,
                "require_device_context",
                return_value=_device(),
            ),
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=None,
            ),
            patch.object(
                safe_reset,
                "_find_cancellation_idempotency_for_update",
                return_value=_authorized_reset(),
            ),
            patch.object(safe_reset.frappe.db, "commit", commit),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "idempotency key was already used",
            ),
        ):
            safe_reset.abandon_unregistered_device_safe_reset_request(
                **_abandonment_kwargs()
            )
        commit.assert_not_called()

    def test_abandonment_tombstone_blocks_delayed_registration(self) -> None:
        captured: list[_AuditDoc] = []

        def make_doc(values: dict[str, object]) -> _AuditDoc:
            doc = _AuditDoc(values)
            captured.append(doc)
            return doc

        safe_reset.frappe.session.user = "device-001@kopos.local"
        with (
            patch.object(
                safe_reset,
                "require_device_context",
                return_value=_device(),
            ),
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=None,
            ),
            patch.object(
                safe_reset,
                "_find_cancellation_idempotency_for_update",
                return_value=None,
            ),
            patch.object(safe_reset.frappe, "get_doc", side_effect=make_doc),
            patch.object(
                safe_reset,
                "privileged_device_api_operation",
                return_value=nullcontext(),
            ),
            patch.object(safe_reset.frappe.db, "commit"),
        ):
            safe_reset.abandon_unregistered_device_safe_reset_request(
                **_abandonment_kwargs()
            )
        tombstone = captured[0]

        new_doc = MagicMock()
        registration_commit = MagicMock()
        with (
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=tombstone,
            ),
            patch.object(safe_reset.frappe, "get_doc", new_doc),
            patch.object(safe_reset.frappe.db, "commit", registration_commit),
        ):
            delayed_response = safe_reset._register_safe_reset_request(
                **_device_registration_kwargs()
            )

        self.assertEqual(delayed_response["reset_id"], tombstone.reset_id)
        self.assertEqual(delayed_response["lifecycle_status"], "cancelled")
        new_doc.assert_not_called()
        registration_commit.assert_not_called()

        for fieldname, value in (
            ("request_id", "different-request-002"),
            ("reset_proof_sha256", "4" * 64),
        ):
            with self.subTest(reused_binding=fieldname):
                conflicting_kwargs = _device_registration_kwargs()
                conflicting_kwargs[fieldname] = value
                conflicting_doc = MagicMock()
                conflicting_commit = MagicMock()
                with (
                    patch.object(
                        safe_reset,
                        "_validate_reset_scope",
                        return_value=_scope(),
                    ),
                    patch.object(safe_reset, "_lock_device_for_update"),
                    patch.object(
                        safe_reset,
                        "get_device_doc",
                        return_value=_device(),
                    ),
                    patch.object(
                        safe_reset,
                        "_find_matching_reset_for_update",
                        return_value=tombstone,
                    ),
                    patch.object(
                        safe_reset.frappe,
                        "get_doc",
                        conflicting_doc,
                    ),
                    patch.object(
                        safe_reset.frappe.db,
                        "commit",
                        conflicting_commit,
                    ),
                    self.assertRaisesRegex(
                        safe_reset.frappe.ValidationError,
                        "reused with different evidence",
                    ),
                ):
                    safe_reset._register_safe_reset_request(
                        **conflicting_kwargs
                    )
                conflicting_doc.assert_not_called()
                conflicting_commit.assert_not_called()

    def test_all_safe_reset_transactions_lock_device_before_reset(self) -> None:
        reset_doc = _authorized_reset()
        lock_events: list[str] = []

        def read_device_name(
            doctype: str,
            name: str,
            fieldname: str,
        ) -> str:
            self.assertEqual(
                (doctype, name, fieldname),
                (
                    safe_reset.SAFE_RESET_DOCTYPE,
                    RESET_ID,
                    "device",
                ),
            )
            lock_events.append("read_identity")
            return "KOPOS-DEVICE-001"

        def lock_device(device_name: str) -> None:
            self.assertEqual(device_name, "KOPOS-DEVICE-001")
            lock_events.append("lock_device")

        def lock_reset(reset_id: str) -> _AuditDoc:
            self.assertEqual(reset_id, RESET_ID)
            lock_events.append("lock_reset")
            return reset_doc

        with (
            patch.object(
                safe_reset.frappe.db,
                "get_value",
                side_effect=read_device_name,
            ),
            patch.object(
                safe_reset,
                "_lock_device_for_update",
                side_effect=lock_device,
            ),
            patch.object(
                safe_reset,
                "_get_reset_for_update",
                side_effect=lock_reset,
            ),
        ):
            locked = safe_reset._get_reset_with_device_lock(RESET_ID)

        self.assertIs(locked, reset_doc)
        self.assertEqual(
            lock_events,
            ["read_identity", "lock_device", "lock_reset"],
        )

        reset_doc.device = "KOPOS-DEVICE-CHANGED"
        with (
            patch.object(
                safe_reset.frappe.db,
                "get_value",
                return_value="KOPOS-DEVICE-001",
            ),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(
                safe_reset,
                "_get_reset_for_update",
                return_value=reset_doc,
            ),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "device binding changed",
            ),
        ):
            safe_reset._get_reset_with_device_lock(RESET_ID)
        reset_doc.device = "KOPOS-DEVICE-001"

        registration_source = inspect.getsource(
            safe_reset._register_safe_reset_request
        )
        registration_device_lock = registration_source.index(
            "_lock_device_for_update("
        )
        self.assertGreater(
            registration_source.index("_find_matching_reset_for_update("),
            registration_device_lock,
        )
        self.assertGreater(
            registration_source.index("_find_active_reset_for_update("),
            registration_device_lock,
        )

        for operation in (
            safe_reset.authorize_device_safe_reset,
            safe_reset.redeem_device_safe_reset_approval,
            safe_reset.complete_device_safe_reset,
        ):
            with self.subTest(operation=operation.__name__):
                source = inspect.getsource(operation)
                self.assertIn("_get_reset_with_device_lock(", source)
                self.assertNotIn("_get_reset_for_update(", source)
                self.assertNotIn("_lock_device_for_update(", source)

        authorization_source = inspect.getsource(
            safe_reset.authorize_device_safe_reset
        )
        self.assertIn(
            'lifecycle_status not in {"requested", "authorized", "redeemed"}',
            authorization_source,
        )
        completion_source = inspect.getsource(
            safe_reset.complete_device_safe_reset
        )
        self.assertIn("expected_device_name=device_name", completion_source)

    def test_registration_and_existing_reset_operation_do_not_deadlock(self) -> None:
        reset_doc = _authorized_reset()
        device_lock = threading.Lock()
        event_lock = threading.Lock()
        transaction_state = threading.local()
        start_barrier = threading.Barrier(2)
        lock_events: list[tuple[str, str]] = []

        def record(event: str) -> None:
            operation = str(getattr(transaction_state, "operation", ""))
            with event_lock:
                lock_events.append((operation, event))

        def lock_device(device_name: str) -> None:
            self.assertEqual(device_name, "KOPOS-DEVICE-001")
            start_barrier.wait(timeout=2)
            device_lock.acquire()
            transaction_state.holds_device_lock = True
            record("device")

        def assert_device_locked() -> None:
            self.assertTrue(
                getattr(transaction_state, "holds_device_lock", False)
            )

        def find_matching(**kwargs: object) -> None:
            assert_device_locked()
            record("reset")
            return None

        def find_active(device_id: str) -> None:
            self.assertEqual(device_id, "tab-a-001")
            assert_device_locked()
            record("reset")
            return None

        def get_reset(reset_id: str) -> _AuditDoc:
            self.assertEqual(reset_id, RESET_ID)
            assert_device_locked()
            record("reset")
            return reset_doc

        def release_transaction(*args: object, **kwargs: object) -> None:
            if getattr(transaction_state, "holds_device_lock", False):
                transaction_state.holds_device_lock = False
                device_lock.release()

        def make_doc(values: dict[str, object]) -> _AuditDoc:
            return _AuditDoc(values)

        def register() -> dict[str, object]:
            transaction_state.operation = "register"
            return safe_reset._register_safe_reset_request(
                **_registration_kwargs()
            )

        def use_existing_reset() -> _AuditDoc:
            transaction_state.operation = "existing"
            locked = safe_reset._get_reset_with_device_lock(RESET_ID)
            safe_reset.frappe.db.commit()
            return locked

        with (
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(
                safe_reset,
                "_lock_device_for_update",
                side_effect=lock_device,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                side_effect=find_matching,
            ),
            patch.object(
                safe_reset,
                "_find_active_reset_for_update",
                side_effect=find_active,
            ),
            patch.object(
                safe_reset,
                "_get_reset_for_update",
                side_effect=get_reset,
            ),
            patch.object(
                safe_reset,
                "_assert_no_open_shift_or_unresolved_projection",
            ),
            patch.object(safe_reset.frappe, "get_doc", side_effect=make_doc),
            patch.object(
                safe_reset.frappe.db,
                "get_value",
                return_value="KOPOS-DEVICE-001",
            ),
            patch.object(
                safe_reset,
                "privileged_device_api_operation",
                return_value=nullcontext(),
            ),
            patch.object(
                safe_reset.frappe.db,
                "commit",
                side_effect=release_transaction,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                registration_future = executor.submit(register)
                existing_future = executor.submit(use_existing_reset)
                registration_response = registration_future.result(timeout=5)
                existing_result = existing_future.result(timeout=5)

        self.assertEqual(registration_response["status"], "requested")
        self.assertIs(existing_result, reset_doc)
        self.assertFalse(device_lock.locked())
        events_by_operation = {
            operation: [
                event
                for current_operation, event in lock_events
                if current_operation == operation
            ]
            for operation in ("register", "existing")
        }
        self.assertEqual(
            events_by_operation,
            {
                "register": ["device", "reset", "reset"],
                "existing": ["device", "reset"],
            },
        )

    def test_admin_recovery_resolves_lost_authenticated_request_by_evidence(self) -> None:
        kwargs = _registration_kwargs()
        scope = _scope()
        exported_at, _, _ = safe_reset._validate_export_timestamp(
            kwargs["exported_at"],
            allow_stale_export=False,
            stale_export_override_reason=None,
            credential_recovery=True,
        )
        evidence_fingerprint = safe_reset._request_fingerprint(
            {
                "request_id": REQUEST_ID,
                "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
                "device_id": "tab-a-001",
                "api_user": "device-001@kopos.local",
                "reason": kwargs["reason"],
                "export_sha256": EXPORT_SHA256,
                "export_content_sha256": EXPORT_CONTENT_SHA256,
                "export_byte_length": EXPORT_BYTE_LENGTH,
                "exported_at": exported_at.isoformat(),
                "drained_row_count": 12,
                "queue_evidence": ZERO_QUEUE,
                **ZERO_MIGRATION_RECOVERY,
                "previous_config_version": 2,
                "reset_proof_sha256": RESET_PROOF_SHA256,
                "scope": scope,
            }
        )
        existing = _AuditDoc(
            {
                "reset_id": RESET_ID,
                "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
                "request_id": REQUEST_ID,
                "request_origin": "device_authenticated",
                "request_fingerprint": "c" * 64,
                "evidence_fingerprint": evidence_fingerprint,
                "status": "requested",
                "device_id": "tab-a-001",
                "previous_config_version": 2,
                "export_sha256": EXPORT_SHA256,
                "export_content_sha256": EXPORT_CONTENT_SHA256,
                "export_byte_length": EXPORT_BYTE_LENGTH,
                "exported_at": exported_at,
                **scope,
            }
        )
        with (
            patch.object(safe_reset, "_validate_reset_scope", return_value=scope),
            patch.object(safe_reset, "_lock_device_for_update"),
            patch.object(safe_reset, "get_device_doc", return_value=_device(config_version=99)),
            patch.object(
                safe_reset,
                "_find_matching_reset_for_update",
                return_value=existing,
            ),
        ):
            response = safe_reset._register_safe_reset_request(**kwargs)
        self.assertEqual(response["reset_id"], RESET_ID)
        self.assertEqual(
            response["registration_resolution"],
            "existing_device_authenticated",
        )
        self.assertEqual(existing.request_origin, "device_authenticated")

    def test_recovery_requires_exact_typed_confirmation(self) -> None:
        with (
            patch.object(safe_reset, "require_system_manager"),
            patch.object(safe_reset, "_register_safe_reset_request") as register_mock,
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "Type RECOVER",
            ),
        ):
            safe_reset.register_device_credential_recovery(
                safe_reset_protocol_version=SAFE_RESET_PROTOCOL_VERSION,
                confirmation="recover tab-a-001",
                request_id=REQUEST_ID,
                device_id="tab-a-001",
                reason="support reset",
                export_sha256=EXPORT_SHA256,
                export_content_sha256=EXPORT_CONTENT_SHA256,
                export_byte_length=EXPORT_BYTE_LENGTH,
                exported_at="2026-03-13T10:00:00Z",
                drained_row_count=0,
                queue_evidence=ZERO_QUEUE,
                **ZERO_MIGRATION_RECOVERY,
                previous_config_version=2,
                reset_proof_sha256=RESET_PROOF_SHA256,
                erp_base_url="https://erp.example.com/tenant-a",
                company="JiJi",
                currency="MYR",
                pos_profile="Counter 1",
                warehouse="Main Warehouse",
            )
        register_mock.assert_not_called()

    def test_export_timestamp_rejects_malformed_future_and_stale_without_admin_override(self) -> None:
        with self.assertRaises(safe_reset.frappe.ValidationError):
            safe_reset._validate_export_timestamp(
                "not-a-time",
                allow_stale_export=False,
                stale_export_override_reason=None,
                credential_recovery=False,
            )
        with self.assertRaisesRegex(
            safe_reset.frappe.ValidationError,
            "timezone offset",
        ):
            safe_reset._validate_export_timestamp(
                "2026-03-13T18:00:00",
                allow_stale_export=False,
                stale_export_override_reason=None,
                credential_recovery=False,
            )

        future, _, _ = safe_reset._validate_export_timestamp(
            "2026-03-13T10:11:00Z",
            allow_stale_export=False,
            stale_export_override_reason=None,
            credential_recovery=False,
        )
        with self.assertRaisesRegex(safe_reset.frappe.ValidationError, "future"):
            safe_reset._enforce_export_timestamp_freshness(
                future,
                stale_override=False,
                stale_override_reason="",
                credential_recovery=False,
            )

        stale, _, _ = safe_reset._validate_export_timestamp(
            "2026-03-13T09:00:00Z",
            allow_stale_export=False,
            stale_export_override_reason=None,
            credential_recovery=False,
        )
        with self.assertRaisesRegex(safe_reset.frappe.ValidationError, "30-minute"):
            safe_reset._enforce_export_timestamp_freshness(
                stale,
                stale_override=False,
                stale_override_reason="",
                credential_recovery=False,
            )

        override_reason = "Manager verified export custody during a long support handoff"
        stale, override, reason = safe_reset._validate_export_timestamp(
            "2026-03-13T09:00:00Z",
            allow_stale_export=True,
            stale_export_override_reason=override_reason,
            credential_recovery=True,
        )
        safe_reset._enforce_export_timestamp_freshness(
            stale,
            stale_override=override,
            stale_override_reason=reason,
            credential_recovery=True,
        )

    def test_full_erp_base_url_and_scope_include_tenant_path_and_warehouse(self) -> None:
        profile = SimpleNamespace(
            company="JiJi",
            currency="MYR",
            warehouse="Main Warehouse",
        )
        with (
            patch.object(safe_reset.frappe, "get_cached_doc", return_value=profile),
            patch.object(
                safe_reset.frappe.utils,
                "get_url",
                return_value="https://ERP.EXAMPLE.com/tenant-a/",
            ),
        ):
            scope = safe_reset._validate_reset_scope(
                _device(),
                erp_base_url="https://erp.example.com/tenant-a",
                company="JiJi",
                currency="MYR",
                pos_profile="Counter 1",
                warehouse="Main Warehouse",
            )
            self.assertEqual(scope["erp_base_url"], "https://erp.example.com/tenant-a")
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "base URL",
            ):
                safe_reset._validate_reset_scope(
                    _device(),
                    erp_base_url="https://erp.example.com/tenant-b",
                    company="JiJi",
                    currency="MYR",
                    pos_profile="Counter 1",
                    warehouse="Main Warehouse",
                )

    def test_open_shift_and_failed_projection_block_authorization(self) -> None:
        with (
            patch.object(
                safe_reset.frappe.db,
                "sql",
                return_value=[{"name": "SHIFT-1", "status": "Open"}],
            ),
            self.assertRaisesRegex(safe_reset.frappe.ValidationError, "closed"),
        ):
            safe_reset._assert_no_open_shift_or_unresolved_projection("tab-a-001")

    def test_active_reset_lookup_treats_null_and_future_status_as_unresolved(
        self,
    ) -> None:
        sql = MagicMock(return_value=[])
        with patch.object(safe_reset.frappe.db, "sql", sql):
            self.assertIsNone(
                safe_reset._find_active_reset_for_update("tab-a-001")
            )

        query = " ".join(sql.call_args.args[0].split())
        self.assertIn("status IS NULL", query)
        self.assertIn(
            "status NOT IN ('completed', 'cancelled', 'expired')",
            query,
        )
        self.assertNotIn("status IN ('requested', 'authorized', 'redeemed')", query)
        self.assertIn("LIMIT 1", query)
        self.assertIn("FOR UPDATE", query)

    def test_unknown_or_null_shift_state_is_fail_closed(self) -> None:
        for anomalous_status in (None, "FutureLifecycleState"):
            sql = MagicMock(
                return_value=[{"name": "SHIFT-ANOMALOUS", "status": anomalous_status}]
            )
            with (
                self.subTest(status=anomalous_status),
                patch.object(safe_reset.frappe.db, "sql", sql),
                self.assertRaisesRegex(
                    safe_reset.frappe.ValidationError,
                    "every FB Shift.*closed",
                ),
            ):
                safe_reset._assert_no_open_shift_or_unresolved_projection(
                    "tab-a-001"
                )
            active_shift_query = " ".join(sql.call_args.args[0].split())
            self.assertIn("status IS NULL", active_shift_query)
            self.assertIn(
                "status NOT IN ('Closed', 'Cancelled')",
                active_shift_query,
            )

        sql_results = [[], [{"name": "PROJECTION-1"}]]
        with (
            patch.object(safe_reset.frappe.db, "sql", side_effect=sql_results),
            self.assertRaisesRegex(safe_reset.frappe.ValidationError, "projections"),
        ):
            safe_reset._assert_no_open_shift_or_unresolved_projection("tab-a-001")

    def test_large_history_business_gate_has_constant_query_and_lock_footprint(
        self,
    ) -> None:
        sql = MagicMock(return_value=[])
        with patch.object(safe_reset.frappe.db, "sql", sql):
            safe_reset._assert_no_open_shift_or_unresolved_projection("tab-a-001")

        self.assertEqual(sql.call_count, 7)
        normalized_queries = [
            " ".join(call.args[0].split()) for call in sql.call_args_list
        ]
        self.assertTrue(all("LIMIT 1" in query for query in normalized_queries))
        self.assertEqual(
            sum("FOR UPDATE" in query for query in normalized_queries),
            1,
        )
        self.assertTrue(
            all(query.count("%s") == 1 for query in normalized_queries)
        )
        self.assertTrue(
            all(call.args[1] == ("tab-a-001",) for call in sql.call_args_list)
        )
        self.assertTrue(
            all(len(query) < 900 for query in normalized_queries),
            "query size must not grow with lifetime order history",
        )

        indexed_fields = {
            "fb_shift": {"device_id", "status"},
            "fb_order": {"device_id"},
            "fb_projection_log": {"source_doctype", "source_name", "state"},
            "fb_return_event": {"fb_order"},
            "fb_waste_event": {"shift"},
            "maybank_qr_transaction": {
                "device_id",
                "status",
                "manual_reconciliation_status",
            },
            "manual_qr_reconciliation": {"device_id", "status"},
        }
        for doctype_path, expected_fields in indexed_fields.items():
            schema = json.loads(
                Path(
                    "kopos_connector/kopos/doctype/"
                    f"{doctype_path}/{doctype_path}.json"
                ).read_text()
            )
            indexed = {
                field["fieldname"]
                for field in schema["fields"]
                if field.get("search_index") == 1
            }
            self.assertTrue(expected_fields.issubset(indexed))

    def test_unresolved_maybank_and_manual_qr_state_block_safe_reset(self) -> None:
        cases = (
            (
                "Maybank QR",
                [[], [], [], [], [], [{"name": "MBQR-1"}]],
            ),
            (
                "manual QR reconciliation",
                [[], [], [], [], [], [], [{"name": "MQR-1"}]],
            ),
        )
        for expected_message, query_results in cases:
            sql = MagicMock(side_effect=query_results)
            with (
                self.subTest(expected_message=expected_message),
                patch.object(safe_reset.frappe.db, "sql", sql),
                self.assertRaisesRegex(
                    safe_reset.frappe.ValidationError,
                    expected_message,
                ),
            ):
                safe_reset._assert_no_open_shift_or_unresolved_projection(
                    "tab-a-001"
                )

        sql = MagicMock(return_value=[])
        with patch.object(safe_reset.frappe.db, "sql", sql):
            safe_reset._assert_no_open_shift_or_unresolved_projection("tab-a-001")
        normalized_queries = [
            " ".join(call.args[0].split()) for call in sql.call_args_list
        ]
        maybank_query = next(
            query
            for query in normalized_queries
            if "tabMaybank QR Transaction" in query
        )
        manual_query = next(
            query
            for query in normalized_queries
            if "tabManual QR Reconciliation" in query
        )
        self.assertIn("status IS NULL", maybank_query)
        self.assertIn("consumed_at IS NULL", maybank_query)
        self.assertIn("fb_order IS NULL", maybank_query)
        self.assertIn("sales_invoice IS NULL", maybank_query)
        self.assertIn("manual_reconciliation_status", maybank_query)
        self.assertIn("status IS NULL", manual_query)
        self.assertIn("reconciliation_failed", manual_query)

    def test_credential_recovery_can_replace_incomplete_old_credentials(self) -> None:
        device = _device()
        generated = iter(["new-api-key-001", "new-api-secret-0000000000000001"])

        get_value_calls = [0]

        def sequenced_get_value(doctype: str, name: str, fieldname: str) -> object:
            if fieldname != "api_key":
                return None
            get_value_calls[0] += 1
            return "" if get_value_calls[0] == 1 else "new-api-key-001"

        with (
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(safe_reset.frappe.db, "exists", return_value=True),
            patch.object(safe_reset.frappe.db, "get_value", side_effect=sequenced_get_value),
            patch.object(safe_reset, "_read_api_secret", side_effect=["", "new-api-secret-0000000000000001"]),
            patch.object(safe_reset.frappe, "generate_hash", side_effect=lambda length: next(generated)),
            patch.object(safe_reset.frappe.db, "set_value"),
            patch.object(safe_reset, "set_encrypted_password"),
        ):
            credentials = safe_reset._rotate_device_api_credentials(
                device,
                allow_incomplete_previous=True,
            )
        self.assertEqual(credentials["previous_credential_state"], "missing_both")
        self.assertEqual(credentials["revoked_api_key_sha256"], "")
        self.assertEqual(credentials["issued_api_key_sha256"], hashlib.sha256(b"new-api-key-001").hexdigest())

    def test_credential_recovery_recreates_and_rebinds_missing_dedicated_user(self) -> None:
        target_user = "kopos.device.tab.a.001@kopos.local"
        device = _device()
        device.api_user = "deleted-device-user@kopos.local"
        reset_doc = SimpleNamespace(api_user=target_user)
        inserted_users: list[object] = []
        set_value_calls: list[tuple[object, ...]] = []

        class NewUser:
            def __init__(self, values: dict[str, object]) -> None:
                self.__dict__.update(values)
                self.roles: list[dict[str, str]] = []

            def append(self, fieldname: str, value: dict[str, str]) -> None:
                self.roles.append(value)

            def insert(self, *, ignore_permissions: bool) -> None:
                self.inserted = ignore_permissions
                inserted_users.append(self)

        def exists(doctype: str, name: str) -> bool:
            if doctype == "Role":
                return True
            return False

        with (
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(provisioning, "ensure_unique_device_api_user"),
            patch.object(safe_reset.frappe.db, "exists", side_effect=exists),
            patch.object(
                safe_reset.frappe.db,
                "get_value",
                side_effect=lambda doctype, name, fieldname: 1
                if fieldname == "enabled"
                else None,
            ),
            patch.object(
                safe_reset.frappe.db,
                "set_value",
                side_effect=lambda *args, **kwargs: set_value_calls.append(args),
            ),
            patch.object(
                safe_reset.frappe,
                "get_doc",
                side_effect=lambda values: NewUser(values),
            ),
        ):
            previous_state = safe_reset._restore_recovery_device_api_identity(
                reset_doc,
                device,
            )

        self.assertEqual(previous_state, "missing_user")
        self.assertEqual(device.api_user, target_user)
        self.assertEqual(len(inserted_users), 1)
        self.assertIn({"role": "KoPOS Device API"}, inserted_users[0].roles)
        self.assertIn(("KoPOS Device", device.name, "api_user", target_user), set_value_calls)

    def test_credential_recovery_reenables_disabled_dedicated_user(self) -> None:
        device = _device()
        reset_doc = SimpleNamespace(api_user=device.api_user)
        enabled_reads = iter([0, 1])

        class ExistingUser:
            def __init__(self) -> None:
                self.first_name = "Counter Tablet"
                self.enabled = 0
                self.saved = False

            def set(self, fieldname: str, value: object) -> None:
                self.roles = value

            def save(self, *, ignore_permissions: bool) -> None:
                self.saved = ignore_permissions

        user_doc = ExistingUser()
        with (
            patch.object(safe_reset, "ensure_unique_device_api_user"),
            patch.object(provisioning, "ensure_unique_device_api_user"),
            patch.object(safe_reset.frappe.db, "exists", return_value=True),
            patch.object(
                safe_reset.frappe.db,
                "get_value",
                side_effect=lambda doctype, name, fieldname: next(enabled_reads)
                if fieldname == "enabled"
                else None,
            ),
            patch.object(safe_reset.frappe, "get_doc", return_value=user_doc),
            patch.object(provisioning, "_read_device_api_secret", return_value=""),
        ):
            previous_state = safe_reset._restore_recovery_device_api_identity(
                reset_doc,
                device,
            )

        self.assertEqual(previous_state, "disabled_user")
        self.assertEqual(user_doc.enabled, 1)
        self.assertTrue(user_doc.saved)

    def test_authorization_is_approval_only_and_reissue_never_rotates(self) -> None:
        reset_doc = _AuditDoc(
            {
                "reset_id": RESET_ID,
                "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
                "status": "requested",
                "request_expires_at": datetime(2026, 3, 14, 18, 5),
                "device": "KOPOS-DEVICE-001",
                "device_id": "tab-a-001",
                "api_user": "device-001@kopos.local",
                "request_origin": "credential_recovery",
                "request_id": REQUEST_ID,
                "previous_config_version": 2,
                "new_config_version": 0,
                "export_sha256": EXPORT_SHA256,
                "export_content_sha256": EXPORT_CONTENT_SHA256,
                "export_byte_length": EXPORT_BYTE_LENGTH,
                "queue_pending_count": 0,
                "queue_failed_count": 0,
                "queue_syncing_count": 0,
                "queue_dead_letter_count": 0,
                **PENDING_MIGRATION_RECOVERY,
                "authorization_count": 0,
                "approval_generation": 0,
                "redemption_count": 0,
                **_scope(),
            }
        )
        device = _device()
        restore = MagicMock()
        rotate = MagicMock()
        create_provisioning = MagicMock()

        with (
            patch.object(safe_reset, "require_system_manager"),
            patch.object(safe_reset, "make_savepoint", return_value="sp"),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=device),
            patch.object(safe_reset, "_validate_recovery_approval_binding"),
            patch.object(
                safe_reset,
                "_restore_recovery_device_api_identity",
                restore,
            ),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_assert_stored_queue_evidence_is_drained"),
            patch.object(safe_reset, "_assert_no_open_shift_or_unresolved_projection"),
            patch.object(safe_reset, "_rotate_device_api_credentials", rotate),
            patch.object(safe_reset.frappe.db, "set_value"),
            patch.object(safe_reset.frappe.db, "commit"),
            patch.object(safe_reset, "_new_256_bit_secret", return_value=APPROVAL_TOKEN),
            patch.object(safe_reset.secrets, "token_hex", return_value="c" * 64),
            patch.object(safe_reset, "get_qr_svg_code", return_value=b"svg-data"),
            patch.object(provisioning, "create_pos_provisioning", create_provisioning),
        ):
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "exact ACK RECOVERY",
            ):
                safe_reset.authorize_device_safe_reset(reset_id=RESET_ID)
            rotate.assert_not_called()

            first = safe_reset.authorize_device_safe_reset(
                reset_id=RESET_ID,
                migration_recovery_confirmation=(
                    MIGRATION_RECOVERY_ACK_CONFIRMATION
                ),
                migration_recovery_acknowledgement_reason=(
                    MIGRATION_RECOVERY_ACK_REASON
                ),
            )
            self.assertEqual(first["approval_generation"], 1)
            self.assertEqual(first["provisioning_mode"], "safe_reset_approval")
            self.assertEqual(
                first["approval_expires_at"],
                "2026-03-13T10:20:00Z",
            )
            self.assertEqual(device.config_version, 2)
            self.assertTrue(first["migration_recovery_review_required"])
            self.assertNotIn("setup", first)
            self.assertNotIn("api_key", json.dumps(first))
            self.assertNotIn("api_secret", json.dumps(first))
            restore.assert_not_called()
            rotate.assert_not_called()
            create_provisioning.assert_not_called()

            reset_doc.status = "redeemed"
            reset_doc.new_config_version = 3
            device.config_version = 3
            reset_doc.request_expires_at = datetime(2026, 3, 12, 18, 5)
            second = safe_reset.authorize_device_safe_reset(reset_id=RESET_ID)
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "reason cannot change",
            ):
                safe_reset.authorize_device_safe_reset(
                    reset_id=RESET_ID,
                    migration_recovery_acknowledgement_reason=(
                        "A different reconciliation reason that must be rejected."
                    ),
                )
        restore.assert_not_called()
        rotate.assert_not_called()
        create_provisioning.assert_not_called()
        self.assertEqual(device.config_version, 3)
        self.assertEqual(second["approval_generation"], 2)
        self.assertEqual(reset_doc.status, "redeemed")
        self.assertEqual(reset_doc.authorization_count, 2)
        self.assertEqual(
            reset_doc.migration_recovery_acknowledged_by,
            "Administrator",
        )
        self.assertEqual(
            reset_doc.migration_recovery_acknowledgement_reason,
            MIGRATION_RECOVERY_ACK_REASON,
        )
        self.assertRegex(
            reset_doc.migration_recovery_ack_fingerprint,
            r"^[0-9a-f]{64}$",
        )
        query = parse_qs(urlsplit(first["approval_link"]).query)
        self.assertEqual(query["provisioning_mode"], ["safe_reset_approval"])
        self.assertEqual(query["approval_generation"], ["1"])
        self.assertEqual(
            query["approval_expires_at"],
            [first["approval_expires_at"]],
        )
        self.assertEqual(query["token"], [APPROVAL_TOKEN])
        self.assertNotIn(RESET_PROOF_NONCE, first["approval_link"])
        self.assertNotIn("content://", first["approval_link"])

    def test_approval_fingerprint_binds_full_request_and_evidence_digests(
        self,
    ) -> None:
        reset_doc = _authorized_reset()
        baseline = reset_doc.approval_fingerprint
        for fieldname, mutated_value in (
            ("evidence_fingerprint", "9" * 64),
            ("request_fingerprint", "a" * 64),
            ("approval_expires_at", datetime(2026, 3, 13, 18, 19)),
        ):
            original = getattr(reset_doc, fieldname)
            setattr(reset_doc, fieldname, mutated_value)
            mutated = safe_reset._approval_challenge_fingerprint(
                reset_doc,
                approval_challenge_id=APPROVAL_CHALLENGE_ID,
                approval_generation=1,
                approval_expires_at=reset_doc.approval_expires_at,
                approval_token_sha256=reset_doc.approval_token_sha256,
                approval_erpnext_url=reset_doc.approval_erpnext_url,
            )
            self.assertNotEqual(mutated, baseline)
            setattr(reset_doc, fieldname, original)

    def test_authenticated_cancellation_is_idempotent_and_never_rotates(
        self,
    ) -> None:
        reset_doc = _authorized_reset()
        device = _device()
        safe_reset.frappe.session.user = device.api_user
        commit = MagicMock()
        rotate = MagicMock()
        with (
            patch.object(safe_reset, "require_device_context", return_value=device),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=device),
            patch.object(safe_reset, "_validate_reset_device_binding"),
            patch.object(safe_reset, "_rotate_device_api_credentials", rotate),
            patch.object(safe_reset.frappe.db, "commit", commit),
        ):
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "confirmation is invalid",
            ):
                safe_reset.cancel_device_safe_reset(
                    **{
                        **_cancellation_kwargs(),
                        "confirmation": f"CANCEL SAFE RESET {RESET_ID} ",
                    }
                )
            cancelled = safe_reset.cancel_device_safe_reset(
                **_cancellation_kwargs()
            )
            replayed = safe_reset.cancel_device_safe_reset(
                **_cancellation_kwargs()
            )
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "already used differently",
            ):
                safe_reset.cancel_device_safe_reset(
                    **{
                        **_cancellation_kwargs(),
                        "idempotency_key": "y" * 43,
                    }
                )
        with (
            patch.object(safe_reset, "_enforce_safe_reset_redemption_rate_limit"),
            patch.object(safe_reset, "make_savepoint", return_value="sp"),
            patch.object(safe_reset, "rollback_to_savepoint"),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ),
            patch.object(safe_reset, "_rotate_device_api_credentials", rotate),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "not redeemable",
            ),
        ):
            safe_reset.redeem_device_safe_reset_approval(
                **_redemption_kwargs()
            )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(replayed["status"], "already_cancelled")
        self.assertFalse(cancelled["credentials_rotated"])
        self.assertEqual(cancelled["new_config_version"], 0)
        self.assertEqual(cancelled["cancelled_at"], "2026-03-13T10:05:00Z")
        self.assertEqual(reset_doc.save_count, 1)
        self.assertNotIn(
            CANCELLATION_IDEMPOTENCY_KEY,
            json.dumps(reset_doc.__dict__, default=str),
        )
        commit.assert_called_once_with()
        rotate.assert_not_called()

    def test_system_manager_can_idempotently_cancel_credential_recovery_without_old_credentials(
        self,
    ) -> None:
        reset_doc = _authorized_reset(request_origin="credential_recovery")
        safe_reset.frappe.session.user = "recovery.manager@example.com"
        commit = MagicMock()
        rotate = MagicMock()
        with (
            patch.object(safe_reset, "require_system_manager") as require_manager,
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ) as locked_reset,
            patch.object(safe_reset, "_rotate_device_api_credentials", rotate),
            patch.object(safe_reset.frappe.db, "commit", commit),
        ):
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "confirmation is invalid",
            ):
                safe_reset.cancel_device_safe_reset_as_system_manager(
                    **{
                        **_manager_cancellation_kwargs(),
                        "confirmation": f"CANCEL SAFE RESET {RESET_ID} ",
                    }
                )
            cancelled = safe_reset.cancel_device_safe_reset_as_system_manager(
                **_manager_cancellation_kwargs()
            )
            replayed = safe_reset.cancel_device_safe_reset_as_system_manager(
                **_manager_cancellation_kwargs()
            )
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "already used differently",
            ):
                safe_reset.cancel_device_safe_reset_as_system_manager(
                    **{
                        **_manager_cancellation_kwargs(),
                        "idempotency_key": "n" * 43,
                    }
                )

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(replayed["status"], "already_cancelled")
        self.assertEqual(cancelled["cancellation_origin"], "system_manager")
        self.assertEqual(
            cancelled["cancelled_by_user"],
            "recovery.manager@example.com",
        )
        self.assertEqual(cancelled["cancelled_by_api_user"], "")
        self.assertFalse(cancelled["credentials_rotated"])
        self.assertEqual(reset_doc.save_count, 1)
        self.assertNotIn(
            CANCELLATION_MANAGER_IDEMPOTENCY_KEY,
            json.dumps(reset_doc.__dict__, default=str),
        )
        self.assertGreaterEqual(require_manager.call_count, 4)
        self.assertEqual(locked_reset.call_count, 3)
        commit.assert_called_once_with()
        rotate.assert_not_called()

    def test_cancellation_rejects_redeemed_or_completed_reset(self) -> None:
        reset_doc = _authorized_reset()
        reset_doc.status = "redeemed"
        reset_doc.new_config_version = 3
        reset_doc.credential_rotated_at = datetime(2026, 3, 13, 18, 10)
        device = _device(config_version=3)
        safe_reset.frappe.session.user = device.api_user
        with (
            patch.object(safe_reset, "require_device_context", return_value=device),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=device),
            patch.object(safe_reset, "_validate_reset_device_binding"),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "only before credential rotation",
            ),
        ):
            safe_reset.cancel_device_safe_reset(**_cancellation_kwargs())

    def test_approval_qr_failure_rolls_back_without_identity_mutation(self) -> None:
        reset_doc = _AuditDoc(
            {
                "reset_id": RESET_ID,
                "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
                "status": "requested",
                "request_expires_at": datetime(2026, 3, 14, 18, 5),
                "device": "KOPOS-DEVICE-001",
                "device_id": "tab-a-001",
                "api_user": "device-001@kopos.local",
                "request_origin": "device_authenticated",
                "request_id": REQUEST_ID,
                "previous_config_version": 2,
                "new_config_version": 0,
                "export_sha256": EXPORT_SHA256,
                "export_content_sha256": EXPORT_CONTENT_SHA256,
                "export_byte_length": EXPORT_BYTE_LENGTH,
                "queue_pending_count": 0,
                "queue_failed_count": 0,
                "queue_syncing_count": 0,
                "queue_dead_letter_count": 0,
                "authorization_count": 0,
                **PENDING_MIGRATION_RECOVERY,
                **_scope(),
            }
        )
        rollback = MagicMock()
        rotate = MagicMock()
        restore = MagicMock()

        with (
            patch.object(safe_reset, "require_system_manager"),
            patch.object(safe_reset, "make_savepoint", return_value="sp"),
            patch.object(safe_reset, "rollback_to_savepoint", rollback),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=_device()),
            patch.object(safe_reset, "_validate_reset_device_binding"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_assert_stored_queue_evidence_is_drained"),
            patch.object(safe_reset, "_assert_no_open_shift_or_unresolved_projection"),
            patch.object(safe_reset, "_rotate_device_api_credentials", rotate),
            patch.object(safe_reset, "_restore_recovery_device_api_identity", restore),
            patch.object(safe_reset, "_new_256_bit_secret", return_value=APPROVAL_TOKEN),
            patch.object(safe_reset.secrets, "token_hex", return_value="c" * 64),
            patch.object(
                safe_reset,
                "get_qr_svg_code",
                side_effect=RuntimeError("QR renderer unavailable"),
            ),
            patch.object(safe_reset, "log_sanitized_error"),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "usable approval QR",
            ),
        ):
            safe_reset.authorize_device_safe_reset(
                reset_id=RESET_ID,
                migration_recovery_confirmation=(
                    MIGRATION_RECOVERY_ACK_CONFIRMATION
                ),
                migration_recovery_acknowledgement_reason=(
                    MIGRATION_RECOVERY_ACK_REASON
                ),
            )
        rollback.assert_called_once_with(
            "sp",
            title="KoPOS safe reset authorization rollback failed",
        )
        rotate.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(reset_doc.save_count, 0)
        self.assertEqual(reset_doc.status, "requested")

    def test_redemption_rejects_wrong_protocol_proof_challenge_and_archive_tuple(self) -> None:
        reset_doc = _authorized_reset()
        rotate = MagicMock()
        invalid_cases = (
            ("safe_reset_protocol_version", 1, "protocol version 2"),
            ("request_id", "wrong-request-001", "identity"),
            ("approval_challenge_id", "KSAC-" + "d" * 64, "stale"),
            ("approval_generation", 2, "stale"),
            ("token", "u" * 43, "stale"),
            ("reset_proof_nonce", "x" * 43, "proof"),
            ("export_sha256", "d" * 64, "archive evidence"),
            ("export_content_sha256", "e" * 64, "archive evidence"),
            ("export_byte_length", EXPORT_BYTE_LENGTH + 1, "archive evidence"),
        )
        with (
            patch.object(safe_reset, "_enforce_safe_reset_redemption_rate_limit"),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ),
            patch.object(safe_reset, "_rotate_device_api_credentials", rotate),
            patch.object(safe_reset, "rollback_to_savepoint"),
        ):
            for fieldname, value, message in invalid_cases:
                with self.subTest(fieldname=fieldname), self.assertRaisesRegex(
                    safe_reset.frappe.ValidationError,
                    message,
                ):
                    safe_reset.redeem_device_safe_reset_approval(
                        **{**_redemption_kwargs(), fieldname: value}
                    )
        rotate.assert_not_called()

    def test_expired_approval_never_rotates_and_reissue_races_bind_original_result(self) -> None:
        expired_reset = _authorized_reset()
        expired_reset.approval_expires_at = datetime(2026, 3, 13, 18, 4)
        expired_reset.approval_fingerprint = safe_reset._approval_challenge_fingerprint(
            expired_reset,
            approval_challenge_id=APPROVAL_CHALLENGE_ID,
            approval_generation=1,
            approval_expires_at=expired_reset.approval_expires_at,
            approval_token_sha256=expired_reset.approval_token_sha256,
            approval_erpnext_url=expired_reset.approval_erpnext_url,
        )
        rotate = MagicMock()
        with (
            patch.object(safe_reset, "_enforce_safe_reset_redemption_rate_limit"),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=expired_reset,
            ),
            patch.object(safe_reset, "_rotate_device_api_credentials", rotate),
            patch.object(safe_reset, "rollback_to_savepoint"),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "expired",
            ),
        ):
            safe_reset.redeem_device_safe_reset_approval(**_redemption_kwargs())
        rotate.assert_not_called()

        reissued_before_redeem = _authorized_reset()
        reissued_before_redeem.__dict__.update(
            {
                "approval_challenge_id": "KSAC-" + "d" * 64,
                "approval_generation": 2,
                "approval_token_sha256": hashlib.sha256(("v" * 43).encode()).hexdigest(),
            }
        )
        with self.assertRaisesRegex(
            safe_reset.frappe.ValidationError,
            "stale",
        ):
            safe_reset._match_redemption_challenge(
                reissued_before_redeem,
                challenge_id=APPROVAL_CHALLENGE_ID,
                generation=1,
                token_sha256=hashlib.sha256(APPROVAL_TOKEN.encode()).hexdigest(),
            )

        raced_reset = _authorized_reset()
        raced_reset.__dict__.update(
            {
                "status": "redeemed",
                "redeemed_approval_challenge_id": APPROVAL_CHALLENGE_ID,
                "redeemed_approval_generation": 1,
                "redeemed_approval_token_sha256": raced_reset.approval_token_sha256,
                "redemption_idempotency_sha256": hashlib.sha256(
                    REDEMPTION_IDEMPOTENCY_KEY.encode()
                ).hexdigest(),
                "redeemed_recovery_expires_at": datetime(2026, 3, 14, 18, 5),
                "approval_challenge_id": "KSAC-" + "d" * 64,
                "approval_generation": 2,
                "approval_token_sha256": hashlib.sha256(("v" * 43).encode()).hexdigest(),
            }
        )
        self.assertEqual(
            safe_reset._match_redemption_challenge(
                raced_reset,
                challenge_id=APPROVAL_CHALLENGE_ID,
                generation=1,
                token_sha256=hashlib.sha256(APPROVAL_TOKEN.encode()).hexdigest(),
            ),
            "committed",
        )
        self.assertEqual(
            safe_reset._match_redemption_challenge(
                raced_reset,
                challenge_id="KSAC-" + "d" * 64,
                generation=2,
                token_sha256=hashlib.sha256(("v" * 43).encode()).hexdigest(),
            ),
            "current",
        )
        with (
            patch.object(safe_reset, "_validate_stored_redemption_result"),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "idempotency key",
            ),
        ):
            safe_reset._retry_redeemed_result(
                raced_reset,
                challenge_kind="current",
                idempotency_sha256=hashlib.sha256(("z" * 43).encode()).hexdigest(),
                export_sha256=EXPORT_SHA256,
                export_content_sha256=EXPORT_CONTENT_SHA256,
                export_byte_length=EXPORT_BYTE_LENGTH,
            )

    def test_redemption_rotates_once_and_lost_response_retry_is_identical(self) -> None:
        reset_doc = _authorized_reset(request_origin="credential_recovery")
        device = _device()
        encrypted_values: dict[tuple[str, str, str], str] = {}
        events: list[str] = []

        def restore(*args: object, **kwargs: object) -> str:
            events.append("restore")
            return "missing_user"

        credentials = {
            "user": device.api_user,
            "api_key": "new-key",
            "api_secret": "new-secret",
            "previous_credential_state": "missing_both",
            "revoked_api_key_sha256": "",
            "issued_api_key_sha256": hashlib.sha256(b"new-key").hexdigest(),
            "issued_api_secret_sha256": hashlib.sha256(b"new-secret").hexdigest(),
        }

        def rotate(*args: object, **kwargs: object) -> dict[str, str]:
            events.append("rotate")
            return credentials

        def set_password(
            doctype: str,
            name: str,
            value: str,
            fieldname: str,
        ) -> None:
            encrypted_values[(doctype, name, fieldname)] = value

        def get_password(
            doctype: str,
            name: str,
            fieldname: str,
            **kwargs: object,
        ) -> str | None:
            return encrypted_values.get((doctype, name, fieldname))

        rotate_mock = MagicMock(side_effect=rotate)
        restore_mock = MagicMock(side_effect=restore)
        current_credentials = MagicMock(return_value=credentials)
        with (
            patch.object(safe_reset, "_enforce_safe_reset_redemption_rate_limit"),
            patch.object(safe_reset, "make_savepoint", return_value="sp"),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=device),
            patch.object(safe_reset, "_validate_reset_device_binding"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_assert_stored_queue_evidence_is_drained"),
            patch.object(safe_reset, "_assert_no_open_shift_or_unresolved_projection"),
            patch.object(safe_reset, "_restore_recovery_device_api_identity", restore_mock),
            patch.object(safe_reset, "_rotate_device_api_credentials", rotate_mock),
            patch.object(safe_reset, "_read_current_device_api_credentials", current_credentials),
            patch.object(
                safe_reset,
                "serialize_device_config",
                return_value={
                    "device_id": "tab-a-001",
                    "config_version": 3,
                    "api_key": "new-key",
                    "api_secret": "new-secret",
                    "users": [{"id": "cashier", "active": True}],
                },
            ),
            patch.object(safe_reset, "set_encrypted_password", side_effect=set_password),
            patch.object(safe_reset, "get_decrypted_password", side_effect=get_password),
            patch.object(safe_reset.frappe.db, "set_value"),
            patch.object(safe_reset.frappe.db, "commit"),
        ):
            first = safe_reset.redeem_device_safe_reset_approval(
                **_redemption_kwargs()
            )
            retry = safe_reset.redeem_device_safe_reset_approval(
                **_redemption_kwargs()
            )
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "idempotency key",
            ):
                safe_reset.redeem_device_safe_reset_approval(
                    **{
                        **_redemption_kwargs(),
                        "redemption_idempotency_key": "z" * 43,
                    }
                )

            expired_recovery_at = datetime(2026, 3, 13, 17, 0)
            reset_doc.redeemed_recovery_expires_at = expired_recovery_at
            reset_doc.redemption_result_fingerprint = (
                safe_reset._redemption_result_fingerprint(
                    reset_doc,
                    redemption_idempotency_sha256=(
                        reset_doc.redemption_idempotency_sha256
                    ),
                    export_sha256=reset_doc.redemption_export_sha256,
                    export_content_sha256=(
                        reset_doc.redemption_export_content_sha256
                    ),
                    export_byte_length=reset_doc.redemption_export_byte_length,
                    setup_sha256=reset_doc.redemption_setup_sha256,
                    issued_api_key_sha256=reset_doc.issued_api_key_sha256,
                    issued_api_secret_sha256=reset_doc.issued_api_secret_sha256,
                    redeemed_at=reset_doc.redemption_issued_at,
                    recovery_expires_at=expired_recovery_at,
                )
            )
            reissued_challenge_id = "KSAC-" + "d" * 64
            reissued_token = "v" * 43
            reset_doc.approval_challenge_id = reissued_challenge_id
            reset_doc.approval_generation = 2
            reset_doc.approval_token_sha256 = hashlib.sha256(
                reissued_token.encode()
            ).hexdigest()
            reset_doc.approval_issued_at = datetime(2026, 3, 13, 18, 6)
            reset_doc.approval_expires_at = datetime(2026, 3, 13, 18, 20)
            reset_doc.approval_fingerprint = (
                safe_reset._approval_challenge_fingerprint(
                    reset_doc,
                    approval_challenge_id=reissued_challenge_id,
                    approval_generation=2,
                    approval_expires_at=reset_doc.approval_expires_at,
                    approval_token_sha256=reset_doc.approval_token_sha256,
                    approval_erpnext_url=reset_doc.approval_erpnext_url,
                )
            )
            reset_doc.current_redemption_idempotency_sha256 = None
            reset_doc.current_redemption_result_fingerprint = None
            recovered_after_original_window = (
                safe_reset.redeem_device_safe_reset_approval(
                    **{
                        **_redemption_kwargs(),
                        "token": reissued_token,
                        "approval_challenge_id": reissued_challenge_id,
                        "approval_generation": 2,
                    }
                )
            )

        self.assertEqual(first, retry)
        self.assertEqual(events, ["restore", "rotate"])
        rotate_mock.assert_called_once()
        restore_mock.assert_called_once()
        self.assertEqual(reset_doc.status, "redeemed")
        self.assertEqual(reset_doc.new_config_version, 3)
        self.assertEqual(device.config_version, 3)
        self.assertEqual(first["setup"]["api_secret"], "new-secret")
        self.assertEqual(first["approval_expires_at"], "2026-03-13T10:20:00Z")
        self.assertEqual(first["issued_at"], "2026-03-13T10:05:00Z")
        self.assertEqual(first["expires_at"], "2026-03-14T10:05:00Z")
        self.assertEqual(
            recovered_after_original_window["recovery_path"],
            "reissued_current_challenge",
        )
        self.assertEqual(
            recovered_after_original_window["committed_result"][
                "approval_challenge_id"
            ],
            APPROVAL_CHALLENGE_ID,
        )
        self.assertEqual(
            recovered_after_original_window["current_approval"][
                "approval_challenge_id"
            ],
            "KSAC-" + "d" * 64,
        )
        self.assertNotEqual(
            recovered_after_original_window["committed_result"][
                "result_recovery_expires_at"
            ],
            recovered_after_original_window["current_approval"][
                "approval_expires_at"
            ],
        )
        self.assertEqual(recovered_after_original_window["setup"], first["setup"])
        stored_snapshot = encrypted_values[
            (safe_reset.SAFE_RESET_DOCTYPE, RESET_ID, "redemption_setup_snapshot")
        ]
        self.assertNotIn("new-key", stored_snapshot)
        self.assertNotIn("new-secret", stored_snapshot)
        serialized_audit = json.dumps(reset_doc.__dict__, default=str)
        for raw_secret in (
            APPROVAL_TOKEN,
            REDEMPTION_IDEMPOTENCY_KEY,
            RESET_PROOF_NONCE,
            "new-key",
            "new-secret",
        ):
            self.assertNotIn(raw_secret, serialized_audit)

    def test_concurrent_redemptions_serialize_and_rotate_exactly_once(self) -> None:
        reset_doc = _authorized_reset()
        device = _device()
        credentials = {
            "user": device.api_user,
            "api_key": "concurrent-key",
            "api_secret": "concurrent-secret",
            "previous_credential_state": "complete",
            "revoked_api_key_sha256": "d" * 64,
            "issued_api_key_sha256": hashlib.sha256(b"concurrent-key").hexdigest(),
            "issued_api_secret_sha256": hashlib.sha256(
                b"concurrent-secret"
            ).hexdigest(),
        }
        encrypted_values: dict[tuple[str, str, str], str] = {}
        transaction_lock = threading.Lock()
        transaction_state = threading.local()
        start_barrier = threading.Barrier(2)
        lock_events: list[str] = []

        def lock_device(device_name: str) -> None:
            self.assertEqual(device_name, "KOPOS-DEVICE-001")
            transaction_lock.acquire()
            transaction_state.holds_lock = True
            lock_events.append("device")

        def lock_reset(reset_id: str) -> _AuditDoc:
            self.assertEqual(reset_id, RESET_ID)
            self.assertTrue(getattr(transaction_state, "holds_lock", False))
            lock_events.append("reset")
            return reset_doc

        def release_transaction(*args: object, **kwargs: object) -> None:
            if getattr(transaction_state, "holds_lock", False):
                transaction_state.holds_lock = False
                transaction_lock.release()

        def set_password(
            doctype: str,
            name: str,
            value: str,
            fieldname: str,
        ) -> None:
            encrypted_values[(doctype, name, fieldname)] = value

        def get_password(
            doctype: str,
            name: str,
            fieldname: str,
            **kwargs: object,
        ) -> str | None:
            return encrypted_values.get((doctype, name, fieldname))

        rotate = MagicMock(return_value=credentials)

        def redeem() -> dict[str, object]:
            start_barrier.wait()
            return safe_reset.redeem_device_safe_reset_approval(
                **_redemption_kwargs()
            )

        with (
            patch.object(safe_reset, "_enforce_safe_reset_redemption_rate_limit"),
            patch.object(safe_reset, "make_savepoint", return_value="sp"),
            patch.object(safe_reset, "rollback_to_savepoint", side_effect=release_transaction),
            patch.object(
                safe_reset.frappe.db,
                "get_value",
                return_value="KOPOS-DEVICE-001",
            ),
            patch.object(safe_reset, "_lock_device_for_update", side_effect=lock_device),
            patch.object(safe_reset, "_get_reset_for_update", side_effect=lock_reset),
            patch.object(safe_reset, "get_device_doc", return_value=device),
            patch.object(safe_reset, "_validate_reset_device_binding"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_assert_stored_queue_evidence_is_drained"),
            patch.object(safe_reset, "_assert_no_open_shift_or_unresolved_projection"),
            patch.object(safe_reset, "_rotate_device_api_credentials", rotate),
            patch.object(safe_reset, "_read_current_device_api_credentials", return_value=credentials),
            patch.object(
                safe_reset,
                "serialize_device_config",
                return_value={
                    "device_id": "tab-a-001",
                    "config_version": 3,
                    "api_key": "concurrent-key",
                    "api_secret": "concurrent-secret",
                    "users": [{"id": "cashier", "active": True}],
                },
            ),
            patch.object(safe_reset, "set_encrypted_password", side_effect=set_password),
            patch.object(safe_reset, "get_decrypted_password", side_effect=get_password),
            patch.object(safe_reset.frappe.db, "set_value"),
            patch.object(safe_reset.frappe.db, "commit", side_effect=release_transaction),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(redeem) for _index in range(2)]
                responses = [future.result(timeout=5) for future in futures]

        self.assertEqual(responses[0], responses[1])
        rotate.assert_called_once()
        self.assertEqual(reset_doc.status, "redeemed")
        self.assertEqual(reset_doc.new_config_version, 3)
        self.assertFalse(transaction_lock.locked())
        self.assertEqual(lock_events, ["device", "reset", "device", "reset"])

    def test_redemption_rate_limit_is_atomic_and_fails_closed(self) -> None:
        unavailable_cache = SimpleNamespace(make_key=lambda key: key)
        with (
            patch.object(safe_reset.frappe, "cache", return_value=unavailable_cache),
            patch.object(safe_reset, "log_sanitized_error"),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "temporarily unavailable",
            ),
        ):
            safe_reset._enforce_safe_reset_redemption_rate_limit(RESET_ID)

        limited_cache = SimpleNamespace(
            make_key=lambda key: key,
            eval=lambda *args: safe_reset.SAFE_RESET_RATE_LIMIT_MAX_ATTEMPTS + 1,
        )
        with (
            patch.object(safe_reset.frappe, "cache", return_value=limited_cache),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "Too many",
            ),
        ):
            safe_reset._enforce_safe_reset_redemption_rate_limit(RESET_ID)

    def test_redemption_precommit_failure_rolls_back_without_a_redeemed_audit(self) -> None:
        reset_doc = _authorized_reset()
        device = _device()
        rollback = MagicMock()
        commit = MagicMock()
        credentials = {
            "user": device.api_user,
            "api_key": "new-key",
            "api_secret": "new-secret",
            "previous_credential_state": "complete",
            "revoked_api_key_sha256": "d" * 64,
            "issued_api_key_sha256": "e" * 64,
            "issued_api_secret_sha256": "f" * 64,
        }
        with (
            patch.object(safe_reset, "_enforce_safe_reset_redemption_rate_limit"),
            patch.object(safe_reset, "make_savepoint", return_value="sp"),
            patch.object(safe_reset, "rollback_to_savepoint", rollback),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=device),
            patch.object(safe_reset, "_validate_reset_device_binding"),
            patch.object(safe_reset, "_validate_reset_scope", return_value=_scope()),
            patch.object(safe_reset, "_assert_stored_queue_evidence_is_drained"),
            patch.object(safe_reset, "_assert_no_open_shift_or_unresolved_projection"),
            patch.object(safe_reset, "_rotate_device_api_credentials", return_value=credentials),
            patch.object(
                safe_reset,
                "serialize_device_config",
                return_value={"device_id": "tab-a-001", "config_version": 3},
            ),
            patch.object(
                safe_reset,
                "_store_redeemed_setup",
                side_effect=RuntimeError("encrypted store unavailable"),
            ),
            patch.object(safe_reset.frappe.db, "set_value"),
            patch.object(safe_reset.frappe.db, "commit", commit),
            patch.object(safe_reset, "log_sanitized_error"),
            self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "before credentials were safely issued",
            ),
        ):
            safe_reset.redeem_device_safe_reset_approval(**_redemption_kwargs())
        rollback.assert_called_once_with(
            "sp",
            title="KoPOS safe reset redemption rollback failed",
        )
        commit.assert_not_called()
        self.assertEqual(reset_doc.status, "authorized")
        self.assertEqual(reset_doc.save_count, 0)

    def test_completion_lost_response_replay_is_identical_and_key_bound(self) -> None:
        reset_doc = _authorized_reset()
        reset_doc.__dict__.update(
            {
                "status": "redeemed",
                "new_config_version": 3,
                "redemption_idempotency_sha256": hashlib.sha256(
                    REDEMPTION_IDEMPOTENCY_KEY.encode()
                ).hexdigest(),
                **ZERO_MIGRATION_RECOVERY,
            }
        )
        device = _device(config_version=3)
        safe_reset.frappe.session.user = device.api_user
        with (
            patch.object(safe_reset, "require_device_context", return_value=device),
            patch.object(
                safe_reset,
                "_get_reset_with_device_lock",
                return_value=reset_doc,
            ),
            patch.object(safe_reset, "get_device_doc", return_value=device),
            patch.object(safe_reset, "_validate_reset_device_binding"),
            patch.object(safe_reset.frappe.db, "commit"),
        ):
            completion_kwargs = {
                "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
                "device_id": "tab-a-001",
                "reset_id": RESET_ID,
                "new_config_version": 3,
                "export_sha256": EXPORT_SHA256,
                "export_content_sha256": EXPORT_CONTENT_SHA256,
                "export_byte_length": EXPORT_BYTE_LENGTH,
                "completion_idempotency_key": COMPLETION_IDEMPOTENCY_KEY,
            }
            for fieldname, value in (
                ("export_sha256", "d" * 64),
                ("export_content_sha256", "e" * 64),
                ("export_byte_length", EXPORT_BYTE_LENGTH + 1),
            ):
                with self.subTest(fieldname=fieldname), self.assertRaisesRegex(
                    safe_reset.frappe.ValidationError,
                    "archive evidence",
                ):
                    safe_reset.complete_device_safe_reset(
                        **{**completion_kwargs, fieldname: value}
                    )
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "distinct idempotency key",
            ):
                safe_reset.complete_device_safe_reset(
                    **{
                        **completion_kwargs,
                        "completion_idempotency_key": REDEMPTION_IDEMPOTENCY_KEY,
                    }
                )
            completed = safe_reset.complete_device_safe_reset(**completion_kwargs)
            repeated = safe_reset.complete_device_safe_reset(**completion_kwargs)
            with self.assertRaisesRegex(
                safe_reset.frappe.ValidationError,
                "idempotency key",
            ):
                safe_reset.complete_device_safe_reset(
                    **{
                        **completion_kwargs,
                        "completion_idempotency_key": "q" * 43,
                    }
                )
        self.assertEqual(completed, repeated)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["export_content_sha256"], EXPORT_CONTENT_SHA256)
        self.assertEqual(completed["export_byte_length"], EXPORT_BYTE_LENGTH)
        self.assertEqual(completed["completed_at"], "2026-03-13T10:05:00Z")
        self.assertEqual(reset_doc.save_count, 1)

    def test_v1_safe_reset_provisioning_is_fail_closed_and_qr_has_no_sensitive_evidence(self) -> None:
        with (
            patch.object(provisioning, "require_system_manager"),
            self.assertRaisesRegex(
                provisioning.frappe.ValidationError,
                "Legacy single-phase",
            ),
        ):
            provisioning.create_pos_provisioning(
                device="KOPOS-DEVICE-001",
                api_key="old-key",
                api_secret="old-secret",
                safe_reset_metadata={"provisioning_mode": "safe_reset"},
            )

        link = safe_reset._approval_link(
            erpnext_url="https://erp.example.com/tenant-a",
            reset_id=RESET_ID,
            request_id=REQUEST_ID,
            approval_challenge_id=APPROVAL_CHALLENGE_ID,
            approval_generation=1,
            approval_expires_at=datetime(2026, 3, 13, 18, 20),
            approval_token=APPROVAL_TOKEN,
        )
        query = parse_qs(urlsplit(link).query)
        self.assertEqual(len(APPROVAL_CHALLENGE_ID.removeprefix("KSAC-")), 64)
        self.assertEqual(
            safe_reset._require_challenge_id(APPROVAL_CHALLENGE_ID),
            APPROVAL_CHALLENGE_ID,
        )
        with self.assertRaisesRegex(
            safe_reset.frappe.ValidationError,
            "challenge is invalid",
        ):
            safe_reset._require_challenge_id("KSAC-" + "c" * 32)
        self.assertEqual(
            set(query),
            {
                "base_url",
                "provisioning_mode",
                "safe_reset_protocol_version",
                "reset_id",
                "request_id",
                "approval_challenge_id",
                "approval_generation",
                "approval_expires_at",
                "token",
            },
        )
        self.assertEqual(query["provisioning_mode"], ["safe_reset_approval"])
        self.assertEqual(query["safe_reset_protocol_version"], ["2"])
        self.assertEqual(query["approval_challenge_id"], [APPROVAL_CHALLENGE_ID])
        self.assertEqual(
            query["approval_expires_at"],
            ["2026-03-13T10:20:00Z"],
        )
        self.assertEqual(query["token"], [APPROVAL_TOKEN])
        for forbidden in (
            "api_key",
            "api_secret",
            RESET_PROOF_NONCE,
            "content://",
            "Counter Tablet",
            "Tablet credential was lost",
        ):
            self.assertNotIn(forbidden, link)


if __name__ == "__main__":
    unittest.main()
