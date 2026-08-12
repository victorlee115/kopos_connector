# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

COMPANY = "KoPOS Malaysia Sdn Bhd"
WAREHOUSE = "KoPOS Store - KMY"
DEVICE_ID = "SMOKE-TAB-A001"
SITE_TIMEZONE = "Asia/Kuala_Lumpur"
CAPTURED_SITE_DATETIME = datetime(2026, 8, 10, 20, 15, 30, 123456)


def _smoke_module():
    install_fake_frappe_modules()

    from kopos_connector import smoke

    return smoke


def _normalized_sql(query: str) -> str:
    return " ".join(query.split())


class AuditDatabase:
    def __init__(
        self,
        *,
        rows: dict[str, list[dict[str, Any]]] | None = None,
        fail_query: str | None = None,
    ) -> None:
        self.rows = rows or {}
        self.fail_query = fail_query
        self.events: list[tuple[Any, ...]] = []

    def rollback(self) -> None:
        raise AssertionError(
            "Frappe rollback reopens a transaction and cannot precede SET TRANSACTION"
        )

    def commit(self) -> None:
        raise AssertionError("inventory mutation evidence must never commit")

    def sql(
        self,
        query: str,
        values: tuple[Any, ...] | None = None,
        *,
        as_dict: bool = False,
    ) -> list[dict[str, Any]]:
        normalized = _normalized_sql(query)
        self.events.append(("sql", normalized, values, as_dict))
        if self.fail_query and self.fail_query in normalized:
            raise RuntimeError(f"forced query failure: {self.fail_query}")
        if normalized == "SELECT NOW(6) AS captured_site_datetime":
            return [{"captured_site_datetime": CAPTURED_SITE_DATETIME}]
        if "FROM `tabStock Entry Detail`" in normalized:
            return self.rows.get("stock_entry_details", [])
        if "FROM `tabStock Entry`" in normalized:
            return self.rows.get("stock_entries", [])
        if "FROM `tabStock Ledger Entry`" in normalized:
            return self.rows.get("stock_ledger_entries", [])
        if "FROM `tabBin`" in normalized:
            return self.rows.get("bins", [])
        return []


def _collect_audit(
    monkeypatch: pytest.MonkeyPatch,
    database: AuditDatabase,
    *,
    window_start_utc: str | None,
) -> dict[str, Any]:
    smoke = _smoke_module()

    import frappe

    monkeypatch.setattr(frappe, "db", database)
    return smoke._collect_inventory_mutation_audit(
        device_id=DEVICE_ID,
        company=COMPANY,
        warehouse=WAREHOUSE,
        site_timezone=SITE_TIMEZONE,
        window_start_utc=window_start_utc,
    )


@pytest.mark.inventory_regression
def test_zero_width_baseline_uses_kl_timezone_and_one_read_only_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = AuditDatabase()

    audit = _collect_audit(monkeypatch, database, window_start_utc=None)

    expected_utc = "2026-08-10T12:15:30.123456Z"
    expected_site = "2026-08-10 20:15:30.123456"
    assert audit["captured_at_utc"] == expected_utc
    assert audit["window_start_utc"] == expected_utc
    assert audit["window_end_utc"] == expected_utc
    assert audit["window_start_site_datetime"] == expected_site
    assert audit["window_end_site_datetime"] == expected_site
    assert audit["stock_entries"] == []
    assert audit["stock_entry_details"] == []
    assert audit["stock_ledger_entries"] == []

    sql_events = [event for event in database.events if event[0] == "sql"]
    assert [event[1] for event in sql_events[:5]] == [
        "ROLLBACK",
        "SELECT NOW(6) AS captured_site_datetime",
        "ROLLBACK",
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
    ]
    assert database.events == sql_events
    assert database.events[-1] == ("sql", "ROLLBACK", None, False)
    assert [
        query
        for _, query, *_ in sql_events[5:]
        if query.startswith("SELECT")
    ] == [
        "SELECT name, docstatus, purpose, stock_entry_type, company, creation, modified FROM `tabStock Entry` WHERE company = %s AND modified > %s AND modified <= %s ORDER BY modified ASC, name ASC",
        "SELECT detail.name, detail.parent, detail.item_code, detail.s_warehouse, detail.t_warehouse, detail.qty, detail.transfer_qty, detail.stock_uom, detail.creation, detail.modified FROM `tabStock Entry Detail` detail INNER JOIN `tabStock Entry` parent ON parent.name = detail.parent WHERE parent.company = %s AND detail.modified > %s AND detail.modified <= %s ORDER BY detail.modified ASC, detail.name ASC",
        "SELECT name, voucher_type, voucher_no, voucher_detail_no, item_code, warehouse, actual_qty, qty_after_transaction, is_cancelled, company, creation, modified FROM `tabStock Ledger Entry` WHERE company = %s AND modified > %s AND modified <= %s ORDER BY modified ASC, name ASC",
        "SELECT name, item_code, warehouse, actual_qty FROM `tabBin` WHERE warehouse = %s ORDER BY item_code ASC, warehouse ASC, name ASC",
    ]
    assert len(database.events) == 10


