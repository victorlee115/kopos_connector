from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

from kopos_connector.kopos.api.fb_orders import _update_projection_log
from kopos_connector.kopos.services.projection import retry_service


class ProjectionLog:
    def __init__(self) -> None:
        self.state = "Failed"
        self.target_name = None
        self.last_error = "previous failure"
        self.last_attempt_at = None
        self.next_retry_at = "2026-08-12 00:00:00"
        self.lease_token = "active-worker-lease"
        self.lease_expires_at = "2026-08-12 00:02:00"
        self.dead_lettered_at = "2026-08-11 23:59:00"
        self.saved = False

    def save(self, *, ignore_permissions: bool) -> None:
        assert ignore_permissions is True
        self.saved = True


def test_worker_success_preserves_lease_until_atomic_finalization() -> None:
    log = ProjectionLog()

    _update_projection_log(
        log,
        "Succeeded",
        "ACC-SINV-RECOVERED",
        None,
        preserve_lease=True,
    )

    assert log.saved is True
    assert log.state == "Succeeded"
    assert log.target_name == "ACC-SINV-RECOVERED"
    assert log.last_error is None
    assert log.lease_token == "active-worker-lease"
    assert log.lease_expires_at == "2026-08-12 00:02:00"
    assert log.next_retry_at == "2026-08-12 00:00:00"
    assert log.dead_lettered_at == "2026-08-11 23:59:00"


def test_direct_success_clears_obsolete_retry_evidence() -> None:
    log = ProjectionLog()

    _update_projection_log(log, "Succeeded", "ACC-SINV-RECOVERED", None)

    assert log.saved is True
    assert log.lease_token is None
    assert log.lease_expires_at is None
    assert log.next_retry_at is None
    assert log.dead_lettered_at is None


def test_worker_requests_lease_preservation_before_finalizing() -> None:
    candidate = SimpleNamespace(
        name="PROJECTION-1",
        source_name="ORDER-1",
        projection_type="Sales Invoice",
        retry_count=0,
        last_attempt_at=None,
        next_retry_at=None,
        lease_expires_at=None,
        dead_lettered_at=None,
    )
    result = {
        "projection_log": "PROJECTION-1",
        "projection_type": "Sales Invoice",
        "state": "Succeeded",
        "target_name": "ACC-SINV-RECOVERED",
    }

    with (
        patch.object(retry_service.frappe, "get_all", return_value=[candidate]),
        patch.object(retry_service, "_claim_projection", return_value=True),
        patch.object(retry_service, "_finalize_projection_attempt") as finalize,
        patch(
            "kopos_connector.kopos.api.fb_orders._retry_projection_log",
            return_value=result,
        ) as retry,
    ):
        observed = retry_service._retry_projection_batch(force=True, batch_size=1)

    assert observed == [result]
    retry.assert_called_once_with("PROJECTION-1", preserve_lease=True)
    finalize.assert_called_once()
