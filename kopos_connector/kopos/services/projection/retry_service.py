from __future__ import annotations

from typing import Any

from kopos_connector.kopos.services.projection.log_service import (
    retry_failed_projections,
)


def retry_projection_failures() -> list[dict[str, Any]]:
    return retry_failed_projections()
