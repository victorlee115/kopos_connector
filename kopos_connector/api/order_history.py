# pyright: reportMissingImports=false

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe import _
from frappe.utils import (
    cint,
    cstr,
    get_datetime,
    get_system_timezone,
    getdate,
    now_datetime,
    nowdate,
)

from kopos_connector.api.devices import (
    elevate_device_api_user,
    get_authenticated_device_doc,
    get_device_doc,
)


DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MONEY_QUANTUM = Decimal("0.01")
HISTORY_CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 4096


def get_order_history_payload(
    *,
    device_id: str | None = None,
    since_date: str | date | datetime | None = None,
    cursor: str | int | None = None,
    limit: str | int | None = None,
) -> dict[str, Any]:
    """Return current-shift paid and durably voided history for one KoPOS device."""
    device_doc = resolve_history_device(device_id=device_id)
    device_id_value = cstr(getattr(device_doc, "device_id", None)).strip()
    if not device_id_value:
        frappe.throw(_("KoPOS Device has no device_id configured"), frappe.ValidationError)
    if not cint(getattr(device_doc, "enabled", 0)):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(device_id_value),
            frappe.ValidationError,
        )

    pos_profile_name = cstr(getattr(device_doc, "pos_profile", None)).strip()
    if not pos_profile_name:
        frappe.throw(
            _("KoPOS Device {0} has no POS Profile configured").format(
                device_id_value
            ),
            frappe.ValidationError,
        )

    pos_profile = frappe.get_cached_doc("POS Profile", pos_profile_name)
    company = cstr(getattr(pos_profile, "company", None)).strip()
    if not company:
        frappe.throw(
            _("POS Profile {0} has no company configured").format(pos_profile_name),
            frappe.ValidationError,
        )

    resolved_limit = normalize_limit(limit)
    effective_since_date, effective_since_datetime = resolve_since_date(
        device_id=device_id_value,
        pos_profile=pos_profile_name,
        requested_since_date=since_date,
    )

    with elevate_device_api_user():
        invoice_rows, next_cursor = query_invoice_rows(
            company=company,
            pos_profile=pos_profile_name,
            since_date=effective_since_date,
            since_datetime=effective_since_datetime,
            limit=resolved_limit + 1,
            cursor=cursor,
            device_id=device_id_value,
        )

        page_rows = invoice_rows[:resolved_limit]
        invoice_names = [cstr(row.get("name")).strip() for row in page_rows]
        items_by_parent = query_child_rows_by_parent(
            "Sales Invoice Item",
            invoice_names,
            get_invoice_item_fields(),
            order_by="parent asc, idx asc",
        )
        payments_by_parent = query_child_rows_by_parent(
            "Sales Invoice Payment",
            invoice_names,
            get_invoice_payment_fields(),
            order_by="parent asc, idx asc",
        )
        refund_rows = query_refund_rows(
            company=company,
            pos_profile=pos_profile_name,
            invoice_names=invoice_names,
        )
        refund_names = [cstr(row.get("name")).strip() for row in refund_rows]
        refund_items_by_parent = query_child_rows_by_parent(
            "Sales Invoice Item",
            refund_names,
            get_invoice_item_fields(),
            order_by="parent asc, idx asc",
        )
        refund_payments_by_parent = query_child_rows_by_parent(
            "Sales Invoice Payment",
            refund_names,
            get_invoice_payment_fields(),
            order_by="parent asc, idx asc",
        )
        fb_items_by_order = query_fb_order_items_by_order(invoice_rows + refund_rows)

    return {
        "status": "ok",
        "timestamp_contract_version": "utc-ms-v1",
        "device_id": device_id_value,
        "company": company,
        "pos_profile": pos_profile_name,
        "since_date": effective_since_date,
        "since_datetime": format_datetime(effective_since_datetime),
        "limit": resolved_limit,
        "next_cursor": next_cursor,
        "orders": [
            serialize_order_row(
                row,
                items=fb_items_by_order.get(cstr(row.get("custom_fb_order")).strip())
                or items_by_parent.get(cstr(row.get("name")).strip(), []),
                payments=payments_by_parent.get(cstr(row.get("name")).strip(), []),
            )
            for row in page_rows
        ],
        "refunds": [
            serialize_refund_row(
                row,
                items=fb_items_by_order.get(cstr(row.get("custom_fb_order")).strip())
                or refund_items_by_parent.get(cstr(row.get("name")).strip(), []),
                payments=refund_payments_by_parent.get(cstr(row.get("name")).strip(), []),
            )
            for row in refund_rows
        ],
    }


