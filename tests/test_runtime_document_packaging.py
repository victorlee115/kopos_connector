from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from zipfile import ZipFile

import pytest


AUTHORING_DOCUMENT = "kopos_connector/docs/FB_MODIFIER_AUTHORING.md"
PROVISIONING_PAGE = "kopos_connector/kopos/page/kopos_provisioning/kopos_provisioning"


def _connector_root() -> Path:
    spec = importlib.util.find_spec("kopos_connector")
    assert spec is not None
    assert spec.submodule_search_locations is not None
    package_locations = tuple(spec.submodule_search_locations)
    assert len(package_locations) == 1
    return Path(package_locations[0]).resolve()


def test_modifier_authoring_document_is_a_runtime_package_asset() -> None:
    document = _connector_root() / "docs" / "FB_MODIFIER_AUTHORING.md"

    assert document.is_file()
    content = document.read_text(encoding="utf-8")
    assert "Temperature" in content
    assert "Ice Level" in content
    assert "Iced" in content


def test_manifest_includes_runtime_markdown_assets() -> None:
    manifest = _connector_root().parent / "MANIFEST.in"

    assert "recursive-include kopos_connector *.md" in manifest.read_text(
        encoding="utf-8"
    ).splitlines()


def test_standard_provisioning_page_uses_the_native_module_directory() -> None:
    document = _connector_root().parent / (PROVISIONING_PAGE + ".json")
    values = json.loads(document.read_text(encoding="utf-8"))
    assert values["doctype"] == "Page"
    assert values["standard"] == "Yes"
    assert values["module"] == "KoPOS"
    assert values["name"] == values["page_name"] == "kopos_provisioning"
    expected = _connector_root() / values["module"].lower() / "page" / values["name"]
    assert document.parent == expected
    assert document.with_suffix(".js").is_file()
    assert not (_connector_root() / "page" / values["name"] / document.name).exists()


@pytest.mark.parametrize("asset", [AUTHORING_DOCUMENT, "kopos_connector/contracts/promotion.v1.schema.json",
    PROVISIONING_PAGE + ".json", PROVISIONING_PAGE + ".js",
    "kopos_connector/workspace_sidebar/kopos_support.json", "kopos_connector/desktop_icon/kopos_support.json",
    "kopos_connector/contracts/promotion-snapshot.v2.schema.json",
    "kopos_connector/contracts/shift.v1.schema.json", "kopos_connector/contracts/receipt-header.v1.schema.json",
    "kopos_connector/pricing/runtime.js", "kopos_connector/pricing/runtime.json", "kopos_connector/pricing/worker.py",
    "kopos_connector/pricing/response.schema.json", "kopos_connector/services/maybank/crypto.py",
    "kopos_connector/services/maybank/client.py",
    "kopos_connector/patches/migrate_branch_scoped_maybank_qr.py",
    "kopos_connector/kopos/doctype/maybank_qrpaybiz_account/maybank_qrpaybiz_account.py",
    "kopos_connector/kopos/page/jiji_promotions/jiji_promotions.js", "kopos_connector/public/js/pos_profile_setup.js",
    "kopos_connector/kopos/doctype/kopos_promotion_publication/kopos_promotion_publication.json"])
def test_built_wheel_contains_exact_runtime_asset(asset: str) -> None:
    wheel_value = os.environ.get("KOPOS_CONNECTOR_WHEEL", "").strip()
    if not wheel_value:
        pytest.skip("KOPOS_CONNECTOR_WHEEL is required by the package acceptance gate")

    wheel_path = Path(wheel_value).resolve(strict=True)
    source_document = _connector_root().parent / asset

    with ZipFile(wheel_path) as wheel:
        member_names = wheel.namelist()
        assert member_names.count(asset) == 1
        packaged_document = wheel.read(asset)
        if asset.startswith(PROVISIONING_PAGE):
            assert not any(name.startswith("kopos_connector/page/kopos_provisioning/") for name in member_names)

        assert packaged_document == source_document.read_bytes()


def test_built_wheel_excludes_removed_service_scaffolding() -> None:
    wheel_value = os.environ.get("KOPOS_CONNECTOR_WHEEL", "").strip()
    if not wheel_value:
        pytest.skip("KOPOS_CONNECTOR_WHEEL is required by the package acceptance gate")

    removed = [
        "recipe/snapshot.py", "recipe/defaults.py", "recipe/substitutions.py",
        "orders/projector.py", "orders/idempotency.py", "orders/ingest.py",
        "orders/validator.py", "projection/reconciliation_service.py",
        "operations/shift_service.py", "accounting/payment_mapping.py",
        "inventory/reconciliation_service.py",
    ]
    with ZipFile(Path(wheel_value).resolve(strict=True)) as wheel:
        for relative in removed:
            member = "kopos_connector/kopos/services/" + relative
            assert not (_connector_root().parent / member).exists()
            assert member not in wheel.namelist()
