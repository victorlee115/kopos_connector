from __future__ import annotations

import importlib
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()


def _import_poll_module() -> Any:
    """Load polling without requiring provider crypto in this unit-test shell."""

    added_modules: list[str] = []
    try:
        importlib.import_module("Crypto.Cipher")
    except ModuleNotFoundError:
        crypto_module = ModuleType("Crypto")
        cipher_module = ModuleType("Crypto.Cipher")
        aes_stub = SimpleNamespace(MODE_CBC=2)
        setattr(cipher_module, "AES", aes_stub)
        setattr(crypto_module, "Cipher", cipher_module)
        sys.modules["Crypto"] = crypto_module
        sys.modules["Crypto.Cipher"] = cipher_module
        added_modules.extend(["Crypto.Cipher", "Crypto"])

    try:
        return importlib.import_module("kopos_connector.tasks.poll_maybank")
    finally:
        for module_name in added_modules:
            sys.modules.pop(module_name, None)


poll_maybank = _import_poll_module()


POLL_NOW = datetime(2026, 3, 13, 18, 6, 0)


def _transaction(
    name: str,
    *,
    status: str = "pending",
    expires_at: datetime = datetime(2026, 3, 13, 18, 10, 0),
    last_polled_at: datetime | None = None,
    fb_order: str | None = None,
    fb_order_payment: str | None = None,
    raw_response: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        transaction_refno=f"ref-{name}",
        status=status,
        last_polled_at=last_polled_at,
        created_at=datetime(2026, 3, 13, 18, 0, 0),
        expires_at=expires_at,
        poll_count=0,
        sale_amount_sen=1000,
        outlet_id="outlet-1",
        currency="MYR",
        device_id="device-1",
        provider="maybank_qr",
        idempotency_key=f"idem-{name}",
        request_fingerprint=name.rjust(64, "0")[-64:],
        maybank_status=2,
        qr_data=f"qr-{name}",
        raw_response=raw_response,
        fb_order=fb_order,
        fb_order_payment=fb_order_payment,
    )


def _redis_cache() -> tuple[SimpleNamespace, Mock]:
    redis_client = Mock()
    redis_client.set.return_value = True
    redis_client.eval.return_value = 1
    return SimpleNamespace(redis_client=lambda: redis_client), redis_client


def test_poll_lock_supports_frappe_v16_direct_redis_cache() -> None:
    redis_client = SimpleNamespace(
        set=Mock(return_value=True),
        eval=Mock(return_value=1),
    )
    lock_key = "maybank_poll_lock:test.localhost"

    token = poll_maybank._acquire_lock(redis_client, lock_key)

    assert token is not None
    redis_client.set.assert_called_once_with(
        lock_key,
        token,
        ex=poll_maybank.SCHEDULER_LOCK_TTL_SECONDS,
        nx=True,
    )
    poll_maybank._release_lock(redis_client, lock_key, token)
    redis_client.eval.assert_called_once_with(
        poll_maybank.LOCK_RELEASE_SCRIPT,
        1,
        lock_key,
        token,
    )


def test_poll_lock_refresh_supports_frappe_v16_direct_redis_cache() -> None:
    redis_client = SimpleNamespace(eval=Mock(return_value=1))

    refreshed = poll_maybank._refresh_lock(
        redis_client,
        "maybank_poll_lock:test.localhost",
        "owner-token",
    )

    assert refreshed is True
    redis_client.eval.assert_called_once_with(
        poll_maybank.LOCK_REFRESH_SCRIPT,
        1,
        "maybank_poll_lock:test.localhost",
        "owner-token",
        poll_maybank.SCHEDULER_LOCK_TTL_SECONDS,
    )