def resolve_history_device(device_id: str | None = None) -> Any:
    resolved_device_id = cstr(device_id).strip()
    if resolved_device_id:
        return get_device_doc(device_id=resolved_device_id)
    return get_authenticated_device_doc()


def normalize_limit(limit: str | int | None) -> int:
    requested_limit = cint(limit) if limit is not None else DEFAULT_LIMIT
    if requested_limit <= 0:
        return DEFAULT_LIMIT
    return min(requested_limit, MAX_LIMIT)


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    if not value or not value.replace("-", "A").replace("_", "A").isalnum():
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def get_history_cursor_secret() -> bytes:
    """Return a site-local secret so a cursor cannot be forged or cross-scoped."""
    local = getattr(frappe, "local", None)
    candidates = [getattr(local, "conf", None), getattr(frappe, "conf", None)]
    for config in candidates:
        if config is None:
            continue
        value = config.get("encryption_key") if hasattr(config, "get") else getattr(
            config, "encryption_key", None
        )
        secret = cstr(value).strip()
        if secret:
            return secret.encode("utf-8")
    frappe.throw(
        _("ERP site encryption key is required for order history paging"),
        frappe.ValidationError,
    )
    raise AssertionError("frappe.throw did not reject a missing cursor secret")


def _canonical_cursor_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _history_scope_digest(scope: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_cursor_json(scope)).hexdigest()


def _cursor_scope(
    *,
    company: str,
    pos_profile: str,
    device_id: str | None,
    since_date: str,
    since_datetime: datetime | None,
) -> dict[str, Any]:
    return {
        "company": company,
        "device_id": cstr(device_id).strip(),
        "pos_profile": pos_profile,
        "since_date": since_date,
        "since_datetime": format_datetime(since_datetime),
    }


