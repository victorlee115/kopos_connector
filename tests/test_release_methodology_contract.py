from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_versioned_agent_rules_preserve_real_service_methodology() -> None:
    rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    for required in (
        "Production readiness belongs to an exact connector commit",
        "Real Frappe integration",
        "restored-production-data rehearsal",
        "Build once",
        "complete catalog",
        "running process set",
        "Current inventory exclusion",
    ):
        assert required in rules


def test_normal_connector_ci_cannot_claim_production_acceptance() -> None:
    workflow = (ROOT / ".github/workflows/connector-ci.yml").read_text(
        encoding="utf-8"
    )

    assert "Run mocked-unit connector contract suite" in workflow
    assert '"evidenceLevel": "mocked_unit"' in workflow
    assert '"testedInput": "source_checkout"' in workflow
    assert '"wheelTested": False' in workflow
    assert '"artifactScope": "packaging_identity_only"' in workflow
    assert '"productionAcceptance": False' in workflow
    assert '"requiredNextGate": "real_frappe_mariadb_redis"' in workflow
    assert 'source_commit = os.environ["KOPOS_SOURCE_COMMIT"]' in workflow
    assert 'wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()' in workflow
    assert '${{ runner.temp }}/kopos-connector-wheel/*.whl' in workflow
    assert "Run complete connector contract suite" not in workflow
    assert "Build exact connector wheel for contract tests" not in workflow


def test_real_frappe_modifier_persistence_contract_is_present() -> None:
    source = (
        ROOT
        / "kopos_connector/kopos/tests/test_catalog_persistence_contract.py"
    ).read_text(encoding="utf-8")

    assert "FrappeTestCase" in source
    assert "row.db_insert()" in source
    assert "override_min_selection" in source
    assert "override_max_selection" in source
    assert "inventory" in source.lower()


def test_methodology_evidence_schema_is_identity_bound_and_honest() -> None:
    workflow = (ROOT / ".github/workflows/connector-ci.yml").read_text(
        encoding="utf-8"
    )

    for required_key in (
        "sourceCommit",
        "wheelFile",
        "wheelSha256",
        "wheelTested",
        "artifactScope",
        "recordedAt",
        "productionAcceptance",
        "requiredNextGate",
    ):
        assert json.dumps(required_key) in workflow


def test_modifier_catalog_incident_has_a_non_inventory_repair_runbook() -> None:
    runbook = (ROOT / "docs/MODIFIER_CATALOG_REPAIR_RUNBOOK.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "AMERICANO_COFFEE_RECIPE",
        "ADDITIONAL_ESPRESSO_SHOT",
        "Create a new recipe version",
        "complete catalog twice for every enabled tablet",
        "Do not edit recipe ingredients",
        "does not make an inventory-readiness claim",
    ):
        assert required in runbook