def test_scheduler_dispatches_scanned_then_current_before_stale_recovery() -> None:
    cache, _redis_client = _redis_cache()
    enqueued: list[dict[str, Any]] = []

    def capture_enqueue(_method: str, **kwargs: Any) -> None:
        enqueued.append(kwargs)

    with (
        patch.object(poll_maybank.frappe, "cache", return_value=cache),
        patch.object(poll_maybank, "now_datetime", return_value=POLL_NOW),
        patch.object(
            poll_maybank.frappe,
            "get_all",
            side_effect=[
                [_transaction("scanned-live", status="scanned")],
                [_transaction("pending-live")],
                [
                    _transaction(
                        "pending-stale",
                        expires_at=datetime(2026, 3, 13, 18, 4, 0),
                        last_polled_at=datetime(2026, 3, 13, 18, 4, 0),
                    )
                ],
            ],
        ),
        patch.object(
            poll_maybank.frappe,
            "enqueue",
            side_effect=capture_enqueue,
            create=True,
        ),
        patch.object(
            poll_maybank.MaybankClient,
            "from_settings",
        ) as client_factory,
    ):
        poll_maybank.poll_pending_maybank_transactions()

    assert [job["transaction_name"] for job in enqueued] == [
        "scanned-live",
        "pending-live",
        "pending-stale",
    ]
    assert [job["queue"] for job in enqueued] == ["short", "short", "long"]
    assert all(job["deduplicate"] is True for job in enqueued)
    client_factory.assert_not_called()


def test_failing_stale_dispatch_cannot_prevent_current_dispatch() -> None:
    cache, _redis_client = _redis_cache()
    attempts: list[tuple[str, str]] = []

    def enqueue_with_stale_failure(_method: str, **kwargs: Any) -> None:
        attempts.append((kwargs["transaction_name"], kwargs["queue"]))
        if kwargs["transaction_name"] == "stale-fails":
            raise RuntimeError("long queue unavailable")

    with (
        patch.object(poll_maybank.frappe, "cache", return_value=cache),
        patch.object(poll_maybank, "now_datetime", return_value=POLL_NOW),
        patch.object(
            poll_maybank.frappe,
            "get_all",
            side_effect=[
                [_transaction("scanned-live", status="scanned")],
                [_transaction("pending-live")],
                [
                    _transaction(
                        "stale-fails",
                        expires_at=datetime(2026, 3, 13, 18, 4, 0),
                        last_polled_at=datetime(2026, 3, 13, 18, 4, 0),
                    ),
                    _transaction(
                        "stale-next",
                        expires_at=datetime(2026, 3, 13, 18, 4, 0),
                        last_polled_at=datetime(2026, 3, 13, 18, 4, 0),
                    ),
                ],
            ],
        ),
        patch.object(
            poll_maybank.frappe,
            "enqueue",
            side_effect=enqueue_with_stale_failure,
            create=True,
        ),
        patch.object(poll_maybank, "log_sanitized_error") as log_error,
    ):
        poll_maybank.poll_pending_maybank_transactions()

    assert attempts == [
        ("scanned-live", "short"),
        ("pending-live", "short"),
        ("stale-fails", "long"),
        ("stale-next", "long"),
    ]
    log_error.assert_called_once()


def test_active_capacity_reserves_progress_for_pending_rows() -> None:
    scanned = [
        _transaction(f"scanned-{index}", status="scanned")
        for index in range(poll_maybank.ACTIVE_POLL_BATCH_SIZE)
    ]
    pending = [
        _transaction(f"pending-{index}")
        for index in range(poll_maybank.ACTIVE_POLL_BATCH_SIZE)
    ]

    with patch.object(
        poll_maybank.frappe,
        "get_all",
        side_effect=[scanned, pending],
    ):
        selected_scanned, selected_pending = (
            poll_maybank._load_due_active_transactions(POLL_NOW)
        )

    assert len(selected_scanned) == 75
    assert len(selected_pending) == 25
    assert len(selected_scanned) + len(selected_pending) == 100


def test_stale_lane_has_an_independent_bounded_budget() -> None:
    stale = [
        _transaction(
            f"stale-{index}",
            expires_at=datetime(2026, 3, 13, 17, 0, 0),
            last_polled_at=datetime(2026, 3, 13, 17, 0, 0),
        )
        for index in range(30)
    ]

    with patch.object(poll_maybank.frappe, "get_all", return_value=stale):
        selected = poll_maybank._load_due_stale_transactions(POLL_NOW)

    assert len(selected) == poll_maybank.STALE_POLL_BATCH_SIZE


