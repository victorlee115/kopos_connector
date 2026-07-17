# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from typing import Any
from uuid import uuid4

import frappe
from frappe.utils import add_to_date, cint, cstr, now_datetime

from kopos_connector.api._maybank_qr_status import _apply_provider_poll_result
from kopos_connector.services.maybank.client import MaybankClient
from kopos_connector.utils.diagnostics import log_sanitized_error, redacted_json

MIN_POLL_INTERVAL_SECONDS = 2
MAX_POLL_INTERVAL_SECONDS = 15
EXPIRED_POLL_INTERVAL_SECONDS = 60
EXPIRED_LONG_TAIL_INTERVAL_SECONDS = 5 * 60
EXPIRED_ARCHIVE_INTERVAL_SECONDS = 15 * 60

# The scheduler only dispatches work. Live customer transactions and historical
# recovery deliberately use different built-in Frappe queues so a slow provider
# call for an expired QR cannot occupy the worker serving a current checkout.
ACTIVE_POLL_BATCH_SIZE = 100
ACTIVE_POLL_SCAN_BATCH_SIZE = 400
ACTIVE_PENDING_RESERVED_CAPACITY = 25
STALE_POLL_BATCH_SIZE = 10
STALE_POLL_SCAN_BATCH_SIZE = 100
ACTIVE_POLL_QUEUE = "short"
STALE_POLL_QUEUE = "long"
POLL_JOB_TIMEOUT_SECONDS = 3 * 60

# Preserve the historical key across rolling deploys so an old sequential
# scheduler and the new dispatcher cannot overlap during process replacement.
SCHEDULER_LOCK_KEY = "maybank_poll_lock"
SCHEDULER_LOCK_TTL_SECONDS = 10 * 60
TRANSACTION_LOCK_PREFIX = "maybank_poll_transaction_lock"
# The Redis execution lease outlives the RQ timeout. This prevents another worker
# entering the same provider call if RQ terminates a wedged job at its deadline.
TRANSACTION_LOCK_TTL_SECONDS = POLL_JOB_TIMEOUT_SECONDS + 60
LOCK_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
LOCK_REFRESH_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""
GRACE_SECONDS = 30
POLLABLE_STATUSES = frozenset({"pending", "scanned"})
POLL_FIELDS = (
    "name",
    "transaction_refno",
    "status",
    "last_polled_at",
    "created_at",
    "expires_at",
    "poll_count",
    "sale_amount_sen",
    "outlet_id",
    "currency",
    "device_id",
    "provider",
)


def poll_pending_maybank_transactions() -> None:
    """Dispatch current QR polling before bounded long-tail recovery.

    No provider request runs in the scheduler process. Each enqueued job gets a
    fresh Frappe worker/DB context; this avoids sharing a connection across
    threads and lets Frappe/RQ isolate slow stale calls on the long queue.
    """

    cache = frappe.cache()
    lock_key = _site_lock_key(SCHEDULER_LOCK_KEY)
    lock_token = _acquire_lock(
        cache,
        lock_key,
        ttl_seconds=SCHEDULER_LOCK_TTL_SECONDS,
    )
    if not lock_token:
        return

    try:
        poll_now = now_datetime()
        heartbeat = lambda: _refresh_lock(
            cache,
            lock_key,
            lock_token,
            ttl_seconds=SCHEDULER_LOCK_TTL_SECONDS,
        )

        scanned, pending = _load_due_active_transactions(poll_now)
        if not _dispatch_poll_jobs(
            [*scanned, *pending],
            lane="active",
            heartbeat=heartbeat,
        ):
            return

        # Long-tail work has a separate, deliberately small budget and queue.
        # It is selected only after every current job has been dispatched.
        stale = _load_due_stale_transactions(poll_now)
        _dispatch_poll_jobs(stale, lane="stale", heartbeat=heartbeat)
    finally:
        _release_lock(cache, lock_key, lock_token)


