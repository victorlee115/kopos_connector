from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, now_datetime

from kopos_connector.api.devices import ensure_unique_device_api_user
from kopos_connector.services.static_qr_commissioning import (
    inspect_paynet_static_qr,
    validate_static_qr_metadata,
)
from kopos_connector.utils.pin import hash_pin, is_supported_pin_hash


class KoPOSDevice(Document):
    def validate(self):
        self.device_id = cstr(self.device_id).strip()
        self.device_name = cstr(self.device_name).strip()
        self.device_prefix = cstr(self.device_prefix).strip().upper()
        self.api_user = cstr(getattr(self, "api_user", None)).strip() or None
        self.static_qr_payload = (
            cstr(getattr(self, "static_qr_payload", None)).strip() or None
        )
        self.app_min_version = cstr(self.app_min_version).strip() or None

        if not self.device_id:
            frappe.throw(_("Device ID is required"), frappe.ValidationError)

        if not self.device_name:
            frappe.throw(_("Device Name is required"), frappe.ValidationError)

        ensure_unique_device_api_user(self.api_user, current_device_name=self.name)
        self._validate_static_qr_commissioning()
        self._validate_printers()
        self._normalize_users()
        self._bump_config_version_if_needed()

    def _validate_static_qr_commissioning(self) -> None:
        metadata_fields = (
            "static_qr_payload_sha256",
            "static_qr_merchant_id",
            "static_qr_acquirer_id",
            "static_qr_merchant_name",
            "static_qr_version",
            "static_qr_company",
        )
        if not self.static_qr_payload:
            for fieldname in metadata_fields:
                setattr(self, fieldname, None)
            self.static_qr_commissioned_at = None
            return

        inspection = inspect_paynet_static_qr(self.static_qr_payload)
        profile_company = cstr(
            frappe.db.get_value("POS Profile", self.pos_profile, "company")
        ).strip()
        if not profile_company:
            frappe.throw(
                _("Static QR requires a POS Profile company"),
                frappe.ValidationError,
            )

        previous = None
        previous_getter = getattr(self, "get_doc_before_save", None)
        if callable(previous_getter):
            previous = previous_getter()
        previous_payload = cstr(
            getattr(previous, "static_qr_payload", None)
        ).strip()
        payload_changed = previous is None or previous_payload != self.static_qr_payload
        metadata_missing = any(
            not cstr(getattr(self, fieldname, None)).strip()
            for fieldname in metadata_fields
        ) or not cstr(
            getattr(self, "static_qr_commissioned_at", None)
        ).strip()

        configured_company = cstr(
            getattr(self, "static_qr_company", None)
        ).strip()
        if (
            not payload_changed
            and configured_company
            and configured_company != profile_company
        ):
            frappe.throw(
                _(
                    "Static QR is commissioned for another company; clear it, "
                    "save, and recommission it for this POS Profile"
                ),
                frappe.ValidationError,
            )

        if payload_changed or metadata_missing:
            self.static_qr_payload_sha256 = inspection["payload_sha256"]
            self.static_qr_merchant_id = inspection["merchant_id"]
            self.static_qr_acquirer_id = inspection["acquirer_id"]
            self.static_qr_merchant_name = inspection["merchant_name"]
            self.static_qr_version = inspection["version"]
            self.static_qr_company = profile_company
            self.static_qr_commissioned_at = now_datetime()

        validate_static_qr_metadata(
            inspection,
            payload_sha256=self.static_qr_payload_sha256,
            merchant_id=self.static_qr_merchant_id,
            acquirer_id=self.static_qr_acquirer_id,
            merchant_name=self.static_qr_merchant_name,
            version=self.static_qr_version,
            commissioned_at=self.static_qr_commissioned_at,
            configured_company=self.static_qr_company,
            expected_company=profile_company,
        )

    def _validate_printers(self) -> None:
        enabled_roles: dict[str, int] = {}
        for row in self.printers or []:
            role = cstr(row.role).strip()
            protocol = cstr(row.protocol).strip()
            row.host = cstr(row.host).strip()
            row.port = cint(row.port or 9100)
            row.copies = max(1, cint(row.copies or 1))
            row.label_width_mm = cint(row.label_width_mm or 0) or None
            row.label_height_mm = cint(row.label_height_mm or 0) or None

            if role not in {"receipt", "sticker"}:
                frappe.throw(
                    _("Printer role must be receipt or sticker"), frappe.ValidationError
                )
            if protocol not in {"escpos_tcp", "tspl_tcp"}:
                frappe.throw(
                    _("Printer protocol must be escpos_tcp or tspl_tcp"),
                    frappe.ValidationError,
                )
            if cint(row.enabled) and not row.host:
                frappe.throw(
                    _("Enabled printer rows require a host"), frappe.ValidationError
                )
            if row.port <= 0:
                frappe.throw(
                    _("Printer port must be a positive integer"), frappe.ValidationError
                )
            if role == "sticker" and cint(row.enabled):
                if not row.label_width_mm or not row.label_height_mm:
                    frappe.throw(
                        _("Sticker printer rows require label width and height"),
                        frappe.ValidationError,
                    )

            if cint(row.enabled):
                enabled_roles[role] = enabled_roles.get(role, 0) + 1

        for role, count in enabled_roles.items():
            if count > 1:
                frappe.throw(
                    _("Only one enabled {0} printer is allowed per device").format(
                        role
                    ),
                    frappe.ValidationError,
                )

    def _normalize_users(self) -> None:
        seen_users: set[str] = set()
        active_user_count = 0
        default_cashier_count = 0

        for row in self.device_users or []:
            row.user = cstr(row.user).strip()
            row.display_name = cstr(row.display_name).strip()
            row.pin = cstr(getattr(row, "pin", None)).strip()
            row.pin_hash = cstr(getattr(row, "pin_hash", None)).strip()

            if not row.user:
                frappe.throw(
                    _("Each device user row must link to a User"),
                    frappe.ValidationError,
                )
            if row.user in seen_users:
                frappe.throw(
                    _("User {0} appears more than once on this device").format(
                        row.user
                    ),
                    frappe.ValidationError,
                )
            seen_users.add(row.user)

            if not row.display_name:
                row.display_name = (
                    cstr(frappe.db.get_value("User", row.user, "full_name")) or row.user
                )

            if row.pin:
                row.pin_hash = hash_pin(row.pin)
            if not row.pin_hash:
                frappe.throw(
                    _("User {0} must have a PIN configured").format(row.user),
                    frappe.ValidationError,
                )
            if not is_supported_pin_hash(row.pin_hash):
                frappe.throw(
                    _(
                        "User {0} has an unsupported PIN verifier; enter a new "
                        "4-digit PIN and save the device"
                    ).format(row.user),
                    frappe.ValidationError,
                )

            row.pin = None

            if cint(row.active):
                active_user_count += 1
                if cint(row.default_cashier):
                    default_cashier_count += 1

        if cint(self.enabled) and active_user_count == 0:
            frappe.throw(
                _("Enabled KoPOS Devices require at least one active device user"),
                frappe.ValidationError,
            )

        if default_cashier_count > 1:
            frappe.throw(
                _("Only one active default cashier is allowed per device"),
                frappe.ValidationError,
            )

    def _bump_config_version_if_needed(self) -> None:
        current_version = cint(self.config_version or 0)
        if self.is_new():
            self.config_version = max(1, current_version)
            return

        previous = frappe.get_doc(self.doctype, self.name)
        previous_signature = _config_signature(previous)
        next_signature = _config_signature(self)
        if previous_signature != next_signature:
            self.config_version = max(1, current_version) + 1
            # A device report is authority for one exact configuration. Any
            # configuration change invalidates the prior inventory authority;
            # the tablet must acknowledge a fresh overlay and report again.
            for fieldname in (
                "inventory_report_schema_version",
                "inventory_report_revision",
                "inventory_observed_at",
                "inventory_report_received_at",
                "inventory_config_version",
                "inventory_overlay_version",
                "inventory_overlay_hash",
                "inventory_sales_pending",
                "inventory_sales_syncing",
                "inventory_sales_failed",
                "inventory_sales_dead_letter",
                "inventory_oldest_unsaved_sale_at",
                "inventory_commands_pending",
                "inventory_commands_failed",
                "inventory_report_payload_hash",
            ):
                if hasattr(self, fieldname):
                    setattr(self, fieldname, None if fieldname.endswith(("_at", "_hash", "_version")) else 0)
        else:
            self.config_version = max(1, current_version)


