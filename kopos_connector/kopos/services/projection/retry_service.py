# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import uuid4

import frappe
from frappe.utils import add_to_date, cint, cstr, get_datetime, now_datetime

from kopos_connector.utils.diagnostics import log_sanitized_error


DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_RETRIES = 8
BASE_RETRY_DELAY_SECONDS = 5
MAX_RETRY_DELAY_SECONDS = 15 * 60
RETRY_LOCK_TTL_SECONDS = 5 * 60
RETRY_LEASE_SECONDS = 2 * 60
RETRY_LOCK_KEY = "kopos:projection-retry-worker:v1"
COMPARE_AND_DELETE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def retry_projection_failures(
    *,
    force: bool = False,
    batch_size: int | None = None,
) -> list[dict[str, Any]]:
    """Retry a bounded batch of failed FB Order projections.

    A Redis lease prevents scheduler fan-out, while the projection row itself
    is claimed durably before the handler runs.  Every projection handler is
    idempotent and the projection log is row-locked again at execution time, so
    an overlapping tablet-initiated retry can only observe/reuse the result.
    """

    cache = frappe.cache()
    lock_token = _acquire_worker_lock(cache)
    if not lock_token:
        return []

    try:
        return _retry_projection_batch(
            force=force,
            batch_size=_bounded_batch_size(batch_size),
        )
    finally:
        _release_worker_lock(cache, lock_token)


def _retry_projection_batch(
    *,
    force: bool,
    batch_size: int,
) -> list[dict[str, Any]]:
    now = now_datetime()
    max_retries = _configured_positive_int(
        "kopos_projection_retry_max_attempts",
        DEFAULT_MAX_RETRIES,
        minimum=1,
        maximum=50,
    )
    candidates = frappe.get_all(
        "FB Projection Log",
        filters={
            "source_doctype": "FB Order",
            "state": "Failed",
        },
        fields=[
            "name",
            "source_name",
            "projection_type",
            "retry_count",
            "last_attempt_at",
            "next_retry_at",
            "lease_expires_at",
            "dead_lettered_at",
        ],
        order_by="last_attempt_at asc, creation asc",
        limit_page_length=max(batch_size * 4, batch_size),
    )

    results: list[dict[str, Any]] = []
    for row in candidates:
        if len(results) >= batch_size:
            break
        retry_count = cint(_row_value(row, "retry_count"))
        if retry_count >= max_retries and not force:
            _mark_dead_letter(row, now, max_retries)
            continue
        if _row_value(row, "dead_lettered_at") and not force:
            continue
        if not force and not _is_retry_due(row, now):
            continue

        log_name = cstr(_row_value(row, "name")).strip()
        if not log_name:
            continue
        lease_token = uuid4().hex
        if not _claim_projection(log_name, lease_token, now):
            continue

        try:
            from kopos_connector.kopos.api.fb_orders import _retry_projection_log

            result = _retry_projection_log(log_name)
            _finalize_projection_attempt(
                log_name=log_name,
                lease_token=lease_token,
                result=result,
                now=now_datetime(),
                max_retries=max_retries,
            )
            results.append(result)
        except Exception as error:
            frappe.db.rollback()
            _release_failed_claim(
                log_name=log_name,
                lease_token=lease_token,
                error=error,
                now=now_datetime(),
                max_retries=max_retries,
            )
            results.append(
                {
                    "projection_log": log_name,
                    "projection_type": cstr(
                        _row_value(row, "projection_type")
                    ),
                    "state": "Failed",
                    "target_name": None,
                    "error": "projection retry raised before a terminal result",
                }
            )
    return results


def _claim_projection(log_name: str, lease_token: str, now: datetime) -> bool:
    lease_expires_at = add_to_date(now, seconds=RETRY_LEASE_SECONDS)
    frappe.db.sql(
        """
        UPDATE `tabFB Projection Log`
        SET lease_token = %s,
            lease_expires_at = %s
        WHERE name = %s
          AND state = 'Failed'
          AND (
            lease_token IS NULL
            OR lease_token = ''
            OR lease_expires_at IS NULL
            OR lease_expires_at <= %s
          )
        """,
        (lease_token, lease_expires_at, log_name, now),
    )
    claimed_token = frappe.db.get_value(
        "FB Projection Log", log_name, "lease_token"
    )
    if cstr(claimed_token) != lease_token:
        frappe.db.rollback()
        return False
    # Persist the lease independently.  A process death after this point leaves
    # a bounded, expiring claim instead of an indefinitely stuck row.
    frappe.db.commit()
    return True


