from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
install_fake_frappe_modules()

retry_service = importlib.import_module(
    "kopos_connector.kopos.services.projection.retry_service"
)
fb_orders = importlib.import_module("kopos_connector.kopos.api.fb_orders")
log_service = importlib.import_module(
    "kopos_connector.kopos.services.projection.log_service"
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(
        self,
        key: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> bool:
        assert ex > 0
        assert nx is True
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script: str, key_count: int, key: str, token: str) -> int:
        assert "redis.call('get'" in script
        assert key_count == 1
        if self.values.get(key) != token:
            return 0
        self.values.pop(key, None)
        return 1


def test_retry_delay_is_deterministic_jittered_and_bounded() -> None:
    first = retry_service._retry_delay_seconds("LOG-1", 1)
    assert first == retry_service._retry_delay_seconds("LOG-1", 1)
    assert retry_service.BASE_RETRY_DELAY_SECONDS <= first <= 7
    assert (
        retry_service._retry_delay_seconds("LOG-1", 100)
        <= retry_service.MAX_RETRY_DELAY_SECONDS
    )


def test_projection_identity_is_deterministic_and_payload_sensitive() -> None:
    first = log_service._canonical_projection_id(
        "FB Order", "FB-ORDER-1", "Sales Invoice", "idem-1"
    )
    assert first == log_service._canonical_projection_id(
        "FB Order", "FB-ORDER-1", "Sales Invoice", "idem-1"
    )
    assert first != log_service._canonical_projection_id(
        "FB Order", "FB-ORDER-1", "Stock Issue", "idem-1"
    )
    assert first.startswith("kopos-proj-")


def test_due_check_respects_active_lease_and_explicit_schedule() -> None:
    now = datetime(2026, 7, 16, 12, 0, 0)
    assert retry_service._is_retry_due(
        {
            "name": "LOG-1",
            "retry_count": 1,
            "last_attempt_at": now - timedelta(minutes=1),
            "next_retry_at": None,
            "lease_expires_at": None,
        },
        now,
    )
    assert not retry_service._is_retry_due(
        {
            "name": "LOG-1",
            "retry_count": 1,
            "last_attempt_at": now - timedelta(minutes=1),
            "next_retry_at": now - timedelta(seconds=1),
            "lease_expires_at": now + timedelta(seconds=30),
        },
        now,
    )
    assert not retry_service._is_retry_due(
        {
            "name": "LOG-1",
            "retry_count": 1,
            "last_attempt_at": now - timedelta(minutes=1),
            "next_retry_at": now + timedelta(seconds=30),
            "lease_expires_at": None,
        },
        now,
    )


def test_worker_runs_real_handler_under_atomic_worker_lease(monkeypatch) -> None:
    redis = FakeRedis()
    candidate = {
        "name": "LOG-1",
        "source_name": "FB-ORDER-1",
        "projection_type": "Sales Invoice",
        "retry_count": 1,
        "last_attempt_at": None,
        "next_retry_at": None,
        "lease_expires_at": None,
        "dead_lettered_at": None,
    }
    finalized: list[dict[str, Any]] = []

    monkeypatch.setattr(
        retry_service.frappe,
        "cache",
        lambda: SimpleNamespace(redis_client=redis),
    )
    monkeypatch.setattr(
        retry_service.frappe,
        "get_all",
        lambda *args, **kwargs: [candidate],
    )
    monkeypatch.setattr(retry_service, "_claim_projection", lambda *args: True)
    monkeypatch.setattr(
        retry_service,
        "_finalize_projection_attempt",
        lambda **kwargs: finalized.append(kwargs),
    )
    monkeypatch.setattr(
        fb_orders,
        "_retry_projection_log",
        lambda name: {
            "projection_log": name,
            "projection_type": "Sales Invoice",
            "state": "Succeeded",
            "target_name": "SINV-1",
        },
    )

    result = retry_service.retry_projection_failures(force=True, batch_size=1)

    assert result == [
        {
            "projection_log": "LOG-1",
            "projection_type": "Sales Invoice",
            "state": "Succeeded",
            "target_name": "SINV-1",
        }
    ]
    assert finalized[0]["log_name"] == "LOG-1"
    assert retry_service.RETRY_LOCK_KEY not in redis.values


def test_forced_worker_can_recover_projection_after_retry_ceiling(monkeypatch) -> None:
    redis = FakeRedis()
    candidate = {
        "name": "LOG-DEAD",
        "source_name": "FB-ORDER-1",
        "projection_type": "Sales Invoice",
        "retry_count": retry_service.DEFAULT_MAX_RETRIES,
        "last_attempt_at": None,
        "next_retry_at": None,
        "lease_expires_at": None,
        "dead_lettered_at": datetime(2026, 7, 16, 12, 0, 0),
    }
    claimed: list[str] = []

    monkeypatch.setattr(
        retry_service.frappe,
        "cache",
        lambda: SimpleNamespace(redis_client=redis),
    )
    monkeypatch.setattr(
        retry_service.frappe,
        "get_all",
        lambda *args, **kwargs: [candidate],
    )
    monkeypatch.setattr(
        retry_service,
        "_claim_projection",
        lambda log_name, *_args: claimed.append(log_name) or True,
    )
    monkeypatch.setattr(
        retry_service,
        "_finalize_projection_attempt",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        fb_orders,
        "_retry_projection_log",
        lambda name: {
            "projection_log": name,
            "projection_type": "Sales Invoice",
            "state": "Succeeded",
            "target_name": "SINV-1",
        },
    )

    result = retry_service.retry_projection_failures(force=True, batch_size=1)

    assert claimed == ["LOG-DEAD"]
    assert result[0]["state"] == "Succeeded"


def test_dead_letter_marker_rechecks_locked_state_after_candidate_scan(
    monkeypatch,
) -> None:
    commits: list[bool] = []
    set_values: list[dict[str, Any]] = []
    monkeypatch.setattr(
        retry_service.frappe.db,
        "sql",
        lambda *args, **kwargs: [
            {
                "name": "LOG-RECOVERED",
                "state": "Succeeded",
                "retry_count": retry_service.DEFAULT_MAX_RETRIES,
                "dead_lettered_at": None,
                "lease_token": None,
                "lease_expires_at": None,
            }
        ],
    )
    monkeypatch.setattr(
        retry_service.frappe.db,
        "set_value",
        lambda *args, **kwargs: set_values.append(kwargs),
    )
    monkeypatch.setattr(
        retry_service.frappe.db,
        "commit",
        lambda: commits.append(True),
    )

    retry_service._mark_dead_letter(
        {"name": "LOG-RECOVERED"},
        datetime(2026, 7, 16, 12, 0, 0),
        retry_service.DEFAULT_MAX_RETRIES,
    )

    assert set_values == []
    assert commits == []


def test_successful_manual_retry_clears_obsolete_dead_letter_evidence(
    monkeypatch,
) -> None:
    saved: list[bool] = []
    log = SimpleNamespace(
        state="Failed",
        target_name=None,
        last_error="temporary ERP error",
        last_attempt_at=datetime(2026, 7, 16, 11, 0, 0),
        next_retry_at=datetime(2026, 7, 16, 12, 5, 0),
        lease_token="expired-token",
        lease_expires_at=datetime(2026, 7, 16, 11, 5, 0),
        dead_lettered_at=datetime(2026, 7, 16, 11, 10, 0),
        save=lambda **kwargs: saved.append(kwargs["ignore_permissions"]),
    )
    retry_time = datetime(2026, 7, 16, 12, 0, 0)
    monkeypatch.setattr(fb_orders, "now_datetime", lambda: retry_time)

    fb_orders._update_projection_log(log, "Succeeded", "SINV-1", None)

    assert log.state == "Succeeded"
    assert log.target_name == "SINV-1"
    assert log.last_error is None
    assert log.last_attempt_at == retry_time
    assert log.next_retry_at is None
    assert log.lease_token is None
    assert log.lease_expires_at is None
    assert log.dead_lettered_at is None
    assert saved == [True]


def test_worker_skips_when_another_scheduler_owns_the_lease(monkeypatch) -> None:
    redis = FakeRedis()
    redis.values[retry_service.RETRY_LOCK_KEY] = "other-worker"
    monkeypatch.setattr(
        retry_service.frappe,
        "cache",
        lambda: SimpleNamespace(redis_client=redis),
    )
    monkeypatch.setattr(
        retry_service.frappe,
        "get_all",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("database scan should not run without the worker lease")
        ),
    )

    assert retry_service.retry_projection_failures() == []


def test_atomic_release_never_deletes_a_replacement_lease() -> None:
    redis = FakeRedis()
    redis.values[retry_service.RETRY_LOCK_KEY] = "replacement-worker"

    retry_service._release_worker_lock(
        SimpleNamespace(redis_client=redis),
        "expired-worker",
    )

    assert redis.values[retry_service.RETRY_LOCK_KEY] == "replacement-worker"


def test_projection_schema_and_scheduler_expose_recovery_evidence() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "kopos_connector/kopos/doctype/fb_projection_log/fb_projection_log.json").read_text()
    )
    fields = {field["fieldname"]: field for field in schema["fields"]}
    for fieldname in (
        "next_retry_at",
        "lease_token",
        "lease_expires_at",
        "dead_lettered_at",
    ):
        assert fieldname in fields
    hooks = (root / "kopos_connector/hooks.py").read_text()
    assert (
        "kopos_connector.kopos.services.projection.retry_service.retry_projection_failures"
        in hooks
    )


def test_fb_shift_projection_has_a_real_retry_handler() -> None:
    config = fb_orders._get_projection_config("FB Order", "FB Shift")
    assert config == {
        "projection_type": "FB Shift",
        "target_field": "shift",
        "target_doctype": "FB Shift",
    }
