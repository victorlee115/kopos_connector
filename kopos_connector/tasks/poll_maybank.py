# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from typing import Any
from uuid import uuid4

import frappe
from frappe.utils import add_to_date, cint, cstr, now_datetime

from kopos_connector.api._maybank_qr_persistence import (
    _durable_generation_release,
    _record_poll_observation,
)
from kopos_connector.api._maybank_qr_status import _apply_provider_poll_result
from kopos_connector.services.maybank.client import MaybankClient
from kopos_connector.utils.diagnostics import log_sanitized_error

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
LATE_SETTLEMENT_POLLABLE_STATUSES = frozenset(
    {"pending", "scanned", "failed", "timeout", "unknown"}
)
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
    "idempotency_key",
    "request_fingerprint",
    "maybank_status",
    "qr_data",
    "raw_response",
    "fb_order",
    "fb_order_payment",
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
        long_tail = _load_due_stale_transactions(poll_now)
        all_due = _deduplicate_attempts([*scanned, *pending, *long_tail])
        cutoff = add_to_date(poll_now, seconds=-GRACE_SECONDS)
        active = [
            transaction
            for transaction in all_due
            if _attempt_in_lane(transaction, cutoff=cutoff, lane="active")
        ]
        stale = [
            transaction
            for transaction in all_due
            if _attempt_in_lane(transaction, cutoff=cutoff, lane="stale")
        ]
        if not _dispatch_poll_jobs(
            active,
            lane="active",
            heartbeat=heartbeat,
        ):
            return

        # Long-tail work has a separate, deliberately small budget and queue.
        # Selection happens up front so a logical sale can expand to siblings
        # in both lanes during one scheduler tick. Dispatch still gives every
        # current attempt priority over the expired recovery queue.
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
        if not is_maybank_attempt_pollable(transaction):
            return {"status": "not_pollable", "transaction_name": resolved_name}
        if cstr(_value(transaction, "provider")).strip().lower() != "maybank_qr":
            return {"status": "invalid_provider", "transaction_name": resolved_name}

        current_now = now_datetime()
        if not is_maybank_poll_due(transaction, current_now):
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
        filters={
            **shared_filters,
            "status": [
                "in",
                sorted(LATE_SETTLEMENT_POLLABLE_STATUSES - {"scanned"}),
            ],
        },
        limit=ACTIVE_POLL_SCAN_BATCH_SIZE,
    )
    due_scanned = [
        row
        for row in scanned_candidates
        if is_maybank_attempt_pollable(row) and is_maybank_poll_due(row, now)
    ]
    due_pending = [
        row
        for row in pending_candidates
        if is_maybank_attempt_pollable(row) and is_maybank_poll_due(row, now)
    ]

    # Scanned payments are served first, while a reserved slice lets pending QR
    # rows advance to scanned/paid even during a sustained scanned backlog.
    pending_reserve = min(
        len({_selection_identity(row) for row in due_pending}),
        ACTIVE_PENDING_RESERVED_CAPACITY,
    )
    scanned_limit = ACTIVE_POLL_BATCH_SIZE - pending_reserve
    selected_scanned, selected_identities = _select_cohort_seeds(
        due_scanned,
        scanned_limit,
    )
    remaining = ACTIVE_POLL_BATCH_SIZE - len(selected_identities)
    selected_pending, selected_identities = _select_cohort_seeds(
        due_pending,
        remaining,
        selected_identities=selected_identities,
    )
    expanded = _expand_selected_sale_attempts(
        [*selected_scanned, *selected_pending],
        now=now,
    )
    return (
        [
            transaction
            for transaction in expanded
            if cstr(_value(transaction, "status")).strip() == "scanned"
        ],
        [
            transaction
            for transaction in expanded
            if cstr(_value(transaction, "status")).strip() != "scanned"
        ],
    )


def _load_due_stale_transactions(now: Any) -> list[Any]:
    cutoff = add_to_date(now, seconds=-GRACE_SECONDS)
    candidates = _get_poll_candidates(
        filters={
            "provider": "maybank_qr",
            "status": ["in", sorted(LATE_SETTLEMENT_POLLABLE_STATUSES)],
            "expires_at": ["<=", cutoff],
        },
        limit=STALE_POLL_SCAN_BATCH_SIZE,
    )
    due = [
        transaction
        for transaction in candidates
        if is_maybank_attempt_pollable(transaction)
        and is_maybank_poll_due(transaction, now)
    ]
    selected, _selected_identities = _select_cohort_seeds(
        due,
        STALE_POLL_BATCH_SIZE,
    )
    return _expand_selected_sale_attempts(
        selected,
        now=now,
    )


