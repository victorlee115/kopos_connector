"""FB Order submission is idempotent, and the database is what guarantees it.

`FB Order.external_idempotency_key` carries `"unique": 1` in its doctype JSON, so
Frappe creates a UNIQUE index for it (frappe/database/schema.py adds a unique
constraint for any non-text column with that flag). A concurrent duplicate insert
therefore fails at the database; `submit_order_payload`'s `except` branch rolls
back, re-reads, and returns `duplicate`.

That correction matters, because docs/audits/idempotency.md originally recorded
the opposite - it listed FB Order, FB Shift and several Maybank QR fields as
having no unique constraint, and built a "structural gap" on top of that. Six of
its ten rows were wrong. These tests pin the schema so the claim cannot drift
again in either direction.

The device lock every public door takes is still real and still checked here, but
it is defence in depth, not the thing that makes a retried sale safe.

Evidence levels: `test_the_schema_declares_the_idempotency_keys_unique` and the
two AST tests are `static`. The concurrency test is `mocked_unit` - its table is
a Python dict that enforces the same constraint the real schema declares, so it
exercises the real recovery branch, not MariaDB.
"""

from __future__ import annotations

import ast
import importlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import BrokenBarrierError
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
fb_orders = importlib.import_module("kopos_connector.kopos.api.fb_orders")

CONNECTOR_ROOT = Path(__file__).resolve().parents[1] / "kopos_connector"

# Captured before any patching, so a test can put the real comparison back.
REAL_VALIDATE_FINGERPRINT = fb_orders._validate_existing_order_fingerprint

IDEMPOTENCY_KEY = "one-key-retried-by-one-tablet"
DEVICE_LOCK = "lock_device_for_operational_mutation"
ORDER_CREATING_PAYLOADS = frozenset(
    {"submit_order_payload", "prepare_automatic_qr_sale_payload"}
)

# The keys the database must enforce, as {doctype slug: (fieldname, ...)}.
# Frappe turns `"unique": 1` on a Data field into a UNIQUE index at migrate time.
UNIQUE_IDEMPOTENCY_KEYS = {
    "fb_order": ("external_idempotency_key",),
    "fb_shift": ("open_idempotency_key", "close_idempotency_key"),
    "maybank_qr_transaction": ("consumption_key", "invoice_consumption_key"),
    # The refund dedupe key. request_fingerprint next to it is only a payload
    # equality check - it detects a different payload under the same return_id -
    # so return_id is the one that has to be enforced.
    "fb_return_event": ("return_id",),
    # Where a manual QR reconciliation's financial effect is recorded. The copy
    # of reconciliation_idempotency_key on the FB Order Payment child row is a
    # carried value with no constraint; these three are what actually stop a
    # second effect, including one reconciliation per payment row and per invoice.
    "manual_qr_reconciliation": (
        "reconciliation_idempotency_key",
        "fb_order_payment",
        "sales_invoice",
    ),
}

# Callers that create an order without taking the device lock, and why that is
# accepted. A new entry here is a decision, not a formality.
UNGUARDED_BY_DESIGN = {
    (
        "acceptance/restored_inventory_acceptance.py",
        "_ensure_order",
    ): "single-threaded acceptance harness; runs against a restored copy, not a request",
    (
        "kopos/api/fb_orders.py",
        "submit_order",
    ): "not whitelisted; reachable only from smoke.py - pinned by "
    "test_request_reading_submit_order_is_not_a_public_door",
}


