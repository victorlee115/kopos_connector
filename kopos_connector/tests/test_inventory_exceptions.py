from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.inventory_autopilot import exceptions


class InventoryExceptionTests(TestCase):
    def test_todo_uses_explicit_responsible_owner(self):
        todo_payload: dict[str, object] = {}
        todo = SimpleNamespace(insert=lambda **_kwargs: None)

        def get_doc(payload):
            todo_payload.update(payload)
            return todo

        database = SimpleNamespace(
            exists=lambda doctype, filters=None: doctype == "DocType" and filters == "ToDo",
            get_value=lambda *args, **kwargs: None,
        )
        with patch.object(exceptions.frappe, "db", database), patch.object(
            exceptions.frappe, "get_doc", side_effect=get_doc
        ):
            exceptions._ensure_todo(
                "INV-EX-1",
                {"summary": "Needs review", "next_action": "Ask the director"},
                responsible_owner="director@example.com",
            )

        self.assertEqual(todo_payload["owner"], "director@example.com")

    def test_todo_uses_source_document_owner_before_session_fallback(self):
        todo_payload: dict[str, object] = {}
        todo = SimpleNamespace(insert=lambda **_kwargs: None)

        def get_doc(payload):
            todo_payload.update(payload)
            return todo

        database = SimpleNamespace(
            exists=lambda doctype, filters=None: doctype == "DocType" and filters == "ToDo",
            get_value=lambda doctype, name, fieldname: (
                "director@example.com"
                if (doctype, name, fieldname) == ("FB Inventory Policy", "POLICY-1", "owner")
                else None
            ),
        )
        with patch.object(exceptions.frappe, "db", database), patch.object(
            exceptions.frappe, "get_doc", side_effect=get_doc
        ), patch.object(exceptions.frappe.session, "user", "Administrator"):
            exceptions._ensure_todo(
                "INV-EX-1",
                {
                    "summary": "Needs review",
                    "next_action": "Ask the director",
                    "source_doctype": "FB Inventory Policy",
                    "source_name": "POLICY-1",
                },
            )

        self.assertEqual(todo_payload["owner"], "director@example.com")

    def test_resolve_closes_exact_record_and_todo(self):
        writes: list[tuple] = []
        database = SimpleNamespace(
            get_value=lambda *args, **kwargs: "INV-EX-1",
            exists=lambda doctype, filters=None: doctype == "DocType" and filters == "ToDo",
            set_value=lambda *args, **kwargs: writes.append((args, kwargs)),
        )
        with patch.object(exceptions.frappe, "db", database), patch.object(
            exceptions.frappe, "get_all", return_value=["TODO-1"], create=True
        ):
            result = exceptions.resolve_inventory_exception(
                reason_code="inventory_plan_gate_failed",
                company="JiJi",
                warehouse="Outlet A",
                source_doctype="FB Inventory Policy",
                source_name="POLICY-A",
            )

        self.assertEqual(result, "INV-EX-1")
        self.assertEqual(writes[0][0][0:2], ("FB Inventory Exception", "INV-EX-1"))
        self.assertEqual(writes[0][0][2]["status"], "Resolved")
        self.assertEqual(writes[1][0][0:3], ("ToDo", "TODO-1", "status"))
        self.assertEqual(writes[1][0][3], "Closed")

    def test_resolve_is_noop_when_condition_never_opened(self):
        writes: list[tuple] = []
        database = SimpleNamespace(
            get_value=lambda *args, **kwargs: None,
            exists=lambda *args, **kwargs: False,
            set_value=lambda *args, **kwargs: writes.append((args, kwargs)),
        )
        with patch.object(exceptions.frappe, "db", database):
            result = exceptions.resolve_inventory_exception(reason_code="never-opened")

        self.assertIsNone(result)
        self.assertEqual(writes, [])