def _get_poll_candidates(*, filters: dict[str, Any], limit: int) -> list[Any]:
    return frappe.get_all(
        "Maybank QR Transaction",
        filters=filters,
        fields=list(POLL_FIELDS),
        order_by="last_polled_at asc, expires_at asc",
        limit=limit,
    )


def _sale_attempt_key(transaction: Any) -> tuple[str, str] | None:
    fb_order = cstr(_value(transaction, "fb_order")).strip()
    fb_order_payment = cstr(
        _value(transaction, "fb_order_payment")
    ).strip()
    if not fb_order or not fb_order_payment:
        return None
    return fb_order, fb_order_payment


def _selection_identity(transaction: Any) -> tuple[str, str, str]:
    sale_key = _sale_attempt_key(transaction)
    if sale_key is not None:
        return "sale", sale_key[0], sale_key[1]
    return "transaction", cstr(_value(transaction, "name")).strip(), ""


def _select_cohort_seeds(
    candidates: Sequence[Any],
    capacity: int,
    *,
    selected_identities: set[tuple[str, str, str]] | None = None,
) -> tuple[list[Any], set[tuple[str, str, str]]]:
    identities = set(selected_identities or set())
    selected: list[Any] = []
    added = 0
    for candidate in candidates:
        identity = _selection_identity(candidate)
        if not identity[1] or identity in identities:
            continue
        if added >= capacity:
            break
        identities.add(identity)
        selected.append(candidate)
        added += 1
    return selected, identities


def _load_linked_poll_attempts(
    sale_keys: set[tuple[str, str]],
) -> list[Any]:
    if not sale_keys:
        return []
    ordered_keys = sorted(sale_keys)
    status_placeholders = ", ".join(
        ["%s"] * len(LATE_SETTLEMENT_POLLABLE_STATUSES)
    )
    sale_clauses = " OR ".join(
        ["(fb_order = %s AND fb_order_payment = %s)"] * len(ordered_keys)
    )
    fields = ", ".join(POLL_FIELDS)
    params: list[Any] = [
        "maybank_qr",
        *sorted(LATE_SETTLEMENT_POLLABLE_STATUSES),
    ]
    for fb_order, fb_order_payment in ordered_keys:
        params.extend((fb_order, fb_order_payment))
    return list(
        frappe.db.sql(
            f"""
            SELECT {fields}
            FROM `tabMaybank QR Transaction`
            WHERE provider = %s
              AND status IN ({status_placeholders})
              AND ({sale_clauses})
            ORDER BY last_polled_at ASC, expires_at ASC, creation ASC, name ASC
            """,
            tuple(params),
            as_dict=True,
        )
        or []
    )


def _attempt_in_lane(transaction: Any, *, cutoff: Any, lane: str) -> bool:
    expires_at = _value(transaction, "expires_at")
    if not expires_at:
        return False
    if lane == "active":
        return expires_at > cutoff
    return expires_at <= cutoff


def _expand_selected_sale_attempts(
    selected: Sequence[Any],
    *,
    now: Any,
) -> list[Any]:
    """Expand selected rows to every due sibling for each logical payment.

    A regenerated QR can expire before its newer sibling. Lane assignment is
    therefore deliberately deferred until after expansion so both references
    are dispatched during the same scheduler tick on their appropriate queues.
    """

    if not selected:
        return []
    sale_keys = {
        key
        for transaction in selected
        if (key := _sale_attempt_key(transaction)) is not None
    }
    linked = _load_linked_poll_attempts(sale_keys)
    allowed_sale_keys = sale_keys
    selected_names = {
        cstr(_value(transaction, "name")).strip()
        for transaction in selected
        if cstr(_value(transaction, "name")).strip()
    }
    seen: set[str] = set()
    expanded: list[Any] = []
    for transaction in [*selected, *linked]:
        transaction_name = cstr(_value(transaction, "name")).strip()
        sale_key = _sale_attempt_key(transaction)
        if (
            not transaction_name
            or transaction_name in seen
            or (
                sale_key not in allowed_sale_keys
                and transaction_name not in selected_names
            )
            or not is_maybank_attempt_pollable(transaction)
            or not is_maybank_poll_due(transaction, now)
        ):
            continue
        seen.add(transaction_name)
        expanded.append(transaction)
    # A scanned attempt gets provider capacity first, while every sibling is
    # still enqueued during the same scheduler pass.
    return [
        transaction
        for transaction in expanded
        if cstr(_value(transaction, "status")).strip() == "scanned"
    ] + [
        transaction
        for transaction in expanded
        if cstr(_value(transaction, "status")).strip() != "scanned"
    ]


