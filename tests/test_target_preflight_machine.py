from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
if "erpnext" not in sys.modules:
    erpnext = ModuleType("erpnext")
    erpnext.__version__ = "16.8.2"
    sys.modules["erpnext"] = erpnext
if "Crypto.Cipher" not in sys.modules:
    crypto = ModuleType("Crypto")
    cipher = ModuleType("Crypto.Cipher")
    cipher.AES = SimpleNamespace(MODE_CBC=2)
    sys.modules["Crypto"] = crypto
    sys.modules["Crypto.Cipher"] = cipher

import frappe  # noqa: E402

from kopos_connector.acceptance import target_preflight_machine as preflight  # noqa: E402


NONCE = "1" * 32
ERP_COMMIT = "2" * 40
ERP_ARTIFACT_SHA256 = "3" * 64
RUNTIME_INVENTORY_SHA256 = "4" * 64
MOBILE_APK_SHA256 = "5" * 64
MOBILE_MANIFEST_SHA256 = "6" * 64
ERP_MANIFEST_SHA256 = "7" * 64
SITE_SHA256 = "8" * 64


def _round_trip() -> dict[str, object]:
    return {
        "passed": True,
        "probeIdSha256": "5" * 64,
        "readBackExact": True,
        "rolledBack": True,
        "residualRows": 0,
    }


