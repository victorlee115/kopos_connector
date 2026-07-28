from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
install_fake_frappe_modules()

public_api = importlib.import_module("kopos_connector.api")
shifts = importlib.import_module("kopos_connector.api.shifts")


STAFF_ID = "staff@example.test"


def _device_doc(*, assigned: bool = True, active: bool = True) -> SimpleNamespace:
    device_users = []
    if assigned:
        device_users.append(
            SimpleNamespace(
                user=STAFF_ID,
                active=1 if active else 0,
                can_open_shift=1,
                can_close_shift=1,
            )
        )
    return SimpleNamespace(
        name="DEVICE-1",
        device_id="DEVICE-1",
        pos_profile="Counter 1",
        device_users=device_users,
    )


def _open_payload() -> dict[str, Any]:
    return {
        "idempotency_key": "open-idem-1",
        "device_id": "DEVICE-1",
        "staff_id": STAFF_ID,
        "shift_id": "SHIFT-REQUESTED",
        "opening_float_sen": 1000,
        "opened_at": "2026-07-17T10:00:00+08:00",
    }


def _patch_open_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing_shift: SimpleNamespace | None = None,
    conflicts: list[SimpleNamespace] | None = None,
) -> dict[str, int]:
    calls = {"conflict_query": 0, "manager_approval": 0, "created": 0}

    monkeypatch.setattr(shifts, "get_device_doc", lambda device_id: _device_doc())
    monkeypatch.setattr(
        shifts.frappe.db,
        "get_value",
        lambda doctype, *_args, **_kwargs: 1
        if doctype in {"KoPOS Device", "User"}
        else None,
    )
    monkeypatch.setattr(
        shifts.frappe.db,
        "exists",
        lambda doctype, *_args, **_kwargs: doctype == "User",
    )
    monkeypatch.setattr(
        shifts.frappe,
        "get_cached_doc",
        lambda *_args, **_kwargs: SimpleNamespace(
            company="JiJi",
            warehouse="WH-1",
        ),
    )
    monkeypatch.setattr(shifts, "_lock_open_shift_scope", lambda *_args: None)
    monkeypatch.setattr(
        shifts,
        "_find_fb_shift_for_update",
        lambda _shift_id: existing_shift,
    )

    def find_conflicts(_device_id: str, _staff_id: str) -> list[SimpleNamespace]:
        calls["conflict_query"] += 1
        return list(conflicts or [])

    def verify_manager(*_args: Any, **_kwargs: Any) -> None:
        calls["manager_approval"] += 1
        raise AssertionError("manager approval must not run for a proven conflict")

    def create_shift(*_args: Any, **_kwargs: Any) -> None:
        calls["created"] += 1
        raise AssertionError("a proven conflict must not create an FB Shift")

    monkeypatch.setattr(
        shifts,
        "_find_open_shift_conflicts_for_update",
        find_conflicts,
    )
    monkeypatch.setattr(
        shifts,
        "_verify_manager_approval_token_optional",
        verify_manager,
    )
    monkeypatch.setattr(shifts, "_ensure_fb_shift_for_kopos_shift", create_shift)
    return calls


def test_open_shift_returns_exact_device_conflict_without_creating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_open_dependencies(
        monkeypatch,
        conflicts=[
            SimpleNamespace(
                name="FB-SHIFT-DEVICE",
                device_id="DEVICE-1",
                staff_id="other@example.test",
            )
        ],
    )
    expected = {
        "status": "conflict",
        "conflict_code": "device_open_shift_conflict",
        "idempotency_key": "open-idem-1",
        "shift_id": "SHIFT-REQUESTED",
        "device_id": "DEVICE-1",
        "staff_id": STAFF_ID,
        "conflicting_fb_shift": "FB-SHIFT-DEVICE",
        "conflicting_device_id": "DEVICE-1",
        "local_release_authorized": True,
        "message": "Device DEVICE-1 already has open FB Shift FB-SHIFT-DEVICE",
    }

    assert shifts.open_shift_payload(_open_payload()) == expected
    assert shifts.open_shift_payload(_open_payload()) == expected
    assert calls == {"conflict_query": 2, "manager_approval": 0, "created": 0}