def _deduplicate_attempts(transactions: Sequence[Any]) -> list[Any]:
    seen: set[str] = set()
    unique: list[Any] = []
    for transaction in transactions:
        transaction_name = cstr(_value(transaction, "name")).strip()
        if not transaction_name or transaction_name in seen:
            continue
        seen.add(transaction_name)
        unique.append(transaction)
    return unique


def enqueue_linked_maybank_sale_poll_attempts(
    fb_order: str,
    fb_order_payment: str,
    *,
    exclude_transaction_name: str | None = None,
) -> int:
    """Queue every due provider-issued attempt for one prepared payment.

    Each attempt has a deterministic RQ job and its own Redis execution lease,
    so different provider references may run concurrently while the same
    reference cannot be checked twice.
    """

    resolved_order = cstr(fb_order).strip()
    resolved_payment = cstr(fb_order_payment).strip()
    excluded_name = cstr(exclude_transaction_name).strip()
    if not resolved_order or not resolved_payment:
        raise ValueError("Maybank linked sale identity is required")
    current_now = now_datetime()
    cutoff = add_to_date(current_now, seconds=-GRACE_SECONDS)
    attempts = _load_linked_poll_attempts(
        {(resolved_order, resolved_payment)}
    )
    dispatched = 0
    for transaction in attempts:
        if not is_maybank_attempt_pollable(
            transaction
        ) or not is_maybank_poll_due(transaction, current_now):
            continue
        lane = (
            "active"
            if _attempt_in_lane(transaction, cutoff=cutoff, lane="active")
            else "stale"
        )
        transaction_name = cstr(_value(transaction, "name")).strip()
        if not transaction_name or transaction_name == excluded_name:
            continue
        try:
            _enqueue_poll_job(transaction_name, lane=lane)
            dispatched += 1
        except Exception as error:
            log_sanitized_error(
                f"Maybank {lane} linked-attempt poll dispatch failed: {transaction_name}",
                error,
            )
    return dispatched


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
    redis_client = _resolve_redis_client(cache)

    if (
        redis_client is not None
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
    redis_client = _resolve_redis_client(cache)

    if redis_client is not None and hasattr(redis_client, "eval"):
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
    redis_client = _resolve_redis_client(cache)
    if redis_client is None or not hasattr(redis_client, "eval"):
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


def _resolve_redis_client(cache: object) -> Any | None:
    """Accept both Frappe's direct RedisWrapper and older wrapped caches."""

    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()
    return redis_client if redis_client is not None else cache


def _commit_poll_writes_or_rollback() -> None:
    """Persist validated attempt evidence while releasing per-transaction locks."""
    try:
        frappe.db.commit()
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("Maybank poll attempt persistence failed", error)


def is_maybank_attempt_pollable(transaction: Any) -> bool:
    """Return whether ERP must keep checking this provider-issued attempt."""

    if cstr(_value(transaction, "provider")).strip().lower() != "maybank_qr":
        return False
    if (
        cstr(_value(transaction, "status")).strip()
        not in LATE_SETTLEMENT_POLLABLE_STATUSES
    ):
        return False
    reference = cstr(_value(transaction, "transaction_refno")).strip()
    if (
        not reference
        or reference.startswith("REQUEST-")
        or reference.lower().startswith("static-")
    ):
        return False
    try:
        # An exact pre-provider rejection/absence or audited provider
        # cancellation is release authority and must not be polled. A provider
        # failed/timeout response without that durable proof remains eligible
        # for late paid truth.
        if _durable_generation_release(transaction) is not None:
            return False
    except Exception as error:
        log_sanitized_error(
            "Maybank poll eligibility evidence is invalid",
            error,
        )
        return False
    return True


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


def is_maybank_poll_due(transaction: Any, now: Any) -> bool:
    """Public policy helper shared by scheduler and status-read compatibility."""

    return _is_poll_due(transaction, now)


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
        # A transport failure is an observation, not stronger provider
        # evidence. Preserve the last authenticated payload for settlement and
        # support while still advancing the poll backoff counters.
        _record_poll_observation(cstr(_value(txn, "name")))
        raise

    _apply_provider_poll_result(cstr(_value(txn, "name")), result)


def _value(source: Any, fieldname: str) -> Any:
    if isinstance(source, dict):
        return source.get(fieldname)
    return getattr(source, fieldname, None)