def _finalize_projection_attempt(
    *,
    log_name: str,
    lease_token: str,
    result: dict[str, Any],
    now: datetime,
    max_retries: int,
) -> None:
    current_token = cstr(
        frappe.db.get_value("FB Projection Log", log_name, "lease_token")
    )
    if current_token != lease_token:
        frappe.db.rollback()
        raise RuntimeError(
            f"Projection retry lease was lost while finalizing {log_name}"
        )

    state = cstr(result.get("state"))
    retry_count = cint(
        frappe.db.get_value("FB Projection Log", log_name, "retry_count")
    )
    updates: dict[str, Any] = {
        "lease_token": None,
        "lease_expires_at": None,
        "next_retry_at": None,
    }
    if state == "Succeeded":
        updates["dead_lettered_at"] = None
    elif retry_count >= max_retries:
        updates["dead_lettered_at"] = now
    else:
        updates["next_retry_at"] = add_to_date(
            now,
            seconds=_retry_delay_seconds(log_name, retry_count),
        )
    frappe.db.set_value(
        "FB Projection Log",
        log_name,
        updates,
        update_modified=False,
    )
    frappe.db.commit()
    if retry_count >= max_retries and state != "Succeeded":
        _log_dead_letter(log_name, retry_count)


def _release_failed_claim(
    *,
    log_name: str,
    lease_token: str,
    error: Exception,
    now: datetime,
    max_retries: int,
) -> None:
    retry_count = cint(
        frappe.db.get_value("FB Projection Log", log_name, "retry_count")
    )
    next_retry_count = max(1, retry_count + 1)
    updates: dict[str, Any] = {
        "state": "Failed",
        "retry_count": next_retry_count,
        "lease_token": None,
        "lease_expires_at": None,
        "last_error": str(error)[:1000],
        "last_attempt_at": now,
    }
    if next_retry_count >= max_retries:
        updates["dead_lettered_at"] = now
        updates["next_retry_at"] = None
    else:
        updates["next_retry_at"] = add_to_date(
            now,
            seconds=_retry_delay_seconds(log_name, next_retry_count),
        )
    current_token = cstr(
        frappe.db.get_value("FB Projection Log", log_name, "lease_token")
    )
    if current_token != lease_token:
        log_sanitized_error(
            f"Projection retry lease was lost after failure for {log_name}"
        )
        return
    frappe.db.set_value(
        "FB Projection Log",
        log_name,
        updates,
        update_modified=False,
    )
    frappe.db.commit()
    log_sanitized_error(f"Projection retry failed for {log_name}", error)
    if next_retry_count >= max_retries:
        _log_dead_letter(log_name, next_retry_count)


def _mark_dead_letter(row: Any, now: datetime, max_retries: int) -> None:
    log_name = cstr(_row_value(row, "name")).strip()
    if not log_name:
        return

    # Re-read under the row lock before applying a terminal support marker.
    # The candidate scan is intentionally unlocked and can be stale by the time
    # a tablet-initiated retry finishes.  Never mark a projection dead-letter
    # after that concurrent retry has already succeeded, and never steal a live
    # worker's claim merely because the global scheduler lease expired.
    locked_rows = frappe.db.sql(
        """
        SELECT
            name, state, retry_count, dead_lettered_at,
            lease_token, lease_expires_at
        FROM `tabFB Projection Log`
        WHERE name = %s
        LIMIT 1
        FOR UPDATE
        """,
        (log_name,),
        as_dict=True,
    )
    if not locked_rows:
        return
    locked = locked_rows[0]
    if (
        cstr(_row_value(locked, "state")) != "Failed"
        or cint(_row_value(locked, "retry_count")) < max_retries
        or _row_value(locked, "dead_lettered_at")
    ):
        return
    lease_expires_at = _optional_datetime(
        _row_value(locked, "lease_expires_at")
    )
    if lease_expires_at and lease_expires_at > now:
        return

    frappe.db.set_value(
        "FB Projection Log",
        log_name,
        {
            "dead_lettered_at": now,
            "next_retry_at": None,
            "lease_token": None,
            "lease_expires_at": None,
        },
        update_modified=False,
    )
    frappe.db.commit()
    _log_dead_letter(log_name, max_retries)