def test_open_shift_returns_exact_staff_conflict_without_creating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_open_dependencies(
        monkeypatch,
        conflicts=[
            SimpleNamespace(
                name="FB-SHIFT-STAFF",
                device_id="DEVICE-2",
                staff_id=STAFF_ID,
            )
        ],
    )

    assert shifts.open_shift_payload(_open_payload()) == {
        "status": "conflict",
        "conflict_code": "staff_open_shift_conflict",
        "idempotency_key": "open-idem-1",
        "shift_id": "SHIFT-REQUESTED",
        "device_id": "DEVICE-1",
        "staff_id": STAFF_ID,
        "conflicting_fb_shift": "FB-SHIFT-STAFF",
        "conflicting_device_id": "DEVICE-2",
        "local_release_authorized": True,
        "message": (
            "User staff@example.test already has open FB Shift FB-SHIFT-STAFF "
            "on device DEVICE-2"
        ),
    }
    assert calls == {"conflict_query": 1, "manager_approval": 0, "created": 0}


def test_open_shift_never_authorizes_release_with_incomplete_conflict_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_open_dependencies(
        monkeypatch,
        conflicts=[
            SimpleNamespace(
                name="",
                device_id="DEVICE-1",
                staff_id=STAFF_ID,
            )
        ],
    )

    with pytest.raises(
        shifts.frappe.ValidationError,
        match="Open FB Shift conflict evidence is incomplete",
    ):
        shifts.open_shift_payload(_open_payload())

    assert calls == {"conflict_query": 1, "manager_approval": 0, "created": 0}


def test_exact_duplicate_retry_precedes_conflict_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _open_payload()
    fingerprint = shifts._shift_request_fingerprint(
        "open",
        {
            "idempotency_key": "open-idem-1",
            "device_id": "DEVICE-1",
            "staff_id": STAFF_ID,
            "shift_id": "SHIFT-REQUESTED",
            "opening_float_sen": 1000,
            "opened_at": "2026-07-17T10:00:00+08:00",
            "reason": None,
        },
    )
    existing = SimpleNamespace(
        name="FB-SHIFT-EXISTING",
        device_id="DEVICE-1",
        staff_id=STAFF_ID,
        open_idempotency_key="open-idem-1",
        open_request_fingerprint=fingerprint,
    )
    calls = _patch_open_dependencies(
        monkeypatch,
        existing_shift=existing,
        conflicts=[
            SimpleNamespace(
                name="FB-SHIFT-CONFLICT",
                device_id="DEVICE-1",
                staff_id=STAFF_ID,
            )
        ],
    )

    assert shifts.open_shift_payload(payload) == {
        "status": "duplicate",
        "fb_shift": "FB-SHIFT-EXISTING",
        "shift_id": "SHIFT-REQUESTED",
        "message": "Shift already opened",
    }
    assert calls == {"conflict_query": 0, "manager_approval": 0, "created": 0}


def test_open_shift_lookup_keeps_existing_shift_shape_when_staff_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def get_all(_doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return [
            {
                "name": "FB-SHIFT-DEVICE",
                "shift_code": "SHIFT-DEVICE",
                "device_id": "DEVICE-1",
                "staff_id": STAFF_ID,
                "opening_float": 10,
                "opened_at": "2026-07-17 10:00:00",
            }
        ]

    monkeypatch.setattr(shifts, "get_device_doc", lambda device_id: _device_doc())
    monkeypatch.setattr(shifts.frappe, "get_all", get_all)

    assert shifts.get_device_open_shift_payload("DEVICE-1", STAFF_ID) == {
        "fb_shift": "FB-SHIFT-DEVICE",
        "shift_id": "SHIFT-DEVICE",
        "device_id": "DEVICE-1",
        "staff_id": STAFF_ID,
        "opening_float_sen": 1000,
        "opened_at": "2026-07-17T02:00:00.000Z",
    }
    assert len(calls) == 1


def test_open_shift_lookup_returns_typed_staff_conflict_only_after_no_device_shift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            [],
            [
                {
                    "name": "FB-SHIFT-STAFF",
                    "device_id": "DEVICE-2",
                    "staff_id": STAFF_ID,
                }
            ],
        ]
    )
    monkeypatch.setattr(shifts, "get_device_doc", lambda device_id: _device_doc())
    monkeypatch.setattr(
        shifts.frappe,
        "get_all",
        lambda *_args, **_kwargs: next(responses),
    )

    assert shifts.get_device_open_shift_payload("DEVICE-1", STAFF_ID) == {
        "staff_conflict": {
            "conflict_code": "staff_open_shift_conflict",
            "staff_id": STAFF_ID,
            "conflicting_fb_shift": "FB-SHIFT-STAFF",
            "conflicting_device_id": "DEVICE-2",
            "message": (
                "User staff@example.test already has open FB Shift FB-SHIFT-STAFF "
                "on device DEVICE-2"
            ),
        }
    }