@pytest.mark.inventory_regression
def test_explicit_utc_window_is_converted_before_every_company_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = AuditDatabase()

    audit = _collect_audit(
        monkeypatch,
        database,
        window_start_utc="2026-08-10T10:00:00Z",
    )

    assert audit["window_start_utc"] == "2026-08-10T10:00:00.000000Z"
    assert audit["window_start_site_datetime"] == "2026-08-10 18:00:00.000000"
    assert audit["window_end_utc"] == "2026-08-10T12:15:30.123456Z"
    assert audit["window_end_site_datetime"] == "2026-08-10 20:15:30.123456"

    company_query_values = [
        event[2]
        for event in database.events
        if event[0] == "sql"
        and (
            "FROM `tabStock Entry`" in event[1]
            or "FROM `tabStock Entry Detail`" in event[1]
            or "FROM `tabStock Ledger Entry`" in event[1]
        )
    ]
    assert company_query_values == [
        (
            COMPANY,
            datetime(2026, 8, 10, 18, 0, 0),
            CAPTURED_SITE_DATETIME,
        )
    ] * 3
    bin_query = next(
        event
        for event in database.events
        if event[0] == "sql" and "FROM `tabBin`" in event[1]
    )
    assert bin_query[2] == (WAREHOUSE,)


@pytest.mark.inventory_regression
@pytest.mark.parametrize(
    "invalid_start",
    [
        "2026-08-10 10:00:00Z",
        "2026-08-10Z",
        "2026-08-10T10:00:00.1234567Z",
        "2026-08-10T10:00:00+00:00",
    ],
)
def test_inventory_window_requires_the_exact_explicit_utc_shape(
    monkeypatch: pytest.MonkeyPatch,
    invalid_start: str,
) -> None:
    smoke = _smoke_module()

    import frappe

    database = AuditDatabase()
    monkeypatch.setattr(frappe, "db", database)

    with pytest.raises(frappe.ValidationError, match="explicit UTC timestamp"):
        smoke._collect_inventory_mutation_audit(
            device_id=DEVICE_ID,
            company=COMPANY,
            warehouse=WAREHOUSE,
            site_timezone=SITE_TIMEZONE,
            window_start_utc=invalid_start,
        )

    assert database.events == []


