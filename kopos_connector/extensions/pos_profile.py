from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cint, cstr, flt, now_datetime


# These are the POS Profile values read by serialize_device_config.  Keep this
# intentionally narrow: price-list/catalog changes have their own content hash
# and unrelated POS Profile saves must not churn managed device config versions.
SERIALIZED_POS_PROFILE_FIELDS = (
    "company",
    "warehouse",
    "currency",
    "custom_kopos_enable_sst",
    "custom_kopos_sst_rate",
    "custom_kopos_static_qr_enabled",
    "custom_kopos_static_qr_payload",
    "custom_kopos_static_qr_payload_sha256",
    "custom_kopos_manual_qr_suspense_account",
    "custom_kopos_automatic_qr_enabled",
    "custom_kopos_maybank_qrpaybiz_account",
    "custom_kopos_maybank_outlet_id",
    "custom_kopos_qr_clearing_account",
    "custom_kopos_qr_settlement_bank_account",
)


class KoPOSPOSProfileConfigMixin:
    """Propagate serialized POS Profile changes to every bound KoPOS device."""

    def on_update(self) -> None:
        parent_on_update = getattr(super(), "on_update", None)
        if callable(parent_on_update):
            parent_on_update()
        invalidate_bound_device_configs_for_profile(self)

    def after_rename(
        self,
        old_name: str,
        new_name: str,
        merge: bool = False,
    ) -> None:
        parent_after_rename = getattr(super(), "after_rename", None)
        if callable(parent_after_rename):
            parent_after_rename(old_name, new_name, merge=merge)
        del old_name, merge
        bump_bound_device_config_versions(new_name)


def invalidate_bound_device_configs_for_profile(profile_doc: Any) -> bool:
    """Atomically bump devices only when serialized profile values changed."""

    get_before_save = getattr(profile_doc, "get_doc_before_save", None)
    if not callable(get_before_save):
        return False
    previous = get_before_save()
    if previous is None:
        # A new POS Profile cannot yet have valid Link-bound devices.
        return False
    if _profile_config_signature(previous) == _profile_config_signature(profile_doc):
        return False

    profile_name = cstr(_value(profile_doc, "name")).strip()
    if not profile_name:
        return False
    bump_bound_device_config_versions(profile_name)
    return True


def bump_bound_device_config_versions(profile_name: str) -> None:
    """Increment all bound devices in one concurrency-safe database statement."""

    resolved_profile = cstr(profile_name).strip()
    if not resolved_profile:
        return

    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    frappe.db.sql(
        """
        UPDATE `tabKoPOS Device`
           SET `config_version` = GREATEST(COALESCE(`config_version`, 0), 1) + 1,
               `modified` = %(modified)s,
               `modified_by` = %(modified_by)s
         WHERE `pos_profile` = %(pos_profile)s
        """,
        {
            "modified": now_datetime(),
            "modified_by": session_user or "Administrator",
            "pos_profile": resolved_profile,
        },
    )


def _profile_config_signature(profile_doc: Any) -> tuple[Any, ...]:
    return (
        cstr(_value(profile_doc, "company")).strip(),
        cstr(_value(profile_doc, "warehouse")).strip(),
        cstr(_value(profile_doc, "currency")).strip(),
        cint(_value(profile_doc, "custom_kopos_enable_sst")),
        flt(_value(profile_doc, "custom_kopos_sst_rate"), 6),
        cint(_value(profile_doc, "custom_kopos_static_qr_enabled")),
        cstr(_value(profile_doc, "custom_kopos_static_qr_payload")).strip(),
        cstr(_value(profile_doc, "custom_kopos_static_qr_payload_sha256")).strip(),
        cstr(_value(profile_doc, "custom_kopos_manual_qr_suspense_account")).strip(),
        cint(_value(profile_doc, "custom_kopos_automatic_qr_enabled")),
        cstr(_value(profile_doc, "custom_kopos_maybank_qrpaybiz_account")).strip(),
        cstr(_value(profile_doc, "custom_kopos_maybank_outlet_id")).strip(),
        cstr(_value(profile_doc, "custom_kopos_qr_clearing_account")).strip(),
        cstr(_value(profile_doc, "custom_kopos_qr_settlement_bank_account")).strip(),
    )


def _value(document: Any, fieldname: str) -> Any:
    if isinstance(document, Mapping):
        return document.get(fieldname)
    return getattr(document, fieldname, None)
