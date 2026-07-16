# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from kopos_connector.api.maybank_qr import (
    _apply_provider_poll_result,
)
from kopos_connector.services.maybank.client import MaybankClient
from kopos_connector.utils.diagnostics import log_sanitized_error, redacted_json

MIN_POLL_INTERVAL_SECONDS = 2
MAX_POLL_INTERVAL_SECONDS = 15
EXPIRED_POLL_INTERVAL_SECONDS = 60
EXPIRED_LONG_TAIL_INTERVAL_SECONDS = 5 * 60
EXPIRED_ARCHIVE_INTERVAL_SECONDS = 15 * 60
POLL_BATCH_SIZE = 100
POLL_SCAN_BATCH_SIZE = 400
LOCK_KEY = "maybank_poll_lock"
LOCK_TTL_SECONDS = 10 * 60
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


def poll_pending_maybank_transactions() -> None:
    """Batch poll pending QR transactions under one renewable site lock."""
    cache = frappe.cache()
    lock_key = f"{LOCK_KEY}:{getattr(frappe.local, 'site', 'default-site')}"
    lock_token = _acquire_lock(cache, lock_key)
    if not lock_token:
        return

    try:
        try:
            client = MaybankClient.from_settings()
        except Exception as error:
            log_sanitized_error("Maybank poll: failed to init client", error)
            return

        poll_now = now_datetime()
        heartbeat = lambda: _refresh_lock(cache, lock_key, lock_token)
        _sweep_stale_pending_transactions(
            client,
            poll_now,
            heartbeat=heartbeat,
        )
        if not heartbeat():
            return

        active_or_grace = frappe.get_all(
            "Maybank QR Transaction",
            filters={
                "status": ["in", ["pending", "scanned"]],
                "expires_at": [
                    ">",
                    add_to_date(poll_now, seconds=-GRACE_SECONDS),
                ],
            },
            fields=[
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
            ],
            order_by="last_polled_at asc, expires_at asc",
            limit=POLL_SCAN_BATCH_SIZE,
        )

        if not active_or_grace:
            return

        due = [
            txn
            for txn in active_or_grace
            if _is_poll_due(txn, poll_now)
        ][:POLL_BATCH_SIZE]

        for txn in due:
            try:
                _poll_single(client, txn)
            except Exception as error:
                _commit_poll_writes_or_rollback()
                log_sanitized_error(f"Maybank poll failed: {txn.name}", error)
            else:
                frappe.db.commit()
            if not heartbeat():
                break
    finally:
        _release_lock(cache, lock_key, lock_token)


def _acquire_lock(cache: object, lock_key: str) -> str | None:
    token = uuid4().hex
    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()

    if (
        redis_client
        and hasattr(redis_client, "set")
        and hasattr(redis_client, "eval")
    ):
        acquired = redis_client.set(lock_key, token, ex=LOCK_TTL_SECONDS, nx=True)
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
        return


def _refresh_lock(cache: object, lock_key: str, token: str) -> bool:
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
            LOCK_TTL_SECONDS,
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


def _minimum_poll_interval_seconds(txn: dict, now=None) -> int:
    current_now = now or now_datetime()
    if txn.expires_at and current_now > txn.expires_at:
        expired_age = (current_now - txn.expires_at).total_seconds()
        if expired_age >= 24 * 60 * 60:
            return EXPIRED_ARCHIVE_INTERVAL_SECONDS
        if expired_age >= 60 * 60:
            return EXPIRED_LONG_TAIL_INTERVAL_SECONDS
        return EXPIRED_POLL_INTERVAL_SECONDS
    base_interval = 1 if txn.status == "scanned" else MIN_POLL_INTERVAL_SECONDS
    extra_delay = min(
        MAX_POLL_INTERVAL_SECONDS - base_interval, max(0, cint(txn.poll_count) // 5)
    )
    return base_interval + extra_delay


def _is_poll_due(txn: dict, now) -> bool:
    if not txn.last_polled_at:
        return True
    elapsed = (now - txn.last_polled_at).total_seconds()
    return elapsed >= _minimum_poll_interval_seconds(txn, now)


def _touch_poll_attempt(txn_name: str, now, payload: object) -> None:
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


def _sweep_stale_pending_transactions(
    client: MaybankClient,
    now,
    *,
    heartbeat: Callable[[], bool] | None = None,
) -> set[str]:
    cutoff = add_to_date(now, seconds=-GRACE_SECONDS)
    stale = frappe.get_all(
        "Maybank QR Transaction",
        filters={
            "status": ["in", ["pending", "scanned"]],
            "expires_at": ["<=", cutoff],
        },
        fields=[
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
        ],
        order_by="last_polled_at asc, expires_at asc",
        limit=POLL_BATCH_SIZE,
    )
    processed_names: set[str] = set()
    for txn in stale:
        processed_names.add(txn.name)
        try:
            _poll_single(client, txn, now=now)
        except Exception as error:
            _commit_poll_writes_or_rollback()
            log_sanitized_error(
                f"Maybank stale sweep poll failed: {txn.name}", error
            )
        else:
            frappe.db.commit()
        if heartbeat is not None and not heartbeat():
            break
    return processed_names


def _poll_single(client: MaybankClient, txn: dict, now=None) -> None:
    current_now = now or now_datetime()

    if txn.last_polled_at:
        elapsed = (current_now - txn.last_polled_at).total_seconds()
        if elapsed < _minimum_poll_interval_seconds(txn, current_now):
            return

    try:
        result = client.check_status(txn.transaction_refno)
    except Exception:
        _touch_poll_attempt(
            txn.name,
            current_now,
            {"status": "error", "message": "poll request failed"},
        )
        raise

    _apply_provider_poll_result(txn.name, result)