def _install_success_helpers(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    reports: list[dict] = []
    monkeypatch.setattr(preflight, "require_acceptance_operator", lambda: None)
    monkeypatch.setattr(preflight, "_producer_closure_sha256", lambda: "9" * 64)
    monkeypatch.setattr(
        preflight,
        "_runtime_inventory_sha256",
        lambda: RUNTIME_INVENTORY_SHA256,
    )
    monkeypatch.setattr(
        preflight,
        "_target_identity",
        lambda **kwargs: {
            "origin": kwargs["expected_origin"],
            "siteIdSha256": kwargs["expected_site_id_sha256"],
            "company": kwargs["company"],
            "currency": kwargs["currency"],
        },
    )
    monkeypatch.setattr(
        preflight,
        "_database_round_trip",
        lambda nonce: {
            "frappeSaveReadRoundTrip": _round_trip(),
            "mariaDbSaveReadRoundTrip": _round_trip(),
        },
    )
    monkeypatch.setattr(
        preflight,
        "_redis_round_trip",
        lambda nonce, site: {
            "passed": True,
            "lockKeySha256": "a" * 64,
            "exclusiveAcquire": True,
            "wrongOwnerReleaseRejected": True,
            "ownerReleaseSucceeded": True,
            "residualKeys": 0,
        },
    )
    monkeypatch.setattr(
        preflight,
        "_static_qr_check",
        lambda company: {
            "passed": True,
            "enabledDeviceCount": 1,
            "devices": [
                {
                    "deviceIdentitySha256": "2" * 64,
                    "posProfileIdentitySha256": "3" * 64,
                }
            ],
            "deviceSetSha256": "b" * 64,
        },
    )
    monkeypatch.setattr(
        preflight,
        "_schema_check",
        lambda: {
            "passed": True,
            "requiredFields": ["FB Order.order_id"],
            "requiredFieldCount": 1,
            "observedDigest": "b" * 64,
            "missing": [],
        },
    )
    monkeypatch.setattr(
        preflight,
        "_index_check",
        lambda: {
            "passed": True,
            "requiredIndexes": [],
            "requiredIndexCount": 0,
            "observedDigest": "c" * 64,
            "missing": [],
        },
    )
    monkeypatch.setattr(
        preflight,
        "_scheduler_check",
        lambda: {
            "passed": True,
            "requiredJobs": list(preflight.REQUIRED_SCHEDULER_JOBS),
            "activeJobs": list(preflight.REQUIRED_SCHEDULER_JOBS),
            "missing": [],
            "obsolete": [],
            "observedDigest": "d" * 64,
        },
    )
    monkeypatch.setattr(
        preflight,
        "_qr_account_check",
        lambda company, currency: {
            "passed": True,
            "account": "KoPOS QR Bank - TEST",
            "accountSha256": "e" * 64,
            "company": company,
            "currency": currency,
            "accountType": "Bank",
            "rootType": "Asset",
            "isGroup": False,
            "disabled": False,
            "cashAccountUsed": False,
        },
    )
    monkeypatch.setattr(
        preflight,
        "_provider_controls_check",
        lambda expected_account_type: {
            "passed": True,
            "mockDisabled": True,
            "simulationDisabled": True,
            "developerModeDisabled": True,
            "testModeDisabled": True,
            "officialProviderOrigin": True,
            "settingsEnabled": True,
            "usernamePresent": True,
            "pinPresent": True,
            "accountType": expected_account_type,
            "providerDeviceNamePresent": True,
            "providerDeviceOsPresent": True,
            "outletIdSha256": "f" * 64,
            "providerDeviceIdSha256": "0" * 64,
            "providerDeviceNameSha256": "1" * 64,
            "providerDeviceOsSha256": "2" * 64,
        },
    )
    monkeypatch.setattr(
        preflight.catalog_preflight,
        "build_enabled_device_catalog_proof_v1",
        lambda: {
            "enabledDeviceCount": 1,
            "referencedPosProfileCount": 1,
            "completeCatalogBuildCount": 2,
            "referencedPosProfileIdentitySha256": ["3" * 64],
            "deviceSetSha256": "4" * 64,
            "devices": [
                {
                    "deviceIdentitySha256": "2" * 64,
                    "posProfileIdentitySha256": "3" * 64,
                }
            ],
            "aggregateSha256": "5" * 64,
        },
    )
    monkeypatch.setattr(preflight, "_runtime_version", lambda *args: "16.8.2")
    monkeypatch.setattr(preflight, "_mariadb_version", lambda: "10.6.22")
    monkeypatch.setattr(preflight, "_redis_version", lambda: "7.4.2")
    monkeypatch.setattr(
        preflight,
        "write_report_atomically",
        lambda report, filename, **kwargs: reports.append(
            json.loads(json.dumps(report))
        ),
    )
    return reports


def test_machine_report_is_target_bound_real_and_non_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = _install_success_helpers(monkeypatch)

    report = preflight.run_v1(
        run_nonce=NONCE,
        erp_commit=ERP_COMMIT,
        erp_artifact_sha256=ERP_ARTIFACT_SHA256,
        expected_runtime_inventory_sha256=RUNTIME_INVENTORY_SHA256,
        candidate_apk_sha256=MOBILE_APK_SHA256,
        mobile_manifest_sha256=MOBILE_MANIFEST_SHA256,
        erp_manifest_sha256=ERP_MANIFEST_SHA256,
        expected_origin="https://erp.example.com",
        expected_site_id_sha256=SITE_SHA256,
        company="Test Company",
        currency="MYR",
        expected_maybank_account_type="corporate",
    )

    assert reports == [report]
    assert report["producerContractId"] == "kopos.target-preflight-machine.v1"
    assert report["runNonce"] == NONCE
    assert report["candidate"] == {
        "erpCommit": ERP_COMMIT,
        "erpArtifactSha256": ERP_ARTIFACT_SHA256,
        "runtimeInventorySha256": RUNTIME_INVENTORY_SHA256,
        "mobileApkSha256": MOBILE_APK_SHA256,
        "mobileManifestSha256": MOBILE_MANIFEST_SHA256,
        "erpManifestSha256": ERP_MANIFEST_SHA256,
    }
    assert report["execution"] == {
        "frappe": "real",
        "mariaDb": "real",
        "redis": "real",
        "providerNetworkCalls": 0,
        "inventoryMutations": 0,
        "committedDbMutations": 0,
    }
    assert report["checks"]["catalog"]["enabledDeviceCount"] == 1
    assert report["checks"]["catalog"]["completeCatalogBuildCount"] == 2
    assert report["checks"]["staticQrCommissioning"]["passed"] is True
    assert report["checks"]["qrAccount"]["cashAccountUsed"] is False
    assert report["checks"]["providerControls"]["mockDisabled"] is True
    assert report["status"] == "passed"
    serialized = json.dumps(report, sort_keys=True)
    assert "private-user" not in serialized
    assert "private-pin" not in serialized


def test_qr_account_probe_requires_an_explicit_bank_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight.payment_service,
        "resolve_verified_qr_settlement_account",
        lambda *args: {"account": "Cash - TEST", "type": "Bank"},
    )
    monkeypatch.setattr(
        preflight.frappe.db,
        "get_value",
        lambda *args, **kwargs: SimpleNamespace(
            name="Cash - TEST",
            company="Test Company",
            account_currency="MYR",
            account_type="Cash",
            root_type="Asset",
            is_group=0,
            disabled=0,
        ),
    )

    with pytest.raises(frappe.ValidationError, match="enabled non-group Bank Asset"):
        preflight._qr_account_check("Test Company", "MYR")


