# pyright: reportMissingImports=false

"""Read-only commissioned static-QR checks for the target preflight."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cstr

from kopos_connector.acceptance.maybank_uat_common import canonical_json_sha256
from kopos_connector.services.static_qr_commissioning import (
    commissioned_static_qr_config,
)


def _fail(message: str) -> None:
    frappe.throw(message, frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")


def _value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _text(value: Any, fieldname: str) -> str:
    resolved = cstr(value).strip()
    if not resolved:
        _fail(f"{fieldname} is required")
    return resolved


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_ascii_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_static_qr_proof(company: str) -> dict[str, Any]:
    rows = frappe.get_all(
        "KoPOS Device",
        filters={"enabled": 1},
        fields=["name", "device_id", "pos_profile"],
        order_by="device_id asc, name asc",
        limit_page_length=0,
    )
    if not rows:
        _fail("Target ERP has no enabled KoPOS Device")

    proofs: list[dict[str, Any]] = []
    for row in rows:
        device_identity = _text(
            _value(row, "device_id") or _value(row, "name"),
            "enabled device identity",
        )
        device_name = _text(_value(row, "name"), "enabled device name")
        pos_profile = _text(
            _value(row, "pos_profile"),
            "enabled device POS Profile",
        )
        commissioned = commissioned_static_qr_config(
            frappe.get_doc("KoPOS Device", device_name),
            expected_company=company,
        )
        if commissioned is None:
            _fail("Every enabled tablet requires a commissioned static QR")
        proofs.append(
            {
                "deviceIdentitySha256": _sha256(device_identity),
                "posProfileIdentitySha256": _sha256(pos_profile),
                "payloadSha256": commissioned["static_qr_payload_sha256"],
                "merchantIdentitySha256": _canonical_ascii_sha256(
                    {
                        "merchantId": commissioned["static_qr_merchant_id"],
                        "acquirerId": commissioned["static_qr_acquirer_id"],
                        "merchantName": commissioned["static_qr_merchant_name"],
                        "version": commissioned["static_qr_version"],
                    }
                ),
                "company": company,
                "commissionedAtPresent": True,
            }
        )

    return {
        "passed": True,
        "enabledDeviceCount": len(proofs),
        "devices": proofs,
        "deviceSetSha256": canonical_json_sha256(proofs),
    }


def require_stable_enabled_device_configuration(
    first_static_qr: Mapping[str, Any],
    second_static_qr: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> None:
    if canonical_json_sha256(first_static_qr) != canonical_json_sha256(
        second_static_qr
    ):
        _fail("Enabled tablet or static QR configuration changed during preflight")

    def device_pairs(proof: Mapping[str, Any], label: str) -> list[tuple[str, str]]:
        return sorted(
            (
                _text(_value(device, "deviceIdentitySha256"), f"{label} device hash"),
                _text(
                    _value(device, "posProfileIdentitySha256"),
                    f"{label} POS Profile hash",
                ),
            )
            for device in _value(proof, "devices") or []
        )

    static_devices = device_pairs(second_static_qr, "static QR")
    catalog_devices = device_pairs(catalog, "catalog")
    if (
        _value(second_static_qr, "enabledDeviceCount") != len(static_devices)
        or _value(catalog, "enabledDeviceCount") != len(catalog_devices)
        or not static_devices
        or static_devices != catalog_devices
    ):
        _fail("Static QR and catalog checks used different enabled tablets")
