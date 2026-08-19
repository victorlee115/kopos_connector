# pyright: reportMissingImports=false

"""Read-only authority and readiness matrix for a restored ERP site.

This producer is intentionally independent from the inventory acceptance
fixture.  It inventories what the restored site actually contains; it does
not create, change, schedule, or reconcile anything.  The output is suitable
for deciding what must be commissioned before an outlet can be activated, but
it is not itself a production-readiness approval.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from kopos_connector.kopos.services.inventory_autopilot.staff_role_contract import (
    AUTHORIZED_DIRECTOR_ROLE as _AUTHORIZED_DIRECTOR_ROLE,
    DEVICE_API_AUDITED_DOCTYPES as _DEVICE_API_AUDITED_DOCTYPES,
    DEVICE_API_ROLE as _DEVICE_API_ROLE,
    LEGACY_MANAGER_ROLES as _LEGACY_MANAGER_ROLES,
    SENSITIVE_BUSINESS_DOCTYPES as _SENSITIVE_BUSINESS_DOCTYPES,
    TECHNICAL_ADMIN_ROLE as _TECHNICAL_ADMIN_ROLE,
)

try:  # The contract validator is also used outside a Frappe bench.
    import frappe
except ImportError:  # pragma: no cover - exercised only outside a bench.
    frappe = None  # type: ignore[assignment]


CONTRACT_ID = "kopos.restored-outlet-matrix.v1"
PRODUCER = "kopos_connector.acceptance.restored_outlet_matrix.run_v1"
READ_ONLY = True
EVIDENCE_LEVEL = "restored_production_data"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")
FIXTURE_PREFIXES = (
    "INV-ACCEPT-",
    "SMOKE-",
    "TEST-",
    "TEST_",
    "QA-",
    "FIXTURE-",
    "DEMO-",
)
REQUIRED_CATEGORIES = (
    "companies",
    "outlets",
    "warehouses",
    "pos",
    "people",
    "accounts",
    "items",
    "recipes",
    "modifiers",
    "uoms",
    "suppliers",
    "quotations",
    "bomManufacturing",
    "availabilityLegacy",
    "operatingDays",
    "health",
    "monitoring",
    "operationalOwners",
)
_MISSING_REASON_LIMIT = 200
_FIXTURE_HASH_LIMIT = 50



def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _valid_commit(value: Any) -> bool:
    return isinstance(value, str) and COMMIT_PATTERN.fullmatch(value) is not None


def validate_outlet_matrix_report(value: Any) -> dict[str, Any]:
    """Validate the stable wire contract without importing Frappe.

    Missing authorities are represented inside a valid report.  A malformed
    binding or a report that claims to write is rejected here so release
    tooling cannot mistake a different document for this evidence.
    """

    if not _is_mapping(value):
        raise ValueError("outlet matrix report must be an object")
    if value.get("contractId") != CONTRACT_ID:
        raise ValueError("outlet matrix report has the wrong contractId")
    if value.get("schemaVersion") != 1:
        raise ValueError("outlet matrix schemaVersion must be 1")
    if value.get("status") not in {"passed", "blocked"}:
        raise ValueError("outlet matrix status must be passed or blocked")
    if value.get("evidenceLevel") != EVIDENCE_LEVEL:
        raise ValueError("outlet matrix evidenceLevel is invalid")
    if value.get("readOnly") is not True:
        raise ValueError("outlet matrix must be readOnly")
    if value.get("providerNetworkCalls") != 0:
        raise ValueError("outlet matrix providerNetworkCalls must be zero")
    for fieldname in ("connectorVersion", "scope"):
        if not isinstance(value.get(fieldname), str) or not value[fieldname].strip():
            raise ValueError(f"{fieldname} is required")
    for fieldname in ("erpArtifactSha256", "restoredBackupSha256"):
        if not _valid_sha256(value.get(fieldname)):
            raise ValueError(f"{fieldname} must be a lowercase SHA-256")
    for fieldname in ("erpCommit", "posCommit"):
        if fieldname in value and value[fieldname] is not None and not _valid_commit(
            value[fieldname]
        ):
            raise ValueError(f"{fieldname} must be a lowercase commit SHA")

    fixture_exclusion = value.get("fixtureExclusion")
    if not _is_mapping(fixture_exclusion):
        raise ValueError("fixtureExclusion is required")
    if fixture_exclusion.get("classification") != "explicit_prefix_and_marker_only":
        raise ValueError("fixture exclusion classification is invalid")
    prefixes = fixture_exclusion.get("prefixes")
    if not isinstance(prefixes, list) or not all(
        isinstance(prefix, str) and prefix for prefix in prefixes
    ):
        raise ValueError("fixture exclusion prefixes must be strings")
    if not _is_mapping(fixture_exclusion.get("excludedCounts")):
        raise ValueError("fixture exclusion counts are required")
    if not _is_mapping(fixture_exclusion.get("excludedIdHashesByDoctype")):
        raise ValueError("fixture exclusion hashes are required")

    for category_name in REQUIRED_CATEGORIES:
        category = value.get(category_name)
        if not _is_mapping(category):
            raise ValueError(f"{category_name} category is required")
        for fieldname in ("realCount", "fixtureCount"):
            if not _is_nonnegative_int(category.get(fieldname)):
                raise ValueError(f"{category_name}.{fieldname} must be a count")
        if not isinstance(category.get("ready"), bool):
            raise ValueError(f"{category_name}.ready must be boolean")
        if not isinstance(category.get("configured"), bool):
            raise ValueError(f"{category_name}.configured must be boolean")
        reasons = category.get("missingReasons")
        if not isinstance(reasons, list) or not all(
            isinstance(reason, str) and reason for reason in reasons
        ):
            raise ValueError(f"{category_name}.missingReasons must be strings")

    if not isinstance(value.get("missingAuthorities"), list) or not all(
        _is_mapping(reason) for reason in value["missingAuthorities"]
    ):
        raise ValueError("missingAuthorities must be a list of objects")
    permission_audit = value.get("permissionAudit")
    if not _is_mapping(permission_audit):
        raise ValueError("permissionAudit is required")
    if permission_audit.get("status") not in {"passed", "blocked"}:
        raise ValueError("permissionAudit.status is invalid")
    if not _is_nonnegative_int(permission_audit.get("outletUserRoleViolations")):
        raise ValueError("permissionAudit.outletUserRoleViolations must be a count")
    if permission_audit.get("systemManagerHandling") != "technical_admin_explicitly_reported":
        raise ValueError("permissionAudit system-manager handling is invalid")
    if permission_audit.get("companyDirectorHandling") != "business_authorized":
        raise ValueError("permissionAudit company-director handling is invalid")
    if permission_audit.get("status") != value.get("status"):
        raise ValueError("permissionAudit.status must match report status")
    if not _is_mapping(value.get("sourceCounts")):
        raise ValueError("sourceCounts is required")
    return dict(value)


def _runtime_frappe() -> Any:
    if frappe is None:
        raise RuntimeError("restored outlet matrix requires a Frappe bench")
    return frappe


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _hash_identity(value: Any) -> str:
    return hashlib.sha256(_text(value).encode("utf-8")).hexdigest()[:16]


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        candidate = value
    else:
        try:
            candidate = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur")).astimezone(
            timezone.utc
        )
    return candidate.astimezone(timezone.utc)


def _normalise_count(value: Any) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, result)


def _category(
    real_count: int,
    fixture_count: int,
    missing_reasons: Sequence[str],
    *,
    configured: bool | None = None,
    ready: bool | None = None,
    **extra: Any,
) -> dict[str, Any]:
    reasons = list(dict.fromkeys(reason for reason in missing_reasons if reason))
    resolved_configured = real_count > 0 if configured is None else configured
    resolved_ready = (
        resolved_configured and not reasons if ready is None else ready
    )
    return {
        "realCount": real_count,
        "fixtureCount": fixture_count,
        "configured": bool(resolved_configured),
        "ready": bool(resolved_ready),
        "missingReasons": reasons,
        **extra,
    }


def _missing(ctx: dict[str, Any], category: str, reason: str) -> None:
    reason = _text(reason)
    if not reason:
        return
    reasons = ctx.setdefault("missing", [])
    entry = {"category": category, "reason": reason}
    if entry not in reasons and len(reasons) < _MISSING_REASON_LIMIT:
        reasons.append(entry)


def _fixture_marker(row: Any) -> str | None:
    values: list[str] = []
    for fieldname in (
        "name",
        "item_code",
        "item_name",
        "recipe_code",
        "recipe_name",
        "supplier",
        "device_id",
        "order_id",
        "code",
        "description",
    ):
        text = _text(_value(row, fieldname))
        if text:
            values.append(text)
    for fieldname in ("is_test", "test_mode", "is_fixture", "fixture"):
        if _value(row, fieldname) in (1, True, "1", "true", "True"):
            return f"marker:{fieldname}"
    for value in values:
        upper = value.upper()
        for prefix in FIXTURE_PREFIXES:
            if upper.startswith(prefix):
                return f"prefix:{prefix}"
    return None


def _record_source(
    ctx: dict[str, Any], doctype: str, rows: Sequence[Any]
) -> list[Any]:
    real: list[Any] = []
    fixture_count = 0
    hashes: list[str] = []
    for row in rows:
        marker = _fixture_marker(row)
        if marker is None:
            real.append(row)
            continue
        fixture_count += 1
        if len(hashes) < _FIXTURE_HASH_LIMIT:
            hashes.append(_hash_identity(_value(row, "name")))
    source_counts = ctx.setdefault("sourceCounts", {})
    source_counts[doctype] = {
        "rawCount": len(rows),
        "realCount": len(real),
        "fixtureCount": fixture_count,
    }
    fixture_counts = ctx.setdefault("fixtureCounts", {})
    fixture_counts[doctype] = fixture_count
    fixture_hashes = ctx.setdefault("fixtureHashes", {})
    if hashes:
        fixture_hashes[doctype] = hashes
    return real


def _rows(
    ctx: dict[str, Any],
    doctype: str,
    fields: Sequence[str],
    category: str,
    *,
    filters: Mapping[str, Any] | None = None,
) -> list[Any]:
    runtime = _runtime_frappe()
    try:
        meta = runtime.get_meta(doctype)
    except Exception:
        _missing(ctx, category, f"doctype_missing:{doctype}")
        return []
    selected: list[str] = []
    for fieldname in ("name", *fields):
        if fieldname in selected:
            continue
        try:
            present = fieldname == "name" or bool(meta.has_field(fieldname))
        except Exception:
            present = fieldname == "name"
        if present:
            selected.append(fieldname)
    if selected == ["name"] and fields:
        _missing(ctx, category, f"fields_missing:{doctype}")
    try:
        result = runtime.get_all(
            doctype,
            filters=dict(filters or {}),
            fields=selected,
            order_by="name asc",
            limit_page_length=0,
        )
    except TypeError:
        try:
            result = runtime.get_all(
                doctype,
                filters=dict(filters or {}),
                fields=selected,
                limit_page_length=0,
            )
        except Exception:
            _missing(ctx, category, f"read_failed:{doctype}")
            return []
    except Exception:
        _missing(ctx, category, f"read_failed:{doctype}")
        return []
    return _record_source(ctx, doctype, list(result or []))


def _field_values(rows: Sequence[Any], fieldname: str) -> list[str]:
    return [_text(_value(row, fieldname)) for row in rows if _text(_value(row, fieldname))]


def _count_values(rows: Sequence[Any], fieldname: str) -> dict[str, int]:
    return dict(sorted(Counter(_field_values(rows, fieldname)).items()))


def _count_identity_values(rows: Sequence[Any], fieldname: str) -> dict[str, int]:
    return dict(
        sorted(
            Counter(_hash_identity(value) for value in _field_values(rows, fieldname)).items()
        )
    )


def _positive_decimal(value: Any) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _has_truthy(row: Any, fieldname: str) -> bool:
    return _value(row, fieldname) in (1, True, "1", "true", "True")


def _read_timezone(ctx: dict[str, Any]) -> str | None:
    runtime = _runtime_frappe()
    database = getattr(runtime, "db", None)
    getter = getattr(database, "get_single_value", None)
    if not callable(getter):
        _missing(ctx, "companies", "system_timezone_unreadable")
        return None
    try:
        value = getter("System Settings", "time_zone")
    except Exception:
        _missing(ctx, "companies", "system_timezone_unreadable")
        return None
    text = _text(value)
    if not text:
        _missing(ctx, "companies", "system_timezone_not_configured")
        return None
    return text


def _companies(ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _rows(
        ctx,
        "Company",
        ("default_currency", "abbr", "default_warehouse", "country"),
        "companies",
    )
    timezone_name = _read_timezone(ctx)
    reasons: list[str] = []
    if not rows:
        reasons.append("no_company_authority")
    if not timezone_name:
        reasons.append("business_timezone_not_configured")
    return _category(
        len(rows),
        ctx["fixtureCounts"].get("Company", 0),
        reasons,
        currencies=sorted(set(_field_values(rows, "default_currency"))),
        configuredTimezone=timezone_name,
        companyIdentityHashes=[_hash_identity(_value(row, "name")) for row in rows[:50]],
        defaultWarehouseReferenceCount=sum(
            bool(_text(_value(row, "default_warehouse"))) for row in rows
        ),
    )


def _warehouses(ctx: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    rows = _rows(
        ctx,
        "Warehouse",
        ("company", "parent_warehouse", "is_group", "disabled", "warehouse_type"),
        "warehouses",
    )
    leaf = [row for row in rows if not _has_truthy(row, "is_group")]
    transit = 0
    quarantine = 0
    for row in leaf:
        marker = " ".join(
            _text(_value(row, fieldname)).lower()
            for fieldname in ("name", "warehouse_type")
        )
        if any(token in marker for token in ("transit", "in-transit", "in transit")):
            transit += 1
        if "quarantine" in marker:
            quarantine += 1
    reasons: list[str] = []
    if not leaf:
        reasons.append("no_leaf_warehouse_authority")
    if transit == 0:
        reasons.append("transit_warehouse_not_identified_by_explicit_marker")
    if quarantine == 0:
        reasons.append("quarantine_warehouse_not_identified_by_explicit_marker")
    return (
        _category(
            len(rows),
            ctx["fixtureCounts"].get("Warehouse", 0),
            reasons,
            configured=bool(leaf),
            ready=bool(leaf) and not reasons,
            leafCount=len(leaf),
            leafCompanyCount=len({_text(_value(row, "company")) for row in leaf if _text(_value(row, "company"))}),
            transitCount=transit,
            quarantineCount=quarantine,
        ),
        {_text(_value(row, "name")) for row in leaf if _text(_value(row, "name"))},
    )


def _outlets(ctx: dict[str, Any], warehouse_names: set[str]) -> dict[str, Any]:
    profiles = _rows(
        ctx,
        "POS Profile",
        ("company", "warehouse", "disabled", "enabled"),
        "outlets",
    )
    policies = _rows(
        ctx,
        "FB Inventory Policy",
        ("company", "warehouse", "automation_state", "cutover_token"),
        "outlets",
    )
    explicit_outlets: list[Any] = []
    for doctype in ("Outlet", "FB Outlet"):
        if doctype == "FB Outlet" and explicit_outlets:
            break
        explicit_outlets.extend(_rows(ctx, doctype, ("company", "warehouse", "disabled"), "outlets"))
    profile_warehouses = {
        _text(_value(row, "warehouse")) for row in profiles if _text(_value(row, "warehouse"))
    }
    bound = len(profile_warehouses & warehouse_names) if warehouse_names else len(profile_warehouses)
    reasons: list[str] = []
    if not profiles:
        reasons.append("no_pos_profile_outlet_authority")
    if not explicit_outlets:
        reasons.append("no_explicit_outlet_doctype_observed")
    if profiles and bound < len(profile_warehouses):
        reasons.append("pos_profile_warehouse_binding_incomplete")
    if not policies:
        reasons.append("no_inventory_policy_authority")
    return _category(
        len(profiles) + len(explicit_outlets),
        ctx["fixtureCounts"].get("POS Profile", 0)
        + ctx["fixtureCounts"].get("Outlet", 0)
        + ctx["fixtureCounts"].get("FB Outlet", 0),
        reasons,
        configured=bool(profiles),
        ready=bool(profiles and policies and bound == len(profile_warehouses)),
        posProfileCount=len(profiles),
        explicitOutletDoctypeCount=len(explicit_outlets),
        activeProfileCount=sum(not _has_truthy(row, "disabled") for row in profiles),
        policyCount=len(policies),
        profileWarehouseBindingCount=bound,
        policyAutomationStateCounts=_count_values(policies, "automation_state"),
    )


def _pos_devices(ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _rows(
        ctx,
        "KoPOS Device",
        (
            "device_id",
            "enabled",
            "pos_profile",
            "config_version",
            "last_seen_at",
            "last_sync_at",
            "inventory_observed_at",
            "inventory_report_received_at",
            "inventory_config_version",
            "inventory_catalog_version",
            "inventory_overlay_version",
            "inventory_overlay_hash",
            "inventory_sales_pending",
            "inventory_sales_syncing",
            "inventory_sales_failed",
            "inventory_sales_dead_letter",
            "inventory_oldest_unsaved_sale_at",
            "inventory_commands_pending",
            "inventory_commands_syncing",
            "inventory_commands_failed",
            "inventory_commands_dead_letter",
        ),
        "pos",
    )
    now = _utc_now()
    current = stale = unknown = 0
    queue_totals = Counter()
    config_version_missing = 0
    duplicate_ids = sum(
        count - 1 for count in Counter(_field_values(rows, "device_id")).values() if count > 1
    )
    for row in rows:
        observed = _parse_datetime(
            _value(row, "inventory_report_received_at")
            or _value(row, "inventory_observed_at")
            or _value(row, "last_seen_at")
        )
        if observed is None:
            unknown += 1
        elif (now - observed).total_seconds() <= 30 * 60:
            current += 1
        else:
            stale += 1
        if not _text(_value(row, "config_version")):
            config_version_missing += 1
        for fieldname in (
            "inventory_sales_pending",
            "inventory_sales_syncing",
            "inventory_sales_failed",
            "inventory_sales_dead_letter",
            "inventory_commands_pending",
            "inventory_commands_syncing",
            "inventory_commands_failed",
            "inventory_commands_dead_letter",
        ):
            queue_totals[fieldname] += _normalise_count(_value(row, fieldname))
    reasons: list[str] = []
    if not rows:
        reasons.append("no_device_authority")
    if unknown:
        reasons.append("device_freshness_not_observed")
    if stale:
        reasons.append("device_report_stale_over_30_minutes")
    if duplicate_ids:
        reasons.append("duplicate_device_ids")
    return _category(
        len(rows),
        ctx["fixtureCounts"].get("KoPOS Device", 0),
        reasons,
        configured=bool(rows),
        ready=bool(rows) and not reasons,
        enabledCount=sum(_has_truthy(row, "enabled") for row in rows),
        currentCount=current,
        staleCount=stale,
        unknownFreshnessCount=unknown,
        duplicateDeviceIdCount=duplicate_ids,
        missingConfigVersionCount=config_version_missing,
        queueTotals=dict(sorted(queue_totals.items())),
        freshnessWindowMinutes=30,
        freshnessBasis="policy_default_when_no_outlet_override_is_bound",
        deviceIdentityHashes=[_hash_identity(_value(row, "device_id")) for row in rows[:50]],
    )


def _docperm_is_effective(row: Any) -> bool:
    """Return whether a DocPerm row grants any direct business capability."""

    return any(
        _has_truthy(row, fieldname)
        for fieldname in (
            "read",
            "write",
            "create",
            "submit",
            "cancel",
            "delete",
            "report",
            "export",
            "import",
            "print",
            "email",
            "share",
        )
    )


def _people(ctx: dict[str, Any]) -> dict[str, Any]:
    employees = _rows(
        ctx,
        "Employee",
        ("user_id", "status", "company", "department"),
        "people",
    )
    users = _rows(ctx, "User", ("enabled", "user_type"), "people")
    central = _rows(
        ctx,
        "KoPOS Staff Access",
        ("user", "employee", "active", "access_level", "pin_hash", "revision"),
        "people",
    )
    legacy = _rows(
        ctx,
        "KoPOS Device User",
        ("parent", "user", "active", "pin_hash"),
        "people",
    )
    # These reads join existing central/legacy POS assignments to the
    # framework's real Has Role and DocPerm authorities.  No role or bundle is
    # created by preflight; the resulting report is only an activation input.
    central_assignments = _rows(
        ctx,
        "KoPOS Staff Outlet",
        ("parent", "company", "outlet", "warehouse"),
        "people",
    )
    devices = _rows(
        ctx,
        "KoPOS Device",
        ("device_id", "pos_profile", "enabled"),
        "people",
    )
    profiles = _rows(
        ctx,
        "POS Profile",
        ("company", "warehouse", "disabled", "enabled"),
        "people",
    )
    role_rows = _rows(
        ctx,
        "Has Role",
        ("parent", "role", "parenttype"),
        "people",
    )
    permission_fields = (
        "parent",
        "role",
        "read",
        "write",
        "create",
        "submit",
        "cancel",
        "delete",
        "report",
        "export",
        "import",
        "print",
        "email",
        "share",
    )
    permission_rows = _rows(
        ctx,
        "DocPerm",
        permission_fields,
        "people",
    )
    permission_rows.extend(
        _rows(ctx, "Custom DocPerm", permission_fields, "people")
    )
    user_ids = {value for value in _field_values(central, "user") if value}
    central_user_counts = Counter(_field_values(central, "user"))
    employee_user_count = sum(bool(_text(_value(row, "user_id"))) for row in employees)
    pin_configured = sum(bool(_text(_value(row, "pin_hash"))) for row in central)
    legacy_hashes: dict[str, set[str]] = defaultdict(set)
    for row in legacy:
        user = _text(_value(row, "user"))
        pin_hash = _text(_value(row, "pin_hash"))
        if user and pin_hash:
            legacy_hashes[user].add(pin_hash)
    conflicts = sum(len(hashes) > 1 for hashes in legacy_hashes.values())
    reasons: list[str] = []
    if not employees:
        reasons.append("no_employee_authority")
    if not users:
        reasons.append("no_user_authority")
    if not central:
        reasons.append("central_staff_access_not_configured")
    if employee_user_count < len(employees):
        reasons.append("employee_user_mapping_incomplete")
    if conflicts:
        reasons.append("legacy_pin_conflict_requires_director_resolution")
    if central and any(count > 1 for count in central_user_counts.values()):
        reasons.append("duplicate_central_staff_access_user")

    central_by_name = {
        _text(_value(row, "name")): _text(_value(row, "user"))
        for row in central
        if _text(_value(row, "name")) and _text(_value(row, "user"))
    }
    profile_by_name = {
        _text(_value(row, "name")): row
        for row in profiles
        if _text(_value(row, "name"))
    }
    device_by_name = {
        _text(_value(row, "name")): row
        for row in devices
        if _text(_value(row, "name"))
    }
    outlet_scopes: dict[str, list[dict[str, str]]] = defaultdict(list)

    def add_scope(user: str, row: Any) -> None:
        resolved_user = _text(user)
        if not resolved_user:
            return
        scope = {
            "company": _text(_value(row, "company")),
            "outlet": _text(_value(row, "outlet")),
            "warehouse": _text(_value(row, "warehouse")),
        }
        # Keep the report sanitised while retaining a stable per-outlet
        # binding that an operator can compare between dry runs.
        identity = {
            f"{fieldname}IdentitySha256": _hash_identity(scope[fieldname])
            for fieldname in ("company", "outlet", "warehouse")
            if scope[fieldname]
        }
        if identity not in outlet_scopes[resolved_user]:
            outlet_scopes[resolved_user].append(identity)

    for assignment in central_assignments:
        add_scope(
            central_by_name.get(_text(_value(assignment, "parent")), ""),
            assignment,
        )
    for legacy_row in legacy:
        if not _has_truthy(legacy_row, "active"):
            continue
        device = device_by_name.get(_text(_value(legacy_row, "parent")))
        profile = profile_by_name.get(_text(_value(device, "pos_profile"))) if device else None
        if profile is not None:
            add_scope(_value(legacy_row, "user"), {
                "company": _value(profile, "company"),
                "outlet": _value(profile, "name"),
                "warehouse": _value(profile, "warehouse"),
            })

    roles_by_user: dict[str, set[str]] = defaultdict(set)
    for role_row in role_rows:
        parenttype = _text(_value(role_row, "parenttype"))
        if parenttype and parenttype != "User":
            continue
        user = _text(_value(role_row, "parent"))
        role = _text(_value(role_row, "role"))
        if user and role:
            roles_by_user[user].add(role)

    permission_doctypes_by_role: dict[str, set[str]] = defaultdict(set)
    for permission in permission_rows:
        role = _text(_value(permission, "role"))
        doctype = _text(_value(permission, "parent"))
        if role and doctype in _SENSITIVE_BUSINESS_DOCTYPES and _docperm_is_effective(permission):
            permission_doctypes_by_role[role].add(doctype)

    pos_users = {
        _text(_value(row, "user"))
        for row in central
        if _has_truthy(row, "active") and _text(_value(row, "user"))
    }
    pos_users.update(
        _text(_value(row, "user"))
        for row in legacy
        if _has_truthy(row, "active") and _text(_value(row, "user"))
    )
    role_assignments: list[dict[str, Any]] = []
    permission_violations: list[dict[str, Any]] = []
    legacy_manager_user_count = 0
    sensitive_permission_user_count = 0
    system_manager_user_count = 0
    company_director_user_count = 0
    for user in sorted(pos_users):
        roles = roles_by_user.get(user, set())
        is_system_manager = _TECHNICAL_ADMIN_ROLE in roles
        is_company_director = _AUTHORIZED_DIRECTOR_ROLE in roles
        if is_system_manager:
            system_manager_user_count += 1
        if is_company_director:
            company_director_user_count += 1
        legacy_roles = sorted(roles & _LEGACY_MANAGER_ROLES)
        effective_doctypes: set[str] = set()
        for role in roles:
            role_doctypes = permission_doctypes_by_role.get(role, set())
            if role == _DEVICE_API_ROLE:
                role_doctypes = role_doctypes - _DEVICE_API_AUDITED_DOCTYPES
            effective_doctypes.update(role_doctypes)
        if not is_system_manager and not is_company_director and legacy_roles:
            legacy_manager_user_count += 1
        if not is_system_manager and not is_company_director and effective_doctypes:
            sensitive_permission_user_count += 1
        violations: list[str] = []
        if not is_system_manager and not is_company_director and legacy_roles:
            violations.append("legacy_manager_role")
        if not is_system_manager and not is_company_director and effective_doctypes:
            violations.append("sensitive_business_erp_permission")
        if violations:
            permission_violations.append(
                {
                    "userIdentitySha256": _hash_identity(user),
                    "scopeIdentitySha256": [
                        _canonical_sha256(scope)
                        for scope in sorted(
                            outlet_scopes.get(user, []),
                            key=lambda value: json.dumps(value, sort_keys=True),
                        )
                    ],
                    "legacyManagerRoles": legacy_roles,
                    "sensitiveDocTypes": sorted(effective_doctypes),
                    "violations": violations,
                }
            )
        role_assignments.append(
            {
                "userIdentitySha256": _hash_identity(user),
                "scopeIdentitySha256": [
                    _canonical_sha256(scope)
                    for scope in sorted(
                        outlet_scopes.get(user, []),
                        key=lambda value: json.dumps(value, sort_keys=True),
                    )
                ],
                "roles": sorted(roles),
                "technicalAdmin": is_system_manager,
                "companyDirector": is_company_director,
                "legacyManagerRoles": legacy_roles,
                "effectiveSensitiveDocTypes": sorted(effective_doctypes),
                "violations": violations,
            }
        )
    if not role_rows and pos_users:
        reasons.append("pos_user_role_assignments_not_observed")
        _missing(ctx, "people", "pos_user_role_assignments_not_observed")
    if not permission_rows and pos_users:
        reasons.append("role_permissions_not_observed")
        _missing(ctx, "people", "role_permissions_not_observed")
    if legacy_manager_user_count:
        reasons.append("pos_user_has_legacy_manager_role")
        _missing(ctx, "people", "pos_user_has_legacy_manager_role")
    if sensitive_permission_user_count:
        reasons.append("pos_user_has_sensitive_business_erp_permission")
        _missing(ctx, "people", "pos_user_has_sensitive_business_erp_permission")
    return _category(
        len(employees) + len(users) + len(central) + len(legacy),
        sum(ctx["fixtureCounts"].get(key, 0) for key in ("Employee", "User", "KoPOS Staff Access", "KoPOS Device User")),
        reasons,
        configured=bool(employees and users),
        ready=bool(employees and users and central) and not reasons,
        employeeCount=len(employees),
        activeEmployeeCount=sum(_text(_value(row, "status")).lower() in {"active", "enabled"} for row in employees),
        userCount=len(users),
        enabledUserCount=sum(_has_truthy(row, "enabled") for row in users),
        centralStaffAccessCount=len(central),
        centralActiveCount=sum(_has_truthy(row, "active") for row in central),
        centralMappedUserCount=len(user_ids),
        centralPinConfiguredCount=pin_configured,
        legacyDeviceUserCount=len(legacy),
        legacyPinConflictUserCount=conflicts,
        posUserCount=len(pos_users),
        roleAssignmentCount=len(role_rows),
        roleAssignments=role_assignments,
        rolePermissionRowCount=len(permission_rows),
        permissionAuditAvailable=bool(permission_rows),
        permissionViolations=permission_violations,
        legacyManagerRoleUserCount=legacy_manager_user_count,
        sensitivePermissionUserCount=sensitive_permission_user_count,
        technicalAdminUserCount=system_manager_user_count,
        companyDirectorUserCount=company_director_user_count,
        technicalAdminRole=_TECHNICAL_ADMIN_ROLE,
        authorisedBusinessRole=_AUTHORIZED_DIRECTOR_ROLE,
        deviceApiAuditedDocTypes=sorted(_DEVICE_API_AUDITED_DOCTYPES),
        credentialValuesExposed=False,
    )


def _accounts(ctx: dict[str, Any]) -> dict[str, Any]:
    accounts = _rows(
        ctx,
        "Account",
        ("company", "root_type", "is_group", "disabled", "account_type"),
        "accounts",
    )
    cost_centers = _rows(ctx, "Cost Center", ("company", "is_group", "disabled"), "accounts")
    projects = _rows(ctx, "Project", ("company", "status", "is_active"), "accounts")
    dimensions = _rows(ctx, "Accounting Dimension", ("disabled",), "accounts")
    leaf_accounts = [row for row in accounts if not _has_truthy(row, "is_group")]
    reasons: list[str] = []
    if not accounts:
        reasons.append("no_account_authority")
    if not leaf_accounts:
        reasons.append("no_leaf_account_authority")
    if not any(_text(_value(row, "root_type")) == "Expense" for row in leaf_accounts):
        reasons.append("expense_account_not_observed")
    return _category(
        len(accounts) + len(cost_centers) + len(projects) + len(dimensions),
        sum(ctx["fixtureCounts"].get(key, 0) for key in ("Account", "Cost Center", "Project", "Accounting Dimension")),
        reasons,
        configured=bool(accounts),
        ready=bool(leaf_accounts) and not reasons,
        accountCount=len(accounts),
        leafAccountCount=len(leaf_accounts),
        accountRootTypeCounts=_count_values(leaf_accounts, "root_type"),
        costCenterCount=len(cost_centers),
        projectCount=len(projects),
        accountingDimensionCount=len(dimensions),
    )


def _items(ctx: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    fields = (
        "item_code",
        "item_name",
        "is_stock_item",
        "is_sales_item",
        "is_purchase_item",
        "disabled",
        "item_group",
        "stock_uom",
        "purchase_uom",
        "min_order_qty",
        "lead_time_days",
        "has_batch_no",
        "has_expiry_date",
        "custom_kopos_inventory_classification",
        "custom_kopos_availability_mode",
        "custom_kopos_track_stock",
        "custom_kopos_min_qty",
        "shelf_life_in_days",
        "custom_kopos_shelf_life_days",
    )
    rows = _rows(ctx, "Item", fields, "items")
    classifications = Counter()
    for row in rows:
        explicit = _text(_value(row, "custom_kopos_inventory_classification"))
        classifications[explicit or "unclassified"] += 1
    reasons: list[str] = []
    if not rows:
        reasons.append("no_item_authority")
    if classifications.get("unclassified", 0):
        reasons.append("item_classification_incomplete")
    return (
        _category(
            len(rows),
            ctx["fixtureCounts"].get("Item", 0),
            reasons,
            configured=bool(rows),
            ready=bool(rows) and not reasons,
            disabledCount=sum(_has_truthy(row, "disabled") for row in rows),
            stockItemCount=sum(_has_truthy(row, "is_stock_item") for row in rows),
            salesItemCount=sum(_has_truthy(row, "is_sales_item") for row in rows),
            purchaseItemCount=sum(_has_truthy(row, "is_purchase_item") for row in rows),
            batchRequiredCount=sum(_has_truthy(row, "has_batch_no") for row in rows),
            expiryRequiredCount=sum(_has_truthy(row, "has_expiry_date") for row in rows),
            shelfLifeConfiguredCount=sum(
                bool(_text(_value(row, "shelf_life_in_days")) or _text(_value(row, "custom_kopos_shelf_life_days")))
                for row in rows
            ),
            purchaseUomConfiguredCount=sum(
                bool(_text(_value(row, "purchase_uom")))
                for row in rows
            ),
            minimumOrderQuantityConfiguredCount=sum(
                _positive_decimal(_value(row, "min_order_qty"))
                for row in rows
            ),
            leadTimeConfiguredCount=sum(
                _positive_decimal(_value(row, "lead_time_days"))
                for row in rows
            ),
            classificationCounts=dict(sorted(classifications.items())),
            itemGroupIdentityCounts=_count_identity_values(rows, "item_group"),
        ),
        rows,
    )


def _recipes(ctx: dict[str, Any], items: Sequence[Any]) -> dict[str, Any]:
    recipes = _rows(
        ctx,
        "FB Recipe",
        (
            "recipe_code",
            "recipe_name",
            "sellable_item",
            "recipe_type",
            "status",
            "version_no",
            "canonical_hash",
            "company",
            "effective_from",
            "effective_to",
        ),
        "recipes",
    )
    components = _rows(
        ctx,
        "FB Recipe Component",
        ("parent", "item", "component_type", "qty", "uom", "stock_qty", "stock_uom", "stock_conversion_factor", "affects_stock", "affects_cogs"),
        "recipes",
    )
    real_recipe_names = {_text(_value(row, "name")) for row in recipes}
    components = [row for row in components if _text(_value(row, "parent")) in real_recipe_names]
    component_parents = {_text(_value(row, "parent")) for row in components}
    published = [row for row in recipes if _text(_value(row, "status")).lower() == "active"]
    stub_like = [
        row
        for row in recipes
        if not _text(_value(row, "canonical_hash"))
        or _text(_value(row, "name")) not in component_parents
    ]
    real_sellable = {
        _text(_value(row, "name"))
        for row in items
        if _has_truthy(row, "is_sales_item") and not _has_truthy(row, "disabled")
    }
    covered_sellables = {
        _text(_value(row, "sellable_item")) for row in published if _text(_value(row, "sellable_item"))
    }
    missing_coverage = real_sellable - covered_sellables
    reasons: list[str] = []
    if not recipes:
        reasons.append("no_recipe_authority")
    if not published:
        reasons.append("no_active_recipe")
    if stub_like:
        reasons.append("stub_like_recipe_requires_commissioning")
    if missing_coverage:
        reasons.append("active_sellable_recipe_coverage_incomplete")
    return _category(
        len(recipes),
        ctx["fixtureCounts"].get("FB Recipe", 0),
        reasons,
        configured=bool(recipes),
        ready=bool(published) and not reasons,
        activeCount=len(published),
        componentRowCount=len(components),
        stubLikeCount=len(stub_like),
        stubLikeBasis="missing_canonical_hash_or_real_component_row",
        sellableItemCandidateCount=len(real_sellable),
        sellableItemWithActiveRecipeCount=len(real_sellable & covered_sellables),
        missingSellableRecipeCount=len(missing_coverage),
        recipeTypeCounts=_count_values(recipes, "recipe_type"),
        statusCounts=_count_values(recipes, "status"),
        historicalRecordsNeverReinterpreted=True,
    )


def _modifiers(ctx: dict[str, Any]) -> dict[str, Any]:
    groups = _rows(
        ctx,
        "FB Modifier Group",
        ("group_code", "group_name", "selection_type", "active", "is_required"),
        "modifiers",
    )
    modifiers = _rows(
        ctx,
        "FB Modifier",
        ("modifier_code", "modifier_name", "modifier_group", "kind", "active", "affects_stock", "affects_recipe"),
        "modifiers",
    )
    group_names = {_text(_value(row, "name")) for row in groups}
    orphan_count = sum(
        bool(_text(_value(row, "modifier_group")))
        and _text(_value(row, "modifier_group")) not in group_names
        for row in modifiers
    )
    effects = _rows(ctx, "FB Recipe Modifier Effect", ("parent", "modifier_group", "modifier", "affects_stock", "affects_recipe", "stock_conversion_factor"), "modifiers")
    reasons: list[str] = []
    if not groups:
        reasons.append("no_modifier_group_authority")
    if orphan_count:
        reasons.append("modifier_group_reference_incomplete")
    if modifiers and not effects:
        reasons.append("recipe_modifier_effects_not_observed")
    return _category(
        len(groups) + len(modifiers) + len(effects),
        sum(ctx["fixtureCounts"].get(key, 0) for key in ("FB Modifier Group", "FB Modifier", "FB Recipe Modifier Effect")),
        reasons,
        configured=bool(groups or modifiers),
        ready=bool(groups) and not orphan_count,
        groupCount=len(groups),
        modifierCount=len(modifiers),
        activeGroupCount=sum(_has_truthy(row, "active") for row in groups),
        activeModifierCount=sum(_has_truthy(row, "active") for row in modifiers),
        orphanModifierGroupCount=orphan_count,
        recipeModifierEffectCount=len(effects),
        kindCounts=_count_values(modifiers, "kind"),
        stockAffectingModifierCount=sum(_has_truthy(row, "affects_stock") for row in modifiers),
    )


def _uoms(ctx: dict[str, Any]) -> dict[str, Any]:
    uoms = _rows(ctx, "UOM", ("must_be_whole_number", "enabled"), "uoms")
    conversions = _rows(ctx, "UOM Conversion Detail", ("parent", "uom", "conversion_factor"), "uoms")
    components = _rows(ctx, "FB Recipe Component", ("item", "uom", "stock_uom", "stock_conversion_factor"), "uoms")
    conversion_ready = sum(
        bool(_text(_value(row, "stock_uom")))
        and _positive_decimal(_value(row, "stock_conversion_factor"))
        for row in components
    )
    reasons: list[str] = []
    if not uoms:
        reasons.append("no_uom_authority")
    if components and conversion_ready < len(components):
        reasons.append("recipe_component_uom_conversion_incomplete")
    if not conversions and uoms:
        reasons.append("item_uom_conversion_rows_not_observed")
    return _category(
        len(uoms) + len(conversions),
        sum(ctx["fixtureCounts"].get(key, 0) for key in ("UOM", "UOM Conversion Detail")),
        reasons,
        configured=bool(uoms),
        ready=bool(uoms) and not reasons,
        uomCount=len(uoms),
        itemUomConversionRowCount=len(conversions),
        recipeComponentConversionRows=len(components),
        recipeComponentConversionReadyCount=conversion_ready,
    )


def _suppliers_and_quotations(ctx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    suppliers = _rows(ctx, "Supplier", ("supplier_group", "disabled", "country"), "suppliers")
    item_suppliers = _rows(
        ctx,
        "Item Supplier",
        # Item Supplier is only the standard supplier allow-list for an Item.
        # Purchase UOM, minimum quantity, and lead time belong to the standard
        # Item and UOM authorities above; they are not supplier-row fields.
        ("parent", "supplier", "supplier_part_no"),
        "suppliers",
    )
    quotations = _rows(
        ctx,
        "Supplier Quotation",
        ("supplier", "company", "transaction_date", "valid_till", "docstatus", "status", "currency"),
        "quotations",
    )
    quotation_items = _rows(
        ctx,
        "Supplier Quotation Item",
        ("parent", "item_code", "qty", "uom", "rate"),
        "quotations",
    )
    reasons: list[str] = []
    if not suppliers:
        reasons.append("no_supplier_authority")
    if suppliers and not item_suppliers:
        reasons.append("item_supplier_allow_list_not_observed")
    supplier_category = _category(
        len(suppliers) + len(item_suppliers),
        sum(ctx["fixtureCounts"].get(key, 0) for key in ("Supplier", "Item Supplier")),
        reasons,
        configured=bool(suppliers),
        ready=bool(suppliers and item_suppliers) and not reasons,
        supplierCount=len(suppliers),
        itemSupplierAllowListCount=len(item_suppliers),
        supplierGroupIdentityCounts=_count_identity_values(suppliers, "supplier_group"),
    )
    quotation_reasons: list[str] = []
    submitted = [row for row in quotations if _value(row, "docstatus") in (1, "1")]
    if not quotations:
        quotation_reasons.append("no_supplier_quotation_authority")
    if quotations and not submitted:
        quotation_reasons.append("no_submitted_supplier_quotation")
    if submitted and not quotation_items:
        quotation_reasons.append("supplier_quotation_items_not_observed")
    quotation_category = _category(
        len(quotations) + len(quotation_items),
        sum(ctx["fixtureCounts"].get(key, 0) for key in ("Supplier Quotation", "Supplier Quotation Item")),
        quotation_reasons,
        configured=bool(quotations),
        ready=bool(submitted and quotation_items) and not quotation_reasons,
        quotationCount=len(quotations),
        submittedQuotationCount=len(submitted),
        quotationItemCount=len(quotation_items),
        quotationStatusCounts=_count_values(quotations, "status"),
    )
    return supplier_category, quotation_category


def _bom_manufacturing(ctx: dict[str, Any]) -> dict[str, Any]:
    boms = _rows(ctx, "BOM", ("item", "quantity", "uom", "is_active", "is_default", "docstatus"), "bomManufacturing")
    bom_items = _rows(ctx, "BOM Item", ("parent", "item_code", "qty", "uom", "stock_uom", "conversion_factor"), "bomManufacturing")
    work_orders = _rows(ctx, "Work Order", ("production_item", "qty", "status", "docstatus", "planned_start_date"), "bomManufacturing")
    batches = _rows(ctx, "Batch", ("item", "batch_id", "expiry_date", "disabled"), "bomManufacturing")
    reasons: list[str] = []
    if not boms:
        reasons.append("no_bom_authority")
    if boms and not bom_items:
        reasons.append("bom_component_rows_not_observed")
    if not batches:
        reasons.append("no_batch_authority_observed")
    if boms and not work_orders:
        reasons.append("no_work_order_authority_observed")
    return _category(
        len(boms) + len(bom_items) + len(work_orders) + len(batches),
        sum(ctx["fixtureCounts"].get(key, 0) for key in ("BOM", "BOM Item", "Work Order", "Batch")),
        reasons,
        configured=bool(boms),
        ready=bool(boms and bom_items) and not reasons,
        bomCount=len(boms),
        bomItemCount=len(bom_items),
        activeBomCount=sum(_has_truthy(row, "is_active") for row in boms),
        defaultBomCount=sum(_has_truthy(row, "is_default") for row in boms),
        workOrderCount=len(work_orders),
        workOrderStatusCounts=_count_values(work_orders, "status"),
        batchCount=len(batches),
        expiredOrDisabledBatchCount=sum(_has_truthy(row, "disabled") for row in batches),
        readinessBasis="observed_standard_erpnext_documents_only",
    )


def _availability_legacy(ctx: dict[str, Any], items: Sequence[Any]) -> dict[str, Any]:
    modes = Counter()
    track_stock = 0
    min_qty_configured = 0
    malformed = 0
    for row in items:
        raw_mode = _text(_value(row, "custom_kopos_availability_mode"))
        mode = raw_mode if raw_mode in {"blank", "auto", "force_available", "force_unavailable"} else "unknown"
        modes[mode] += 1
        if mode == "unknown":
            malformed += 1
        if _has_truthy(row, "custom_kopos_track_stock"):
            track_stock += 1
        if _text(_value(row, "custom_kopos_min_qty")):
            min_qty_configured += 1
    reasons: list[str] = []
    if malformed:
        reasons.append("legacy_availability_mode_malformed")
    return _category(
        len(items),
        ctx["fixtureCounts"].get("Item", 0),
        reasons,
        configured=bool(items),
        ready=not malformed,
        modeCounts=dict(sorted(modes.items())),
        trackStockCount=track_stock,
        minimumQuantityConfiguredCount=min_qty_configured,
        malformedModeCount=malformed,
        authority="report_only_legacy_values",
    )


def _operating_days(ctx: dict[str, Any]) -> dict[str, Any]:
    orders = _rows(ctx, "FB Order", ("sale_datetime", "status", "company", "booth_warehouse", "source"), "operatingDays")
    timestamps = [_parse_datetime(_value(row, "sale_datetime")) for row in orders]
    dates = sorted({value.astimezone(ZoneInfo("Asia/Kuala_Lumpur")).date().isoformat() for value in timestamps if value})
    reasons: list[str] = []
    if not orders:
        reasons.append("no_order_history_observed")
    if not dates:
        reasons.append("no_valid_sale_dates_observed")
    return _category(
        len(orders),
        ctx["fixtureCounts"].get("FB Order", 0),
        reasons,
        configured=bool(dates),
        ready=bool(dates),
        historicalOrderCount=len(orders),
        distinctHistoricalOperatingDayCount=len(dates),
        earliestObservedDate=dates[0] if dates else None,
        latestObservedDate=dates[-1] if dates else None,
        datesAreHistoricalOnly=True,
        postCutoverForecastEvidenceEvaluated=False,
    )


def _health(ctx: dict[str, Any], device_category: Mapping[str, Any]) -> dict[str, Any]:
    logs = _rows(
        ctx,
        "FB Projection Log",
        ("projection_type", "state", "retry_count", "created_at", "last_attempt_at", "target_doctype", "target_name", "source_name"),
        "health",
    )
    # A projection log is autonamed and carries none of the fields the generic
    # fixture marker inspects, so an acceptance fixture's own log would be
    # counted as a production projection failure and hold health.ready false on
    # a site that is actually clean.  Resolve the fixture through its source
    # order instead.
    fixture_orders = {
        _text(_value(row, "name"))
        for row in _rows(
            ctx,
            "FB Order",
            ("external_idempotency_key",),
            "health",
            filters={"external_idempotency_key": ["like", "INV-ACCEPT-%"]},
        )
    }
    if fixture_orders:
        logs = [
            row for row in logs if _text(_value(row, "source_name")) not in fixture_orders
        ]
    exceptions = _rows(ctx, "FB Inventory Exception", ("severity", "status", "first_seen", "last_seen"), "health")
    state_counts = _count_values(logs, "state")
    projection_type_counts = _count_values(logs, "projection_type")
    critical_open = sum(
        _text(_value(row, "severity")).lower() == "critical"
        and _text(_value(row, "status")).lower() == "open"
        for row in exceptions
    )
    failed = state_counts.get("Failed", 0) + state_counts.get("Dead Letter", 0)
    reasons: list[str] = []
    if failed:
        reasons.append("projection_failures_observed")
    if critical_open:
        reasons.append("open_critical_inventory_exception")
    if device_category.get("staleCount", 0):
        reasons.append("stale_device_observed")
    if not logs:
        reasons.append("projection_log_authority_not_observed")
    return _category(
        len(logs) + len(exceptions),
        ctx["fixtureCounts"].get("FB Projection Log", 0) + ctx["fixtureCounts"].get("FB Inventory Exception", 0),
        reasons,
        configured=bool(logs or exceptions),
        ready=not reasons,
        projectionLogCount=len(logs),
        projectionStateCounts=state_counts,
        projectionTypeCounts=projection_type_counts,
        failedOrDeadLetterCount=failed,
        inventoryExceptionCount=len(exceptions),
        openCriticalExceptionCount=critical_open,
        deviceQueueTotals=device_category.get("queueTotals", {}),
        schedulerEvaluated=False,
        redisEvaluated=False,
        schedulerRedisReason="requires_runtime_process_health_check",
    )


def _monitoring(ctx: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime_frappe()
    config = getattr(runtime, "conf", None)
    known_keys = (
        "kopos_inventory_alert_destination",
        "kopos_inventory_monitor_destination",
        "monitor_destination",
        "alert_destination",
    )
    configured_keys: list[str] = []
    if isinstance(config, Mapping):
        configured_keys = [key for key in known_keys if _text(config.get(key))]
    elif config is not None:
        configured_keys = [key for key in known_keys if _text(getattr(config, key, None))]
    reasons: list[str] = []
    if not configured_keys:
        reasons.append("monitor_destination_not_configured_or_not_discoverable")
    return _category(
        len(configured_keys),
        0,
        reasons,
        configured=bool(configured_keys),
        ready=bool(configured_keys),
        configuredDestinationKeyCount=len(configured_keys),
        configuredDestinationKeys=configured_keys,
        destinationValuesExposed=False,
        externalMonitorPollingRequired=True,
    )


def _operational_owners(ctx: dict[str, Any]) -> dict[str, Any]:
    roles = _rows(ctx, "Has Role", ("parent", "role", "parenttype"), "operationalOwners")
    policies = _rows(ctx, "FB Inventory Policy", ("automation_user", "inventory_exception_owner", "purchase_review_owner"), "operationalOwners")
    role_counts = _count_values(roles, "role")
    director_count = role_counts.get("Company Director", 0)
    review_owner_count = sum(bool(_text(_value(row, "purchase_review_owner"))) for row in policies)
    exception_owner_count = sum(bool(_text(_value(row, "inventory_exception_owner"))) for row in policies)
    reasons: list[str] = []
    if director_count == 0:
        reasons.append("company_director_role_not_observed")
    if policies and review_owner_count < len(policies):
        reasons.append("purchase_review_owner_mapping_incomplete")
    if policies and exception_owner_count < len(policies):
        reasons.append("inventory_exception_owner_mapping_incomplete")
    return _category(
        len(roles) + len(policies),
        ctx["fixtureCounts"].get("Has Role", 0) + ctx["fixtureCounts"].get("FB Inventory Policy", 0),
        reasons,
        configured=bool(roles),
        ready=bool(director_count) and not reasons,
        roleCounts=role_counts,
        companyDirectorRoleAssignmentCount=director_count,
        inventoryPolicyCount=len(policies),
        purchaseReviewOwnerConfiguredCount=review_owner_count,
        inventoryExceptionOwnerConfiguredCount=exception_owner_count,
        ownerIdentitiesExposed=False,
    )


def run_v1(
    restored_backup_sha256: str,
    erp_artifact_sha256: str,
    expected_connector_version: str,
    *,
    erp_commit: str | None = None,
    pos_commit: str | None = None,
) -> dict[str, Any]:
    """Produce the restored-site matrix using reads only.

    The caller supplies the backup and artifact hashes because the running
    Bench cannot safely infer which backup or wheel the operator intended.
    Frappe metadata is read to bind the installed connector version exactly.
    """

    runtime = _runtime_frappe()
    if not _valid_sha256(restored_backup_sha256):
        raise ValueError("restored_backup_sha256 must be a lowercase SHA-256")
    if not _valid_sha256(erp_artifact_sha256):
        raise ValueError("erp_artifact_sha256 must be a lowercase SHA-256")
    expected_version = _text(expected_connector_version)
    if not expected_version:
        raise ValueError("expected_connector_version is required")
    if erp_commit is not None and not _valid_commit(erp_commit):
        raise ValueError("erp_commit must be a lowercase commit SHA")
    if pos_commit is not None and not _valid_commit(pos_commit):
        raise ValueError("pos_commit must be a lowercase commit SHA")
    try:
        installed_version = metadata.version("kopos_connector")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError("installed connector package metadata is unavailable") from exc
    if installed_version != expected_version:
        raise RuntimeError("installed connector version does not match candidate binding")

    ctx: dict[str, Any] = {
        "missing": [],
        "sourceCounts": {},
        "fixtureCounts": {},
        "fixtureHashes": {},
    }
    companies = _companies(ctx)
    warehouses, warehouse_names = _warehouses(ctx)
    outlets = _outlets(ctx, warehouse_names)
    devices = _pos_devices(ctx)
    people = _people(ctx)
    accounts = _accounts(ctx)
    items, item_rows = _items(ctx)
    recipes = _recipes(ctx, item_rows)
    modifiers = _modifiers(ctx)
    uoms = _uoms(ctx)
    suppliers, quotations = _suppliers_and_quotations(ctx)
    bom = _bom_manufacturing(ctx)
    legacy = _availability_legacy(ctx, item_rows)
    operating_days = _operating_days(ctx)
    health = _health(ctx, devices)
    monitoring = _monitoring(ctx)
    owners = _operational_owners(ctx)

    categories = {
        "companies": companies,
        "outlets": outlets,
        "warehouses": warehouses,
        "pos": devices,
        "people": people,
        "accounts": accounts,
        "items": items,
        "recipes": recipes,
        "modifiers": modifiers,
        "uoms": uoms,
        "suppliers": suppliers,
        "quotations": quotations,
        "bomManufacturing": bom,
        "availabilityLegacy": legacy,
        "operatingDays": operating_days,
        "health": health,
        "monitoring": monitoring,
        "operationalOwners": owners,
    }
    permission_audit_blocked = bool(
        people.get("permissionViolations")
        or (
            people.get("posUserCount", 0) > 0
            and (
                not people.get("permissionAuditAvailable")
                or not people.get("roleAssignmentCount")
            )
        )
    )
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "contractId": CONTRACT_ID,
        # Matrix generation is read-only, but an actual outlet-user role
        # violation must be machine-visible as blocked rather than merely a
        # warning buried in the people category. Missing master data remains
        # represented by category readiness and missingAuthorities.
        "status": "blocked" if permission_audit_blocked else "passed",
        "evidenceLevel": EVIDENCE_LEVEL,
        "producer": PRODUCER,
        "readOnly": READ_ONLY,
        "providerNetworkCalls": 0,
        "connectorVersion": installed_version,
        "erpArtifactSha256": erp_artifact_sha256,
        "restoredBackupSha256": restored_backup_sha256,
        "erpCommit": erp_commit,
        "posCommit": pos_commit,
        "scope": "restored_site",
        "evaluatedAtUtc": _utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "fixtureExclusion": {
            "prefixes": list(FIXTURE_PREFIXES),
            "classification": "explicit_prefix_and_marker_only",
            "excludedCounts": dict(sorted(ctx["fixtureCounts"].items())),
            "excludedIdHashesByDoctype": dict(sorted(ctx["fixtureHashes"].items())),
            "productionAuthoritiesUnaffected": True,
        },
        **categories,
        "missingAuthorities": list(ctx["missing"]),
        "sourceCounts": dict(sorted(ctx["sourceCounts"].items())),
        "externalEffects": {
            "databaseWrites": 0,
            "providerCalls": 0,
            "queueEnqueues": 0,
            "documentMutations": 0,
        },
        "historicalEvidence": {
            "preCutoverTransactionsReinterpreted": False,
            "historicalRecipesRewritten": False,
            "historicalStockEntriesCreated": False,
        },
        "permissionAudit": {
            "status": "blocked" if permission_audit_blocked else "passed",
            "outletUserRoleViolations": len(people.get("permissionViolations") or []),
            "systemManagerHandling": "technical_admin_explicitly_reported",
            "companyDirectorHandling": "business_authorized",
        },
    }
    validate_outlet_matrix_report(report)
    return report


def canonical_report_sha256(report: Mapping[str, Any]) -> str:
    """Return a stable digest after removing the evaluation timestamp."""

    content = {key: value for key, value in report.items() if key != "evaluatedAtUtc"}
    return _canonical_sha256(content)