def test_static_qr_probe_requires_exact_commissioning_for_every_enabled_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        "00020201021126410014A000000615000101065016640209123456789"
        "5204999953034585802MY5909QRCSDNBHD6005BANGI6304184A"
    )
    payload_sha256 = preflight.hashlib.sha256(payload.encode("ascii")).hexdigest()
    row = {
        "name": "DEVICE-1",
        "device_id": "tablet-private-1",
        "pos_profile": "Counter 1",
    }
    device = SimpleNamespace(
        static_qr_payload=payload,
        static_qr_payload_sha256=payload_sha256,
        static_qr_merchant_id="123456789",
        static_qr_acquirer_id="501664",
        static_qr_merchant_name="QRCSDNBHD",
        static_qr_version="02",
        static_qr_company="Test Company",
        static_qr_commissioned_at="2026-08-10 12:00:00",
    )
    monkeypatch.setattr(
        preflight.frappe,
        "get_all",
        lambda *args, **kwargs: [dict(row)],
    )
    monkeypatch.setattr(
        preflight.frappe,
        "get_doc",
        lambda doctype, name: device,
    )

    proof = preflight._static_qr_check("Test Company")

    assert proof["passed"] is True
    assert proof["enabledDeviceCount"] == 1
    assert payload not in json.dumps(proof)

    device.static_qr_merchant_id = "another-merchant"
    with pytest.raises(frappe.ValidationError, match="merchant ID does not match"):
        preflight._static_qr_check("Test Company")


def test_target_preflight_rejects_configuration_changes_between_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_success_helpers(monkeypatch)
    checks = [
        {
            "passed": True,
            "enabledDeviceCount": 1,
            "devices": [
                {
                    "deviceIdentitySha256": "2" * 64,
                    "posProfileIdentitySha256": "3" * 64,
                }
            ],
            "deviceSetSha256": "b" * 64,
        },
        {
            "passed": True,
            "enabledDeviceCount": 1,
            "devices": [
                {
                    "deviceIdentitySha256": "6" * 64,
                    "posProfileIdentitySha256": "3" * 64,
                }
            ],
            "deviceSetSha256": "7" * 64,
        },
    ]
    monkeypatch.setattr(preflight, "_static_qr_check", lambda company: checks.pop(0))

    with pytest.raises(
        frappe.ValidationError,
        match="configuration changed during preflight",
    ):
        preflight.run_v1(
            run_nonce=NONCE,
            erp_commit=ERP_COMMIT,
            erp_artifact_sha256=ERP_ARTIFACT_SHA256,
            expected_runtime_inventory_sha256=RUNTIME_INVENTORY_SHA256,
            candidate_apk_sha256=MOBILE_APK_SHA256,
            mobile_manifest_sha256=MOBILE_MANIFEST_SHA256,
            erp_manifest_sha256=ERP_MANIFEST_SHA256,
            expected_origin="https://erp.example.test",
            expected_site_id_sha256=SITE_SHA256,
            company="Test Company",
            currency="MYR",
            expected_maybank_account_type="corporate",
        )


def test_schema_probe_requires_reviewed_field_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field = SimpleNamespace(
        fieldtype="Data",
        reqd=1,
        unique=1,
        search_index=1,
        options=None,
        default=None,
    )
    meta = SimpleNamespace(get_field=lambda fieldname: field)
    monkeypatch.setattr(
        preflight,
        "REQUIRED_FIELD_SPECS",
        {"Example": {"identity": ("Data", True, True, True)}},
    )
    monkeypatch.setattr(preflight.preflight_contract, "REQUIRED_FIELD_OPTIONS", {})
    monkeypatch.setattr(preflight.preflight_contract, "REQUIRED_FIELD_DEFAULTS", {})
    monkeypatch.setattr(preflight.frappe.db, "exists", lambda *args: True)
    monkeypatch.setattr(
        preflight.frappe.db,
        "get_table_columns",
        lambda doctype: ["identity"],
        raising=False,
    )
    monkeypatch.setattr(
        preflight.frappe,
        "get_meta",
        lambda doctype: meta,
        raising=False,
    )

    proof = preflight._schema_check()
    assert proof["requiredMetadata"][0]["unique"] is True

    field.fieldtype = "Link"
    with pytest.raises(frappe.ValidationError, match="reviewed schema"):
        preflight._schema_check()

    field.options = None
    field.default = "unsafe"
    monkeypatch.setattr(preflight.preflight_contract, "REQUIRED_FIELD_OPTIONS", {})
    monkeypatch.setattr(
        preflight.preflight_contract,
        "REQUIRED_FIELD_DEFAULTS",
        {"Example": {"identity": "safe"}},
    )
    with pytest.raises(frappe.ValidationError, match="reviewed schema"):
        preflight._schema_check()

    field.fieldtype = "Data"
    field.options = "Wrong DocType"
    monkeypatch.setattr(
        preflight.preflight_contract,
        "REQUIRED_FIELD_OPTIONS",
        {"Example": {"identity": "Expected DocType"}},
    )
    with pytest.raises(frappe.ValidationError, match="reviewed schema"):
        preflight._schema_check()


