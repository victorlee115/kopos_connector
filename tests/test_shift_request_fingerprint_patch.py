from __future__ import annotations

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.patches import backfill_shift_request_fingerprints as patch


def test_patch_backfills_fail_closed_shift_guards_without_losing_rows(monkeypatch):
    rows = [
        {
            "name": "FB-SHIFT-OPEN",
            "status": "Open",
            "closed_at": None,
            "open_idempotency_key": None,
            "open_request_fingerprint": None,
            "close_idempotency_key": None,
            "close_request_fingerprint": None,
        },
        {
            "name": "FB-SHIFT-CLOSED",
            "status": "Closed",
            "closed_at": "2026-07-14 10:00:00",
            "open_idempotency_key": None,
            "open_request_fingerprint": None,
            "close_idempotency_key": None,
            "close_request_fingerprint": None,
        },
    ]
    updates: list[tuple[str, dict[str, str]]] = []

    monkeypatch.setattr(patch.frappe, "reload_doc", lambda *_args: None, raising=False)
    monkeypatch.setattr(
        patch.frappe.db,
        "table_exists",
        lambda doctype: doctype == "FB Shift",
        raising=False,
    )
    monkeypatch.setattr(patch.frappe.db, "has_column", lambda *_args: True)
    monkeypatch.setattr(
        patch.frappe.db,
        "sql",
        lambda statement, as_dict=False: rows,
    )

    def set_value(
        doctype: str,
        name: str,
        values: dict[str, str],
        *,
        update_modified: bool = False,
    ) -> None:
        assert doctype == "FB Shift"
        assert update_modified is False
        updates.append((name, values))

    monkeypatch.setattr(patch.frappe.db, "set_value", set_value)

    patch.execute()

    assert updates[0][0] == "FB-SHIFT-OPEN"
    assert set(updates[0][1]) == {
        "open_idempotency_key",
        "open_request_fingerprint",
    }
    assert updates[1][0] == "FB-SHIFT-CLOSED"
    assert set(updates[1][1]) == patch.REQUIRED_COLUMNS
    assert len(updates[0][1]["open_idempotency_key"]) == 64
    assert len(updates[1][1]["close_idempotency_key"]) == 64
    assert len(updates[1][1]["close_request_fingerprint"]) == 64


def test_patch_preserves_existing_exact_shift_proofs(monkeypatch):
    monkeypatch.setattr(patch.frappe, "reload_doc", lambda *_args: None, raising=False)
    monkeypatch.setattr(
        patch.frappe.db, "table_exists", lambda *_args: True, raising=False
    )
    monkeypatch.setattr(patch.frappe.db, "has_column", lambda *_args: True)
    monkeypatch.setattr(
        patch.frappe.db,
        "sql",
        lambda statement, as_dict=False: [
            {
                "name": "FB-SHIFT-1",
                "status": "Closed",
                "closed_at": "2026-07-14 10:00:00",
                "open_idempotency_key": "open-1",
                "open_request_fingerprint": "a" * 64,
                "close_idempotency_key": "close-1",
                "close_request_fingerprint": "b" * 64,
            }
        ],
    )
    monkeypatch.setattr(
        patch.frappe.db,
        "set_value",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing exact proofs must not be overwritten")
        ),
    )

    patch.execute()


def test_patch_fails_closed_when_reloaded_schema_is_still_missing(monkeypatch):
    monkeypatch.setattr(patch.frappe, "reload_doc", lambda *_args: None, raising=False)
    monkeypatch.setattr(
        patch.frappe.db, "table_exists", lambda *_args: True, raising=False
    )
    monkeypatch.setattr(
        patch.frappe.db,
        "has_column",
        lambda doctype, fieldname: fieldname != "close_request_fingerprint",
    )
    monkeypatch.setattr(
        patch.frappe.db,
        "sql",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing guards must abort before reading rows")
        ),
    )

    with pytest.raises(
        patch.frappe.ValidationError,
        match="columns are unavailable",
    ):
        patch.execute()