def test_transaction_job_skips_duplicate_provider_request_under_redis_lease() -> None:
    cache, redis_client = _redis_cache()
    redis_client.set.return_value = False

    with (
        patch.object(poll_maybank.frappe, "cache", return_value=cache),
        patch.object(poll_maybank, "_load_transaction_snapshot") as load_snapshot,
        patch.object(
            poll_maybank.MaybankClient,
            "from_settings",
        ) as client_factory,
    ):
        result = poll_maybank.poll_maybank_transaction("txn-running", "active")

    assert result["status"] == "already_running"
    load_snapshot.assert_not_called()
    client_factory.assert_not_called()


def test_provider_failure_is_contained_inside_one_stale_job() -> None:
    cache, redis_client = _redis_cache()
    transaction = _transaction(
        "stale-error",
        expires_at=datetime(2026, 3, 13, 18, 4, 0),
        last_polled_at=datetime(2026, 3, 13, 18, 4, 0),
    )

    with (
        patch.object(poll_maybank.frappe, "cache", return_value=cache),
        patch.object(poll_maybank, "now_datetime", return_value=POLL_NOW),
        patch.object(
            poll_maybank,
            "_load_transaction_snapshot",
            return_value=transaction,
        ),
        patch.object(
            poll_maybank.MaybankClient,
            "from_settings",
            return_value=Mock(),
        ),
        patch.object(
            poll_maybank,
            "_poll_single",
            side_effect=RuntimeError("provider timeout"),
        ),
        patch.object(poll_maybank, "_commit_poll_writes_or_rollback") as persist,
        patch.object(poll_maybank, "log_sanitized_error") as log_error,
    ):
        result = poll_maybank.poll_maybank_transaction("stale-error", "stale")

    assert result["status"] == "provider_error"
    persist.assert_called_once()
    log_error.assert_called_once()
    release_calls = [
        call
        for call in redis_client.eval.call_args_list
        if call.args[0] == poll_maybank.LOCK_RELEASE_SCRIPT
    ]
    assert len(release_calls) == 1


def test_job_identity_deduplicates_the_same_transaction_across_lanes() -> None:
    with patch.object(
        poll_maybank.frappe,
        "enqueue",
        create=True,
    ) as enqueue:
        poll_maybank._enqueue_poll_job(
            "txn-one",
            lane="active",
        )
        poll_maybank._enqueue_poll_job(
            "txn-one",
            lane="stale",
        )

    first = enqueue.call_args_list[0].kwargs
    second = enqueue.call_args_list[1].kwargs
    assert first["job_id"] == second["job_id"]
    assert first["deduplicate"] is True
    assert second["deduplicate"] is True


def test_selected_sale_expands_to_every_due_linked_attempt_in_same_tick() -> None:
    first = _transaction(
        "attempt-current",
        fb_order="FB-ORDER-1",
        fb_order_payment="PAY-1",
    )
    earlier = _transaction(
        "attempt-earlier",
        status="timeout",
        fb_order="FB-ORDER-1",
        fb_order_payment="PAY-1",
    )

    with (
        patch.object(
            poll_maybank.frappe,
            "get_all",
            side_effect=[[], [first]],
        ),
        patch.object(
            poll_maybank,
            "_load_linked_poll_attempts",
            return_value=[first, earlier],
        ) as load_linked,
    ):
        scanned, pending = poll_maybank._load_due_active_transactions(POLL_NOW)

    assert scanned == []
    assert [row.name for row in pending] == [
        "attempt-current",
        "attempt-earlier",
    ]
    load_linked.assert_called_once_with({("FB-ORDER-1", "PAY-1")})


def test_scheduler_dispatches_cross_lane_siblings_in_the_same_tick() -> None:
    cache, _redis_client = _redis_cache()
    active = _transaction(
        "attempt-active",
        fb_order="FB-ORDER-1",
        fb_order_payment="PAY-1",
    )
    expired = _transaction(
        "attempt-expired",
        status="timeout",
        expires_at=datetime(2026, 3, 13, 18, 4, 0),
        last_polled_at=datetime(2026, 3, 13, 18, 4, 0),
        fb_order="FB-ORDER-1",
        fb_order_payment="PAY-1",
    )
    jobs: list[tuple[str, str]] = []

    with (
        patch.object(poll_maybank.frappe, "cache", return_value=cache),
        patch.object(poll_maybank, "now_datetime", return_value=POLL_NOW),
        patch.object(
            poll_maybank.frappe,
            "get_all",
            side_effect=[[], [active], []],
        ),
        patch.object(
            poll_maybank,
            "_load_linked_poll_attempts",
            return_value=[active, expired],
        ),
        patch.object(
            poll_maybank,
            "_enqueue_poll_job",
            side_effect=lambda name, *, lane: jobs.append((name, lane)),
        ),
    ):
        poll_maybank.poll_pending_maybank_transactions()

    assert jobs == [
        ("attempt-active", "active"),
        ("attempt-expired", "stale"),
    ]


