from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from zipfile import ZipFile

import pytest


AUTHORING_DOCUMENT = "kopos_connector/docs/FB_MODIFIER_AUTHORING.md"


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


def test_built_wheel_contains_exact_modifier_authoring_document() -> None:
    wheel_value = os.environ.get("KOPOS_CONNECTOR_WHEEL", "").strip()
    if not wheel_value:
        pytest.skip("KOPOS_CONNECTOR_WHEEL is required by the package acceptance gate")

    wheel_path = Path(wheel_value).resolve(strict=True)
    source_document = _connector_root().parent / AUTHORING_DOCUMENT

    with ZipFile(wheel_path) as wheel:
        member_names = wheel.namelist()
        assert member_names.count(AUTHORING_DOCUMENT) == 1
        packaged_document = wheel.read(AUTHORING_DOCUMENT)

    assert packaged_document == source_document.read_bytes()
