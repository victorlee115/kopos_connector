from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
install_fake_frappe_modules()

patch = importlib.import_module(
    "kopos_connector.patches.requeue_stranded_commercial_projections"
)
retry_service = importlib.import_module(
    "kopos_connector.kopos.services.projection.retry_service"
)


class FakeSite:
    """Projection logs and orders; records every write the patch makes."""

    def __init__(self, logs: list[dict[str, Any]], orders: dict[str, int], *, doctype_exists: bool = True) -> None:
        self.logs = logs
        self.orders = orders
        self.doctype_exists = doctype_exists
        self.writes: list[tuple[str, dict[str, Any], bool]] = []

    def exists(self, doctype: str, name: str) -> bool:
        assert (doctype, name) == ("DocType", "FB Projection Log")
        return self.doctype_exists

    def get_all(self, doctype: str, **kwargs: Any) -> list[Any]:
        filters = kwargs["filters"]
        assert kwargs["limit_page_length"] == 0
        if doctype == "FB Projection Log":
            assert filters["projection_type"][0] == "in"
            return [
                {key: row[key] for key in ("name", "source_name", "last_error")}
                for row in self.logs
                if row["source_doctype"] == filters["source_doctype"]
                and row["projection_type"] in filters["projection_type"][1]
                and row["state"] == filters["state"]
            ]
        if doctype == "FB Order":
            assert kwargs["pluck"] == "name" and filters["name"][0] == "in"
            return [
                name
                for name in filters["name"][1]
                if self.orders.get(name) == filters["docstatus"]
            ]
        raise AssertionError(f"unexpected get_all for {doctype}")

    def set_value(self, doctype: str, name: str, values: dict[str, Any], *, update_modified: bool) -> None:
        assert doctype == "FB Projection Log"
        self.writes.append((name, values, update_modified))
        row = next(row for row in self.logs if row["name"] == name)
        row.update(values)


def _install(monkeypatch, site: FakeSite) -> None:
    monkeypatch.setattr(patch.frappe.db, "exists", site.exists, raising=False)
    monkeypatch.setattr(patch.frappe, "get_all", site.get_all, raising=False)
    monkeypatch.setattr(patch.frappe.db, "set_value", site.set_value, raising=False)


def _log(name: str, order: str, state: str, projection_type: str = "Sales Invoice", last_error: str | None = "Sales Invoice projection failed") -> dict[str, Any]:
    return {
        "name": name,
        "source_doctype": "FB Order",
        "source_name": order,
        "projection_type": projection_type,
        "state": state,
        "last_error": last_error,
        "retry_count": 2,
        "last_attempt_at": "2026-06-12 01:57:24",
        "next_retry_at": None,
        "lease_token": None,
        "lease_expires_at": None,
        "dead_lettered_at": None,
    }


def test_stranded_commercial_rows_of_submitted_sales_join_the_retry_worker(monkeypatch) -> None:
    site = FakeSite(
        logs=[
            _log("LOG-INVOICE", "ORD-APRIL", "Pending"),
            _log("LOG-SHIFT", "ORD-APRIL", "Pending", projection_type="FB Shift", last_error=None),
        ],
        orders={"ORD-APRIL": 1},
    )
    _install(monkeypatch, site)

    patch.execute()

    invoice, shift = site.logs
    assert invoice["state"] == shift["state"] == "Failed"
    assert invoice["last_error"] == f"{patch.REQUEUE_MESSAGE} Previous error: Sales Invoice projection failed"
    assert shift["last_error"] == patch.REQUEUE_MESSAGE
    for row in site.logs:
        assert (row["retry_count"], row["last_attempt_at"]) == (2, "2026-06-12 01:57:24")
        assert row["next_retry_at"] is row["lease_token"] is row["dead_lettered_at"] is None
    assert all(update_modified is False for *_rest, update_modified in site.writes)
    # The worker's own selection now includes both rows.
    assert set(patch.COMMERCIAL_PROJECTION_TYPES) == set(retry_service.COMMERCIAL_ORDER_PROJECTION_TYPES)

    writes = len(site.writes)
    patch.execute()
    assert len(site.writes) == writes, "replay must not write again"


def test_inventory_finished_failed_and_unsubmitted_rows_are_never_touched(monkeypatch) -> None:
    site = FakeSite(
        logs=[
            _log("LOG-STOCK", "ORD-APRIL", "Pending", projection_type="Stock Issue"),
            _log("LOG-POSTED", "ORD-APRIL", "Succeeded"),
            _log("LOG-FAILED", "ORD-OTHER", "Failed"),
            _log("LOG-DRAFT", "ORD-DRAFT", "Pending"),
            _log("LOG-CANCELLED", "ORD-CANCELLED", "Pending"),
            _log("LOG-ORPHAN", "ORD-GONE", "Pending"),
        ],
        orders={"ORD-APRIL": 1, "ORD-OTHER": 1, "ORD-DRAFT": 0, "ORD-CANCELLED": 2},
    )
    _install(monkeypatch, site)

    patch.execute()

    assert site.writes == []
    assert [row["state"] for row in site.logs] == ["Pending", "Succeeded", "Failed", "Pending", "Pending", "Pending"]


def test_sites_without_projection_logs_are_skipped(monkeypatch) -> None:
    site = FakeSite(logs=[], orders={}, doctype_exists=False)
    _install(monkeypatch, site)
    monkeypatch.setattr(
        patch.frappe,
        "get_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no query expected")),
        raising=False,
    )

    patch.execute()

    assert site.writes == []