def poll_maybank_transaction(transaction_name: str, lane: str) -> dict[str, str]:
    """Poll one transaction in an isolated Frappe/RQ worker.

    RQ deduplication prevents queued/running duplicates. The Redis lease closes
    the remaining race between different scheduler ticks, while the fresh due
    check suppresses a second provider request after an earlier job completes.
    The provider transition itself remains monotonic and row-locked in
    ``_apply_provider_poll_result``.
    """

    resolved_name = cstr(transaction_name).strip()
    resolved_lane = cstr(lane).strip().lower()
    if not resolved_name:
        raise ValueError("Maybank QR transaction name is required")
    if resolved_lane not in {"active", "stale"}:
        raise ValueError("Maybank QR poll lane must be active or stale")

    cache = frappe.cache()
    lock_key = _transaction_lock_key(resolved_name)
    lock_token = _acquire_lock(
        cache,
        lock_key,
        ttl_seconds=TRANSACTION_LOCK_TTL_SECONDS,
    )
    if not lock_token:
        return {"status": "already_running", "transaction_name": resolved_name}

    try:
        transaction = _load_transaction_snapshot(resolved_name)
        if transaction is None:
            return {"status": "not_found", "transaction_name": resolved_name}
        if cstr(_value(transaction, "status")).strip() not in POLLABLE_STATUSES:
            return {"status": "not_pollable", "transaction_name": resolved_name}
        if cstr(_value(transaction, "provider")).strip().lower() != "maybank_qr":
            return {"status": "invalid_provider", "transaction_name": resolved_name}

        current_now = now_datetime()
        if not _is_poll_due(transaction, current_now):
            return {"status": "not_due", "transaction_name": resolved_name}

        try:
            client = MaybankClient.from_settings()
            if not _refresh_lock(
                cache,
                lock_key,
                lock_token,
                ttl_seconds=TRANSACTION_LOCK_TTL_SECONDS,
            ):
                return {"status": "lease_lost", "transaction_name": resolved_name}
            _poll_single(client, transaction, now=current_now)
        except Exception as error:
            _commit_poll_writes_or_rollback()
            log_sanitized_error(
                f"Maybank {resolved_lane} poll failed: {resolved_name}",
                error,
            )
            return {"status": "provider_error", "transaction_name": resolved_name}

        frappe.db.commit()
        return {"status": "polled", "transaction_name": resolved_name}
    finally:
        _release_lock(cache, lock_key, lock_token)


def _load_due_active_transactions(now: Any) -> tuple[list[Any], list[Any]]:
    cutoff = add_to_date(now, seconds=-GRACE_SECONDS)
    shared_filters: dict[str, Any] = {
        "provider": "maybank_qr",
        "expires_at": [">", cutoff],
    }
    scanned_candidates = _get_poll_candidates(
        filters={**shared_filters, "status": "scanned"},
        limit=ACTIVE_POLL_SCAN_BATCH_SIZE,
    )
    pending_candidates = _get_poll_candidates(
        filters={**shared_filters, "status": "pending"},
        limit=ACTIVE_POLL_SCAN_BATCH_SIZE,
    )
    due_scanned = [row for row in scanned_candidates if _is_poll_due(row, now)]
    due_pending = [row for row in pending_candidates if _is_poll_due(row, now)]

    # Scanned payments are served first, while a reserved slice lets pending QR
    # rows advance to scanned/paid even during a sustained scanned backlog.
    pending_reserve = min(len(due_pending), ACTIVE_PENDING_RESERVED_CAPACITY)
    scanned_limit = ACTIVE_POLL_BATCH_SIZE - pending_reserve
    selected_scanned = due_scanned[:scanned_limit]
    remaining = ACTIVE_POLL_BATCH_SIZE - len(selected_scanned)
    selected_pending = due_pending[:remaining]
    return selected_scanned, selected_pending


def _load_due_stale_transactions(now: Any) -> list[Any]:
    cutoff = add_to_date(now, seconds=-GRACE_SECONDS)
    candidates = _get_poll_candidates(
        filters={
            "provider": "maybank_qr",
            "status": ["in", sorted(POLLABLE_STATUSES)],
            "expires_at": ["<=", cutoff],
        },
        limit=STALE_POLL_SCAN_BATCH_SIZE,
    )
    return [
        transaction
        for transaction in candidates
        if _is_poll_due(transaction, now)
    ][:STALE_POLL_BATCH_SIZE]


def _get_poll_candidates(*, filters: dict[str, Any], limit: int) -> list[Any]:
    return frappe.get_all(
        "Maybank QR Transaction",
        filters=filters,
        fields=list(POLL_FIELDS),
        order_by="last_polled_at asc, expires_at asc",
        limit=limit,
    )


def _dispatch_poll_jobs(
    transactions: Sequence[Any],
    *,
    lane: str,
    heartbeat: Callable[[], bool],
) -> bool:
    for transaction in transactions:
        transaction_name = cstr(_value(transaction, "name")).strip()
        if not transaction_name:
            continue
        try:
            _enqueue_poll_job(
                transaction_name,
                lane=lane,
            )
        except Exception as error:
            # An overloaded or temporarily unavailable queue must not stop other
            # current transactions from being dispatched.
            log_sanitized_error(
                f"Maybank {lane} poll dispatch failed: {transaction_name}",
                error,
            )
        if not heartbeat():
            return False
    return True


def _enqueue_poll_job(
    transaction_name: str,
    *,
    lane: str,
) -> Any:
    digest = hashlib.sha256(transaction_name.encode("utf-8")).hexdigest()
    # Keep FIFO inside each lane. Scanned rows are enqueued first by the
    # scheduler, while avoiding LIFO insertion that could starve older current
    # payments during a sustained rush.
    return frappe.enqueue(
        "kopos_connector.tasks.poll_maybank.poll_maybank_transaction",
        queue=ACTIVE_POLL_QUEUE if lane == "active" else STALE_POLL_QUEUE,
        timeout=POLL_JOB_TIMEOUT_SECONDS,
        job_id=f"kopos-maybank-poll-{digest}",
        deduplicate=True,
        transaction_name=transaction_name,
        lane=lane,
    )