def test_index_probe_requires_ordered_nonunique_btree_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "column_name": "device_id",
            "non_unique": 1,
            "index_type": "BTREE",
            "sub_part": None,
        },
        {
            "column_name": "status",
            "non_unique": 1,
            "index_type": "BTREE",
            "sub_part": None,
        },
    ]
    monkeypatch.setattr(
        preflight.connector_install,
        "OPERATIONAL_INDEX_SPECS",
        (("Example", ["device_id", "status"], "idx_example"),),
    )
    monkeypatch.setattr(preflight.frappe.db, "sql", lambda *args, **kwargs: rows)

    proof = preflight._index_check()
    assert proof["requiredIndexes"][0]["indexType"] == "BTREE"
    assert proof["requiredIndexes"][0]["nonUnique"] is True
    assert proof["requiredIndexes"][0]["columnPrefixLengths"] == [None, None]

    rows[1]["index_type"] = "HASH"
    with pytest.raises(frappe.ValidationError, match="ordered connector indexes"):
        preflight._index_check()

    rows[1]["index_type"] = "BTREE"
    rows[1]["sub_part"] = 12
    with pytest.raises(frappe.ValidationError, match="ordered connector indexes"):
        preflight._index_check()


def test_scheduler_probe_requires_one_active_all_frequency_row_per_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_rows = [
        {"method": method, "stopped": 0, "frequency": "All"}
        for method in preflight.REQUIRED_SCHEDULER_JOBS
    ]

    def get_all(doctype, *, filters, **kwargs):
        requested = set(filters["method"][1])
        if requested == set(preflight.OBSOLETE_SCHEDULER_JOBS):
            return []
        return list(required_rows)

    monkeypatch.setattr(preflight.frappe, "get_all", get_all)
    monkeypatch.setattr(preflight, "_config_value", lambda *args: 0)

    proof = preflight._scheduler_check()
    assert len(proof["requiredFrequencies"]) == len(
        preflight.REQUIRED_SCHEDULER_JOBS
    )

    required_rows.append(dict(required_rows[0]))
    with pytest.raises(frappe.ValidationError, match="duplicated"):
        preflight._scheduler_check()


def test_provider_probe_rejects_any_mock_or_simulation_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight,
        "_config_value",
        lambda fieldname, default=None: 1 if fieldname == "allow_maybank_mock" else 0,
    )
    monkeypatch.setattr(preflight.frappe, "in_test", 0, raising=False)
    monkeypatch.setattr(
        preflight.frappe,
        "flags",
        SimpleNamespace(in_test=0),
        raising=False,
    )
    monkeypatch.setattr(
        preflight.frappe,
        "get_single",
        lambda doctype: _production_maybank_settings(),
        raising=False,
    )

    with pytest.raises(frappe.ValidationError, match="production-only"):
        preflight._provider_controls_check("corporate")


def _production_maybank_settings(
    *,
    pin: str = "private-pin",
    account_type: str = "corporate",
) -> SimpleNamespace:
    settings = SimpleNamespace(
        enabled=1,
        username="private-user",
        user_type=account_type,
        outlet_id="outlet-private",
        provider_device_id="a" * 32,
        provider_device_name="Samsung Galaxy Tab A11 Small",
        provider_device_os="Android",
        base_url=preflight.maybank_client.DEFAULT_BASE_URL,
    )
    settings.get_password = lambda fieldname: pin
    return settings