class _UniqueOrderTable:
    """An FB Order table with the UNIQUE index the doctype JSON declares.

    `insert` refuses a second row carrying a key it already holds, the way the
    database does. That refusal is the behaviour under test: it is what turns an
    unlocked read-then-insert into a safe retry.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Any] = {}
        self._guard = threading.Lock()

    def name_for_key(self, key: str) -> str | None:
        with self._guard:
            row = self._by_key.get(key)
        return row.name if row else None

    def insert(self, row: Any) -> None:
        with self._guard:
            if row.external_idempotency_key in self._by_key:
                raise fb_orders.frappe.DuplicateEntryError(
                    "Duplicate entry for key 'external_idempotency_key'"
                )
            row.name = f"FB-ORD-{len(self._by_key) + 1:03d}"
            self._by_key[row.external_idempotency_key] = row

    def get(self, name: str) -> Any:
        with self._guard:
            return next(row for row in self._by_key.values() if row.name == name)

    def __len__(self) -> int:
        with self._guard:
            return len(self._by_key)


class _OrderDoc:
    def __init__(self, table: _UniqueOrderTable, key: str, fingerprint: str = "") -> None:
        self._table = table
        self.external_idempotency_key = key
        self.request_fingerprint = fingerprint
        self.name = ""
        self.docstatus = 0

    def insert(self, ignore_permissions: bool = False) -> None:
        self._table.insert(self)

    def submit(self) -> None:
        self.docstatus = 1


def _payload(device_id: str = "DEVICE-A") -> dict[str, Any]:
    return {
        "external_idempotency_key": IDEMPOTENCY_KEY,
        "shift": "SHIFT-1",
        "device_id": device_id,
        "staff_id": "STAFF-1",
        "request_fingerprint": f"fingerprint-of-{device_id}",
    }


def _doctype_json(slug: str) -> dict[str, Any]:
    path = next(
        p
        for p in CONNECTOR_ROOT.rglob(f"{slug}.json")
        if "doctype" in p.parts and "build" not in p.parts
    )
    return json.loads(path.read_text())


def _submit_seams(table: _UniqueOrderTable, barrier: threading.Barrier) -> list[Any]:
    """Patch everything around the duplicate check, so the check itself is real."""

    def get_existing(key: str) -> str | None:
        name = table.name_for_key(key)
        # Both threads complete their duplicate check before either inserts.
        # That is reachable because the read takes no lock - which is exactly why
        # the database constraint, not the read, has to be what protects this.
        try:
            barrier.wait(timeout=0.5)
        except BrokenBarrierError:
            pass
        return name

    def validate_shift(**_kwargs: Any) -> Any:
        return SimpleNamespace(name="SHIFT-1", status="Open")

    return [
        patch.object(fb_orders, "_get_existing_fb_order_name", side_effect=get_existing),
        patch.object(fb_orders, "_normalize_submit_order_payload", side_effect=dict),
        patch.object(fb_orders, "_validate_new_submit_order_state", side_effect=dict),
        patch.object(fb_orders, "_validate_submit_shift", side_effect=validate_shift),
        patch.object(fb_orders, "recorded_after_shift_close", return_value=0),
        patch.object(fb_orders, "_validate_existing_order_fingerprint"),
        patch.object(
            fb_orders,
            "_build_fb_order",
            side_effect=lambda validated: _OrderDoc(
                table,
                validated["external_idempotency_key"],
                validated.get("request_fingerprint", ""),
            ),
        ),
        patch.object(
            fb_orders,
            "_build_submit_response",
            side_effect=lambda status, doc: {"status": status, "fb_order": doc.name},
        ),
        patch.object(
            fb_orders.frappe, "get_doc", side_effect=lambda _dt, name: table.get(name)
        ),
    ]


def _function_call_lines(path: Path, function_name: str) -> dict[str, list[int]]:
    tree = ast.parse(path.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    calls: dict[str, list[int]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        called = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        calls.setdefault(called, []).append(node.lineno)
    return calls


def _is_whitelisted(function: ast.FunctionDef) -> bool:
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else target.id
            if isinstance(target, ast.Name)
            else ""
        )
        if name == "whitelist":
            return True
    return False


class TestOrderIdempotency(unittest.TestCase):
    def test_the_schema_declares_the_idempotency_keys_unique(self) -> None:
        """The guarantee lives in the database. This is the pin that matters.

        Frappe creates a UNIQUE index for a `"unique": 1` Data field, so a
        concurrent duplicate insert fails at the database rather than producing a
        second financial record. Removing the flag would silently move the whole
        guarantee into application code that does its dedupe read without a lock.
        """
        for slug, fieldnames in UNIQUE_IDEMPOTENCY_KEYS.items():
            doctype = _doctype_json(slug)
            by_name = {field["fieldname"]: field for field in doctype["fields"]}
            for fieldname in fieldnames:
                with self.subTest(doctype=slug, field=fieldname):
                    field = by_name.get(fieldname)
                    self.assertIsNotNone(field, f"{slug}.{fieldname} is missing")
                    self.assertEqual(
                        field.get("unique", 0),
                        1,
                        f"{slug}.{fieldname} lost its unique constraint. Frappe "
                        "builds the UNIQUE index from this flag; without it a "
                        "retried request can create a second financial record.",
                    )
                    # Frappe skips the unique index on text/longtext columns.
                    self.assertNotIn(
                        field.get("fieldtype"),
                        {"Text", "Long Text", "Code", "Text Editor"},
                        f"{slug}.{fieldname} is a text column, so Frappe will not "
                        "create the unique index even though the flag is set",
                    )

    def test_a_concurrent_duplicate_is_reported_as_duplicate_not_an_error(self) -> None:
        """The recovery branch is real code, and this is what exercises it.

        Two callers complete their duplicate check before either inserts. The
        loser's insert violates the unique index; `submit_order_payload` rolls
        back, re-reads, and reports `duplicate`. Exactly one order exists.
        """
        table = _UniqueOrderTable()
        barrier = threading.Barrier(2)

        with ExitStack() as stack:
            for seam in _submit_seams(table, barrier):
                stack.enter_context(seam)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(fb_orders.submit_order_payload, _payload())
                    for _ in range(2)
                ]
                results = [future.result(timeout=5) for future in futures]

        self.assertEqual(len(table), 1, "the unique key must admit exactly one order")
        self.assertEqual(sorted(r["status"] for r in results), ["duplicate", "ok"])
        self.assertEqual(
            len({r["fb_order"] for r in results}),
            1,
            "both callers must be told about the same FB Order",
        )

    def test_every_caller_of_an_order_creating_payload_locks_first(self) -> None:
        """Defence in depth: every public door still locks before it delegates.

        This is no longer what makes a retry safe - the unique key is - but the
        lock also revalidates credentials and refuses a device with an unresolved
        safe reset, so a caller that skips it loses those too. Walks the whole
        connector rather than an allowlist, so a new door cannot quietly appear.
        """
        unguarded: list[tuple[str, str]] = []
        found_any = False

        for path in sorted(CONNECTOR_ROOT.rglob("*.py")):
            relative = path.relative_to(CONNECTOR_ROOT).as_posix()
            if "/tests/" in f"/{relative}" or relative.startswith("build/"):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name in ORDER_CREATING_PAYLOADS:
                    continue  # the definition is not a caller
                calls = _function_call_lines(path, node.name)
                creating = [
                    line
                    for name in ORDER_CREATING_PAYLOADS
                    for line in calls.get(name, [])
                ]
                if not creating:
                    continue
                found_any = True
                guard = calls.get(DEVICE_LOCK, [])
                if not guard or max(guard) > min(creating):
                    unguarded.append((relative, node.name))

        self.assertTrue(found_any, "the AST walk found no callers at all - check the paths")
        self.assertEqual(
            sorted(unguarded),
            sorted(UNGUARDED_BY_DESIGN),
            "a caller creates an FB Order without taking the device lock first. "
            "Add lock_device_for_operational_mutation before the call, or record "
            "why it is safe in UNGUARDED_BY_DESIGN.",
        )

    def test_request_reading_submit_order_is_not_a_public_door(self) -> None:
        """`kopos.api.fb_orders.submit_order` must stay unwhitelisted.

        It reads an HTTP request payload and creates an order with no device
        lock, so it skips credential revalidation and the safe-reset check. The
        audited public door is `kopos_connector.api.fb_orders.submit_order`,
        which locks first.
        """
        path = CONNECTOR_ROOT / "kopos/api/fb_orders.py"
        tree = ast.parse(path.read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "submit_order"
        )

        self.assertFalse(
            _is_whitelisted(function),
            "kopos.api.fb_orders.submit_order is whitelisted. It reads a request "
            "payload and creates an FB Order without the device lock, so it must "
            "either take the lock or stay internal.",
        )
        self.assertIn(
            "_get_request_payload",
            _function_call_lines(path, "submit_order"),
            "this pin assumes the function still reads a request payload; if it "
            "no longer does, re-check whether the pin is still the right one",
        )

        guarded = CONNECTOR_ROOT / "api/fb_orders.py"
        guarded_calls = _function_call_lines(guarded, "submit_order")
        self.assertLess(
            max(guarded_calls[DEVICE_LOCK]),
            min(guarded_calls["submit_order_payload"]),
            "the audited public door must lock before it delegates",
        )

    def test_a_colliding_key_from_another_device_is_refused_cleanly(self) -> None:
        """Audit question 5: a global unique key means collisions get refused.

        The key is unique across the whole table, not per device, so a key that
        collides across devices is refused rather than duplicated. What matters
        is that the refusal is a ValidationError carrying a usable message - the
        public door maps that to HTTP 400 - and not an unhandled error that would
        surface as a 500.
        """
        table = _UniqueOrderTable()
        barrier = threading.Barrier(2)

        with ExitStack() as stack:
            for seam in _submit_seams(table, barrier):
                stack.enter_context(seam)
            # Let the real fingerprint comparison run rather than the no-op seam.
            stack.enter_context(
                patch.object(
                    fb_orders,
                    "_validate_existing_order_fingerprint",
                    REAL_VALIDATE_FINGERPRINT,
                )
            )
            first = fb_orders.submit_order_payload(_payload("DEVICE-A"))
            with self.assertRaises(fb_orders.frappe.ValidationError) as caught:
                fb_orders.submit_order_payload(_payload("DEVICE-B"))

        self.assertEqual(first["status"], "ok")
        self.assertEqual(len(table), 1, "the collision must not create a second order")
        self.assertIn("different canonical", str(caught.exception))

        # The public door turns that class into a 400, not a 500.
        door = _function_call_lines(CONNECTOR_ROOT / "api/__init__.py", "submit_order")
        self.assertIn("_validation_error_payload", door)


if __name__ == "__main__":
    unittest.main()