def _load_transaction_snapshot(transaction_name: str) -> Any | None:
    return frappe.db.get_value(
        "Maybank QR Transaction",
        transaction_name,
        list(POLL_FIELDS),
        as_dict=True,
    )


def _site_lock_key(prefix: str) -> str:
    site = cstr(getattr(frappe.local, "site", None)).strip() or "default-site"
    return f"{prefix}:{site}"


def _transaction_lock_key(transaction_name: str) -> str:
    digest = hashlib.sha256(transaction_name.encode("utf-8")).hexdigest()
    return _site_lock_key(f"{TRANSACTION_LOCK_PREFIX}:{digest}")


def _acquire_lock(
    cache: object,
    lock_key: str,
    *,
    ttl_seconds: int = SCHEDULER_LOCK_TTL_SECONDS,
) -> str | None:
    token = uuid4().hex
    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()

    if (
        redis_client
        and hasattr(redis_client, "set")
        and hasattr(redis_client, "eval")
    ):
        acquired = redis_client.set(lock_key, token, ex=ttl_seconds, nx=True)
        return token if acquired else None

    frappe.log_error(
        "Maybank poll lock requires Redis atomic set", "Maybank poll lock unavailable"
    )
    return None


def _release_lock(cache: object, lock_key: str, token: str) -> None:
    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()

    if redis_client and hasattr(redis_client, "eval"):
        try:
            redis_client.eval(LOCK_RELEASE_SCRIPT, 1, lock_key, token)
        except Exception as error:
            log_sanitized_error("Maybank poll lock release failed", error)


def _refresh_lock(
    cache: object,
    lock_key: str,
    token: str,
    *,
    ttl_seconds: int = SCHEDULER_LOCK_TTL_SECONDS,
) -> bool:
    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()
    if not redis_client or not hasattr(redis_client, "eval"):
        return False
    try:
        refreshed = redis_client.eval(
            LOCK_REFRESH_SCRIPT,
            1,
            lock_key,
            token,
            ttl_seconds,
        )
    except Exception as error:
        log_sanitized_error("Maybank poll lock refresh failed", error)
        return False
    return bool(cint(refreshed))


def _commit_poll_writes_or_rollback() -> None:
    """Persist validated attempt evidence while releasing per-transaction locks."""
    try:
        frappe.db.commit()
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("Maybank poll attempt persistence failed", error)


def _minimum_poll_interval_seconds(txn: Any, now: Any = None) -> int:
    current_now = now or now_datetime()
    expires_at = _value(txn, "expires_at")
    if expires_at and current_now > expires_at:
        expired_age = (current_now - expires_at).total_seconds()
        if expired_age >= 24 * 60 * 60:
            return EXPIRED_ARCHIVE_INTERVAL_SECONDS
        if expired_age >= 60 * 60:
            return EXPIRED_LONG_TAIL_INTERVAL_SECONDS
        return EXPIRED_POLL_INTERVAL_SECONDS
    status = cstr(_value(txn, "status"))
    base_interval = 1 if status == "scanned" else MIN_POLL_INTERVAL_SECONDS
    extra_delay = min(
        MAX_POLL_INTERVAL_SECONDS - base_interval,
        max(0, cint(_value(txn, "poll_count")) // 5),
    )
    return base_interval + extra_delay


def _is_poll_due(txn: Any, now: Any) -> bool:
    last_polled_at = _value(txn, "last_polled_at")
    if not last_polled_at:
        return True
    elapsed = (now - last_polled_at).total_seconds()
    return elapsed >= _minimum_poll_interval_seconds(txn, now)


def _touch_poll_attempt(txn_name: str, now: Any, payload: object) -> None:
    frappe.db.sql(
        """
        UPDATE `tabMaybank QR Transaction`
        SET last_polled_at = %s,
            poll_count = poll_count + 1,
            raw_response = %s
        WHERE name = %s
        """,
        (now, redacted_json(payload), txn_name),
    )


def _poll_single(client: MaybankClient, txn: Any, now: Any = None) -> None:
    current_now = now or now_datetime()

    last_polled_at = _value(txn, "last_polled_at")
    if last_polled_at:
        elapsed = (current_now - last_polled_at).total_seconds()
        if elapsed < _minimum_poll_interval_seconds(txn, current_now):
            return

    try:
        result = client.check_status(cstr(_value(txn, "transaction_refno")))
    except Exception:
        _touch_poll_attempt(
            cstr(_value(txn, "name")),
            current_now,
            {"status": "error", "message": "poll request failed"},
        )
        raise

    _apply_provider_poll_result(cstr(_value(txn, "name")), result)


def _value(source: Any, fieldname: str) -> Any:
    if isinstance(source, dict):
        return source.get(fieldname)
    return getattr(source, fieldname, None)
