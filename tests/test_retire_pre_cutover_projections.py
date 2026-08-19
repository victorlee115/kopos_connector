from __future__ import annotations

import importlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
install_fake_frappe_modules()

patch = importlib.import_module(
    "kopos_connector.patches.retire_pre_cutover_stock_issue_projections"
)
projection_worker = importlib.import_module(
    "kopos_connector.kopos.services.inventory_autopilot.projection_worker"
)


CUTOVER_AT = datetime(2026, 8, 1, 0, 0, 0)


class FakeSite:
    """Minimal projection-log/order store that records every mutation."""

    def __init__(self, logs: list[dict[str, Any]], orders: list[dict[str, Any]], *, policy: bool = True) -> None:
        self.logs = logs
        self.orders = orders
        self.policy = policy
        self.sql_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0

    def get_all(self, doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        filters = kwargs.get("filters") or {}
        if doctype == "FB Projection Log":
            wanted = set(filters["state"][1])
            return [
                {"name": row["name"], "source_name": row["source_name"]}
                for row in self.logs
                if row["projection_type"] == filters["projection_type"]
                and row["source_doctype"] == filters["source_doctype"]
                and row["state"] in wanted
            ]
        if doctype == "FB Order":
            wanted = set(filters["name"][1])
            return [dict(row) for row in self.orders if row["name"] in wanted]
        if doctype == "FB Inventory Policy":
            if not self.policy:
                return []
            return ["POLICY-1"] if kwargs.get("pluck") == "name" else [{"name": "POLICY-1"}]
        raise AssertionError(f"unexpected get_all for {doctype}")

    def get_doc(self, doctype: str, name: str) -> Any:
        assert (doctype, name) == ("FB Inventory Policy", "POLICY-1")
        return SimplePolicy()

    def sql(self, statement: str, values: tuple[Any, ...] = ()) -> None:
        self.sql_calls.append((statement, values))
        assert "`tabFB Projection Log`" in statement
        assert "Stock Entry" not in statement and "GL Entry" not in statement
        retired = set(values[1 : -(1 + len(patch.LEGACY_STATES))])
        for row in self.logs:
            if row["name"] in retired and row["state"] in patch.LEGACY_STATES:
                row["state"] = values[0]
                row["next_retry_at"] = None
                row["lease_token"] = None

    def commit(self) -> None:
        self.commits += 1


class SimplePolicy:
    cutover_token = "CUTOVER-1"
    cutover_at = CUTOVER_AT


def _install(monkeypatch, site: FakeSite) -> None:
    for module in (patch, projection_worker):
        monkeypatch.setattr(module.frappe, "get_all", site.get_all, raising=False)
        monkeypatch.setattr(module.frappe, "get_doc", site.get_doc, raising=False)
    monkeypatch.setattr(patch.frappe.db, "exists", lambda *_args: True)
    monkeypatch.setattr(patch.frappe, "reload_doc", lambda *_args: None, raising=False)
    monkeypatch.setattr(patch.frappe.db, "sql", site.sql)
    monkeypatch.setattr(patch.frappe.db, "commit", site.commit)


def _log(name: str, order: str, state: str, projection_type: str = "Stock Issue") -> dict[str, Any]:
    return {
        "name": name,
        "source_name": order,
        "source_doctype": "FB Order",
        "projection_type": projection_type,
        "state": state,
        "last_error": f"legacy failure for {order}",
        "dead_lettered_at": "2026-07-01 10:00:00",
        "retry_count": 8,
        "next_retry_at": "2026-07-01 11:00:00",
        "lease_token": "stale-token",
    }


def _order(name: str, sale_datetime: datetime) -> dict[str, Any]:
    return {
        "name": name,
        "company": "JiJi",
        "booth_warehouse": "Outlet - JiJi",
        "sale_datetime": sale_datetime,
    }


def test_pre_cutover_failures_are_retired_and_replay_is_a_no_op(monkeypatch) -> None:
    site = FakeSite(
        logs=[
            _log("LOG-PRE-1", "ORD-PRE-1", "Failed"),
            _log("LOG-PRE-2", "ORD-PRE-2", "Dead Letter"),
        ],
        orders=[
            _order("ORD-PRE-1", datetime(2026, 7, 15, 9, 0, 0)),
            _order("ORD-PRE-2", datetime(2026, 7, 20, 9, 0, 0)),
        ],
    )
    _install(monkeypatch, site)

    patch.execute()
    assert [row["state"] for row in site.logs] == ["Not Evaluated", "Not Evaluated"]

    first_pass_sql = len(site.sql_calls)
    patch.execute()
    assert len(site.sql_calls) == first_pass_sql, "replay must not issue another update"
    assert site.commits == 1


def test_retirement_preserves_the_original_failure_evidence(monkeypatch) -> None:
    site = FakeSite(
        logs=[_log("LOG-PRE-1", "ORD-PRE-1", "Failed")],
        orders=[_order("ORD-PRE-1", datetime(2026, 7, 15, 9, 0, 0))],
    )
    _install(monkeypatch, site)

    patch.execute()

    row = site.logs[0]
    assert row["state"] == "Not Evaluated"
    assert row["last_error"] == "legacy failure for ORD-PRE-1"
    assert row["dead_lettered_at"] == "2026-07-01 10:00:00"
    assert row["retry_count"] == 8
    assert row["next_retry_at"] is None and row["lease_token"] is None


def test_post_cutover_failures_and_successes_are_never_touched(monkeypatch) -> None:
    site = FakeSite(
        logs=[
            _log("LOG-POST", "ORD-POST", "Failed"),
            _log("LOG-OK", "ORD-PRE-1", "Succeeded"),
            _log("LOG-SALES", "ORD-PRE-1", "Failed", projection_type="Sales Invoice"),
        ],
        orders=[
            _order("ORD-POST", datetime(2026, 8, 15, 9, 0, 0)),
            _order("ORD-PRE-1", datetime(2026, 7, 15, 9, 0, 0)),
        ],
    )
    _install(monkeypatch, site)

    patch.execute()

    assert site.sql_calls == [], "no Stock Issue row was pre-cutover"
    assert [row["state"] for row in site.logs] == ["Failed", "Succeeded", "Failed"]


def test_history_without_a_cutover_policy_is_pre_cutover(monkeypatch) -> None:
    """The restored backup has no policy at all, so every row is pre-cutover."""

    site = FakeSite(
        logs=[_log("LOG-PRE-1", "ORD-PRE-1", "Failed")],
        orders=[_order("ORD-PRE-1", datetime(2026, 9, 30, 9, 0, 0))],
        policy=False,
    )
    _install(monkeypatch, site)

    patch.execute()

    assert site.logs[0]["state"] == "Not Evaluated"


def test_orphaned_projection_rows_are_left_alone(monkeypatch) -> None:
    site = FakeSite(logs=[_log("LOG-ORPHAN", "ORD-GONE", "Failed")], orders=[])
    _install(monkeypatch, site)

    patch.execute()

    assert site.sql_calls == []
    assert site.logs[0]["state"] == "Failed"
