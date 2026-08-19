# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

"""Machine-verifiable preflight executed inside the real target ERP site."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import erpnext
import frappe
from frappe.utils import cint, cstr

import kopos_connector.acceptance.maybank_uat_common as maybank_uat_common
import kopos_connector.acceptance.restored_catalog_preflight as catalog_preflight
import kopos_connector.acceptance.target_preflight_contract as preflight_contract
import kopos_connector.acceptance.target_preflight_static_qr as static_qr_preflight
import kopos_connector.api.catalog as catalog_api
import kopos_connector.hooks as connector_hooks
import kopos_connector.install.install as connector_install
import kopos_connector.kopos.services.accounting.maybank_payment_service as payment_service
import kopos_connector.services.maybank.client as maybank_client
import kopos_connector.services.static_qr_commissioning as static_qr_commissioning
from kopos_connector.acceptance.maybank_uat_common import (
    canonical_json_sha256,
    require_acceptance_operator,
    write_report_atomically,
)


PRODUCER_CONTRACT_ID = "kopos.target-preflight-machine.v1"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")
NONCE_PATTERN = re.compile(r"^[a-f0-9]{32,128}$")
MAYBANK_ACCOUNT_TYPES = frozenset({"merchant", "cashier", "corporate"})
REQUIRED_TARGET_TIME_ZONE = "Asia/Kuala_Lumpur"
IGNORED_RUNTIME_PARTS = frozenset({"__pycache__", ".pytest_cache"})
IGNORED_RUNTIME_SUFFIXES = frozenset({".pyc", ".pyo"})
MAX_RUNTIME_FILE_BYTES = 32 * 1024 * 1024
REDIS_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

REQUIRED_FIELD_SPECS = preflight_contract.REQUIRED_FIELD_SPECS
REQUIRED_SCHEDULER_FREQUENCIES = (
    preflight_contract.REQUIRED_SCHEDULER_FREQUENCIES
)
REQUIRED_CRON_SCHEDULER_FREQUENCIES = (
    preflight_contract.REQUIRED_CRON_SCHEDULER_FREQUENCIES
)
REQUIRED_SCHEDULER_JOBS = tuple(
    (*REQUIRED_SCHEDULER_FREQUENCIES, *REQUIRED_CRON_SCHEDULER_FREQUENCIES)
)
OBSOLETE_SCHEDULER_JOBS = preflight_contract.OBSOLETE_SCHEDULER_JOBS

PRODUCER_CLOSURE_MODULES: tuple[ModuleType, ...] = (
    maybank_uat_common,
    catalog_preflight,
    preflight_contract,
    static_qr_preflight,
    catalog_api,
    connector_hooks,
    connector_install,
    payment_service,
    maybank_client,
    static_qr_commissioning,
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


def _require_sha256(value: Any, fieldname: str) -> str:
    resolved = _text(value, fieldname)
    if not SHA256_PATTERN.fullmatch(resolved):
        _fail(f"{fieldname} must be a lowercase SHA-256")
    return resolved


def _require_commit(value: Any) -> str:
    resolved = _text(value, "erp_commit")
    if not COMMIT_PATTERN.fullmatch(resolved):
        _fail("erp_commit must be a lowercase 40-character commit SHA")
    return resolved


def _require_nonce(value: Any) -> str:
    resolved = _text(value, "run_nonce")
    if not NONCE_PATTERN.fullmatch(resolved):
        _fail("run_nonce must be 32-128 lowercase hexadecimal characters")
    return resolved


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical_origin(value: Any, fieldname: str) -> str:
    resolved = _text(value, fieldname)
    parsed = urlsplit(resolved)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        _fail(f"{fieldname} must be a credential-free HTTPS origin")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"https://{parsed.hostname.lower()}{port}"


def _config_value(fieldname: str, default: Any = None) -> Any:
    config = getattr(frappe, "conf", None)
    if isinstance(config, Mapping):
        return config.get(fieldname, default)
    return getattr(config, fieldname, default) if config is not None else default


def _producer_closure_sha256() -> str:
    modules = [(__name__, Path(__file__))]
    modules.extend(
        (module.__name__, Path(_text(module.__file__, module.__name__)))
        for module in PRODUCER_CLOSURE_MODULES
    )
    entries = []
    for module_name, module_path in sorted(modules, key=lambda entry: entry[0]):
        resolved = module_path.resolve()
        entries.append(
            {
                "module": module_name,
                "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
            }
        )
    return _canonical_ascii_sha256(entries)


def _runtime_inventory_sha256(package_root: Path | None = None) -> str:
    """Hash the installed package using the release artifact inventory contract."""

    unresolved_root = (
        package_root.absolute()
        if package_root is not None
        else Path(__file__).absolute().parents[1]
    )
    if unresolved_root.is_symlink():
        _fail("Installed KoPOS connector package root is invalid")
    root = unresolved_root.resolve()
    if root.name != "kopos_connector" or not root.is_dir():
        _fail("Installed KoPOS connector package root is invalid")

    rows: list[dict[str, Any]] = []
    source_root = root.parent
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(source_root)
        if any(part in IGNORED_RUNTIME_PARTS for part in relative.parts):
            continue
        if candidate.suffix in IGNORED_RUNTIME_SUFFIXES:
            continue
        if candidate.is_symlink():
            _fail("Installed KoPOS connector package contains a symlink")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            _fail("Installed KoPOS connector package contains a special file")
        contents = candidate.read_bytes()
        if len(contents) > MAX_RUNTIME_FILE_BYTES:
            _fail("Installed KoPOS connector package contains an oversized file")
        rows.append(
            {
                "bytes": len(contents),
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(contents).hexdigest(),
            }
        )
    if not rows:
        _fail("Installed KoPOS connector package inventory is empty")
    canonical_rows = sorted(rows, key=lambda row: str(row["path"]))
    return _canonical_ascii_sha256(canonical_rows)


def _runtime_version(module: ModuleType, distribution: str, fieldname: str) -> str:
    version = cstr(getattr(module, "__version__", None)).strip()
    if not version:
        try:
            version = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            version = ""
    if not version:
        _fail(f"{fieldname} version is unavailable")
    return version


def _target_identity(
    *,
    expected_origin: str,
    expected_site_id_sha256: str,
    company: str,
    currency: str,
) -> dict[str, str]:
    actual_origin = _canonical_origin(_config_value("host_name"), "site host_name")
    if actual_origin != expected_origin:
        _fail("Target ERP origin does not match the protected identity")

    site = _text(getattr(frappe.local, "site", None), "target site")
    if _sha256(site) != expected_site_id_sha256:
        _fail("Target ERP site identity does not match the protected digest")

    company_row = frappe.db.get_value(
        "Company",
        company,
        ["name", "default_currency"],
        as_dict=True,
    )
    if not company_row:
        _fail("Protected target company does not exist")
    if cstr(_value(company_row, "default_currency")).strip().upper() != currency:
        _fail("Target company currency does not match the protected currency")

    device_profiles = frappe.get_all(
        "KoPOS Device",
        filters={"enabled": 1},
        fields=["pos_profile"],
        order_by="pos_profile asc",
        limit_page_length=0,
    )
    if not device_profiles:
        _fail("Target ERP has no enabled KoPOS Device")
    for row in device_profiles:
        profile = _text(_value(row, "pos_profile"), "enabled device POS Profile")
        profile_row = frappe.db.get_value(
            "POS Profile",
            profile,
            ["company", "currency", "selling_price_list"],
            as_dict=True,
        )
        if not profile_row:
            _fail("An enabled tablet POS Profile does not exist")
        profile_company = cstr(_value(profile_row, "company")).strip()
        if profile_company != company:
            _fail("An enabled tablet POS Profile uses another company")
        if cstr(_value(profile_row, "currency")).strip().upper() != currency:
            _fail("An enabled tablet POS Profile uses another currency")

        price_list = _text(
            _value(profile_row, "selling_price_list"),
            "enabled device POS Profile selling Price List",
        )
        price_list_row = frappe.db.get_value(
            "Price List",
            price_list,
            ["enabled", "selling", "currency"],
            as_dict=True,
        )
        if not price_list_row:
            _fail("An enabled tablet POS Profile selling Price List does not exist")
        if not cint(_value(price_list_row, "enabled")) or not cint(
            _value(price_list_row, "selling")
        ):
            _fail("An enabled tablet POS Profile requires an enabled selling Price List")
        if cstr(_value(price_list_row, "currency")).strip().upper() != currency:
            _fail("An enabled tablet POS Profile selling Price List uses another currency")

    return {
        "origin": actual_origin,
        "siteIdSha256": expected_site_id_sha256,
        "company": company,
        "currency": currency,
    }


def _target_time_zone(expected_time_zone: str) -> str:
    protected_time_zone = _text(expected_time_zone, "expected_time_zone")
    if protected_time_zone != REQUIRED_TARGET_TIME_ZONE:
        _fail(
            "expected_time_zone must be the protected Asia/Kuala_Lumpur value"
        )

    actual_time_zone = _text(
        frappe.db.get_single_value("System Settings", "time_zone"),
        "System Settings time_zone",
    )
    if actual_time_zone != protected_time_zone:
        _fail("Target ERP timezone does not match the protected timezone")
    return actual_time_zone


def _database_round_trip(run_nonce: str) -> dict[str, dict[str, Any]]:
    probe = uuid4().hex
    probe_sha256 = _sha256(f"{run_nonce}:{probe}")
    description = f"KoPOS target preflight {probe_sha256[:20]}"
    savepoint = f"kopos_target_preflight_{uuid4().hex}"
    document_name = ""
    frappe.db.savepoint(savepoint)
    try:
        document = frappe.get_doc(
            {
                "doctype": "ToDo",
                "description": description,
                "status": "Open",
            }
        )
        document.insert(ignore_permissions=True)
        document_name = _text(document.name, "preflight ToDo name")
        frappe.clear_document_cache("ToDo", document_name)
        persisted = frappe.get_doc("ToDo", document_name)
        if cstr(persisted.description) != description:
            _fail("Frappe save/read round trip changed the probe value")
        rows = frappe.db.sql(
            "SELECT description FROM `tabToDo` WHERE name = %s",
            (document_name,),
            as_list=True,
        )
        if len(rows) != 1 or cstr(rows[0][0]) != description:
            _fail("MariaDB save/read round trip changed the probe value")
    finally:
        frappe.db.rollback(save_point=savepoint)

    if document_name:
        frappe.clear_document_cache("ToDo", document_name)
        if frappe.db.exists("ToDo", document_name):
            _fail("Target preflight database probe was not rolled back")

    proof = {
        "passed": True,
        "probeIdSha256": probe_sha256,
        "readBackExact": True,
        "rolledBack": True,
        "residualRows": 0,
    }
    return {
        "frappeSaveReadRoundTrip": dict(proof),
        "mariaDbSaveReadRoundTrip": dict(proof),
    }


_static_qr_check = static_qr_preflight.build_static_qr_proof
_require_stable_enabled_device_configuration = (
    static_qr_preflight.require_stable_enabled_device_configuration
)


def _redis_client(cache: Any) -> Any:
    client = getattr(cache, "redis_client", None)
    if callable(client):
        client = client()
    resolved = client if client is not None else cache
    if not all(hasattr(resolved, method) for method in ("set", "get", "eval")):
        _fail("Target Redis client does not support atomic lock operations")
    return resolved


def _redis_round_trip(run_nonce: str, site_id_sha256: str) -> dict[str, Any]:
    client = _redis_client(frappe.cache())
    token = uuid4().hex + uuid4().hex
    wrong_token = uuid4().hex + uuid4().hex
    key = f"kopos:target-preflight:{site_id_sha256}:{_sha256(run_nonce)}"
    acquired = False
    try:
        acquired = bool(client.set(key, token, ex=60, nx=True))
        if not acquired:
            _fail("Target Redis atomic lock could not be acquired")
        if client.set(key, wrong_token, ex=60, nx=True):
            _fail("Target Redis atomic lock allowed a second owner")
        stored = client.get(key)
        if isinstance(stored, bytes):
            stored = stored.decode("utf-8")
        if stored != token:
            _fail("Target Redis atomic lock returned another owner")
        if cint(client.eval(REDIS_RELEASE_SCRIPT, 1, key, wrong_token)) != 0:
            _fail("Target Redis lock allowed a non-owner release")
        if cint(client.eval(REDIS_RELEASE_SCRIPT, 1, key, token)) != 1:
            _fail("Target Redis lock owner could not release its key")
        acquired = False
        if client.get(key) is not None:
            _fail("Target Redis lock key remained after owner release")
    finally:
        if acquired:
            client.eval(REDIS_RELEASE_SCRIPT, 1, key, token)

    return {
        "passed": True,
        "lockKeySha256": _sha256(key),
        "exclusiveAcquire": True,
        "wrongOwnerReleaseRejected": True,
        "ownerReleaseSucceeded": True,
        "residualKeys": 0,
    }


def _schema_check() -> dict[str, Any]:
    required = sorted(
        f"{doctype}.{fieldname}"
        for doctype, field_specs in REQUIRED_FIELD_SPECS.items()
        for fieldname in field_specs
    )
    required_metadata: list[dict[str, Any]] = []
    missing: list[str] = []
    mismatched: list[str] = []
    for doctype, field_specs in REQUIRED_FIELD_SPECS.items():
        if not frappe.db.exists("DocType", doctype):
            missing.extend(f"{doctype}.{fieldname}" for fieldname in field_specs)
            continue
        meta = frappe.get_meta(doctype)
        columns = set(frappe.db.get_table_columns(doctype) or [])
        for fieldname, expected in field_specs.items():
            field = meta.get_field(fieldname)
            if field is None or fieldname not in columns:
                missing.append(f"{doctype}.{fieldname}")
                continue
            expected_type, expected_required, expected_unique, expected_index = (
                expected
            )
            actual = (
                cstr(getattr(field, "fieldtype", None)).strip(),
                bool(cint(getattr(field, "reqd", 0))),
                bool(cint(getattr(field, "unique", 0))),
                bool(cint(getattr(field, "search_index", 0))),
            )
            expected_options = preflight_contract.REQUIRED_FIELD_OPTIONS.get(
                doctype, {}
            ).get(fieldname)
            expected_default = preflight_contract.REQUIRED_FIELD_DEFAULTS.get(
                doctype, {}
            ).get(fieldname)
            actual_options = cstr(getattr(field, "options", None)).replace("\r\n", "\n")
            options_match = expected_options is None or actual_options == expected_options
            actual_default = cstr(getattr(field, "default", None))
            default_matches = expected_default is None or actual_default == expected_default
            if actual != expected or not options_match or not default_matches:
                mismatched.append(f"{doctype}.{fieldname}")
            required_metadata.append(
                {
                    "doctype": doctype,
                    "fieldname": fieldname,
                    "fieldtype": expected_type,
                    "required": expected_required,
                    "unique": expected_unique,
                    "searchIndex": expected_index,
                    "options": expected_options,
                    "default": expected_default,
                }
            )
    if missing or mismatched:
        _fail("Target ERP connector fields or metadata differ from the reviewed schema")
    required_metadata.sort(key=lambda row: (row["doctype"], row["fieldname"]))
    return {
        "passed": True,
        "requiredFields": required,
        "requiredFieldCount": len(required),
        "requiredMetadata": required_metadata,
        "observedDigest": canonical_json_sha256(required_metadata),
        "missing": [],
        "mismatched": [],
    }


def _index_check() -> dict[str, Any]:
    required: list[dict[str, Any]] = []
    missing: list[str] = []
    for doctype, columns, index_name in connector_install.OPERATIONAL_INDEX_SPECS:
        rows = frappe.db.sql(
            """
            SELECT INDEX_NAME AS index_name, COLUMN_NAME AS column_name,
                   SEQ_IN_INDEX AS sequence_number,
                   NON_UNIQUE AS non_unique, INDEX_TYPE AS index_type, SUB_PART AS sub_part
              FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = %s
               AND INDEX_NAME = %s
             ORDER BY SEQ_IN_INDEX ASC
            """,
            (f"tab{doctype}", index_name),
            as_dict=True,
        )
        observed_columns = [
            cstr(_value(row, "column_name"))
            for row in rows or []
        ]
        expected_columns = list(columns)
        metadata_matches = bool(rows) and all(
            cint(_value(row, "non_unique")) == 1
            and cstr(_value(row, "index_type")).strip().upper() == "BTREE"
            and _value(row, "sub_part") is None
            for row in rows
        )
        if observed_columns != expected_columns or not metadata_matches:
            missing.append(index_name)
        required.append(
            {
                "doctype": doctype,
                "indexName": index_name,
                "columns": expected_columns,
                "indexType": "BTREE",
                "nonUnique": True,
                "columnPrefixLengths": [None for _column in expected_columns],
            }
        )
    if missing:
        _fail("Target ERP is missing required ordered connector indexes")
    required.sort(key=lambda row: row["indexName"])
    return {
        "passed": True,
        "requiredIndexes": required,
        "requiredIndexCount": len(required),
        "observedDigest": canonical_json_sha256(required),
        "missing": [],
    }


def _flatten_scheduler_hooks(value: Any) -> set[str]:
    if isinstance(value, str):
        method = value.strip()
        return {method} if method else set()
    if isinstance(value, Mapping):
        methods: set[str] = set()
        for nested in value.values():
            methods.update(_flatten_scheduler_hooks(nested))
        return methods
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        methods = set()
        for nested in value:
            methods.update(_flatten_scheduler_hooks(nested))
        return methods
    return set()


def _scheduler_check(*, allow_paused_scheduler: bool = False) -> dict[str, Any]:
    """Verify the reviewed scheduler topology and its live job rows.

    Production preflight must reject a paused scheduler.  The isolated
    restored-data rehearsal deliberately pauses it so no retained business
    data can trigger work; its integration test may opt into that one
    containment condition while still proving the job records and timings.
    """
    required = sorted(REQUIRED_SCHEDULER_JOBS)
    configured = _flatten_scheduler_hooks(connector_hooks.scheduler_events)
    all_methods = (
        connector_hooks.scheduler_events.get("all")
        if isinstance(connector_hooks.scheduler_events, Mapping)
        else None
    )
    normalized_all = (
        [cstr(method).strip() for method in all_methods]
        if isinstance(all_methods, Sequence)
        and not isinstance(all_methods, (str, bytes))
        else []
    )
    configured_cron = (
        connector_hooks.scheduler_events.get("cron", {})
        if isinstance(connector_hooks.scheduler_events, Mapping)
        else {}
    )
    normalized_cron: dict[str, str] = {}
    if isinstance(configured_cron, Mapping):
        for expression, methods in configured_cron.items():
            for method in _flatten_scheduler_hooks(methods):
                normalized_cron[method] = cstr(expression).strip()
    commercial_required = set(REQUIRED_SCHEDULER_FREQUENCIES)
    expected_cron = dict(REQUIRED_CRON_SCHEDULER_FREQUENCIES)
    if (
        configured != set(required)
        or len(normalized_all) != len(set(normalized_all))
        or set(normalized_all) != commercial_required
        or normalized_cron != expected_cron
    ):
        _fail("Connector scheduler hooks differ from the reviewed required jobs")
    rows = frappe.get_all(
        "Scheduled Job Type",
        filters={"method": ["in", required]},
        fields=["method", "stopped", "frequency", "cron_format"],
        order_by="method asc",
        limit_page_length=0,
    )
    rows_by_method: dict[str, list[Any]] = {method: [] for method in required}
    for row in rows or []:
        method = cstr(_value(row, "method")).strip()
        if method in rows_by_method:
            rows_by_method[method].append(row)
    observed: list[dict[str, str]] = []
    invalid = []
    for method in required:
        matches = rows_by_method[method]
        if method in REQUIRED_SCHEDULER_FREQUENCIES:
            expected_frequency = "All"
            expected_cron_format = ""
        else:
            expected_frequency = "Cron"
            expected_cron_format = expected_cron[method]
        if (
            len(matches) != 1
            or cint(_value(matches[0], "stopped"))
            or cstr(_value(matches[0], "frequency")).strip()
            != expected_frequency
            or cstr(_value(matches[0], "cron_format")).strip()
            != expected_cron_format
        ):
            invalid.append(method)
            continue
        observed_row = {"method": method, "frequency": expected_frequency}
        if expected_cron_format:
            observed_row["cronFormat"] = expected_cron_format
        observed.append(observed_row)
    obsolete = sorted(
        cstr(row)
        for row in frappe.get_all(
            "Scheduled Job Type",
            filters={"method": ["in", list(OBSOLETE_SCHEDULER_JOBS)]},
            pluck="method",
            limit_page_length=0,
        )
        or []
    )
    if (
        invalid
        or obsolete
        or (
            cint(_config_value("pause_scheduler", 0))
            and not allow_paused_scheduler
        )
    ):
        _fail(
            "Target ERP scheduler jobs are missing, duplicated, paused, "
            "mis-timed, or obsolete"
        )
    return {
        "passed": True,
        "requiredJobs": required,
        "activeJobs": required,
        "requiredFrequencies": observed,
        "missing": [],
        "obsolete": [],
        "observedDigest": canonical_json_sha256(observed),
    }


def _qr_account_check(company: str, currency: str) -> dict[str, Any]:
    resolved = payment_service.resolve_verified_qr_settlement_account(
        "DuitNow QR",
        company,
        currency,
    )
    account = _text(resolved.get("account"), "DuitNow QR account")
    row = frappe.db.get_value(
        "Account",
        account,
        [
            "name",
            "company",
            "account_currency",
            "account_type",
            "root_type",
            "is_group",
            "disabled",
        ],
        as_dict=True,
    )
    if not row:
        _fail("DuitNow QR settlement account was not found")
    account_type = cstr(_value(row, "account_type")).strip()
    root_type = cstr(_value(row, "root_type")).strip()
    account_currency = cstr(_value(row, "account_currency")).strip().upper()
    if (
        cstr(resolved.get("type")).strip() != "Bank"
        or cstr(_value(row, "company")).strip() != company
        or account_currency != currency
        or account_type != "Bank"
        or root_type != "Asset"
        or cint(_value(row, "is_group"))
        or cint(_value(row, "disabled"))
    ):
        _fail(
            "DuitNow QR must use an enabled non-group Bank Asset for the "
            "target company and currency"
        )
    return {
        "passed": True,
        "account": account,
        "accountSha256": _sha256(account),
        "company": company,
        "currency": currency,
        "accountType": "Bank",
        "rootType": "Asset",
        "isGroup": False,
        "disabled": False,
        "cashAccountUsed": False,
    }


def _provider_controls_check(expected_account_type: str, company: str) -> dict[str, Any]:
    allow_mock = bool(cint(_config_value("allow_maybank_mock", 0)))
    allow_simulation = bool(
        cint(_config_value("allow_maybank_desk_simulation", 0))
    )
    developer_mode = bool(cint(_config_value("developer_mode", 0)))
    test_mode = bool(
        cint(getattr(frappe, "in_test", 0))
        or cint(getattr(getattr(frappe, "flags", None), "in_test", 0))
    )
    required_account_type = _text(
        expected_account_type,
        "expected_maybank_account_type",
    ).lower()
    if required_account_type not in MAYBANK_ACCOUNT_TYPES:
        _fail("expected_maybank_account_type is not supported")
    # Reject any mock, simulation, developer, or test opt-in before looking up
    # branch data.  A production preflight must fail for the unsafe mode itself
    # rather than masking it behind a missing-profile error.
    if allow_mock or allow_simulation or developer_mode or test_mode:
        _fail("Target Maybank configuration is not production-only and complete")

    profiles = frappe.get_all(
        "POS Profile",
        filters={"company": company, "custom_kopos_automatic_qr_enabled": 1},
        fields=["name", "custom_kopos_maybank_qrpaybiz_account", "custom_kopos_maybank_outlet_id"],
        limit_page_length=0,
    )
    if not profiles:
        _fail("No enabled branch-scoped Maybank POS Profile was found")
    profile = profiles[0]
    account_name = cstr(profile.get("custom_kopos_maybank_qrpaybiz_account")).strip()
    if not account_name:
        _fail("The target POS Profile has no Maybank QRPayBiz Account")
    settings = frappe.get_doc("Maybank QRPayBiz Account", account_name)
    enabled = bool(cint(getattr(settings, "enabled", 0)))
    username = cstr(getattr(settings, "username", None)).strip()
    account_type = cstr(getattr(settings, "user_type", None)).strip().lower()
    outlet_id = cstr(profile.get("custom_kopos_maybank_outlet_id")).strip()
    provider_device_id = cstr(
        getattr(settings, maybank_client.PROVIDER_DEVICE_ID_FIELD, None)
    ).strip().lower()
    provider_device_name = " ".join(
        cstr(
            getattr(settings, maybank_client.PROVIDER_DEVICE_NAME_FIELD, None)
        ).strip().split()
    )
    provider_device_os = " ".join(
        cstr(
            getattr(settings, maybank_client.PROVIDER_DEVICE_OS_FIELD, None)
        ).strip().split()
    )
    base_url = cstr(getattr(settings, "base_url", None)).strip()
    get_password = getattr(settings, "get_password", None)
    encrypted_pin = (
        cstr(get_password("encrypted_pin")).strip()
        if callable(get_password)
        else ""
    )
    provider_origin = maybank_client.validate_base_url(
        base_url or maybank_client.DEFAULT_BASE_URL,
        allow_mock=False,
    )
    official_origin = provider_origin == maybank_client.DEFAULT_BASE_URL
    provider_identity_valid = bool(
        maybank_client.PROVIDER_DEVICE_ID_PATTERN.fullmatch(provider_device_id)
    )
    provider_name_valid = bool(
        maybank_client.PROVIDER_DEVICE_METADATA_PATTERN.fullmatch(
            provider_device_name
        )
    )
    provider_os_valid = bool(
        maybank_client.PROVIDER_DEVICE_METADATA_PATTERN.fullmatch(
            provider_device_os
        )
    )
    if (
        allow_mock
        or allow_simulation
        or developer_mode
        or test_mode
        or not enabled
        or not username
        or not encrypted_pin
        or account_type != required_account_type
        or not outlet_id
        or not provider_identity_valid
        or not provider_name_valid
        or not provider_os_valid
        or not official_origin
    ):
        _fail("Target Maybank configuration is not production-only and complete")
    return {
        "passed": True,
        "mockDisabled": True,
        "simulationDisabled": True,
        "developerModeDisabled": True,
        "testModeDisabled": True,
        "officialProviderOrigin": True,
        "settingsEnabled": True,
        "usernamePresent": True,
        "pinPresent": True,
        "accountType": account_type,
        "providerDeviceNamePresent": True,
        "providerDeviceOsPresent": True,
        "outletIdSha256": _sha256(outlet_id),
        "providerDeviceIdSha256": _sha256(provider_device_id),
        "providerDeviceNameSha256": _sha256(provider_device_name),
        "providerDeviceOsSha256": _sha256(provider_device_os),
    }


def _redis_version() -> str:
    client = _redis_client(frappe.cache())
    info = client.info(section="server")
    if not isinstance(info, Mapping):
        _fail("Target Redis server information is unavailable")
    version = info.get("redis_version") or info.get(b"redis_version")
    if isinstance(version, bytes):
        version = version.decode("utf-8")
    return _text(version, "Redis version")


def _mariadb_version() -> str:
    rows = frappe.db.sql("SELECT VERSION()", as_list=True)
    if len(rows) != 1 or not rows[0]:
        _fail("Target MariaDB version is unavailable")
    return _text(rows[0][0], "MariaDB version")


def run_v1(
    run_nonce: str,
    erp_commit: str,
    erp_artifact_sha256: str,
    expected_runtime_inventory_sha256: str,
    candidate_apk_sha256: str,
    mobile_manifest_sha256: str,
    erp_manifest_sha256: str,
    expected_origin: str,
    expected_site_id_sha256: str,
    company: str,
    currency: str,
    expected_time_zone: str,
    expected_maybank_account_type: str,
    output_filename: str | None = None,
) -> dict[str, Any]:
    """Run the protected target checks and write one canonical private report.

    The only transient database document is rolled back to a savepoint. Redis
    uses a short owner-token lease that is compare-and-delete released. The
    function performs no provider call, no inventory mutation, and makes no
    inventory acceptance claim. Catalog reads remain part of menu validation.
    """

    require_acceptance_operator()
    started_at = _utc_now()
    nonce = _require_nonce(run_nonce)
    source_commit = _require_commit(erp_commit)
    artifact_sha256 = _require_sha256(
        erp_artifact_sha256,
        "erp_artifact_sha256",
    )
    protected_runtime_inventory_sha256 = _require_sha256(
        expected_runtime_inventory_sha256,
        "expected_runtime_inventory_sha256",
    )
    runtime_inventory_sha256 = _runtime_inventory_sha256()
    if runtime_inventory_sha256 != protected_runtime_inventory_sha256:
        _fail("Installed KoPOS connector files do not match the accepted ERP package")
    apk_sha256 = _require_sha256(candidate_apk_sha256, "candidate_apk_sha256")
    mobile_manifest_digest = _require_sha256(
        mobile_manifest_sha256,
        "mobile_manifest_sha256",
    )
    erp_manifest_digest = _require_sha256(
        erp_manifest_sha256,
        "erp_manifest_sha256",
    )
    target_origin = _canonical_origin(expected_origin, "expected_origin")
    site_id_sha256 = _require_sha256(
        expected_site_id_sha256,
        "expected_site_id_sha256",
    )
    target_company = _text(company, "company")
    target_currency = _text(currency, "currency").upper()
    if not re.fullmatch(r"[A-Z]{3}", target_currency):
        _fail("currency must be an uppercase three-letter code")
    target_time_zone = _target_time_zone(expected_time_zone)

    target = _target_identity(
        expected_origin=target_origin,
        expected_site_id_sha256=site_id_sha256,
        company=target_company,
        currency=target_currency,
    )
    target["timeZone"] = target_time_zone
    database_checks = _database_round_trip(nonce)
    redis_check = _redis_round_trip(nonce, site_id_sha256)
    static_qr_check = _static_qr_check(target_company)
    schema_check = _schema_check()
    index_check = _index_check()
    job_check = _scheduler_check()
    qr_account_check = _qr_account_check(target_company, target_currency)
    provider_controls = _provider_controls_check(expected_maybank_account_type, target_company)
    catalog = catalog_preflight.build_enabled_device_catalog_proof_v1()
    final_static_qr_check = _static_qr_check(target_company)
    _require_stable_enabled_device_configuration(
        static_qr_check,
        final_static_qr_check,
        catalog,
    )
    completed_at = _utc_now()
    report = {
        "schemaVersion": "1",
        "producerContractId": PRODUCER_CONTRACT_ID,
        "producerSourceSha256": _producer_closure_sha256(),
        "runNonce": nonce,
        "startedAt": started_at,
        "completedAt": completed_at,
        "generatedAt": completed_at,
        "target": target,
        "candidate": {
            "erpCommit": source_commit,
            "erpArtifactSha256": artifact_sha256,
            "runtimeInventorySha256": runtime_inventory_sha256,
            "mobileApkSha256": apk_sha256,
            "mobileManifestSha256": mobile_manifest_digest,
            "erpManifestSha256": erp_manifest_digest,
        },
        "runtime": {
            "frappeVersion": _runtime_version(frappe, "frappe", "Frappe"),
            "erpnextVersion": _runtime_version(erpnext, "erpnext", "ERPNext"),
            "mariaDbVersion": _mariadb_version(),
            "redisVersion": _redis_version(),
        },
        "execution": {
            "frappe": "real",
            "mariaDb": "real",
            "redis": "real",
            "providerNetworkCalls": 0,
            "inventoryMutations": 0,
            "committedDbMutations": 0,
        },
        "checks": {
            **database_checks,
            "redisAtomicLockRoundTrip": redis_check,
            "staticQrCommissioning": final_static_qr_check,
            "schema": schema_check,
            "indexes": index_check,
            "jobs": job_check,
            "qrAccount": qr_account_check,
            "providerControls": provider_controls,
            "catalog": {
                "passed": True,
                **catalog,
            },
        },
        "status": "passed",
    }
    expected_filename = f"target-machine-preflight-{_sha256(nonce)[:16]}.json"
    filename = cstr(output_filename).strip() if output_filename else expected_filename
    if filename != expected_filename:
        _fail("output_filename must match the protected campaign nonce")
    write_report_atomically(report, filename, must_not_exist=True)
    return report