def _config_signature(doc: Document) -> str:
    payload: dict[str, Any] = {
        "device_id": cstr(doc.device_id).strip(),
        "device_name": cstr(doc.device_name).strip(),
        "device_prefix": cstr(doc.device_prefix).strip().upper(),
        "static_qr_payload": cstr(getattr(doc, "static_qr_payload", None)).strip(),
        "static_qr_payload_sha256": cstr(
            getattr(doc, "static_qr_payload_sha256", None)
        ).strip(),
        "static_qr_merchant_id": cstr(
            getattr(doc, "static_qr_merchant_id", None)
        ).strip(),
        "static_qr_acquirer_id": cstr(
            getattr(doc, "static_qr_acquirer_id", None)
        ).strip(),
        "static_qr_merchant_name": cstr(
            getattr(doc, "static_qr_merchant_name", None)
        ).strip(),
        "static_qr_version": cstr(
            getattr(doc, "static_qr_version", None)
        ).strip(),
        "static_qr_commissioned_at": cstr(
            getattr(doc, "static_qr_commissioned_at", None)
        ).strip(),
        "static_qr_company": cstr(
            getattr(doc, "static_qr_company", None)
        ).strip(),
        "enabled": cint(doc.enabled),
        "pos_profile": cstr(doc.pos_profile).strip(),
        "allow_training_mode": cint(doc.allow_training_mode),
        "allow_manual_settings_override": cint(doc.allow_manual_settings_override),
        "app_min_version": cstr(doc.app_min_version).strip(),
        "printers": [
            {
                "role": cstr(row.role).strip(),
                "enabled": cint(row.enabled),
                "protocol": cstr(row.protocol).strip(),
                "host": cstr(row.host).strip(),
                "port": cint(row.port or 0),
                "label_width_mm": cint(row.label_width_mm or 0),
                "label_height_mm": cint(row.label_height_mm or 0),
                "copies": cint(row.copies or 1),
            }
            for row in (doc.printers or [])
        ],
        "device_users": [
            {
                "user": cstr(row.user).strip(),
                "display_name": cstr(row.display_name).strip(),
                "pin_hash": cstr(getattr(row, "pin_hash", None)).strip(),
                "active": cint(row.active),
                "can_manager_override": cint(row.can_manager_override),
                "can_refund": cint(row.can_refund),
                "can_void": cint(row.can_void),
                "can_open_shift": cint(row.can_open_shift),
                "can_close_shift": cint(row.can_close_shift),
                "default_cashier": cint(row.default_cashier),
            }
            for row in (doc.device_users or [])
        ],
    }
    return json.dumps(payload, sort_keys=True)
