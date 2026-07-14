import frappe
from frappe.model.document import Document
from frappe.utils import cint, cstr

from kopos_connector.services.maybank.client import (
    PROVIDER_DEVICE_ID_FIELD,
    PROVIDER_DEVICE_NAME_FIELD,
    PROVIDER_DEVICE_OS_FIELD,
    _explicit_mock_mode_enabled,
    _valid_provider_device_id,
    _valid_provider_metadata,
    validate_base_url,
)


class MaybankSettings(Document):
    def validate(self) -> None:
        existing_device_id = _valid_provider_device_id(
            frappe.db.get_single_value("Maybank Settings", PROVIDER_DEVICE_ID_FIELD)
        )
        submitted_device_id = _valid_provider_device_id(
            getattr(self, PROVIDER_DEVICE_ID_FIELD, None)
        )
        if existing_device_id and submitted_device_id != existing_device_id:
            frappe.throw(
                "Maybank provider device identity cannot be rotated through settings",
                frappe.ValidationError,
            )
        if existing_device_id:
            self.provider_device_id = existing_device_id

        for fieldname in (PROVIDER_DEVICE_NAME_FIELD, PROVIDER_DEVICE_OS_FIELD):
            existing_metadata = _valid_provider_metadata(
                frappe.db.get_single_value("Maybank Settings", fieldname)
            )
            submitted_metadata = _valid_provider_metadata(
                getattr(self, fieldname, None)
            )
            if existing_metadata and submitted_metadata != existing_metadata:
                frappe.throw(
                    f"{fieldname} cannot be rotated through settings",
                    frappe.ValidationError,
                )
            if existing_metadata:
                setattr(self, fieldname, existing_metadata)

        self.base_url = validate_base_url(
            cstr(getattr(self, "base_url", None)),
            allow_mock=_explicit_mock_mode_enabled(),
        )
        if cint(getattr(self, "enabled", 0)) and not _valid_provider_device_id(
            getattr(self, PROVIDER_DEVICE_ID_FIELD, None)
        ):
            frappe.throw(
                "Maybank provider device identity is missing; run bench migrate before enabling Maybank",
                frappe.ValidationError,
            )
        if cint(getattr(self, "enabled", 0)) and any(
            not _valid_provider_metadata(getattr(self, fieldname, None))
            for fieldname in (PROVIDER_DEVICE_NAME_FIELD, PROVIDER_DEVICE_OS_FIELD)
        ):
            frappe.throw(
                "Maybank provider device metadata is missing; run bench migrate before enabling Maybank",
                frappe.ValidationError,
            )
