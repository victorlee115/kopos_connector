# pyright: reportMissingImports=false

"""Read-only complete-catalog proof for a restored production database."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import re
from collections.abc import Mapping
from typing import Any, Callable

import frappe
from frappe.utils import cint, cstr

from kopos_connector.api.catalog import build_catalog_payload


PRODUCER = "kopos_connector.acceptance.restored_catalog_preflight.run_v1"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
CATALOG_VERSION_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
CATALOG_ARRAY_FIELDS = (
    "categories",
    "items",
    "modifier_groups",
    "modifier_options",
)


def _fail(message: str) -> None:
    frappe.throw(message, frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")


def _value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _exact_text(value: Any, fieldname: str) -> str:
    text = cstr(value).strip()
    if not text:
        _fail(f"{fieldname} is required")
    return text


def _exact_sha256(value: Any, fieldname: str) -> str:
    text = _exact_text(value, fieldname)
    if not SHA256_PATTERN.fullmatch(text):
        _fail(f"{fieldname} must be a lowercase SHA-256")
    return text


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _catalog_content(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude the request timestamp while retaining all catalog content."""
    return {
        key: value
        for key, value in payload.items()
        if key != "timestamp"
    }


def _validate_full_payload(
    payload: Any,
    *,
    expected_pos_profile: str,
) -> tuple[str, str, dict[str, int]]:
    if not isinstance(payload, Mapping):
        _fail("Complete catalog generation returned a non-object payload")
    if payload.get("sync_mode") != "full" or cint(payload.get("unchanged")) != 0:
        _fail("Complete catalog preflight did not return a full snapshot")

    catalog_version = cstr(payload.get("catalog_version")).strip()
    if not CATALOG_VERSION_PATTERN.fullmatch(catalog_version):
        _fail("Complete catalog preflight returned an invalid catalog version")

    metadata_value = payload.get("metadata")
    if not isinstance(metadata_value, Mapping):
        _fail("Complete catalog preflight returned invalid metadata")
    if cstr(metadata_value.get("pos_profile")).strip() != expected_pos_profile:
        _fail("Complete catalog preflight resolved a different POS Profile")

    counts: dict[str, int] = {}
    for fieldname in CATALOG_ARRAY_FIELDS:
        rows = payload.get(fieldname)
        if not isinstance(rows, list):
            _fail(f"Complete catalog field {fieldname} is not an array")
        counts[fieldname] = len(rows)

    content_sha256 = _sha256_text(_canonical_json(_catalog_content(payload)))
    return catalog_version, content_sha256, counts


def _enabled_devices() -> list[dict[str, str]]:
    rows = frappe.get_all(
        "KoPOS Device",
        filters={"enabled": 1},
        fields=["name", "device_id", "pos_profile"],
        order_by="device_id asc, name asc",
        limit_page_length=0,
    )
    devices: list[dict[str, str]] = []
    device_ids: set[str] = set()
    for row in rows or []:
        device_name = _exact_text(_value(row, "name"), "KoPOS Device name")
        device_id = _exact_text(
            _value(row, "device_id"),
            f"KoPOS Device {device_name} device_id",
        )
        pos_profile = _exact_text(
            _value(row, "pos_profile"),
            f"KoPOS Device {device_name} POS Profile",
        )
        if device_id in device_ids:
            _fail("Enabled KoPOS Device device_id values are not unique")
        device_ids.add(device_id)
        devices.append(
            {
                "name": device_name,
                "device_id": device_id,
                "pos_profile": pos_profile,
            }
        )

    if not devices:
        _fail("Restored database has no enabled KoPOS Device to validate")
    return sorted(devices, key=lambda row: (row["device_id"], row["name"]))


def _build_device_proof(
    device: Mapping[str, str],
    *,
    builder: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    device_id = device["device_id"]
    pos_profile = device["pos_profile"]

    first = builder(device_id=device_id, known_version=None)
    first_version, first_sha256, first_counts = _validate_full_payload(
        first,
        expected_pos_profile=pos_profile,
    )
    second = builder(device_id=device_id, known_version=None)
    second_version, second_sha256, second_counts = _validate_full_payload(
        second,
        expected_pos_profile=pos_profile,
    )

    if (
        second_version != first_version
        or second_sha256 != first_sha256
        or second_counts != first_counts
    ):
        _fail("Complete catalog generation is not deterministic across two reads")

    return {
        "deviceIdentitySha256": _sha256_text(device_id),
        "posProfileIdentitySha256": _sha256_text(pos_profile),
        "catalogVersion": first_version,
        "catalogPayloadSha256": first_sha256,
        "counts": first_counts,
        "fullBuildCount": 2,
    }


def run_v1(
    restored_backup_sha256: str,
    erp_artifact_sha256: str,
    expected_connector_version: str,
) -> dict[str, Any]:
    """Generate every enabled device catalog twice without provider or DB writes.

    This function deliberately calls the internal catalog builder instead of the
    HTTP facade. The HTTP facade updates the device heartbeat; the catalog
    builder performs the same complete validation without that operational
    write. It does not inspect or assert inventory balances.
    """

    backup_sha256 = _exact_sha256(
        restored_backup_sha256,
        "restored_backup_sha256",
    )
    artifact_sha256 = _exact_sha256(
        erp_artifact_sha256,
        "erp_artifact_sha256",
    )
    expected_version = _exact_text(
        expected_connector_version,
        "expected_connector_version",
    )
    installed_version = metadata.version("kopos_connector")
    if installed_version != expected_version:
        _fail("Installed connector version does not match the candidate binding")

    devices = _enabled_devices()
    device_proofs = [
        _build_device_proof(device, builder=build_catalog_payload)
        for device in devices
    ]
    profile_hashes = sorted(
        {proof["posProfileIdentitySha256"] for proof in device_proofs}
    )
    aggregate_sha256 = _sha256_text(_canonical_json(device_proofs))

    return {
        "schemaVersion": 1,
        "status": "passed",
        "evidenceLevel": "restored_production_data",
        "producer": PRODUCER,
        "readOnly": True,
        "providerNetworkCalls": 0,
        "inventoryAssertions": 0,
        "connectorVersion": installed_version,
        "erpArtifactSha256": artifact_sha256,
        "restoredBackupSha256": backup_sha256,
        "enabledDeviceCount": len(device_proofs),
        "referencedPosProfileCount": len(profile_hashes),
        "completeCatalogBuildCount": len(device_proofs) * 2,
        "referencedPosProfileIdentitySha256": profile_hashes,
        "devices": device_proofs,
        "aggregateSha256": aggregate_sha256,
    }
