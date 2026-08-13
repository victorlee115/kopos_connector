from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()


EXPECTED_PATCHES = (
    "kopos_connector.patches.remove_duplicate_modifier_client_script",
    "kopos_connector.patches.normalize_duplicate_device_api_users",
    "kopos_connector.patches.add_order_reference_to_fb_stock_override_log",
    "kopos_connector.patches.backfill_fb_modifiers_from_kopos",
    "kopos_connector.patches.quarantine_legacy_modifier_report",
    "kopos_connector.patches.backfill_maybank_transaction_authenticity",
    "kopos_connector.patches.backfill_privileged_request_fingerprints",
    "kopos_connector.patches.backfill_shift_request_fingerprints",
    "kopos_connector.patches.migrate_branch_scoped_maybank_qr",
)
EXPECTED_ACCEPTANCE_MODULES = (
    "kopos_connector.acceptance.maybank_uat_accounting",
    "kopos_connector.acceptance.maybank_uat_business_state",
    "kopos_connector.acceptance.maybank_uat_common",
    "kopos_connector.acceptance.maybank_uat_transport",
)


def _connector_root() -> Path:
    spec = importlib.util.find_spec("kopos_connector")
    assert spec is not None
    assert spec.submodule_search_locations is not None
    package_locations = tuple(spec.submodule_search_locations)
    assert len(package_locations) == 1
    return Path(package_locations[0]).resolve()


def _patch_entries(manifest: Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_frappe_app_path_contains_the_canonical_patch_manifest() -> None:
    connector_root = _connector_root()
    manifest = connector_root / "patches.txt"

    assert manifest.is_file()
    assert _patch_entries(manifest) == EXPECTED_PATCHES
    assert not (connector_root.parent / "patches.txt").exists()


def test_patch_manifest_is_ordered_unique_and_importable() -> None:
    patch_entries = _patch_entries(_connector_root() / "patches.txt")

    assert patch_entries == EXPECTED_PATCHES
    assert len(patch_entries) == len(set(patch_entries))
    for patch_path in patch_entries:
        patch_module = importlib.import_module(patch_path)
        assert callable(getattr(patch_module, "execute", None))


def test_distribution_configuration_includes_the_canonical_manifest() -> None:
    connector_root = _connector_root()
    source_root = connector_root.parent
    manifest_template = (source_root / "MANIFEST.in").read_text(encoding="utf-8")
    setup_source = (source_root / "setup.py").read_text(encoding="utf-8")

    assert "include kopos_connector/patches.txt" in manifest_template.splitlines()
    assert 'package_data={"kopos_connector": ["patches.txt"]}' in setup_source


def test_acceptance_evidence_producers_are_importable_source_modules() -> None:
    connector_root = _connector_root()
    for module_name in EXPECTED_ACCEPTANCE_MODULES:
        module = importlib.import_module(module_name)
        module_path = Path(module.__file__).resolve()
        assert module_path.is_file()
        assert connector_root in module_path.parents
    assert callable(
        getattr(
            importlib.import_module(EXPECTED_ACCEPTANCE_MODULES[1]),
            "export_v1",
            None,
        )
    )
    assert callable(
        getattr(
            importlib.import_module(EXPECTED_ACCEPTANCE_MODULES[3]),
            "export_v1",
            None,
        )
    )


def test_built_wheel_matches_the_exact_connector_source_inventory() -> None:
    wheel_value = os.environ.get("KOPOS_CONNECTOR_WHEEL", "").strip()
    if not wheel_value:
        pytest.skip("KOPOS_CONNECTOR_WHEEL is required by the package acceptance gate")

    wheel_path = Path(wheel_value).resolve(strict=True)
    connector_root = _connector_root()
    source_inventory = {
        path.relative_to(connector_root.parent).as_posix(): path.read_bytes()
        for path in connector_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }

    with ZipFile(wheel_path) as wheel:
        member_names = wheel.namelist()
        assert len(member_names) == len(set(member_names))
        wheel_inventory = {
            member_name: wheel.read(member_name)
            for member_name in member_names
            if member_name.startswith("kopos_connector/")
            and not member_name.endswith("/")
        }

    assert wheel_inventory == source_inventory
    assert wheel_inventory["kopos_connector/patches.txt"] == (
        connector_root / "patches.txt"
    ).read_bytes()
    for module_name in EXPECTED_ACCEPTANCE_MODULES:
        member_name = f"{module_name.replace('.', '/')}.py"
        assert member_name in wheel_inventory
    assert "patches.txt" not in member_names
