# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import frappe
from frappe.utils import cint, cstr

from kopos_connector.api._maybank_qr_contract import (
    MAYBANK_CURRENCY,
    MAYBANK_MOCK_REFERENCE_PATTERN,
    MAYBANK_PROVIDER,
    MAX_QR_PER_MINUTE,
    PREFLIGHT_REASON_RATE_LIMIT,
    QR_RATE_LIMIT_WINDOW_SECONDS,
    _coerce_site_datetime,
    _has_explicit_timezone,
    _parse_integer_sen,
    _require_provider_transaction_reference,
)
from kopos_connector.api._maybank_qr_persistence import (
    _build_persisted_preflight_rejection_response,
)
from kopos_connector.api.devices import (
    KOPOS_DEVICE_API_ROLE,
    get_session_roles,
    require_system_manager,
)
from kopos_connector.services.maybank.client import (
    DEFAULT_BASE_URL,
    validate_base_url,
)


SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
NONCE_PATTERN = re.compile(r"^[a-f0-9]{32,128}$")
DEVICE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{4,128}$")
OUTPUT_FILENAME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}\.json$"
)
TRANSACTION_FIELDS = (
    "name",
    "transaction_refno",
    "status",
    "maybank_status",
    "sale_amount_sen",
    "currency",
    "provider",
    "company",
    "device_id",
    "outlet_id",
    "idempotency_key",
    "request_fingerprint",
    "fb_order",
    "fb_order_payment",
    "sales_invoice",
    "consumption_key",
    "invoice_consumption_key",
    "consumed_at",
    "creation",
    "created_at",
    "is_test_simulation",
)
FENCE_FIELDS = TRANSACTION_FIELDS + (
    "raw_response",
    "replacement_reason",
    "replaces_transaction_refno",
    "round_number",
)


@dataclass(frozen=True)
class ExportBindings:
    candidate_apk_sha256: str
    erp_artifact_sha256: str
    mobile_manifest_sha256: str
    device_udid: str
    run_nonce: str


@dataclass(frozen=True)
class AcceptanceContext:
    bindings: ExportBindings
    transaction_references: tuple[str, ...]
    transactions: tuple[dict[str, Any], ...]
    capacity_fence: dict[str, Any]
    capacity_rejection: dict[str, Any]
    provider_origin: str
    outlet_id: str
    outlet_id_sha256: str
    company: str
    currency: str
    amount_sen: int
    simulation_audit_record_count: int


