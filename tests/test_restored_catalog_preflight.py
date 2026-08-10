from __future__ import annotations

import json

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe  # noqa: E402

from kopos_connector.acceptance import restored_catalog_preflight as preflight  # noqa: E402


BACKUP_SHA256 = "a" * 64
ARTIFACT_SHA256 = "b" * 64
CONNECTOR_VERSION = "1.0.12"
CATALOG_VERSION = f"sha256:{'c' * 64}"


def _payload(*, item_id: str = "ITEM-1", timestamp: str = "first") -> dict:
    return {
        "sync_mode": "full",
        "unchanged": 0,
        "catalog_version": CATALOG_VERSION,
        "timestamp": timestamp,
        "categories": [{"id": "DRINKS"}],
        "items": [{"id": item_id}],
        "modifier_groups": [],
        "modifier_options": [],
        "metadata": {"pos_profile": "Main Profile"},
    }


def test_preflight_builds_every_enabled_device_twice_and_sanitizes_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = [
        {"name": "DEVICE-DOC-1", "device_id": "tablet-private-1", "pos_profile": "Main Profile"},
        {"name": "DEVICE-DOC-2", "device_id": "tablet-private-2", "pos_profile": "Main Profile"},
    ]
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(preflight.frappe, "get_all", lambda *args, **kwargs: devices)
    monkeypatch.setattr(preflight.metadata, "version", lambda name: CONNECTOR_VERSION)

    def build(*, device_id: str, known_version: object) -> dict:
        calls.append((device_id, known_version))
        return _payload(timestamp=f"request-{len(calls)}")

    monkeypatch.setattr(preflight, "build_catalog_payload", build)

    report = preflight.run_v1(
        BACKUP_SHA256,
        ARTIFACT_SHA256,
        CONNECTOR_VERSION,
    )

    assert calls == [
        ("tablet-private-1", None),
        ("tablet-private-1", None),
        ("tablet-private-2", None),
        ("tablet-private-2", None),
    ]
    assert report["status"] == "passed"
    assert report["evidenceLevel"] == "restored_production_data"
    assert report["enabledDeviceCount"] == 2
    assert report["referencedPosProfileCount"] == 1
    assert report["completeCatalogBuildCount"] == 4
    assert report["readOnly"] is True
    assert report["providerNetworkCalls"] == 0
    assert report["inventoryAssertions"] == 0

    serialized = json.dumps(report, sort_keys=True)
    assert "tablet-private" not in serialized
    assert "Main Profile" not in serialized
    assert "DEVICE-DOC" not in serialized


def test_preflight_fails_when_the_second_complete_catalog_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight.frappe,
        "get_all",
        lambda *args, **kwargs: [
            {
                "name": "DEVICE-DOC-1",
                "device_id": "tablet-private-1",
                "pos_profile": "Main Profile",
            }
        ],
    )
    monkeypatch.setattr(preflight.metadata, "version", lambda name: CONNECTOR_VERSION)
    calls = 0

    def build(*, device_id: str, known_version: object) -> dict:
        nonlocal calls
        calls += 1
        return _payload(item_id=f"ITEM-{calls}")

    monkeypatch.setattr(preflight, "build_catalog_payload", build)

    with pytest.raises(
        frappe.ValidationError,
        match="not deterministic",
    ):
        preflight.run_v1(
            BACKUP_SHA256,
            ARTIFACT_SHA256,
            CONNECTOR_VERSION,
        )


def test_preflight_propagates_catalog_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight.frappe,
        "get_all",
        lambda *args, **kwargs: [
            {
                "name": "DEVICE-DOC-1",
                "device_id": "tablet-private-1",
                "pos_profile": "Main Profile",
            }
        ],
    )
    monkeypatch.setattr(preflight.metadata, "version", lambda name: CONNECTOR_VERSION)

    escaped_error = frappe.ValidationError(
        "Recipe AMERICANO_COFFEE_RECIPE changes the selection rules"
    )

    def fail_catalog(*, device_id: str, known_version: object) -> dict:
        raise escaped_error

    monkeypatch.setattr(preflight, "build_catalog_payload", fail_catalog)

    with pytest.raises(frappe.ValidationError) as captured:
        preflight.run_v1(
            BACKUP_SHA256,
            ARTIFACT_SHA256,
            CONNECTOR_VERSION,
        )

    assert captured.value is escaped_error


def test_preflight_fails_closed_without_an_enabled_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.frappe, "get_all", lambda *args, **kwargs: [])
    monkeypatch.setattr(preflight.metadata, "version", lambda name: CONNECTOR_VERSION)

    with pytest.raises(frappe.ValidationError, match="no enabled KoPOS Device"):
        preflight.run_v1(
            BACKUP_SHA256,
            ARTIFACT_SHA256,
            CONNECTOR_VERSION,
        )