def test_provider_probe_requires_credentials_account_type_and_device_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.frappe, "in_test", 0, raising=False)
    monkeypatch.setattr(
        preflight.frappe,
        "flags",
        SimpleNamespace(in_test=0),
        raising=False,
    )
    monkeypatch.setattr(preflight, "_config_value", lambda *args: 0)
    monkeypatch.setattr(
        preflight.frappe,
        "get_single",
        lambda doctype: _production_maybank_settings(),
        raising=False,
    )

    proof = preflight._provider_controls_check("corporate")

    assert proof["usernamePresent"] is True
    assert proof["pinPresent"] is True
    assert proof["accountType"] == "corporate"
    assert proof["providerDeviceNamePresent"] is True
    assert proof["providerDeviceOsPresent"] is True
    assert "private-user" not in json.dumps(proof)
    assert "private-pin" not in json.dumps(proof)

    monkeypatch.setattr(
        preflight.frappe,
        "get_single",
        lambda doctype: _production_maybank_settings(pin=""),
    )
    with pytest.raises(frappe.ValidationError, match="production-only"):
        preflight._provider_controls_check("corporate")

    monkeypatch.setattr(
        preflight.frappe,
        "get_single",
        lambda doctype: _production_maybank_settings(account_type="merchant"),
    )
    with pytest.raises(frappe.ValidationError, match="production-only"):
        preflight._provider_controls_check("corporate")


def test_machine_report_rejects_another_installed_runtime_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = _install_success_helpers(monkeypatch)
    monkeypatch.setattr(preflight, "_runtime_inventory_sha256", lambda: "f" * 64)

    with pytest.raises(frappe.ValidationError, match="accepted ERP package"):
        preflight.run_v1(
            run_nonce=NONCE,
            erp_commit=ERP_COMMIT,
            erp_artifact_sha256=ERP_ARTIFACT_SHA256,
            expected_runtime_inventory_sha256=RUNTIME_INVENTORY_SHA256,
            candidate_apk_sha256=MOBILE_APK_SHA256,
            mobile_manifest_sha256=MOBILE_MANIFEST_SHA256,
            erp_manifest_sha256=ERP_MANIFEST_SHA256,
            expected_origin="https://erp.example.com",
            expected_site_id_sha256=SITE_SHA256,
            company="Test Company",
            currency="MYR",
            expected_maybank_account_type="corporate",
        )

    assert reports == []


def test_machine_report_filename_is_bound_to_the_campaign_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = _install_success_helpers(monkeypatch)

    with pytest.raises(frappe.ValidationError, match="campaign nonce"):
        preflight.run_v1(
            run_nonce=NONCE,
            erp_commit=ERP_COMMIT,
            erp_artifact_sha256=ERP_ARTIFACT_SHA256,
            expected_runtime_inventory_sha256=RUNTIME_INVENTORY_SHA256,
            candidate_apk_sha256=MOBILE_APK_SHA256,
            mobile_manifest_sha256=MOBILE_MANIFEST_SHA256,
            erp_manifest_sha256=ERP_MANIFEST_SHA256,
            expected_origin="https://erp.example.com",
            expected_site_id_sha256=SITE_SHA256,
            company="Test Company",
            currency="MYR",
            expected_maybank_account_type="corporate",
            output_filename="another-report.json",
        )

    assert reports == []


def test_runtime_inventory_uses_the_release_artifact_canonical_contract(
    tmp_path,
) -> None:
    package_root = tmp_path / "kopos_connector"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("version = '1'\n", encoding="utf-8")
    services = package_root / "services"
    services.mkdir()
    (services / "worker.py").write_bytes(b"result = 1\n")
    cache = services / "__pycache__"
    cache.mkdir()
    (cache / "worker.pyc").write_bytes(b"ignored")
    rows = []
    for path in (package_root / "__init__.py", services / "worker.py"):
        contents = path.read_bytes()
        rows.append(
            {
                "bytes": len(contents),
                "path": path.relative_to(tmp_path).as_posix(),
                "sha256": preflight.hashlib.sha256(contents).hexdigest(),
            }
        )

    encoded = json.dumps(
        sorted(rows, key=lambda row: row["path"]),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert preflight._runtime_inventory_sha256(package_root) == (
        preflight.hashlib.sha256(encoded).hexdigest()
    )


def test_producer_closure_hash_binds_every_first_party_dependency() -> None:
    digest = preflight._producer_closure_sha256()

    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)
    module_names = {module.__name__ for module in preflight.PRODUCER_CLOSURE_MODULES}
    assert module_names == {
        "kopos_connector.acceptance.maybank_uat_common",
        "kopos_connector.acceptance.restored_catalog_preflight",
        "kopos_connector.acceptance.target_preflight_contract",
        "kopos_connector.acceptance.target_preflight_static_qr",
        "kopos_connector.api.catalog",
        "kopos_connector.hooks",
        "kopos_connector.install.install",
        "kopos_connector.kopos.services.accounting.maybank_payment_service",
        "kopos_connector.kopos.services.recipe.modifier_bounds",
        "kopos_connector.services.maybank.client",
        "kopos_connector.services.static_qr_commissioning",
    }
