from __future__ import annotations

from unittest.mock import patch

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

from kopos_connector.kopos.services.projection import log_service
from kopos_connector.kopos.services.projection.log_service import (
    ProjectionLogError,
    create_projection_log,
    get_pending_projections,
    retry_failed_projections,
    update_projection_state,
)


class FailingProjectionLogDoc:
    def __init__(self) -> None:
        self.doctype = "FB Projection Log"

    def insert(self, ignore_permissions: bool = False) -> None:
        raise RuntimeError("projection insert failed")


def test_create_projection_log_raises_when_insert_fails() -> None:
    with (
        patch.object(log_service.frappe.db, "get_value", return_value=None),
        patch.object(
            log_service.frappe,
            "new_doc",
            return_value=FailingProjectionLogDoc(),
        ),
        patch.object(
            log_service.frappe.utils,
            "now",
            return_value="2026-03-13 18:05:00",
            create=True,
        ),
    ):
        with pytest.raises(
            ProjectionLogError,
            match="Projection log creation failed for FB Order FB-ORDER-1 Sales Invoice: projection insert failed",
        ):
            create_projection_log(
                source_doctype="FB Order",
                source_name="FB-ORDER-1",
                projection_type="Sales Invoice",
                idempotency_key="idem-1:Sales Invoice",
                payload_hash="hash-1",
            )


def test_update_projection_state_raises_when_save_fails() -> None:
    with patch.object(
        log_service.frappe,
        "get_doc",
        side_effect=RuntimeError("projection update failed"),
    ):
        with pytest.raises(
            ProjectionLogError,
            match="Projection log update failed for LOG-1 to Failed: projection update failed",
        ):
            update_projection_state(
                log_name="LOG-1",
                state="Failed",
                target_doctype="Sales Invoice",
                target_name=None,
                error="invoice insert failed",
            )


def test_pending_projection_fetch_failure_is_not_an_empty_queue() -> None:
    with patch.object(
        log_service.frappe,
        "get_all",
        side_effect=RuntimeError("projection query failed"),
    ):
        with pytest.raises(
            ProjectionLogError,
            match="Fetching pending projections failed: projection query failed",
        ):
            get_pending_projections()


def test_failed_projection_fetch_failure_is_not_an_empty_queue() -> None:
    with patch.object(
        log_service.frappe,
        "get_all",
        side_effect=RuntimeError("failed query failed"),
    ):
        with pytest.raises(
            ProjectionLogError,
            match="Fetching failed projections failed: failed query failed",
        ):
            retry_failed_projections()