def _db_datetime(value: Any, fieldname: str) -> str:
    try:
        parsed = value if isinstance(value, datetime) else get_datetime(value)
    except Exception:
        frappe.throw(
            _("Order history {0} is invalid").format(fieldname),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw did not reject an invalid cursor datetime")
    if not isinstance(parsed, datetime):
        frappe.throw(
            _("Order history {0} is invalid").format(fieldname),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw did not reject an invalid cursor datetime")
    if parsed.tzinfo is not None:
        timezone_name = cstr(get_system_timezone()).strip()
        try:
            parsed = parsed.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
        except ZoneInfoNotFoundError:
            frappe.throw(
                _("ERP system timezone {0} is invalid").format(timezone_name),
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw did not reject an invalid timezone")
    return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")


def _db_time(value: Any, fieldname: str) -> str:
    try:
        if isinstance(value, time):
            parsed = value
        elif isinstance(value, timedelta):
            if value < timedelta(0) or value >= timedelta(days=1):
                raise ValueError("database time is outside one day")
            hour, remainder = divmod(value.seconds, 60 * 60)
            minute, second = divmod(remainder, 60)
            parsed = time(hour, minute, second, value.microseconds)
        else:
            parsed = time.fromisoformat(cstr(value).strip())
    except (TypeError, ValueError):
        frappe.throw(
            _("Order history {0} is invalid").format(fieldname),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw did not reject an invalid cursor time")
    if not isinstance(parsed, time) or parsed.tzinfo is not None:
        frappe.throw(
            _("Order history {0} is invalid").format(fieldname),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw did not reject an invalid cursor time")
    return parsed.isoformat(timespec="microseconds")


def _history_sort_key(row: dict[str, Any]) -> dict[str, str]:
    posting_date = format_date(row.get("posting_date"))
    creation = row.get("creation")
    name = cstr(row.get("name")).strip()
    if not posting_date or not creation or not name:
        frappe.throw(
            _("Order history row is missing its stable paging identity"),
            frappe.ValidationError,
        )
    return {
        "posting_date": posting_date,
        "posting_time": _db_time(
            row.get("posting_time") or "00:00:00", "posting time"
        ),
        "creation": _db_datetime(creation, "creation"),
        "name": name,
    }


def _validate_sort_key(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "posting_date",
        "posting_time",
        "creation",
        "name",
    }:
        raise ValueError("invalid cursor sort key")
    posting_date = cstr(value.get("posting_date")).strip()
    posting_time = _db_time(value.get("posting_time"), "cursor posting time")
    creation = cstr(value.get("creation")).strip()
    name = cstr(value.get("name")).strip()
    if (
        not posting_date
        or parse_date_value(posting_date) is None
        or not posting_time
        or not creation
        or not name
    ):
        raise ValueError("invalid cursor sort key")
    return {
        "posting_date": posting_date,
        "posting_time": posting_time,
        "creation": _db_datetime(creation, "cursor creation"),
        "name": name,
    }


def encode_history_cursor(
    *, snapshot_ceiling: str, after: dict[str, str], scope: dict[str, Any]
) -> str:
    payload = {
        "after": after,
        "scope_sha256": _history_scope_digest(scope),
        "snapshot_ceiling": snapshot_ceiling,
        "v": HISTORY_CURSOR_VERSION,
    }
    encoded_payload = _urlsafe_b64encode(_canonical_cursor_json(payload))
    signature = hmac.new(
        get_history_cursor_secret(),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_urlsafe_b64encode(signature)}"


def decode_history_cursor(
    cursor: str | int | None, *, scope: dict[str, Any]
) -> tuple[str, dict[str, str] | None]:
    if cursor in (None, ""):
        return _db_datetime(now_datetime(), "snapshot ceiling"), None
    encoded = cstr(cursor).strip()
    if len(encoded) > MAX_CURSOR_LENGTH or encoded.count(".") != 1:
        frappe.throw(_("Order history cursor is invalid"), frappe.ValidationError)
    try:
        encoded_payload, encoded_signature = encoded.split(".", 1)
        supplied_signature = _urlsafe_b64decode(encoded_signature)
        expected_signature = hmac.new(
            get_history_cursor_secret(),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("cursor signature mismatch")
        payload = json.loads(_urlsafe_b64decode(encoded_payload).decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "after",
            "scope_sha256",
            "snapshot_ceiling",
            "v",
        }:
            raise ValueError("invalid cursor payload")
        if payload.get("v") != HISTORY_CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        if not hmac.compare_digest(
            cstr(payload.get("scope_sha256")), _history_scope_digest(scope)
        ):
            raise ValueError("cursor scope mismatch")
        snapshot_ceiling = _db_datetime(
            payload.get("snapshot_ceiling"), "cursor snapshot ceiling"
        )
        after = _validate_sort_key(payload.get("after"))
        return snapshot_ceiling, after
    except frappe.ValidationError:
        raise
    except Exception:
        frappe.throw(_("Order history cursor is invalid"), frappe.ValidationError)
    raise AssertionError("frappe.throw did not reject an invalid order history cursor")


def resolve_since_date(
    *,
    device_id: str,
    pos_profile: str,
    requested_since_date: str | date | datetime | None,
) -> tuple[str, datetime | None]:
    shift_since_datetime = get_current_shift_since_datetime(
        device_id=device_id,
        pos_profile=pos_profile,
    )
    shift_since_date = (
        shift_since_datetime.date()
        if shift_since_datetime
        else get_current_shift_since_date(
            device_id=device_id,
            pos_profile=pos_profile,
        )
    )
    requested_date = parse_date_value(requested_since_date)
    if requested_date and shift_since_date:
        return max(requested_date, shift_since_date).isoformat(), shift_since_datetime
    if shift_since_date:
        return shift_since_date.isoformat(), shift_since_datetime
    if requested_date:
        return requested_date.isoformat(), shift_since_datetime
    return cstr(nowdate()), shift_since_datetime


def get_current_shift_since_datetime(
    device_id: str, pos_profile: str
) -> datetime | None:
    filters: dict[str, Any] = {"device_id": device_id, "status": "Open"}
    entries = frappe.get_all(
        "FB Shift",
        filters=filters,
        fields=["name", "opened_at", "creation"],
        order_by="opened_at desc, creation desc",
        limit_page_length=1,
    )
    if not entries:
        return None
    entry = dict(entries[0])
    creation = entry.get("creation")
    return get_datetime(creation) if creation else None


def get_current_shift_since_date(device_id: str, pos_profile: str) -> date | None:
    filters: dict[str, Any] = {"device_id": device_id, "status": "Open"}
    entries = frappe.get_all(
        "FB Shift",
        filters=filters,
        fields=["name", "opened_at"],
        order_by="opened_at desc, creation desc",
        limit_page_length=1,
    )
    if not entries:
        return None
    entry = dict(entries[0])
    return parse_date_value(entry.get("opened_at"))


def query_invoice_rows(
    *,
    company: str,
    pos_profile: str,
    since_date: str,
    since_datetime: datetime | None,
    limit: int,
    cursor: str | int | None,
    device_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    filters: dict[str, Any] = {
        "docstatus": ["in", [1, 2]],
        "is_return": 0,
        "company": company,
        "pos_profile": pos_profile,
        "posting_date": [">=", since_date],
    }
    if device_id:
        filters["custom_fb_device_id"] = device_id

    scope = _cursor_scope(
        company=company,
        pos_profile=pos_profile,
        device_id=device_id,
        since_date=since_date,
        since_datetime=since_datetime,
    )
    snapshot_ceiling, scan_after = decode_history_cursor(cursor, scope=scope)
    accepted_rows: list[dict[str, Any]] = []
    cursor_after_accepted_row: list[dict[str, str]] = []
    batch_size = max(limit * 2, MAX_LIMIT + 1)
    while len(accepted_rows) < limit:
        raw_rows = query_invoice_keyset_batch(
            filters=filters,
            fields=filter_existing_fields("Sales Invoice", get_invoice_fields()),
            snapshot_ceiling=snapshot_ceiling,
            after=scan_after,
            limit=batch_size,
        )
        if not raw_rows:
            break

        for row in raw_rows:
            row_sort_key = _history_sort_key(row)
            scan_after = row_sort_key
            if not is_visible_history_invoice(row, device_id=device_id):
                continue
            if since_datetime and get_datetime(row.get("creation")) < since_datetime:
                continue
            accepted_rows.append(row)
            cursor_after_accepted_row.append(row_sort_key)
            if len(accepted_rows) >= limit:
                break

        if len(accepted_rows) >= limit or len(raw_rows) < batch_size:
            break

    visible_page_length = max(limit - 1, 0)
    next_cursor = (
        encode_history_cursor(
            snapshot_ceiling=snapshot_ceiling,
            after=cursor_after_accepted_row[visible_page_length - 1],
            scope=scope,
        )
        if visible_page_length > 0 and len(accepted_rows) > visible_page_length
        else None
    )
    return accepted_rows, next_cursor


def query_invoice_keyset_batch(
    *,
    filters: dict[str, Any],
    fields: list[str],
    snapshot_ceiling: str,
    after: dict[str, str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Read one immutable newest-first page without a lifetime-history offset."""
    if not fields or "name" not in fields:
        frappe.throw(
            _("Sales Invoice history fields are incomplete"),
            frappe.ValidationError,
        )
    allowed_fields = set(get_invoice_fields()) | {
        "docstatus",
        "creation",
        "modified",
        "name",
    }
    if any(field not in allowed_fields for field in fields):
        frappe.throw(
            _("Sales Invoice history fields are invalid"),
            frappe.ValidationError,
        )

    if after is None:
        initial_filters = dict(filters)
        initial_filters["creation"] = ["<=", snapshot_ceiling]
        return [
            dict(row)
            for row in frappe.get_all(
                "Sales Invoice",
                filters=initial_filters,
                fields=fields,
                order_by="posting_date desc, posting_time desc, creation desc, name desc",
                limit_page_length=max(1, min(int(limit), (MAX_LIMIT + 1) * 4)),
            )
        ]

    selected_fields = ", ".join(f"`{field}`" for field in fields)
    clauses = [
        "`docstatus` IN (1, 2)",
        "`is_return` = 0",
        "`company` = %s",
        "`pos_profile` = %s",
        "`posting_date` >= %s",
        "`creation` <= %s",
    ]
    values: list[Any] = [
        filters["company"],
        filters["pos_profile"],
        filters["posting_date"][1],
        snapshot_ceiling,
    ]
    device_id = filters.get("custom_fb_device_id")
    if device_id:
        clauses.append("`custom_fb_device_id` = %s")
        values.append(device_id)

    if after is not None:
        clauses.append(
            "("
            "`posting_date` < %s OR "
            "(`posting_date` = %s AND COALESCE(`posting_time`, '00:00:00') < %s) OR "
            "(`posting_date` = %s AND COALESCE(`posting_time`, '00:00:00') = %s AND `creation` < %s) OR "
            "(`posting_date` = %s AND COALESCE(`posting_time`, '00:00:00') = %s AND `creation` = %s AND `name` < %s)"
            ")"
        )
        values.extend(
            [
                after["posting_date"],
                after["posting_date"],
                after["posting_time"],
                after["posting_date"],
                after["posting_time"],
                after["creation"],
                after["posting_date"],
                after["posting_time"],
                after["creation"],
                after["name"],
            ]
        )
    values.append(max(1, min(int(limit), (MAX_LIMIT + 1) * 4)))
    rows = frappe.db.sql(
        f"""SELECT {selected_fields}
            FROM `tabSales Invoice`
            WHERE {' AND '.join(clauses)}
            ORDER BY `posting_date` DESC,
                     COALESCE(`posting_time`, '00:00:00') DESC,
                     `creation` DESC,
                     `name` DESC
            LIMIT %s""",
        values=values,
        as_dict=True,
    )
    return [dict(row) for row in rows]


def is_visible_history_invoice(
    row: dict[str, Any], *, device_id: str | None
) -> bool:
    docstatus = cint(row.get("docstatus"))
    if docstatus == 1:
        return True
    if docstatus != 2:
        return False
    return has_durable_kopos_void_history_proof(row, device_id=device_id)


def has_durable_kopos_void_history_proof(
    row: dict[str, Any], *, device_id: str | None
) -> bool:
    """Re-prove an exact KoPOS void before exposing a cancelled invoice."""
    invoice_name = cstr(row.get("name")).strip()
    order_name = cstr(row.get("custom_fb_order")).strip()
    shift_name = cstr(row.get("custom_fb_shift")).strip()
    invoice_device_id = cstr(row.get("custom_fb_device_id")).strip()
    sale_idempotency_key = cstr(row.get("custom_fb_idempotency_key")).strip()
    void_idempotency_key = cstr(
        row.get("custom_fb_void_idempotency_key")
    ).strip()
    void_fingerprint = cstr(
        row.get("custom_fb_void_request_fingerprint")
    ).strip().lower()
    void_manager = cstr(row.get("custom_fb_void_manager")).strip()
    void_token_id = cstr(row.get("custom_fb_void_approval_token_id")).strip()
    if (
        not invoice_name
        or not order_name
        or not shift_name
        or not invoice_device_id
        or (device_id and invoice_device_id != device_id)
        or not sale_idempotency_key
        or not void_idempotency_key
        or not is_sha256_hex(void_fingerprint)
        or not void_manager
        or not void_token_id
    ):
        return False

    order = frappe.db.get_value(
        "FB Order",
        order_name,
        [
            "name",
            "docstatus",
            "sales_invoice",
            "external_idempotency_key",
            "device_id",
            "shift",
            "company",
            "currency",
            "status",
            "invoice_status",
        ],
        as_dict=True,
    )
    if (
        not order
        or cstr(row_value(order, "name")).strip() != order_name
        or cint(row_value(order, "docstatus")) != 1
        or cstr(row_value(order, "sales_invoice")).strip() != invoice_name
        or cstr(row_value(order, "external_idempotency_key")).strip()
        != sale_idempotency_key
        or cstr(row_value(order, "device_id")).strip() != invoice_device_id
        or cstr(row_value(order, "shift")).strip() != shift_name
        or cstr(row_value(order, "company")).strip()
        != cstr(row.get("company")).strip()
        or cstr(row_value(order, "currency")).strip().upper()
        != cstr(row.get("currency")).strip().upper()
        or cstr(row_value(order, "status")).strip() != "Cancelled"
        or cstr(row_value(order, "invoice_status")).strip() != "Reversed"
    ):
        return False

    approval = frappe.db.get_value(
        "KoPOS Manager Approval",
        {"token_id": void_token_id},
        [
            "token_id",
            "status",
            "manager_id",
            "action",
            "resource_id",
            "context_hash",
            "consumed_idempotency_key",
        ],
        as_dict=True,
    )
    return bool(
        approval
        and cstr(row_value(approval, "token_id")).strip() == void_token_id
        and cstr(row_value(approval, "status")).strip() == "consumed"
        and cstr(row_value(approval, "manager_id")).strip() == void_manager
        and cstr(row_value(approval, "action")).strip() == "void_order"
        and cstr(row_value(approval, "resource_id")).strip() == invoice_name
        and cstr(row_value(approval, "consumed_idempotency_key")).strip()
        == void_idempotency_key
        and is_sha256_hex(cstr(row_value(approval, "context_hash")).strip().lower())
    )


def is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def query_refund_rows(
    *,
    company: str,
    pos_profile: str,
    invoice_names: list[str],
) -> list[dict[str, Any]]:
    if not invoice_names:
        return []
    return [
        dict(row)
        for row in frappe.get_all(
            "Sales Invoice",
            filters={
                "docstatus": 1,
                "is_return": 1,
                "company": company,
                "pos_profile": pos_profile,
                "return_against": ["in", invoice_names],
            },
            fields=filter_existing_fields("Sales Invoice", get_invoice_fields() + ["return_against"]),
            order_by="posting_date desc, posting_time desc, creation desc, name desc",
        )
    ]


def query_child_rows_by_parent(
    doctype: str,
    parent_names: list[str],
    fields: list[str],
    *,
    order_by: str,
) -> dict[str, list[dict[str, Any]]]:
    if not parent_names:
        return {}
    rows = frappe.get_all(
        doctype,
        filters={"parent": ["in", parent_names], "parenttype": "Sales Invoice"},
        fields=filter_existing_fields(doctype, fields),
        order_by=order_by,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        parent = cstr(row.get("parent")).strip()
        if parent:
            grouped.setdefault(parent, []).append(dict(row))
    return grouped


def query_fb_order_items_by_order(
    invoice_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    fb_order_names = sorted(
        {
            cstr(row.get("custom_fb_order")).strip()
            for row in invoice_rows
            if cstr(row.get("custom_fb_order")).strip()
        }
    )
    if not fb_order_names:
        return {}

    line_rows = [
        dict(row)
        for row in frappe.get_all(
            "FB Order Line",
            filters={"parent": ["in", fb_order_names], "parenttype": "FB Order"},
            fields=filter_existing_fields(
                "FB Order Line",
                [
                    "name",
                    "parent",
                    "idx",
                    "item",
                    "item_name_snapshot",
                    "qty",
                    "unit_price",
                    "modifier_total",
                    "discount_amount",
                    "line_total",
                    "resolved_sale",
                    "remarks",
                ],
            ),
            order_by="parent asc, idx asc",
        )
    ]
    modifier_parent_names = []
    for row in line_rows:
        line_name = cstr(row.get("name")).strip()
        resolved_sale = cstr(row.get("resolved_sale")).strip()
        if line_name:
            modifier_parent_names.append(line_name)
        if resolved_sale:
            modifier_parent_names.append(resolved_sale)
    modifiers_by_parent = query_fb_selected_modifiers_by_parent(modifier_parent_names)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in line_rows:
        parent = cstr(row.get("parent")).strip()
        line_name = cstr(row.get("name")).strip()
        modifier_parent = cstr(row.get("resolved_sale")).strip() or line_name
        modifiers = modifiers_by_parent.get(modifier_parent, [])
        grouped.setdefault(parent, []).append(
            {
                "idx": row.get("idx"),
                "item_code": row.get("item"),
                "item_name": row.get("item_name_snapshot"),
                "description": row.get("remarks"),
                "qty": row.get("qty"),
                "rate": row.get("unit_price"),
                "amount": row.get("line_total"),
                "net_amount": row.get("line_total"),
                "base_rate": row.get("unit_price"),
                "base_amount": row.get("line_total"),
                "discount_amount": row.get("discount_amount"),
                "warehouse": None,
                "custom_kopos_modifier_total": row.get("modifier_total"),
                "custom_kopos_modifiers": serialize_fb_modifiers_snapshot(modifiers),
            }
        )
    return grouped


def query_fb_selected_modifiers_by_parent(
    parent_names: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not parent_names:
        return {}
    rows = frappe.get_all(
        "FB Selected Modifier",
        filters={
            "parent": ["in", parent_names],
            "parenttype": ["in", ["FB Order Line", "FB Resolved Sale"]],
        },
        fields=filter_existing_fields(
            "FB Selected Modifier",
            ["parent", "modifier_group", "modifier", "price_adjustment"],
        ),
        order_by="parent asc, idx asc",
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        parent = cstr(row.get("parent")).strip()
        if parent:
            grouped.setdefault(parent, []).append(dict(row))
    return grouped


def serialize_fb_modifiers_snapshot(modifiers: list[dict[str, Any]]) -> str | None:
    rows = []
    for modifier in modifiers:
        modifier_id = empty_to_none(modifier.get("modifier"))
        if not modifier_id:
            continue
        rows.append(
            {
                "id": modifier_id,
                "name": frappe.db.get_value("FB Modifier", modifier_id, "modifier_name")
                or modifier_id,
                "group_id": empty_to_none(modifier.get("modifier_group")),
                "price_adjustment": modifier.get("price_adjustment"),
            }
        )
    if not rows:
        return None
    return json.dumps({"modifiers": rows}, separators=(",", ":"))


def serialize_invoice_row(
    row: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    payments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "name": cstr(row.get("name")),
        "display_number": empty_to_none(row.get("custom_kopos_display_number"))
        or extract_display_number_from_remarks(row.get("remarks")),
        "idempotency_key": empty_to_none(row.get("custom_kopos_idempotency_key"))
        or empty_to_none(row.get("custom_fb_idempotency_key")),
        "device_id": empty_to_none(row.get("custom_kopos_device_id"))
        or empty_to_none(row.get("custom_fb_device_id")),
        "company": cstr(row.get("company")),
        "pos_profile": cstr(row.get("pos_profile")),
        "customer": empty_to_none(row.get("customer")),
        "currency": empty_to_none(row.get("currency")),
        "posting_date": format_date(row.get("posting_date")),
        "posting_time": format_time(row.get("posting_time")),
        "created_at": format_datetime(row.get("creation")),
        "modified_at": format_datetime(row.get("modified")),
        "net_total": money_string(row.get("net_total")),
        "total_taxes_and_charges": money_string(row.get("total_taxes_and_charges")),
        "discount_amount": money_string(row.get("discount_amount")),
        "grand_total": money_string(row.get("grand_total")),
        "rounded_total": money_string(row.get("rounded_total")),
        "paid_amount": money_string(row.get("paid_amount")),
        "change_amount": money_string(row.get("change_amount")),
        "items": [serialize_item_row(item) for item in items],
        "payments": [serialize_payment_row(payment) for payment in payments],
    }


def serialize_order_row(
    row: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    payments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **serialize_invoice_row(row, items=items, payments=payments),
        "status": "voided" if cint(row.get("docstatus")) == 2 else "paid",
    }


def serialize_refund_row(
    row: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    payments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **serialize_invoice_row(row, items=items, payments=payments),
        "return_against": cstr(row.get("return_against")),
        "refund_reason_code": empty_to_none(row.get("custom_kopos_refund_reason_code")),
        "refund_reason": empty_to_none(row.get("custom_kopos_refund_reason")),
    }


def serialize_item_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "idx": cint(row.get("idx")),
        "item_code": cstr(row.get("item_code")),
        "item_name": cstr(row.get("item_name")),
        "description": empty_to_none(row.get("description")),
        "qty": decimal_string(row.get("qty")),
        "rate": money_string(row.get("rate")),
        "amount": money_string(row.get("amount")),
        "net_amount": money_string(row.get("net_amount")),
        "base_rate": money_string(row.get("base_rate")),
        "base_amount": money_string(row.get("base_amount")),
        "discount_amount": money_string(row.get("discount_amount")),
        "warehouse": empty_to_none(row.get("warehouse")),
        "modifier_total": money_string(row.get("custom_kopos_modifier_total")),
        "modifiers": serialize_modifiers_snapshot(row.get("custom_kopos_modifiers")),
    }


def serialize_payment_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "idx": cint(row.get("idx")),
        "mode_of_payment": cstr(row.get("mode_of_payment")),
        "amount": money_string(row.get("amount")),
        "account": empty_to_none(row.get("account")),
        "type": empty_to_none(row.get("type")),
        "default": bool(cint(row.get("default"))),
    }


def get_invoice_fields() -> list[str]:
    return [
        "name",
        "docstatus",
        "company",
        "pos_profile",
        "customer",
        "currency",
        "posting_date",
        "posting_time",
        "creation",
        "modified",
        "custom_kopos_idempotency_key",
        "custom_kopos_device_id",
        "custom_kopos_display_number",
        "custom_fb_order",
        "custom_fb_shift",
        "custom_fb_idempotency_key",
        "custom_fb_device_id",
        "custom_fb_void_idempotency_key",
        "custom_fb_void_request_fingerprint",
        "custom_fb_void_manager",
        "custom_fb_void_approval_token_id",
        "net_total",
        "total_taxes_and_charges",
        "discount_amount",
        "grand_total",
        "rounded_total",
        "paid_amount",
        "change_amount",
        "custom_kopos_refund_reason_code",
        "custom_kopos_refund_reason",
        "remarks",
    ]


def filter_existing_fields(doctype: str, fields: list[str]) -> list[str]:
    meta = frappe.get_meta(doctype)
    document_fields = {
        "name",
        "creation",
        "modified",
        "docstatus",
        "parent",
        "parenttype",
        "idx",
    }
    return [
        fieldname
        for fieldname in fields
        if fieldname in document_fields or meta.has_field(fieldname)
    ]


def get_invoice_item_fields() -> list[str]:
    return [
        "parent",
        "idx",
        "item_code",
        "item_name",
        "description",
        "qty",
        "rate",
        "amount",
        "net_amount",
        "base_rate",
        "base_amount",
        "discount_amount",
        "warehouse",
        "custom_kopos_modifiers",
        "custom_kopos_modifier_total",
        "custom_kopos_has_modifiers",
    ]


def serialize_modifiers_snapshot(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        snapshot = json.loads(cstr(value))
    except (TypeError, ValueError):
        return []
    modifiers = snapshot.get("modifiers") if isinstance(snapshot, dict) else None
    if not isinstance(modifiers, list):
        return []
    rows: list[dict[str, Any]] = []
    for modifier in modifiers:
        if not isinstance(modifier, dict):
            continue
        modifier_id = empty_to_none(modifier.get("id")) or empty_to_none(
            modifier.get("modifier")
        )
        name = empty_to_none(modifier.get("name")) or modifier_id
        if not modifier_id or not name:
            continue
        rows.append(
            {
                "id": modifier_id,
                "name": name,
                "group_id": empty_to_none(modifier.get("group_id"))
                or empty_to_none(modifier.get("modifier_group")),
                "price_adjustment": money_string(
                    modifier.get("price", modifier.get("price_adjustment"))
                ),
            }
        )
    return rows


def extract_display_number_from_remarks(value: Any) -> str | None:
    prefix = "KoPOS display number:"
    for line in cstr(value).splitlines():
        if line.startswith(prefix):
            display_number = line.removeprefix(prefix).strip()
            return display_number if display_number and display_number != "N/A" else None
    return None


def get_invoice_payment_fields() -> list[str]:
    return [
        "parent",
        "idx",
        "mode_of_payment",
        "amount",
        "account",
        "type",
        "default",
    ]


def money_string(value: Any) -> str:
    return decimal_string(value, quantize=MONEY_QUANTUM)


def decimal_string(value: Any, *, quantize: Decimal | None = None) -> str:
    try:
        decimal_value = Decimal(cstr(value) or "0")
    except (InvalidOperation, ValueError):
        decimal_value = Decimal("0")
    if quantize:
        decimal_value = decimal_value.quantize(quantize, rounding=ROUND_HALF_UP)
    return format(decimal_value, "f")


def parse_date_value(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return getdate(value)
    except Exception:
        frappe.throw(_("since_date is invalid"), frappe.ValidationError)
    return None


def format_date(value: Any) -> str | None:
    parsed = parse_date_value(value)
    return parsed.isoformat() if parsed else None


def format_time(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    return cstr(value)


def format_datetime(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else get_datetime(value)
    except Exception:
        frappe.throw(_("Order history timestamp is invalid"), frappe.ValidationError)
        raise AssertionError("frappe.throw did not reject an invalid timestamp")
    if not isinstance(parsed, datetime):
        frappe.throw(_("Order history timestamp is invalid"), frappe.ValidationError)
        raise AssertionError("frappe.throw did not reject an invalid timestamp")

    if parsed.tzinfo is None:
        timezone_name = cstr(get_system_timezone()).strip()
        if not timezone_name:
            frappe.throw(
                _("ERP system timezone is required for order history"),
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw did not reject a missing timezone")
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            frappe.throw(
                _("ERP system timezone {0} is invalid").format(timezone_name),
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw did not reject an invalid timezone")

    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def empty_to_none(value: Any) -> str | None:
    text = cstr(value).strip()
    return text or None
