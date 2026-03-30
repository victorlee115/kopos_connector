from __future__ import annotations

from uuid import uuid4

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from kopos_connector.api.maybank_qr import (
    STATUS_MAP,
    UNKNOWN_STATUS,
    _extract_status_entry,
    _update_txn_status,
)
from kopos_connector.services.maybank.client import MaybankClient

MIN_POLL_INTERVAL_SECONDS = 2
MAX_POLL_INTERVAL_SECONDS = 15
POLL_BATCH_SIZE = 100
POLL_SCAN_BATCH_SIZE = 400
LOCK_KEY = "maybank_poll_lock"
LOCK_TTL_SECONDS = 120
GRACE_SECONDS = 30


def poll_pending_maybank_transactions() -> None:
    """Batch poll pending Maybank QR transactions. Uses distributed lock to prevent concurrent runs."""
    cache = frappe.cache()
    lock_key = f"{LOCK_KEY}:{getattr(frappe.local, 'site', 'default-site')}"
    lock_token = _acquire_lock(cache, lock_key)
    if not lock_token:
        return

    try:
        try:
            client = MaybankClient.from_settings()
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "Maybank poll: failed to init client"
            )
            return

        poll_now = now_datetime()
        processed_names = _sweep_stale_pending_transactions(client, poll_now)

        pending = frappe.get_all(
            "Maybank QR Transaction",
            filters={"status": ["in", ["pending", "scanned"]]},
            fields=[
                "name",
                "transaction_refno",
                "status",
                "last_polled_at",
                "created_at",
                "expires_at",
                "poll_count",
            ],
            order_by="expires_at asc, last_polled_at asc",
            limit=POLL_SCAN_BATCH_SIZE,
        )

        if not pending:
            return

        due = [
            txn
            for txn in pending
            if txn.name not in processed_names and _is_poll_due(txn, poll_now)
        ][:POLL_BATCH_SIZE]

        for txn in due:
            try:
                _poll_single(client, txn)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Maybank poll failed: {txn.name}",
                )
    finally:
        _release_lock(cache, lock_key, lock_token)


def _acquire_lock(cache: object, lock_key: str) -> str | None:
    token = uuid4().hex
    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()

    if redis_client and hasattr(redis_client, "set"):
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

    if redis_client and hasattr(redis_client, "get"):
        current = redis_client.get(lock_key)
        if isinstance(current, bytes):
            current = current.decode()
        if current == token:
            redis_client.delete(lock_key)
        return

    if _cache_get(cache, lock_key) == token:
        _cache_delete(cache, lock_key)


def _cache_get(cache: object, key: str) -> str | None:
    if hasattr(cache, "get_value"):
        return getattr(cache, "get_value")(key)
    if hasattr(cache, "get"):
        return getattr(cache, "get")(key)
    return None


def _cache_set(cache: object, key: str, value: str) -> None:
    if hasattr(cache, "set_value"):
        getattr(cache, "set_value")(key, value, expires_in_sec=LOCK_TTL_SECONDS)
        return
    if hasattr(cache, "setex"):
        getattr(cache, "setex")(key, LOCK_TTL_SECONDS, value)


def _cache_delete(cache: object, key: str) -> None:
    if hasattr(cache, "delete_value"):
        getattr(cache, "delete_value")(key)
        return
    if hasattr(cache, "delete"):
        getattr(cache, "delete")(key)


def _minimum_poll_interval_seconds(txn: dict) -> int:
    base_interval = 1 if txn.status == "scanned" else MIN_POLL_INTERVAL_SECONDS
    extra_delay = min(
        MAX_POLL_INTERVAL_SECONDS - base_interval, max(0, cint(txn.poll_count) // 5)
    )
    return base_interval + extra_delay


def _is_poll_due(txn: dict, now) -> bool:
    if txn.expires_at and now > txn.expires_at:
        return True
    if not txn.last_polled_at:
        return True
    elapsed = (now - txn.last_polled_at).total_seconds()
    return elapsed >= _minimum_poll_interval_seconds(txn)


def _touch_poll_attempt(txn_name: str, now, payload: object) -> None:
    frappe.db.sql(
        "UPDATE `tabMaybank QR Transaction` SET last_polled_at = %s, poll_count = poll_count + 1, raw_response = %s WHERE name = %s",
        (now, frappe.as_json(payload), txn_name),
    )
    frappe.db.commit()


def _sweep_stale_pending_transactions(client: MaybankClient, now) -> set[str]:
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
        ],
        order_by="expires_at asc, last_polled_at asc",
        limit=POLL_BATCH_SIZE,
    )
    processed_names: set[str] = set()
    for txn in stale:
        processed_names.add(txn.name)
        try:
            _poll_single(client, txn, now=now)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Maybank stale sweep poll failed: {txn.name}",
            )
    return processed_names


def _poll_single(client: MaybankClient, txn: dict, now=None) -> None:
    current_now = now or now_datetime()

    expired = bool(txn.expires_at and current_now > txn.expires_at)

    if txn.last_polled_at and not expired:
        elapsed = (current_now - txn.last_polled_at).total_seconds()
        if elapsed < _minimum_poll_interval_seconds(txn):
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

    entry = _extract_status_entry(result)
    if entry is None:
        frappe.log_error(
            f"Maybank empty response for {txn.transaction_refno}",
            "Maybank poll: empty data",
        )
        _touch_poll_attempt(txn.name, current_now, {"status": "empty", "raw": result})
        return

    raw_status = cint(entry.get("status", 0))
    new_status = STATUS_MAP.get(str(raw_status), UNKNOWN_STATUS)

    if new_status != txn.status:
        _update_txn_status(txn.name, new_status, raw_status, result)
        frappe.db.commit()
    elif (
        expired
        and txn.status in ("pending", "scanned")
        and new_status in ("pending", "scanned")
    ):
        past_grace = bool(
            txn.expires_at
            and (current_now - txn.expires_at).total_seconds() > GRACE_SECONDS
        )
        if past_grace:
            _update_txn_status(txn.name, "timeout", raw_status, result)
            frappe.db.commit()
        else:
            _touch_poll_attempt(txn.name, current_now, result)
    else:
        _touch_poll_attempt(txn.name, current_now, result)
