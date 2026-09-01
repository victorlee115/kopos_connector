from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.api import supplier_orders


def _order(*, modified: str = "2026-09-01 10:00:00", docstatus: int = 0):
    document = SimpleNamespace(
        company="Moonleaf",
        set_warehouse="Moonleaf Counter",
        modified=modified,
        docstatus=docstatus,
        supplier="Moonleaf Provisions",
        items=[
            SimpleNamespace(
                warehouse="Moonleaf Counter",
                qty="2",
                rate="95",
            )
        ],
        flags=SimpleNamespace(ignore_permissions=False),
    )

    def submit() -> None:
        document.docstatus = 1

    document.submit = Mock(side_effect=submit)
    return document


def _context():
    return SimpleNamespace(company="Moonleaf", warehouse="Moonleaf Counter")


def test_approval_submits_the_scoped_existing_order_once() -> None:
    document = _order()
    with patch.object(supplier_orders, "resolve_director_context", return_value=_context()), patch.object(
        frappe.db, "sql", return_value=[]
    ), patch.object(frappe, "get_doc", return_value=document):
        result = supplier_orders.approve_supplier_order(
            order="PO-1",
            outlet="Moonleaf Counter",
            expected_modified="2026-09-01 10:00:00",
        )

    assert result["status"] == "approved"
    assert document.flags.ignore_permissions is True
    document.submit.assert_called_once_with()


def test_approval_refuses_a_changed_or_multi_outlet_order() -> None:
    changed = _order(modified="2026-09-01 10:01:00")
    with patch.object(supplier_orders, "resolve_director_context", return_value=_context()), patch.object(
        frappe.db, "sql", return_value=[]
    ), patch.object(frappe, "get_doc", return_value=changed), pytest.raises(frappe.ValidationError):
        supplier_orders.approve_supplier_order(
            order="PO-1",
            outlet="Moonleaf Counter",
            expected_modified="2026-09-01 10:00:00",
        )
    changed.submit.assert_not_called()

    another_outlet = _order()
    another_outlet.items.append(
        SimpleNamespace(warehouse="Moonleaf Storeroom", qty="1", rate="10")
    )
    with patch.object(supplier_orders, "resolve_director_context", return_value=_context()), patch.object(
        frappe.db, "sql", return_value=[]
    ), patch.object(frappe, "get_doc", return_value=another_outlet), pytest.raises(frappe.ValidationError):
        supplier_orders.approve_supplier_order(
            order="PO-1",
            outlet="Moonleaf Counter",
            expected_modified="2026-09-01 10:00:00",
        )
    another_outlet.submit.assert_not_called()


def test_approval_replay_does_not_submit_again() -> None:
    document = _order(docstatus=1)
    with patch.object(supplier_orders, "resolve_director_context", return_value=_context()), patch.object(
        frappe.db, "sql", return_value=[]
    ), patch.object(frappe, "get_doc", return_value=document):
        result = supplier_orders.approve_supplier_order(
            order="PO-1",
            outlet="Moonleaf Counter",
            expected_modified="old-view-is-safe-on-replay",
        )

    assert result["status"] == "already_approved"
    document.submit.assert_not_called()
