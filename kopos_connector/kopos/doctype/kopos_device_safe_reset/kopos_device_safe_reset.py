from __future__ import annotations

import hashlib
import json
import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, get_datetime


SAFE_RESET_STATES = {
    "requested",
    "authorized",
    "redeemed",
    "completed",
    "cancelled",
    "expired",
}
SAFE_RESET_ORIGINS = {"device_authenticated", "credential_recovery"}
SAFE_RESET_TRANSITIONS = {
    "requested": {"requested", "authorized", "cancelled", "expired"},
    "authorized": {"authorized", "redeemed", "cancelled", "expired"},
    "redeemed": {"redeemed", "completed"},
    "completed": {"completed"},
    "cancelled": {"cancelled"},
    "expired": {"expired"},
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHALLENGE_ID_PATTERN = re.compile(r"^KSAC-[0-9a-f]{64}$")
SAFE_RESET_PROTOCOL_VERSION = 2
MAX_SUPPORT_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024 + 64 * 1024 * 1024
MAX_MIGRATION_RECOVERY_COUNT = 2_147_483_647
MIGRATION_RECOVERY_ACK_FIELDS = (
    "migration_recovery_acknowledged_by",
    "migration_recovery_acknowledged_at",
    "migration_recovery_acknowledgement_reason",
    "migration_recovery_ack_fingerprint",
)
IMMUTABLE_REQUEST_FIELDS = (
    "reset_id",
    "safe_reset_protocol_version",
    "request_id",
    "device",
    "device_id",
    "api_user",
    "reason",
    "request_origin",
    "registered_by_system_manager",
    "credential_recovery_confirmed_at",
    "stale_export_override",
    "stale_export_override_reason",
    "export_sha256",
    "export_content_sha256",
    "export_byte_length",
    "exported_at",
    "drained_row_count",
    "queue_pending_count",
    "queue_failed_count",
    "queue_syncing_count",
    "queue_dead_letter_count",
    "migration_recovery_point_count",
    "migration_recovery_valid_point_count",
    "migration_recovery_invalid_point_count",
    "migration_recovery_captured_pending_total",
    "migration_recovery_review_required",
    "previous_config_version",
    "reset_proof_sha256",
    "evidence_fingerprint",
    "request_fingerprint",
    "requested_by_api_user",
    "requested_at",
    "request_expires_at",
    "erp_base_url",
    "company",
    "currency",
    "pos_profile",
    "warehouse",
)
APPROVAL_FIELDS = (
    "approval_challenge_id",
    "approval_token_sha256",
    "approval_generation",
    "approval_issued_by",
    "approval_issued_at",
    "approval_expires_at",
    "approval_fingerprint",
    "approval_erpnext_url",
)
ROTATION_FIELDS = (
    "credential_rotated_at",
    "new_config_version",
    "previous_credential_state",
    "issued_api_key_sha256",
    "issued_api_secret_sha256",
)
IMMUTABLE_REDEMPTION_FIELDS = (
    "redeemed_approval_challenge_id",
    "redeemed_approval_generation",
    "redeemed_approval_token_sha256",
    "redeemed_approval_fingerprint",
    "redeemed_approval_expires_at",
    "redemption_idempotency_sha256",
    "redemption_export_sha256",
    "redemption_export_content_sha256",
    "redemption_export_byte_length",
    "redemption_setup_sha256",
    "redemption_result_fingerprint",
    "redemption_issued_at",
    "redeemed_at",
    "redeemed_recovery_expires_at",
    "credential_rotated_at",
    "new_config_version",
    "previous_credential_state",
    "revoked_api_key_sha256",
    "issued_api_key_sha256",
    "issued_api_secret_sha256",
)
IMMUTABLE_COMPLETION_FIELDS = (
    "completion_idempotency_sha256",
    "completion_export_sha256",
    "completion_export_content_sha256",
    "completion_export_byte_length",
    "completion_result_fingerprint",
    "completed_by_api_user",
    "completed_at",
)
CANCELLATION_ORIGINS = {"device_authenticated", "system_manager"}
CANCELLATION_REQUIRED_FIELDS = (
    "cancellation_idempotency_sha256",
    "cancellation_reason",
    "cancellation_origin",
    "cancelled_by_user",
    "cancelled_at",
    "cancellation_result_fingerprint",
)
CANCELLATION_FIELDS = CANCELLATION_REQUIRED_FIELDS + ("cancelled_by_api_user",)
INTEGER_AUDIT_FIELDS = {
    "approval_generation",
    "redeemed_approval_generation",
    "redemption_export_byte_length",
    "new_config_version",
    "completion_export_byte_length",
}
DATETIME_AUDIT_FIELDS = {
    "approval_issued_at",
    "approval_expires_at",
    "redeemed_approval_expires_at",
    "redemption_issued_at",
    "redeemed_at",
    "redeemed_recovery_expires_at",
    "credential_rotated_at",
    "completed_at",
    "cancelled_at",
}


def _audit_field_value(doc: Document, fieldname: str) -> object:
    value = getattr(doc, fieldname, None)
    if fieldname in INTEGER_AUDIT_FIELDS:
        return cint(value)
    if fieldname in DATETIME_AUDIT_FIELDS:
        return get_datetime(value) if value else None
    return cstr(value).strip()


class KoPOSDeviceSafeReset(Document):
    def validate(self) -> None:
        self.status = cstr(self.status).strip().lower()
        if self.status not in SAFE_RESET_STATES:
            frappe.throw(_("Invalid KoPOS device safe reset state"), frappe.ValidationError)

        self.request_origin = cstr(self.request_origin).strip().lower()
        if self.request_origin not in SAFE_RESET_ORIGINS:
            frappe.throw(
                _("Invalid KoPOS device safe reset request origin"),
                frappe.ValidationError,
            )
        self.safe_reset_protocol_version = cint(
            getattr(self, "safe_reset_protocol_version", 0)
        )
        if self.safe_reset_protocol_version != SAFE_RESET_PROTOCOL_VERSION:
            frappe.throw(
                _("KoPOS device safe reset protocol version 2 is required"),
                frappe.ValidationError,
            )
        self.export_byte_length = cint(getattr(self, "export_byte_length", 0))
        if (
            self.export_byte_length <= 0
            or self.export_byte_length > MAX_SUPPORT_ARCHIVE_BYTES
        ):
            frappe.throw(
                _("export_byte_length is outside the supported archive range"),
                frappe.ValidationError,
            )
        if self.request_origin == "device_authenticated":
            if cstr(self.requested_by_api_user).strip() != cstr(self.api_user).strip():
                frappe.throw(
                    _("Authenticated safe reset requester must match the device API user"),
                    frappe.ValidationError,
                )
            if cstr(self.registered_by_system_manager).strip():
                frappe.throw(
                    _("Authenticated safe reset must not impersonate credential recovery"),
                    frappe.ValidationError,
                )
            if cint(getattr(self, "stale_export_override", 0)):
                frappe.throw(
                    _("Authenticated safe reset cannot override stale export evidence"),
                    frappe.ValidationError,
                )
        elif not cstr(self.registered_by_system_manager).strip() or not getattr(
            self, "credential_recovery_confirmed_at", None
        ):
            frappe.throw(
                _("Credential recovery requires an audited System Manager confirmation"),
                frappe.ValidationError,
            )
        override_enabled = bool(cint(getattr(self, "stale_export_override", 0)))
        override_reason = cstr(
            getattr(self, "stale_export_override_reason", None)
        ).strip()
        if override_enabled and len(override_reason) < 20:
            frappe.throw(
                _("Stale export override reason must contain at least 20 characters"),
                frappe.ValidationError,
            )
        if not override_enabled and override_reason:
            frappe.throw(
                _("Stale export override reason requires an explicit override"),
                frappe.ValidationError,
            )

        for fieldname in (
            "export_sha256",
            "export_content_sha256",
            "reset_proof_sha256",
            "evidence_fingerprint",
            "request_fingerprint",
        ):
            value = cstr(getattr(self, fieldname, None)).strip().lower()
            if not SHA256_PATTERN.fullmatch(value):
                frappe.throw(
                    _("{0} must be a lowercase SHA-256 digest").format(fieldname),
                    frappe.ValidationError,
                )
            setattr(self, fieldname, value)

        for fieldname in (
            "approval_token_sha256",
            "approval_fingerprint",
            "current_redemption_idempotency_sha256",
            "current_redemption_result_fingerprint",
            "revoked_api_key_sha256",
            "issued_api_key_sha256",
            "issued_api_secret_sha256",
            "redeemed_approval_token_sha256",
            "redeemed_approval_fingerprint",
            "redemption_idempotency_sha256",
            "redemption_export_sha256",
            "redemption_export_content_sha256",
            "redemption_setup_sha256",
            "redemption_result_fingerprint",
            "completion_idempotency_sha256",
            "completion_export_sha256",
            "completion_export_content_sha256",
            "completion_result_fingerprint",
            "cancellation_idempotency_sha256",
            "cancellation_result_fingerprint",
        ):
            value = cstr(getattr(self, fieldname, None)).strip().lower()
            if value and not SHA256_PATTERN.fullmatch(value):
                frappe.throw(
                    _("{0} must be a lowercase SHA-256 digest").format(fieldname),
                    frappe.ValidationError,
                )
            setattr(self, fieldname, value)

        for fieldname in (
            "drained_row_count",
            "queue_pending_count",
            "queue_failed_count",
            "queue_syncing_count",
            "queue_dead_letter_count",
            "migration_recovery_point_count",
            "migration_recovery_valid_point_count",
            "migration_recovery_invalid_point_count",
            "migration_recovery_captured_pending_total",
        ):
            value = cint(getattr(self, fieldname, 0))
            if value < 0 or (
                fieldname.startswith("migration_recovery_")
                and value > MAX_MIGRATION_RECOVERY_COUNT
            ):
                frappe.throw(
                    _("{0} is outside the supported non-negative range").format(
                        fieldname
                    ),
                    frappe.ValidationError,
                )
            setattr(self, fieldname, value)

        self.migration_recovery_review_required = cint(
            getattr(self, "migration_recovery_review_required", 0)
        )
        if self.migration_recovery_review_required not in {0, 1}:
            frappe.throw(
                _("migration_recovery_review_required must be true or false"),
                frappe.ValidationError,
            )
        self._validate_migration_recovery_evidence()
        self._validate_migration_recovery_acknowledgement()
        self._validate_lifecycle_fields()

        if self.is_new():
            return

        previous = frappe.get_doc(self.doctype, self.name)
        previous_status = cstr(getattr(previous, "status", None)).strip().lower()
        if self.status not in SAFE_RESET_TRANSITIONS.get(previous_status, set()):
            frappe.throw(
                _("Invalid safe reset state transition from {0} to {1}").format(
                    previous_status or _("unknown"), self.status
                ),
                frappe.ValidationError,
            )
        for fieldname in IMMUTABLE_REQUEST_FIELDS:
            if getattr(previous, fieldname, None) != getattr(self, fieldname, None):
                frappe.throw(
                    _("Safe reset request evidence is immutable: {0}").format(fieldname),
                    frappe.ValidationError,
                )
        self._validate_migration_recovery_ack_transition(previous, previous_status)
        self._validate_protocol_transitions(previous, previous_status)

    def _validate_migration_recovery_evidence(self) -> None:
        point_count = cint(self.migration_recovery_point_count)
        valid_point_count = cint(self.migration_recovery_valid_point_count)
        invalid_point_count = cint(self.migration_recovery_invalid_point_count)
        captured_pending_total = cint(
            self.migration_recovery_captured_pending_total
        )
        review_required = bool(cint(self.migration_recovery_review_required))
        if valid_point_count + invalid_point_count != point_count:
            frappe.throw(
                _(
                    "Migration recovery valid and invalid point counts must equal "
                    "the total point count"
                ),
                frappe.ValidationError,
            )
        if review_required != (point_count > 0):
            frappe.throw(
                _(
                    "Migration recovery review requirement must equal whether "
                    "recovery points exist"
                ),
                frappe.ValidationError,
            )
        if point_count == 0 and captured_pending_total != 0:
            frappe.throw(
                _(
                    "Captured migration recovery pending total must be zero when "
                    "no recovery points exist"
                ),
                frappe.ValidationError,
            )

    def _validate_migration_recovery_acknowledgement(self) -> None:
        raw_values = [getattr(self, fieldname, None) for fieldname in MIGRATION_RECOVERY_ACK_FIELDS]
        presence = [bool(cstr(value).strip()) for value in raw_values]
        if any(presence) and not all(presence):
            frappe.throw(
                _("Migration recovery acknowledgement must be complete"),
                frappe.ValidationError,
            )
        has_acknowledgement = all(presence)
        review_required = bool(cint(self.migration_recovery_review_required))
        if not review_required and has_acknowledgement:
            frappe.throw(
                _("Migration recovery acknowledgement requires recovery points"),
                frappe.ValidationError,
            )
        if review_required and self.status in {"authorized", "redeemed", "completed"}:
            if not has_acknowledgement:
                frappe.throw(
                    _(
                        "Migration recovery review must be acknowledged before "
                        "authorization"
                    ),
                    frappe.ValidationError,
                )
        if self.status == "requested" and has_acknowledgement:
            frappe.throw(
                _("Requested safe reset cannot contain a recovery acknowledgement"),
                frappe.ValidationError,
            )
        if not has_acknowledgement:
            return

        raw_reason = cstr(self.migration_recovery_acknowledgement_reason)
        reason = raw_reason.strip()
        if raw_reason != reason or len(reason) < 20 or len(reason) > 500:
            frappe.throw(
                _(
                    "Migration recovery acknowledgement reason must contain "
                    "20-500 characters without padding"
                ),
                frappe.ValidationError,
            )
        self.migration_recovery_acknowledgement_reason = reason
        fingerprint = cstr(self.migration_recovery_ack_fingerprint).strip().lower()
        if not SHA256_PATTERN.fullmatch(fingerprint):
            frappe.throw(
                _(
                    "migration_recovery_ack_fingerprint must be a lowercase "
                    "SHA-256 digest"
                ),
                frappe.ValidationError,
            )
        self.migration_recovery_ack_fingerprint = fingerprint
        acknowledged_by = cstr(self.migration_recovery_acknowledged_by).strip()
        if self.status in {"authorized", "redeemed", "completed"} and (
            acknowledged_by != cstr(getattr(self, "authorized_by", None)).strip()
        ):
            frappe.throw(
                _("Recovery acknowledgement manager must match the authorizer"),
                frappe.ValidationError,
            )
        expected_fingerprint = self._migration_recovery_ack_fingerprint()
        if fingerprint != expected_fingerprint:
            frappe.throw(
                _("Migration recovery acknowledgement fingerprint is invalid"),
                frappe.ValidationError,
            )

    def _migration_recovery_ack_fingerprint(self) -> str:
        acknowledged_at = get_datetime(self.migration_recovery_acknowledged_at)
        payload = {
            "reset_id": cstr(self.reset_id or self.name).strip(),
            "safe_reset_protocol_version": cint(
                self.safe_reset_protocol_version
            ),
            "export_sha256": cstr(self.export_sha256).strip(),
            "export_content_sha256": cstr(self.export_content_sha256).strip(),
            "export_byte_length": cint(self.export_byte_length),
            "migration_recovery_point_count": cint(
                self.migration_recovery_point_count
            ),
            "migration_recovery_valid_point_count": cint(
                self.migration_recovery_valid_point_count
            ),
            "migration_recovery_invalid_point_count": cint(
                self.migration_recovery_invalid_point_count
            ),
            "migration_recovery_captured_pending_total": cint(
                self.migration_recovery_captured_pending_total
            ),
            "migration_recovery_review_required": bool(
                cint(self.migration_recovery_review_required)
            ),
            "acknowledged_by": cstr(
                self.migration_recovery_acknowledged_by
            ).strip(),
            "acknowledged_at": acknowledged_at.isoformat(),
            "acknowledgement_reason": cstr(
                self.migration_recovery_acknowledgement_reason
            ),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _validate_migration_recovery_ack_transition(
        self,
        previous: Document,
        previous_status: str,
    ) -> None:
        changed_fields = [
            fieldname
            for fieldname in MIGRATION_RECOVERY_ACK_FIELDS
            if getattr(previous, fieldname, None) != getattr(self, fieldname, None)
        ]
        if not changed_fields:
            return
        previous_has_ack = any(
            cstr(getattr(previous, fieldname, None)).strip()
            for fieldname in MIGRATION_RECOVERY_ACK_FIELDS
        )
        if previous_has_ack or not (
            previous_status == "requested" and self.status == "authorized"
        ):
            frappe.throw(
                _(
                    "Migration recovery acknowledgement is immutable after first "
                    "authorization"
                ),
                frappe.ValidationError,
            )

    def _validate_lifecycle_fields(self) -> None:
        generation = cint(getattr(self, "approval_generation", 0))
        approval_presence = [
            bool(cstr(getattr(self, fieldname, None)).strip())
            for fieldname in APPROVAL_FIELDS
            if fieldname != "approval_generation"
        ] + [generation > 0]
        has_complete_approval = all(approval_presence)
        has_any_approval = any(approval_presence)
        challenge_id = cstr(getattr(self, "approval_challenge_id", None)).strip()
        if has_any_approval != has_complete_approval or (
            has_complete_approval
            and (
                generation <= 0
                or not CHALLENGE_ID_PATTERN.fullmatch(challenge_id)
                or get_datetime(self.approval_expires_at)
                <= get_datetime(self.approval_issued_at)
            )
        ):
            frappe.throw(
                _("Safe reset approval challenge audit is incomplete or invalid"),
                frappe.ValidationError,
            )
        if has_complete_approval and (
            not cstr(getattr(self, "authorized_by", None)).strip()
            or not getattr(self, "authorized_at", None)
        ):
            frappe.throw(
                _("Approved safe reset requires an immutable manager audit"),
                frappe.ValidationError,
            )

        has_rotation = cint(getattr(self, "new_config_version", 0)) > 0 or any(
            bool(cstr(getattr(self, fieldname, None)).strip())
            for fieldname in ROTATION_FIELDS
            if fieldname != "new_config_version"
        )
        cancellation_presence = [
            bool(cstr(getattr(self, fieldname, None)).strip())
            for fieldname in CANCELLATION_FIELDS
        ]
        has_any_cancellation = any(cancellation_presence)
        has_complete_cancellation = all(
            bool(cstr(getattr(self, fieldname, None)).strip())
            for fieldname in CANCELLATION_REQUIRED_FIELDS
        )
        if has_any_cancellation != has_complete_cancellation:
            frappe.throw(
                _("Safe reset cancellation audit is incomplete"),
                frappe.ValidationError,
            )
        if self.status == "cancelled":
            cancellation_reason = cstr(
                getattr(self, "cancellation_reason", None)
            )
            cancellation_origin = cstr(
                getattr(self, "cancellation_origin", None)
            ).strip()
            cancelled_by_user = cstr(
                getattr(self, "cancelled_by_user", None)
            ).strip()
            cancelled_by_api_user = cstr(
                getattr(self, "cancelled_by_api_user", None)
            ).strip()
            api_user = cstr(self.api_user).strip()
            valid_device_actor = (
                cancellation_origin == "device_authenticated"
                and cancelled_by_user == api_user
                and cancelled_by_api_user == api_user
            )
            valid_manager_actor = (
                cancellation_origin == "system_manager"
                and bool(cancelled_by_user)
                and not cancelled_by_api_user
            )
            if (
                not has_complete_cancellation
                or cancellation_reason != cancellation_reason.strip()
                or not cancellation_reason
                or len(cancellation_reason) > 500
                or cancellation_origin not in CANCELLATION_ORIGINS
                or not (valid_device_actor or valid_manager_actor)
            ):
                frappe.throw(
                    _("Cancelled safe reset audit identity or reason is invalid"),
                    frappe.ValidationError,
                )
        elif has_any_cancellation:
            frappe.throw(
                _("Cancellation evidence is only valid for a cancelled safe reset"),
                frappe.ValidationError,
            )
        if self.status == "requested":
            if has_any_approval or generation or has_rotation:
                frappe.throw(
                    _("Requested safe reset cannot contain approval or rotation data"),
                    frappe.ValidationError,
                )
            return
        if self.status in {"cancelled", "expired"}:
            has_redemption = cint(
                getattr(self, "redemption_export_byte_length", 0)
            ) > 0 or cint(
                getattr(self, "redeemed_approval_generation", 0)
            ) > 0 or any(
                bool(cstr(getattr(self, fieldname, None)).strip())
                for fieldname in IMMUTABLE_REDEMPTION_FIELDS
                if fieldname
                not in {
                    "redeemed_approval_generation",
                    "redemption_export_byte_length",
                    "new_config_version",
                }
            )
            has_completion = cint(
                getattr(self, "completion_export_byte_length", 0)
            ) > 0 or any(
                bool(cstr(getattr(self, fieldname, None)).strip())
                for fieldname in IMMUTABLE_COMPLETION_FIELDS
                if fieldname != "completion_export_byte_length"
            )
            if has_rotation or has_redemption or has_completion:
                frappe.throw(
                    _(
                        "Cancelled or expired safe reset cannot contain credential "
                        "rotation, redemption, or completion evidence"
                    ),
                    frappe.ValidationError,
                )
            return
        if self.status in {"authorized", "redeemed", "completed"} and not (
            has_complete_approval and generation > 0
        ):
            frappe.throw(
                _("Authorized safe reset requires a complete approval challenge"),
                frappe.ValidationError,
            )
        current_idempotency = cstr(
            getattr(self, "current_redemption_idempotency_sha256", None)
        ).strip()
        current_result = cstr(
            getattr(self, "current_redemption_result_fingerprint", None)
        ).strip()
        if bool(current_idempotency) != bool(current_result):
            frappe.throw(
                _("Current safe reset redemption binding must be complete"),
                frappe.ValidationError,
            )
        if current_result and current_result != cstr(
            getattr(self, "redemption_result_fingerprint", None)
        ).strip():
            frappe.throw(
                _("Current safe reset redemption result binding is invalid"),
                frappe.ValidationError,
            )
        if self.status == "authorized":
            if has_rotation or cint(getattr(self, "new_config_version", 0)):
                frappe.throw(
                    _("Authorization must not rotate credentials or config"),
                    frappe.ValidationError,
                )
            return
        if self.status in {"redeemed", "completed"}:
            self._validate_redeemed_fields()
        if self.status == "completed":
            self._validate_completed_fields()
        elif cint(getattr(self, "completion_export_byte_length", 0)) > 0 or any(
            bool(cstr(getattr(self, fieldname, None)).strip())
            for fieldname in IMMUTABLE_COMPLETION_FIELDS
            if fieldname != "completion_export_byte_length"
        ):
            frappe.throw(
                _("Completion evidence is only valid for a completed safe reset"),
                frappe.ValidationError,
            )

    def _validate_redeemed_fields(self) -> None:
        required_fields = (
            "redeemed_approval_challenge_id",
            "redeemed_approval_generation",
            "redeemed_approval_token_sha256",
            "redeemed_approval_fingerprint",
            "redeemed_approval_expires_at",
            "redemption_idempotency_sha256",
            "redemption_export_sha256",
            "redemption_export_content_sha256",
            "redemption_export_byte_length",
            "redemption_setup_sha256",
            "redemption_result_fingerprint",
            "redemption_issued_at",
            "redeemed_at",
            "redeemed_recovery_expires_at",
            "credential_rotated_at",
            "new_config_version",
            "previous_credential_state",
            "issued_api_key_sha256",
            "issued_api_secret_sha256",
        )
        if not all(
            bool(cstr(getattr(self, fieldname, None)).strip())
            for fieldname in required_fields
        ):
            frappe.throw(
                _("Redeemed safe reset audit is incomplete"),
                frappe.ValidationError,
            )
        if cint(self.new_config_version) != cint(self.previous_config_version) + 1:
            frappe.throw(
                _("Redeemed safe reset must increment config exactly once"),
                frappe.ValidationError,
            )
        if not CHALLENGE_ID_PATTERN.fullmatch(
            cstr(self.redeemed_approval_challenge_id).strip()
        ) or cint(self.redeemed_approval_generation) <= 0:
            frappe.throw(
                _("Redeemed approval identity is invalid"),
                frappe.ValidationError,
            )
        if (
            cstr(self.redemption_export_sha256).strip()
            != cstr(self.export_sha256).strip()
            or cstr(self.redemption_export_content_sha256).strip()
            != cstr(self.export_content_sha256).strip()
            or cint(self.redemption_export_byte_length) != cint(self.export_byte_length)
        ):
            frappe.throw(
                _("Redemption archive evidence must match the immutable request"),
                frappe.ValidationError,
            )
        if get_datetime(self.redeemed_recovery_expires_at) <= get_datetime(
            self.redeemed_at
        ):
            frappe.throw(
                _("Redeemed response recovery expiry is invalid"),
                frappe.ValidationError,
            )
        if get_datetime(self.redeemed_approval_expires_at) <= get_datetime(
            self.redeemed_at
        ):
            frappe.throw(
                _("Redeemed approval expiry must be after the redemption time"),
                frappe.ValidationError,
            )

    def _validate_completed_fields(self) -> None:
        if not all(
            bool(cstr(getattr(self, fieldname, None)).strip())
            for fieldname in IMMUTABLE_COMPLETION_FIELDS
        ):
            frappe.throw(
                _("Completed safe reset audit is incomplete"),
                frappe.ValidationError,
            )
        if (
            cstr(self.completion_export_sha256).strip()
            != cstr(self.export_sha256).strip()
            or cstr(self.completion_export_content_sha256).strip()
            != cstr(self.export_content_sha256).strip()
            or cint(self.completion_export_byte_length) != cint(self.export_byte_length)
            or cstr(self.completed_by_api_user).strip() != cstr(self.api_user).strip()
        ):
            frappe.throw(
                _("Completion evidence does not match the redeemed safe reset"),
                frappe.ValidationError,
            )
        if cstr(self.completion_idempotency_sha256).strip() == cstr(
            self.redemption_idempotency_sha256
        ).strip():
            frappe.throw(
                _(
                    "Completion idempotency digest must differ from the redemption "
                    "idempotency digest"
                ),
                frappe.ValidationError,
            )

    def _validate_protocol_transitions(
        self,
        previous: Document,
        previous_status: str,
    ) -> None:
        previous_generation = cint(getattr(previous, "approval_generation", 0))
        current_generation = cint(getattr(self, "approval_generation", 0))
        approval_changed = any(
            _audit_field_value(previous, fieldname)
            != _audit_field_value(self, fieldname)
            for fieldname in APPROVAL_FIELDS
        )
        if previous_status == "requested" and self.status == "authorized":
            if current_generation != 1 or cint(
                getattr(self, "authorization_count", 0)
            ) != 1:
                frappe.throw(
                    _("First safe reset approval generation must be 1"),
                    frappe.ValidationError,
                )
        elif approval_changed:
            if self.status not in {"authorized", "redeemed"} or (
                current_generation != previous_generation + 1
            ) or cint(getattr(self, "authorization_count", 0)) != cint(
                getattr(previous, "authorization_count", 0)
            ) + 1:
                frappe.throw(
                    _("Safe reset approval reissue must increment generation once"),
                    frappe.ValidationError,
                )
        elif cint(getattr(self, "authorization_count", 0)) != cint(
            getattr(previous, "authorization_count", 0)
        ):
            frappe.throw(
                _("Authorization count may change only with an approval generation"),
                frappe.ValidationError,
            )

        current_binding_fields = (
            "current_redemption_idempotency_sha256",
            "current_redemption_result_fingerprint",
        )
        current_binding_changed = any(
            cstr(getattr(previous, fieldname, None)).strip()
            != cstr(getattr(self, fieldname, None)).strip()
            for fieldname in current_binding_fields
        )
        if approval_changed:
            if any(
                cstr(getattr(self, fieldname, None)).strip()
                for fieldname in current_binding_fields
            ):
                frappe.throw(
                    _("Approval reissue must invalidate the prior redemption binding"),
                    frappe.ValidationError,
                )
        elif current_binding_changed:
            previous_had_binding = any(
                cstr(getattr(previous, fieldname, None)).strip()
                for fieldname in current_binding_fields
            )
            current_has_binding = all(
                cstr(getattr(self, fieldname, None)).strip()
                for fieldname in current_binding_fields
            )
            if previous_had_binding or self.status != "redeemed" or not current_has_binding:
                frappe.throw(
                    _("Current approval redemption binding is one-time and immutable"),
                    frappe.ValidationError,
                )
            if cstr(
                getattr(self, "current_redemption_idempotency_sha256", None)
            ).strip() != cstr(
                getattr(self, "redemption_idempotency_sha256", None)
            ).strip() or cstr(
                getattr(self, "current_redemption_result_fingerprint", None)
            ).strip() != cstr(
                getattr(self, "redemption_result_fingerprint", None)
            ).strip():
                frappe.throw(
                    _(
                        "Current approval binding must match the committed redemption "
                        "result"
                    ),
                    frappe.ValidationError,
                )

        redemption_changed = [
            fieldname
            for fieldname in IMMUTABLE_REDEMPTION_FIELDS
            if _audit_field_value(previous, fieldname)
            != _audit_field_value(self, fieldname)
        ]
        if redemption_changed and not (
            previous_status == "authorized" and self.status == "redeemed"
        ):
            frappe.throw(
                _("Committed safe reset redemption result is immutable"),
                frappe.ValidationError,
            )

        completion_changed = [
            fieldname
            for fieldname in IMMUTABLE_COMPLETION_FIELDS
            if _audit_field_value(previous, fieldname)
            != _audit_field_value(self, fieldname)
        ]
        if completion_changed and not (
            previous_status == "redeemed" and self.status == "completed"
        ):
            frappe.throw(
                _("Committed safe reset completion result is immutable"),
                frappe.ValidationError,
            )

        cancellation_changed = [
            fieldname
            for fieldname in CANCELLATION_FIELDS
            if _audit_field_value(previous, fieldname)
            != _audit_field_value(self, fieldname)
        ]
        if cancellation_changed and not (
            previous_status in {"requested", "authorized"}
            and self.status == "cancelled"
        ):
            frappe.throw(
                _("Committed safe reset cancellation result is immutable"),
                frappe.ValidationError,
            )

    def on_trash(self) -> None:
        frappe.throw(
            _("KoPOS device safe reset audit records cannot be deleted"),
            frappe.ValidationError,
        )