def _is_retry_due(row: Any, now: datetime) -> bool:
    lease_expires_at = _optional_datetime(_row_value(row, "lease_expires_at"))
    if lease_expires_at and lease_expires_at > now:
        return False
    explicit_next_retry = _optional_datetime(_row_value(row, "next_retry_at"))
    if explicit_next_retry:
        return explicit_next_retry <= now
    last_attempt_at = _optional_datetime(_row_value(row, "last_attempt_at"))
    if not last_attempt_at:
        return True
    retry_count = max(1, cint(_row_value(row, "retry_count")))
    due_at = add_to_date(
        last_attempt_at,
        seconds=_retry_delay_seconds(
            cstr(_row_value(row, "name")), retry_count
        ),
    )
    return due_at <= now


def _retry_delay_seconds(log_name: str, retry_count: int) -> int:
    exponent = max(0, min(int(retry_count), 20) - 1)
    base_delay = min(
        MAX_RETRY_DELAY_SECONDS,
        BASE_RETRY_DELAY_SECONDS * (2**exponent),
    )
    # Stable jitter avoids a thundering herd after an ERP outage while keeping
    # retries deterministic and supportable for a given projection.
    digest = hashlib.sha256(
        f"{log_name}:{retry_count}".encode("utf-8")
    ).digest()
    jitter_cap = max(1, base_delay // 4)
    jitter = int.from_bytes(digest[:4], "big") % (jitter_cap + 1)
    return min(MAX_RETRY_DELAY_SECONDS, base_delay + jitter)


def _bounded_batch_size(batch_size: int | None) -> int:
    configured = batch_size or _configured_positive_int(
        "kopos_projection_retry_batch_size",
        DEFAULT_BATCH_SIZE,
        minimum=1,
        maximum=100,
    )
    return max(1, min(int(configured), 100))


def _configured_positive_int(
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    config = getattr(frappe, "conf", None)
    value = None
    if config is not None:
        getter = getattr(config, "get", None)
        value = getter(key) if callable(getter) else getattr(config, key, None)
    parsed = cint(value)
    if parsed < minimum:
        parsed = default
    return max(minimum, min(parsed, maximum))


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return get_datetime(value)


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(fieldname)
    return getattr(row, fieldname, None)


def _acquire_worker_lock(cache: Any) -> str | None:
    token = uuid4().hex
    redis_client = _resolve_redis_client(cache)
    if redis_client is not None and hasattr(redis_client, "set"):
        acquired = redis_client.set(
            RETRY_LOCK_KEY,
            token,
            ex=RETRY_LOCK_TTL_SECONDS,
            nx=True,
        )
        return token if acquired else None
    log_sanitized_error(
        "Projection retry worker requires an atomic Redis lease"
    )
    return None


def _release_worker_lock(cache: Any, token: str) -> None:
    redis_client = _resolve_redis_client(cache)
    if redis_client is None or not hasattr(redis_client, "eval"):
        # Never use a get-then-delete fallback here.  The lease can expire
        # between those two commands, allowing an old worker to delete a new
        # worker's lease.  Leaving the short-lived key to expire is safe.
        return
    redis_client.eval(
        COMPARE_AND_DELETE_LUA,
        1,
        RETRY_LOCK_KEY,
        token,
    )


def _resolve_redis_client(cache: Any) -> Any | None:
    """Accept both Frappe's direct RedisWrapper and older wrapped caches."""

    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()
    return redis_client if redis_client is not None else cache


def _log_dead_letter(log_name: str, retry_count: int) -> None:
    frappe.log_error(
        title="KoPOS projection requires manual recovery",
        message=(
            f"Projection {log_name} exhausted {retry_count} automatic retries; "
            "shift close remains blocked until the supported retry workflow succeeds"
        ),
    )