def _value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _fail(message: str) -> None:
    frappe.throw(message, frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")


def require_acceptance_operator() -> None:
    require_system_manager()
    if KOPOS_DEVICE_API_ROLE in get_session_roles():
        _fail(
            "Maybank production-acceptance evidence requires a non-device System Manager"
        )


def _require_sha256(value: Any, fieldname: str) -> str:
    text = cstr(value)
    if not SHA256_PATTERN.fullmatch(text):
        _fail(f"{fieldname} must be a lowercase SHA-256 digest")
    return text


def build_export_bindings(
    *,
    candidate_apk_sha256: Any,
    erp_artifact_sha256: Any,
    mobile_manifest_sha256: Any,
    device_udid: Any,
    run_nonce: Any,
) -> ExportBindings:
    device = cstr(device_udid)
    nonce = cstr(run_nonce)
    if not DEVICE_PATTERN.fullmatch(device):
        _fail("device_udid is invalid")
    if not NONCE_PATTERN.fullmatch(nonce):
        _fail("run_nonce must be 32-128 lowercase hexadecimal characters")
    return ExportBindings(
        candidate_apk_sha256=_require_sha256(
            candidate_apk_sha256, "candidate_apk_sha256"
        ),
        erp_artifact_sha256=_require_sha256(
            erp_artifact_sha256, "erp_artifact_sha256"
        ),
        mobile_manifest_sha256=_require_sha256(
            mobile_manifest_sha256, "mobile_manifest_sha256"
        ),
        device_udid=device,
        run_nonce=nonce,
    )


def parse_transaction_references(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            _fail("transaction_references must be a JSON array")
    else:
        parsed = value
    if not isinstance(parsed, (list, tuple)) or len(parsed) != MAX_QR_PER_MINUTE:
        _fail(
            f"transaction_references must contain exactly {MAX_QR_PER_MINUTE} values"
        )

    references: list[str] = []
    for index, raw_reference in enumerate(parsed):
        if not isinstance(raw_reference, str):
            _fail(f"transaction_references[{index}] must be an exact string")
        reference = _require_provider_transaction_reference(
            raw_reference,
            f"transaction_references[{index}]",
        )
        if len(reference) > 256 or MAYBANK_MOCK_REFERENCE_PATTERN.fullmatch(
            reference
        ):
            _fail(
                f"transaction_references[{index}] is not a live Maybank reference"
            )
        references.append(reference)
    if len(set(references)) != len(references):
        _fail("transaction_references contains a duplicate")
    return tuple(references)


def _config_value(name: str, default: Any = None) -> Any:
    config = getattr(frappe, "conf", None)
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default) if config is not None else default


def _provider_configuration() -> tuple[str, str]:
    enabled = cint(
        frappe.db.get_single_value("Maybank Settings", "enabled")
    )
    outlet_id = cstr(
        frappe.db.get_single_value("Maybank Settings", "outlet_id")
    ).strip()
    base_url = cstr(
        frappe.db.get_single_value("Maybank Settings", "base_url")
    ).strip()
    if not enabled or not outlet_id:
        _fail("Enabled Maybank Settings with an outlet are required")
    provider_origin = validate_base_url(
        base_url or DEFAULT_BASE_URL,
        allow_mock=False,
    )
    if provider_origin != DEFAULT_BASE_URL:
        _fail("Maybank UAT evidence must use the official production API origin")
    if any(
        (
            cint(_config_value("allow_maybank_mock", 0)),
            cint(_config_value("developer_mode", 0)),
            cint(_config_value("allow_maybank_desk_simulation", 0)),
            cint(getattr(frappe, "in_test", 0)),
            cint(getattr(getattr(frappe, "flags", None), "in_test", 0)),
        )
    ):
        _fail(
            "Maybank UAT evidence cannot run with mock, developer, test, or simulation controls enabled"
        )
    return provider_origin, outlet_id


def _get_all(
    doctype: str,
    *,
    filters: dict[str, Any],
    fields: tuple[str, ...] | list[str],
    order_by: str,
) -> list[dict[str, Any]]:
    rows = frappe.get_all(
        doctype,
        filters=filters,
        fields=list(fields),
        order_by=order_by,
        limit_page_length=0,
    )
    return [
        {
            fieldname: _value(row, fieldname)
            for fieldname in fields
        }
        for row in (rows or [])
    ]


def _load_transactions(
    references: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    rows = _get_all(
        "Maybank QR Transaction",
        filters={"transaction_refno": ["in", list(references)]},
        fields=TRANSACTION_FIELDS,
        order_by="creation asc, name asc",
    )
    if len(rows) != MAX_QR_PER_MINUTE:
        _fail("The ten supplied Maybank references are not all durable in ERP")
    by_reference = {
        cstr(row.get("transaction_refno")): row
        for row in rows
    }
    if len(by_reference) != len(rows) or set(by_reference) != set(references):
        _fail("The supplied Maybank references are missing or ambiguous in ERP")
    return tuple(rows)


def _require_exact_text(value: Any, fieldname: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        _fail(f"{fieldname} must be a nonempty exact value")
    return value


def _strict_amount_sen(value: Any, fieldname: str) -> int:
    try:
        amount_sen = _parse_integer_sen(value, fieldname)
    except frappe.ValidationError:
        raise
    if amount_sen <= 0:
        _fail(f"{fieldname} must be greater than zero")
    return amount_sen


def _validate_transactions(
    transactions: tuple[dict[str, Any], ...],
    *,
    bindings: ExportBindings,
    provider_origin: str,
    configured_outlet: str,
) -> tuple[str, str, int]:
    companies: set[str] = set()
    outlets: set[str] = set()
    amounts: set[int] = set()
    idempotency_keys: set[str] = set()
    request_fingerprints: set[str] = set()
    paid_count = 0
    for index, row in enumerate(transactions):
        if cstr(row.get("provider")).strip().lower() != MAYBANK_PROVIDER:
            _fail(f"Maybank transaction {index} has the wrong provider")
        reference = _require_provider_transaction_reference(
            row.get("transaction_refno"),
            f"Maybank transaction {index} reference",
        )
        if MAYBANK_MOCK_REFERENCE_PATTERN.fullmatch(reference):
            _fail(f"Maybank transaction {index} is a mock reference")
        if cstr(row.get("device_id")) != bindings.device_udid:
            _fail(f"Maybank transaction {index} belongs to another device")
        outlet = _require_exact_text(
            row.get("outlet_id"),
            f"Maybank transaction {index} outlet_id",
        )
        if outlet != configured_outlet:
            _fail(f"Maybank transaction {index} belongs to another outlet")
        company = _require_exact_text(
            row.get("company"),
            f"Maybank transaction {index} company",
        )
        currency = cstr(row.get("currency")).strip().upper()
        if currency != MAYBANK_CURRENCY:
            _fail(f"Maybank transaction {index} currency must be MYR")
        amount_sen = _strict_amount_sen(
            row.get("sale_amount_sen"),
            f"Maybank transaction {index} sale_amount_sen",
        )
        status = cstr(row.get("status")).strip()
        raw_status = cint(row.get("maybank_status"))
        if status == "paid" and raw_status == 1:
            paid_count += 1
        elif not (
            (status == "pending" and raw_status == 2)
            or (status == "scanned" and raw_status == 3)
        ):
            _fail(
                f"Maybank transaction {index} is not a retained pending/scanned reference"
            )
        if cint(row.get("is_test_simulation")):
            _fail(f"Maybank transaction {index} is a test simulation")

        idempotency_key = _require_exact_text(
            row.get("idempotency_key"),
            f"Maybank transaction {index} idempotency_key",
        )
        request_fingerprint = _require_sha256(
            row.get("request_fingerprint"),
            f"Maybank transaction {index} request_fingerprint",
        )
        if (
            idempotency_key in idempotency_keys
            or request_fingerprint in request_fingerprints
        ):
            _fail("Maybank transaction identity is duplicated")
        idempotency_keys.add(idempotency_key)
        request_fingerprints.add(request_fingerprint)
        companies.add(company)
        outlets.add(outlet)
        amounts.add(amount_sen)

    if paid_count != 1:
        _fail("Exactly one supplied Maybank reference must be durably paid")
    if len(companies) != 1 or len(outlets) != 1 or len(amounts) != 1:
        _fail(
            "The ten supplied Maybank references do not share one company, outlet, and amount"
        )
    if provider_origin != DEFAULT_BASE_URL:
        _fail("Maybank provider origin is invalid")
    return next(iter(companies)), MAYBANK_CURRENCY, next(iter(amounts))


def _load_capacity_fence(name: Any) -> dict[str, Any]:
    fence_name = _require_exact_text(name, "capacity_fence_transaction")
    row = frappe.db.get_value(
        "Maybank QR Transaction",
        fence_name,
        list(FENCE_FIELDS),
        as_dict=True,
    )
    if not row:
        _fail("The durable eleventh-request capacity fence was not found")
    return {
        fieldname: _value(row, fieldname)
        for fieldname in FENCE_FIELDS
    }


def _validate_capacity_fence(
    fence: dict[str, Any],
    *,
    transactions: tuple[dict[str, Any], ...],
    bindings: ExportBindings,
    outlet_id: str,
    company: str,
    currency: str,
    amount_sen: int,
) -> dict[str, Any]:
    request_fingerprint = _require_sha256(
        fence.get("request_fingerprint"),
        "capacity fence request_fingerprint",
    )
    if (
        cstr(fence.get("transaction_refno"))
        != f"REQUEST-{request_fingerprint.upper()}"
        or cstr(fence.get("status")).strip() != "failed"
        or fence.get("maybank_status") not in (None, "", 0)
        or cstr(fence.get("provider")).strip().lower() != MAYBANK_PROVIDER
        or cstr(fence.get("device_id")) != bindings.device_udid
        or cstr(fence.get("outlet_id")) != outlet_id
        or cstr(fence.get("company")) != company
        or cstr(fence.get("currency")).strip().upper() != currency
        or _strict_amount_sen(
            fence.get("sale_amount_sen"),
            "capacity fence sale_amount_sen",
        )
        != amount_sen
        or cstr(fence.get("replacement_reason"))
        or cstr(fence.get("replaces_transaction_refno"))
        or cint(fence.get("is_test_simulation"))
    ):
        _fail("The eleventh-request capacity fence identity is invalid")

    idempotency_key = _require_exact_text(
        fence.get("idempotency_key"),
        "capacity fence idempotency_key",
    )
    response = _build_persisted_preflight_rejection_response(
        fence,
        device_id=bindings.device_udid,
        idempotency_key=idempotency_key,
        amount_sen=amount_sen,
    )
    if (
        not response
        or cstr(response.get("preflight_reason_code"))
        != PREFLIGHT_REASON_RATE_LIMIT
        or response.get("provider_request_attempted") is not False
        or response.get("rejection_fence_registered") is not True
        or response.get("local_release_authorized") is not True
    ):
        _fail(
            "The eleventh request is not a durable no-provider-call capacity fence"
        )
    checked_at = cstr(response.get("checked_at"))
    if not _has_explicit_timezone(checked_at):
        _fail("The capacity fence timestamp lacks an explicit timezone")
    checked_datetime = _coerce_site_datetime(checked_at)
    window_start = checked_datetime - timedelta(
        seconds=QR_RATE_LIMIT_WINDOW_SECONDS
    )

    reference_set = {
        cstr(row.get("transaction_refno"))
        for row in transactions
    }
    for index, row in enumerate(transactions):
        created_at = row.get("created_at") or row.get("creation")
        if not created_at:
            _fail(f"Maybank transaction {index} has no creation timestamp")
        created_datetime = _coerce_site_datetime(created_at)
        if not window_start <= created_datetime <= checked_datetime:
            _fail(
                f"Maybank transaction {index} was not active in the capacity window"
            )

    window_rows = _get_all(
        "Maybank QR Transaction",
        filters={
            "provider": MAYBANK_PROVIDER,
            "device_id": bindings.device_udid,
            "outlet_id": outlet_id,
            "created_at": [
                "between",
                [
                    window_start.replace(tzinfo=None),
                    checked_datetime.replace(tzinfo=None),
                ],
            ],
        },
        fields=("transaction_refno",),
        order_by="creation asc, name asc",
    )
    active_provider_references = {
        cstr(row.get("transaction_refno"))
        for row in window_rows
        if cstr(row.get("transaction_refno"))
        and not cstr(row.get("transaction_refno")).startswith("REQUEST-")
    }
    if active_provider_references != reference_set:
        _fail(
            "The supplied references are not the exact ten active provider calls behind the capacity fence"
        )

    reference_digests = sorted(
        hashlib.sha256(reference.encode("utf-8")).hexdigest()
        for reference in reference_set
    )
    return {
        "status": "blocked",
        "reason": PREFLIGHT_REASON_RATE_LIMIT,
        "capacityLimit": MAX_QR_PER_MINUTE,
        "activeProviderReferenceDigests": reference_digests,
        "providerRequestSent": False,
        "attemptedAt": checked_at,
        "requestFingerprintSha256": request_fingerprint,
    }


def _simulation_audit_count() -> int:
    count = frappe.db.count(
        "Maybank QR Transaction",
        filters={"is_test_simulation": 1},
    )
    parsed = cint(count)
    if parsed != count and cstr(count).strip() != cstr(parsed):
        _fail("Maybank test-simulation audit count is invalid")
    return parsed


def load_acceptance_context(
    *,
    transaction_references: Any,
    capacity_fence_transaction: Any,
    bindings: ExportBindings,
) -> AcceptanceContext:
    require_acceptance_operator()
    references = parse_transaction_references(transaction_references)
    provider_origin, outlet_id = _provider_configuration()
    transactions = _load_transactions(references)
    company, currency, amount_sen = _validate_transactions(
        transactions,
        bindings=bindings,
        provider_origin=provider_origin,
        configured_outlet=outlet_id,
    )
    fence = _load_capacity_fence(capacity_fence_transaction)
    capacity_rejection = _validate_capacity_fence(
        fence,
        transactions=transactions,
        bindings=bindings,
        outlet_id=outlet_id,
        company=company,
        currency=currency,
        amount_sen=amount_sen,
    )
    simulation_count = _simulation_audit_count()
    if simulation_count:
        _fail("Maybank UAT evidence cannot include test-simulation audit rows")
    return AcceptanceContext(
        bindings=bindings,
        transaction_references=references,
        transactions=transactions,
        capacity_fence=fence,
        capacity_rejection=capacity_rejection,
        provider_origin=provider_origin,
        outlet_id=outlet_id,
        outlet_id_sha256=hashlib.sha256(
            outlet_id.encode("utf-8")
        ).hexdigest(),
        company=company,
        currency=currency,
        amount_sen=amount_sen,
        simulation_audit_record_count=simulation_count,
    )


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def producer_source_sha256(module_file: str) -> str:
    return hashlib.sha256(Path(module_file).read_bytes()).hexdigest()


def decimal_money_to_sen(value: Any, fieldname: str) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        _fail(f"{fieldname} is invalid")
    if not amount.is_finite():
        _fail(f"{fieldname} is invalid")
    scaled = amount * Decimal("100")
    if scaled != scaled.to_integral_value():
        _fail(f"{fieldname} has fractional sen")
    return int(scaled)


def canonical_decimal(value: Any, fieldname: str) -> str:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        _fail(f"{fieldname} is invalid")
    if not number.is_finite():
        _fail(f"{fieldname} is invalid")
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def write_report_atomically(report: dict[str, Any], output_filename: Any) -> None:
    filename = cstr(output_filename)
    if not OUTPUT_FILENAME_PATTERN.fullmatch(filename):
        _fail("output_filename must be a simple .json filename")
    private_files = Path(
        frappe.get_site_path("private", "files")
    ).resolve()
    output_directory = private_files / "kopos-acceptance"
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_directory = output_directory.resolve()
    if private_files not in resolved_directory.parents:
        _fail("Acceptance evidence directory escaped the private site path")

    payload = (
        json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.",
        suffix=".tmp",
        dir=resolved_directory,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1
        target = resolved_directory / filename
        os.replace(temporary_name, target)
        os.chmod(target, 0o600)
        directory_descriptor = os.open(
            resolved_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
