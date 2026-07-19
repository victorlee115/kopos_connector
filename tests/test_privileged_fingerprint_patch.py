from __future__ import annotations

import hashlib

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.patches import backfill_privileged_request_fingerprints as patch


def test_patch_backfills_deterministic_fail_closed_legacy_guards(monkeypatch):
    rows_by_doctype = {
        "FB Order": [
            {"name": "FB-ORDER-1", "external_idempotency_key": "SALE-1"}
        ],
        "FB Return Event": [
            {"name": "FB-RETURN-1", "return_id": "REFUND-1"}
        ],
    }
    reload_calls: list[tuple[str, str, str]] = []
    queried_doctypes: list[str] = []
    updates: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        patch.frappe.db, "table_exists", lambda doctype: True, raising=False
    )
    monkeypatch.setattr(
        patch.frappe,
        "reload_doc",
        lambda *args: reload_calls.append(args),
        raising=False,
    )
    monkeypatch.setattr(
        patch.frappe.db,
        "has_column",
        lambda doctype, fieldname: fieldname == "request_fingerprint",
    )

    def sql(statement: str, *, as_dict: bool = False):
        assert as_dict is True
        doctype = "FB Order" if "`tabFB Order`" in statement else "FB Return Event"
        queried_doctypes.append(doctype)
        return rows_by_doctype[doctype]

    def set_value(
        doctype: str,
        name: str,
        fieldname: str,
        value: str,
        *,
        update_modified: bool = False,
    ) -> None:
        assert fieldname == "request_fingerprint"
        assert update_modified is False
        updates.append((doctype, name, value))

    monkeypatch.setattr(patch.frappe.db, "sql", sql)
    monkeypatch.setattr(patch.frappe.db, "set_value", set_value)

    patch.execute()

    assert reload_calls == [
        ("kopos", "doctype", "fb_order"),
        ("kopos", "doctype", "fb_return_event"),
    ]
    assert queried_doctypes == ["FB Order", "FB Return Event"]
    assert updates == [
        (
            "FB Order",
            "FB-ORDER-1",
            hashlib.sha256(
                b"legacy-unverifiable\0FB Order\0FB-ORDER-1\0SALE-1"
            ).hexdigest(),
        ),
        (
            "FB Return Event",
            "FB-RETURN-1",
            hashlib.sha256(
                b"legacy-unverifiable\0FB Return Event\0FB-RETURN-1\0REFUND-1"
            ).hexdigest(),
        ),
    ]


def test_patch_fails_closed_when_reloaded_fingerprint_column_is_missing(
    monkeypatch,
):
    reload_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        patch.frappe.db, "table_exists", lambda doctype: True, raising=False
    )
    monkeypatch.setattr(
        patch.frappe,
        "reload_doc",
        lambda *args: reload_calls.append(args),
        raising=False,
    )
    monkeypatch.setattr(patch.frappe.db, "has_column", lambda *args: False)
    monkeypatch.setattr(
        patch.frappe.db,
        "sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("schema guard must run before querying rows")
        ),
    )

    with pytest.raises(
        patch.frappe.ValidationError,
        match="column is unavailable after reload",
    ):
        patch.execute()

    assert reload_calls == [("kopos", "doctype", "fb_order")]