def test_linked_status_read_enqueues_active_and_expired_attempts_independently() -> None:
    active = _transaction(
        "attempt-active",
        fb_order="FB-ORDER-1",
        fb_order_payment="PAY-1",
    )
    expired = _transaction(
        "attempt-expired",
        status="failed",
        expires_at=datetime(2026, 3, 13, 18, 5, 0),
        last_polled_at=datetime(2026, 3, 13, 18, 4, 0),
        fb_order="FB-ORDER-1",
        fb_order_payment="PAY-1",
    )
    jobs: list[tuple[str, str]] = []

    with (
        patch.object(poll_maybank, "now_datetime", return_value=POLL_NOW),
        patch.object(
            poll_maybank,
            "_load_linked_poll_attempts",
            return_value=[active, expired],
        ),
        patch.object(
            poll_maybank,
            "_enqueue_poll_job",
            side_effect=lambda name, *, lane: jobs.append((name, lane)),
        ),
    ):
        dispatched = poll_maybank.enqueue_linked_maybank_sale_poll_attempts(
            "FB-ORDER-1",
            "PAY-1",
        )

    assert dispatched == 2
    assert jobs == [
        ("attempt-active", "active"),
        ("attempt-expired", "stale"),
    ]


def test_on_demand_poll_excludes_reference_already_checked_by_request() -> None:
    requested = _transaction(
        "attempt-requested",
        fb_order="FB-ORDER-1",
        fb_order_payment="PAY-1",
    )
    sibling = _transaction(
        "attempt-sibling",
        fb_order="FB-ORDER-1",
        fb_order_payment="PAY-1",
    )
    jobs: list[tuple[str, str]] = []

    with (
        patch.object(poll_maybank, "now_datetime", return_value=POLL_NOW),
        patch.object(
            poll_maybank,
            "_load_linked_poll_attempts",
            return_value=[requested, sibling],
        ),
        patch.object(
            poll_maybank,
            "_enqueue_poll_job",
            side_effect=lambda name, *, lane: jobs.append((name, lane)),
        ),
    ):
        dispatched = poll_maybank.enqueue_linked_maybank_sale_poll_attempts(
            "FB-ORDER-1",
            "PAY-1",
            exclude_transaction_name="attempt-requested",
        )

    assert dispatched == 1
    assert jobs == [("attempt-sibling", "active")]


def test_transport_failure_preserves_last_provider_evidence() -> None:
    transaction = _transaction("attempt-network-error")
    client = Mock()
    client.check_status.side_effect = TimeoutError("provider unavailable")

    with patch.object(
        poll_maybank,
        "_record_poll_observation",
    ) as record_observation:
        with pytest.raises(TimeoutError, match="provider unavailable"):
            poll_maybank._poll_single(client, transaction, now=POLL_NOW)

    record_observation.assert_called_once_with("attempt-network-error")


def test_provider_timeout_remains_pollable_without_durable_cancellation() -> None:
    provider_timeout = _transaction("attempt-timeout", status="timeout")
    provider_timeout.maybank_status = 6

    assert poll_maybank.is_maybank_attempt_pollable(provider_timeout) is True


def test_audited_provider_cancellation_is_not_polled_again() -> None:
    cancelled = _transaction(
        "attempt-cancelled",
        status="timeout",
        raw_response=(
            '{"status":"timeout","resolution":"provider_transaction_cancelled",'
            '"reason":"Provider portal confirms cancellation",'
            '"evidence_reference":"case-123",'
            '"resolved_by":"manager@example.test",'
            '"resolved_at":"2026-03-13T18:05:00+08:00",'
            '"provider_reference":"ref-attempt-cancelled"}'
        ),
    )
    cancelled.maybank_status = None

    assert poll_maybank.is_maybank_attempt_pollable(cancelled) is False