@pytest.mark.inventory_regression
def test_audit_rows_and_digests_are_canonical_and_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        "stock_entries": [
            {
                "modified": datetime(2026, 8, 10, 19, 5, 0, 100),
                "name": "STE-Café-1",
                "company": COMPANY,
                "docstatus": 1,
                "purpose": "Material Issue",
                "stock_entry_type": "Material Issue",
                "creation": date(2026, 8, 10),
            }
        ],
        "stock_entry_details": [
            {
                "name": "SED-1",
                "parent": "STE-Café-1",
                "item_code": "BEAN-1",
                "s_warehouse": WAREHOUSE,
                "t_warehouse": None,
                "qty": Decimal("1.2500"),
                "transfer_qty": Decimal("1.2500"),
                "stock_uom": "Kg",
                "creation": datetime(2026, 8, 10, 19, 5, 0),
                "modified": datetime(2026, 8, 10, 19, 5, 0, 100),
            }
        ],
        "stock_ledger_entries": [
            {
                "name": "SLE-1",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-Café-1",
                "voucher_detail_no": "SED-1",
                "item_code": "BEAN-1",
                "warehouse": WAREHOUSE,
                "actual_qty": Decimal("-1.2500"),
                "qty_after_transaction": Decimal("98.7500"),
                "is_cancelled": 0,
                "company": COMPANY,
                "creation": datetime(2026, 8, 10, 19, 5, 0),
                "modified": datetime(2026, 8, 10, 19, 5, 0, 100),
            }
        ],
        "bins": [
            {
                "name": "BIN-1",
                "item_code": "BEAN-1",
                "warehouse": WAREHOUSE,
                "actual_qty": Decimal("98.7500"),
            }
        ],
    }
    database = AuditDatabase(rows=rows)

    audit = _collect_audit(
        monkeypatch,
        database,
        window_start_utc="2026-08-10T10:00:00.000000Z",
    )

    result_fields = [
        "stock_entries",
        "stock_entry_details",
        "stock_ledger_entries",
    ]
    assert audit["result_counts"] == {fieldname: 1 for fieldname in result_fields}
    assert audit["stock_entry_details"][0]["qty"] == "1.2500"
    assert audit["stock_ledger_entries"][0]["actual_qty"] == "-1.2500"
    assert audit["stock_entries"][0]["creation"] == "2026-08-10"
    assert audit["stock_entries"][0]["modified"] == (
        "2026-08-10 19:05:00.000100"
    )
    for fieldname in result_fields:
        canonical_bytes = json.dumps(
            audit[fieldname],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        assert audit["result_sha256"][fieldname] == hashlib.sha256(
            canonical_bytes
        ).hexdigest()
    canonical_bin_bytes = json.dumps(
        [
            {
                "actual_qty": "98.7500",
                "item_code": "BEAN-1",
                "name": "BIN-1",
                "warehouse": WAREHOUSE,
            }
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert audit["bin_snapshot"] == {
        "row_count": 1,
        "rows_sha256": hashlib.sha256(canonical_bin_bytes).hexdigest(),
    }

    required_keys = {
        "schema_version",
        "source",
        "read_only",
        "complete",
        "snapshot_consistency",
        "device_id",
        "company",
        "warehouse",
        "site_timezone",
        "captured_at_utc",
        "window_start_utc",
        "window_end_utc",
        "window_start_site_datetime",
        "window_end_site_datetime",
        "query_identity",
        "stock_entries",
        "stock_entry_details",
        "stock_ledger_entries",
        "result_counts",
        "result_sha256",
        "bin_snapshot",
    }
    assert set(audit) == required_keys
    for query in audit["query_identity"].values():
        if isinstance(query, dict) and "fields" in query:
            assert len(query["fields"]) == len(set(query["fields"]))


@pytest.mark.inventory_regression
def test_canonical_digest_matches_javascript_stable_json_contract() -> None:
    smoke = _smoke_module()
    value = {
        "z": [True, None, "Café"],
        "a": {"y": "2.00", "x": 1},
    }
    canonical = b'{"a":{"x":1,"y":"2.00"},"z":[true,null,"Caf\xc3\xa9"]}'

    assert smoke._canonical_json_sha256(value) == hashlib.sha256(canonical).hexdigest()


@pytest.mark.inventory_regression
def test_inventory_query_failure_always_rolls_back_without_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = AuditDatabase(fail_query="FROM `tabStock Ledger Entry`")

    with pytest.raises(RuntimeError, match="forced query failure"):
        _collect_audit(
            monkeypatch,
            database,
            window_start_utc="2026-08-10T10:00:00Z",
        )

    rollback_event = ("sql", "ROLLBACK", None, False)
    assert database.events[-1] == rollback_event
    assert sum(event == rollback_event for event in database.events) == 3


@pytest.mark.inventory_regression
def test_boundary_query_failure_always_closes_the_implicit_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = AuditDatabase(fail_query="SELECT NOW(6)")

    with pytest.raises(RuntimeError, match="forced query failure"):
        _collect_audit(monkeypatch, database, window_start_utc=None)

    assert database.events == [
        ("sql", "ROLLBACK", None, False),
        ("sql", "SELECT NOW(6) AS captured_site_datetime", None, True),
        ("sql", "ROLLBACK", None, False),
    ]


def test_invoice_payment_dump_proves_configured_bank_account_and_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe

    observed_calls: list[tuple[Any, ...]] = []

    def get_value(
        doctype: str,
        name_or_filters: Any,
        fieldname: Any,
        *,
        as_dict: bool = False,
    ) -> Any:
        observed_calls.append(
            (doctype, name_or_filters, fieldname, as_dict)
        )
        if doctype == "Account":
            assert name_or_filters == "KoPOS QR Clearing - KMY"
            assert fieldname == ["account_currency", "account_type", "company"]
            assert as_dict is True
            return {
                "account_currency": "MYR",
                "account_type": "Bank",
                "company": COMPANY,
            }
        if doctype == "Mode of Payment Account":
            assert name_or_filters == {
                "parent": "DuitNow QR",
                "company": COMPANY,
            }
            assert fieldname == "default_account"
            assert as_dict is False
            return "KoPOS QR Clearing - KMY"
        raise AssertionError(f"unexpected evidence query: {doctype}")

    monkeypatch.setattr(
        frappe,
        "db",
        SimpleNamespace(get_value=get_value),
    )
    invoice = SimpleNamespace(
        company=COMPANY,
        payments=[
            SimpleNamespace(
                mode_of_payment="DuitNow QR",
                amount=Decimal("12.30"),
                account="KoPOS QR Clearing - KMY",
                custom_fb_source_payment_id="PAY-QR-0001",
            )
        ],
    )

    assert smoke._collect_invoice_payments(invoice) == [
        {
            "mode_of_payment": "DuitNow QR",
            "amount": "12.30",
            "account": "KoPOS QR Clearing - KMY",
            "account_currency": "MYR",
            "account_type": "Bank",
            "account_company": COMPANY,
            "configured_mode_of_payment_account": "KoPOS QR Clearing - KMY",
            "payment_id": "PAY-QR-0001",
        }
    ]
    assert len(observed_calls) == 2


def test_invoice_payment_dump_does_not_hide_a_mismatched_configured_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe

    def get_value(
        doctype: str,
        name_or_filters: Any,
        fieldname: Any,
        *,
        as_dict: bool = False,
    ) -> Any:
        if doctype == "Account":
            return {
                "account_currency": "MYR",
                "account_type": "Bank",
                "company": COMPANY,
            }
        if doctype == "Mode of Payment Account":
            return "Wrong Bank - KMY"
        raise AssertionError(f"unexpected evidence query: {doctype}")

    monkeypatch.setattr(
        frappe,
        "db",
        SimpleNamespace(get_value=get_value),
    )
    invoice = SimpleNamespace(
        company=COMPANY,
        payments=[
            SimpleNamespace(
                mode_of_payment="DuitNow QR",
                amount=Decimal("12.30"),
                account="KoPOS QR Clearing - KMY",
                custom_fb_source_payment_id="PAY-QR-0001",
            )
        ],
    )

    payment = smoke._collect_invoice_payments(invoice)[0]

    assert payment["account"] == "KoPOS QR Clearing - KMY"
    assert payment["configured_mode_of_payment_account"] == "Wrong Bank - KMY"
