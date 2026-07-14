from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.patches import backfill_maybank_transaction_authenticity as patch


def _prepare_schema(monkeypatch) -> None:
    monkeypatch.setattr(
        patch.frappe.db,
        "table_exists",
        lambda doctype: True,
        raising=False,
    )
    monkeypatch.setattr(
        patch.frappe.db,
        "has_column",
        lambda doctype, fieldname: True,
    )


def test_preflight_finds_duplicate_device_idempotency_scopes_deterministically():
    rows = [
        SimpleNamespace(name="MBQR-2", device_id="DEVICE-1", idempotency_key="KEY-1"),
        SimpleNamespace(name="MBQR-1", device_id="DEVICE-1", idempotency_key="KEY-1"),
        SimpleNamespace(name="MBQR-3", device_id="DEVICE-2", idempotency_key="KEY-1"),
    ]

    assert patch.find_duplicate_idempotency_scopes(rows) == [
        {
            "device_id": "DEVICE-1",
            "idempotency_key": "KEY-1",
            "names": ["MBQR-1", "MBQR-2"],
        }
    ]


def test_patch_aborts_before_mutation_and_preserves_duplicate_evidence(monkeypatch):
    rows = [
        SimpleNamespace(
            name="MBQR-1",
            device_id="DEVICE-1",
            idempotency_key="KEY-1",
            request_fingerprint=None,
        ),
        SimpleNamespace(
            name="MBQR-2",
            device_id="DEVICE-1",
            idempotency_key="KEY-1",
            request_fingerprint=None,
        ),
    ]
    _prepare_schema(monkeypatch)
    monkeypatch.setattr(patch.frappe, "get_all", lambda *args, **kwargs: rows)
    monkeypatch.setattr(
        patch.frappe.db,
        "sql",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("preflight must run before SQL mutation")
        ),
    )
    monkeypatch.setattr(
        patch.frappe.db,
        "set_value",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate evidence must not be mutated")
        ),
    )
    logged: dict[str, str] = {}
    monkeypatch.setattr(
        patch.frappe,
        "log_error",
        lambda *, title, message: logged.update(title=title, message=message),
    )

    with pytest.raises(
        patch.frappe.ValidationError,
        match="no records were changed",
    ):
        patch.execute()

    assert "MBQR-1,MBQR-2" in logged["message"]
    assert "DEVICE-1" in logged["message"]
    assert "KEY-1" in logged["message"]


def test_patch_backfills_clean_rows_after_preflight(monkeypatch):
    rows = [
        SimpleNamespace(
            name="MBQR-1",
            device_id="DEVICE-1",
            idempotency_key="KEY-1",
            request_fingerprint=None,
        ),
        SimpleNamespace(
            name="MBQR-2",
            device_id="DEVICE-1",
            idempotency_key="KEY-2",
            request_fingerprint=None,
        ),
    ]
    sql_calls: list[str] = []
    updates: list[tuple[str, str]] = []
    _prepare_schema(monkeypatch)
    monkeypatch.setattr(
        patch.frappe.db,
        "sql",
        lambda statement: sql_calls.append(statement),
    )
    monkeypatch.setattr(patch.frappe, "get_all", lambda *args, **kwargs: rows)

    def set_value(doctype, name, fieldname, value, update_modified=False):
        assert doctype == "Maybank QR Transaction"
        assert fieldname == "request_fingerprint"
        assert update_modified is False
        updates.append((name, value))

    monkeypatch.setattr(patch.frappe.db, "set_value", set_value)

    patch.execute()

    assert len(sql_calls) == 3
    assert "provider = 'maybank_qr'" in sql_calls[0]
    assert "currency = 'MYR'" in sql_calls[1]
    assert "business_date = DATE(created_at)" in sql_calls[2]
    assert len(updates) == 2
    assert updates[0][1] != updates[1][1]
    assert all(len(fingerprint) == 64 for _, fingerprint in updates)


def test_patch_reloads_schema_and_fails_closed_when_columns_remain_missing(
    monkeypatch,
):
    reloaded: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        patch.frappe,
        "reload_doc",
        lambda *args: reloaded.append(args),
        raising=False,
    )
    monkeypatch.setattr(
        patch.frappe.db,
        "table_exists",
        lambda doctype: True,
        raising=False,
    )
    monkeypatch.setattr(
        patch.frappe.db,
        "has_column",
        lambda doctype, fieldname: fieldname != "request_fingerprint",
    )
    monkeypatch.setattr(
        patch.frappe,
        "get_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing authenticity columns must abort before preflight")
        ),
    )

    with pytest.raises(
        patch.frappe.ValidationError,
        match="columns are unavailable",
    ):
        patch.execute()

    assert reloaded == [("kopos", "doctype", "maybank_qr_transaction")]


def test_duplicate_preflight_is_unbounded_beyond_frappe_default_page(monkeypatch):
    rows = [
        SimpleNamespace(
            name=f"MBQR-{index:02d}",
            device_id=f"DEVICE-{index:02d}",
            idempotency_key=f"KEY-{index:02d}",
            request_fingerprint=None,
        )
        for index in range(20)
    ]
    rows.extend(
        [
            SimpleNamespace(
                name="MBQR-21",
                device_id="DEVICE-DUP",
                idempotency_key="KEY-DUP",
                request_fingerprint=None,
            ),
            SimpleNamespace(
                name="MBQR-22",
                device_id="DEVICE-DUP",
                idempotency_key="KEY-DUP",
                request_fingerprint=None,
            ),
        ]
    )
    _prepare_schema(monkeypatch)

    def get_all(*_args, **kwargs):
        assert kwargs["limit_page_length"] == 0
        return rows

    monkeypatch.setattr(patch.frappe, "get_all", get_all)
    monkeypatch.setattr(patch.frappe, "log_error", lambda **_kwargs: None)
    monkeypatch.setattr(
        patch.frappe.db,
        "sql",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate beyond row 20 must block before mutation")
        ),
    )

    with pytest.raises(
        patch.frappe.ValidationError,
        match="duplicate.*no records were changed",
    ):
        patch.execute()