def test_get_device_open_shift_endpoint_validates_staff_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_doc = _device_doc()
    validated: list[tuple[SimpleNamespace, str]] = []
    marked: list[str] = []
    conflict = {
        "conflict_code": "staff_open_shift_conflict",
        "staff_id": STAFF_ID,
        "conflicting_fb_shift": "FB-SHIFT-STAFF",
        "conflicting_device_id": "DEVICE-2",
        "message": "Staff conflict",
    }
    monkeypatch.setattr(
        public_api,
        "require_device_context",
        lambda device_id: device_doc,
    )
    monkeypatch.setattr(
        shifts,
        "resolve_and_validate_device_user",
        lambda doc, staff_id: validated.append((doc, staff_id)),
    )
    monkeypatch.setattr(
        public_api,
        "mark_device_seen",
        lambda device_id: marked.append(device_id),
    )
    monkeypatch.setattr(
        shifts,
        "get_device_open_shift_payload",
        lambda device_id, staff_id: {"staff_conflict": conflict},
    )
    public_api.frappe.local.response = {}

    public_api.get_device_open_shift("DEVICE-1", STAFF_ID)

    assert validated == [(device_doc, STAFF_ID)]
    assert marked == ["DEVICE-1"]
    assert public_api.frappe.local.response == {
        "status": "ok",
        "shift": None,
        "staff_conflict": conflict,
        "http_status_code": 200,
    }


@pytest.mark.parametrize(
    ("device_doc", "message"),
    [
        (_device_doc(assigned=False), "is not assigned to KoPOS Device DEVICE-1"),
        (_device_doc(active=False), "is not active on this device"),
    ],
)
def test_get_device_open_shift_rejects_unassigned_or_inactive_staff(
    monkeypatch: pytest.MonkeyPatch,
    device_doc: SimpleNamespace,
    message: str,
) -> None:
    looked_up: list[bool] = []
    marked: list[bool] = []
    monkeypatch.setattr(
        public_api,
        "require_device_context",
        lambda device_id: device_doc,
    )
    monkeypatch.setattr(
        public_api,
        "mark_device_seen",
        lambda device_id: marked.append(True),
    )
    monkeypatch.setattr(
        shifts,
        "get_device_open_shift_payload",
        lambda **_kwargs: looked_up.append(True),
    )
    monkeypatch.setattr(shifts.frappe.db, "exists", lambda *_args: True)
    monkeypatch.setattr(shifts.frappe.db, "get_value", lambda *_args: 1)
    public_api.frappe.local.response = {}

    public_api.get_device_open_shift("DEVICE-1", STAFF_ID)

    assert public_api.frappe.local.response["status"] == "error"
    assert public_api.frappe.local.response["http_status_code"] == 400
    assert message in public_api.frappe.local.response["message"]
    assert looked_up == []
    assert marked == []


def test_get_device_open_shift_requires_authenticated_device_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    looked_up: list[bool] = []

    def reject_context(*_args: Any, **_kwargs: Any) -> None:
        raise public_api.frappe.ValidationError("Authenticated device mismatch")

    monkeypatch.setattr(public_api, "require_device_context", reject_context)
    monkeypatch.setattr(
        shifts,
        "get_device_open_shift_payload",
        lambda **_kwargs: looked_up.append(True),
    )
    public_api.frappe.local.response = {}

    public_api.get_device_open_shift("DEVICE-1", STAFF_ID)

    assert public_api.frappe.local.response["status"] == "error"
    assert public_api.frappe.local.response["http_status_code"] == 400
    assert public_api.frappe.local.response["message"] == (
        "Authenticated device mismatch"
    )
    assert looked_up == []
