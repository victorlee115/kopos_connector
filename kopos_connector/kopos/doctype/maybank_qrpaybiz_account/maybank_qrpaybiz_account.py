from __future__ import annotations

import re
import secrets
from urllib.parse import urlsplit

import frappe
from frappe.model.document import Document
from frappe.utils import cstr, now_datetime

from kopos_connector.services.maybank.client import (
    DEFAULT_BASE_URL,
    DEFAULT_DEVICE_NAME,
    DEFAULT_DEVICE_OS,
    PROVIDER_DEVICE_NAME_FIELD,
    PROVIDER_DEVICE_OS_FIELD,
    _explicit_mock_mode_enabled,
    invalidate_account_auth_cache,
    validate_base_url,
)


_DEVICE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class MaybankQRPayBizAccount(Document):
    """One reusable Maybank credential principal.

    The provider device identity is generated once and is deliberately kept
    when credentials rotate.  Active duplicate principals are rejected so two
    branches cannot race to register the same provider login.
    """

    def before_insert(self) -> None:
        if not cstr(self.provider_device_id).strip():
            self.provider_device_id = secrets.token_hex(16)
        if not cstr(self.provider_device_name).strip():
            self.provider_device_name = DEFAULT_DEVICE_NAME
        if not cstr(self.provider_device_os).strip():
            self.provider_device_os = DEFAULT_DEVICE_OS

    def validate(self) -> None:
        self.username = cstr(self.username).strip()
        self.user_type = cstr(self.user_type).strip().lower() or "merchant"
        self.environment = cstr(self.environment).strip().lower() or "production"
        self.base_url = validate_base_url(
            cstr(self.base_url).strip() or DEFAULT_BASE_URL,
            allow_mock=_explicit_mock_mode_enabled(),
        )
        if not self.username:
            frappe.throw("Maybank QRPayBiz Account username is required", frappe.ValidationError)
        if self.user_type not in {"merchant", "cashier", "corporate"}:
            frappe.throw("Maybank QRPayBiz Account user type is invalid", frappe.ValidationError)
        if self.environment not in {"sandbox", "production"}:
            frappe.throw("Maybank QRPayBiz Account environment is invalid", frappe.ValidationError)
        if not _DEVICE_ID_PATTERN.fullmatch(cstr(self.provider_device_id).strip()):
            frappe.throw("Maybank provider device identity is invalid", frappe.ValidationError)
        for fieldname in (PROVIDER_DEVICE_NAME_FIELD, PROVIDER_DEVICE_OS_FIELD):
            value = " ".join(cstr(getattr(self, fieldname, None)).strip().split())
            if not value or not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9 ._()/-]{1,63}$", value):
                frappe.throw(f"Maybank provider {fieldname} is invalid", frappe.ValidationError)
            setattr(self, fieldname, value)
        if not self.is_new():
            original_device_id = cstr(
                frappe.db.get_value("Maybank QRPayBiz Account", self.name, "provider_device_id")
            ).strip()
            if original_device_id and cstr(self.provider_device_id).strip() != original_device_id:
                frappe.throw(
                    "Maybank provider device identity is immutable; rotate the PIN instead",
                    frappe.ValidationError,
                )
        self._reject_duplicate_active_principal()

    def on_update(self) -> None:
        if self.has_value_changed("encrypted_pin"):
            self.credential_rotated_at = now_datetime()
            invalidate_account_auth_cache(self)

    def _reject_duplicate_active_principal(self) -> None:
        if not int(self.enabled or 0):
            return
        filters = {
            "enabled": 1,
            "environment": self.environment,
            "user_type": self.user_type,
        }
        for candidate in frappe.get_all(
            "Maybank QRPayBiz Account",
            filters=filters,
            fields=["name", "base_url", "username"],
        ):
            if candidate.name == self.name:
                continue
            if (
                _normalized_api_origin(candidate.base_url) == _normalized_api_origin(self.base_url)
                and cstr(candidate.username).strip().casefold() == self.username.casefold()
            ):
                frappe.throw(
                    "An enabled Maybank QRPayBiz Account already exists for this login and API origin",
                    frappe.ValidationError,
                )


def _normalized_api_origin(value: str) -> str:
    """Normalize the provider origin used for active-principal uniqueness."""

    raw = cstr(value).strip()
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    return raw.rstrip("/").casefold()
