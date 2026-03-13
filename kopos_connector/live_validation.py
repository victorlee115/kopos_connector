from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Callable

import frappe

from .smoke import (
    _ensure_cash_account,
    _ensure_cost_center,
    _ensure_customer,
    _ensure_expense_account,
    _ensure_mode_of_payment,
    _ensure_pos_settings,
    _ensure_warehouse,
)
from .utils.pin import hash_pin


@dataclass
class ValidationContext:
    company: str
    pos_profile: str
    allowed_user: str
    disallowed_user: str
    disabled_user: str
    first_device_id: str
    second_device_id: str
    run_id: str


def _log(message: str) -> None:
    print(message)


def _ensure_user(email: str, *, enabled: bool) -> None:
    existing = frappe.db.exists("User", email)
    if existing:
        frappe.db.set_value("User", email, "enabled", 1 if enabled else 0)
        return

    doc = frappe.get_doc(
        {
            "doctype": "User",
            "email": email,
            "first_name": email.split("@")[0].replace(".", " ").title(),
            "enabled": 1 if enabled else 0,
            "send_welcome_email": 0,
        }
    )
    doc.insert(ignore_permissions=True)


def _ensure_device(
    device_id: str, pos_profile: str, users: list[dict[str, object]]
) -> None:
    existing = frappe.db.exists("KoPOS Device", {"device_id": device_id})
    payload = {
        "device_id": device_id,
        "device_name": f"Validation {device_id}",
        "device_prefix": device_id[-4:].upper(),
        "pos_profile": pos_profile,
        "enabled": 1,
        "allow_training_mode": 0,
        "allow_manual_settings_override": 0,
        "device_users": users,
    }

    if existing:
        doc = frappe.get_doc("KoPOS Device", existing)
        doc.update(payload)
        doc.device_users = []
        for row in users:
            doc.append("device_users", row)
        doc.save(ignore_permissions=True)
        return

    doc = frappe.get_doc({"doctype": "KoPOS Device", **payload})
    doc.insert(ignore_permissions=True)


def _create_validation_pos_profile(
    *,
    company: str,
    warehouse: str,
    customer: str,
    write_off_account: str,
    write_off_cost_center: str,
    run_id: str,
) -> str:
    name = f"KoPOS Validation {run_id}"
    existing = frappe.db.exists("POS Profile", name)
    if existing:
        return existing

    currency = frappe.db.get_value("Company", company, "default_currency") or "USD"
    doc = frappe.get_doc(
        {
            "doctype": "POS Profile",
            "name": name,
            "company": company,
            "currency": currency,
            "warehouse": warehouse,
            "customer": customer,
            "write_off_account": write_off_account,
            "write_off_cost_center": write_off_cost_center,
            "write_off_limit": 0,
            "payments": [{"mode_of_payment": "Cash", "default": 1}],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _setup_context() -> ValidationContext:
    from erpnext.setup.utils import before_tests

    before_tests()

    company = frappe.get_all("Company", pluck="name", limit=1)[0]
    customer = _ensure_customer(company)
    warehouse = _ensure_warehouse(company)
    cost_center = _ensure_cost_center(company)
    cash_account = _ensure_cash_account(company)
    expense_account = _ensure_expense_account(company)
    _ensure_mode_of_payment("Cash", company, cash_account, "Cash")
    run_id = str(int(time()))
    pos_profile = _create_validation_pos_profile(
        company=company,
        warehouse=warehouse,
        customer=customer,
        write_off_account=expense_account,
        write_off_cost_center=cost_center,
        run_id=run_id,
    )
    _ensure_pos_settings()

    allowed_user = f"validation.allowed.{run_id}@example.com"
    disallowed_user = f"validation.disallowed.{run_id}@example.com"
    disabled_user = f"validation.disabled.{run_id}@example.com"
    _ensure_user(allowed_user, enabled=True)
    _ensure_user(disallowed_user, enabled=True)
    _ensure_user(disabled_user, enabled=False)

    first_device_id = f"VALIDATION-{run_id}-A"
    second_device_id = f"VALIDATION-{run_id}-B"

    common_rows = [
        {
            "user": allowed_user,
            "active": 1,
            "can_open_shift": 1,
            "can_close_shift": 1,
            "display_name": "Validation Allowed",
            "pin_hash": hash_pin("1234"),
            "default_cashier": 1,
        },
        {
            "user": disallowed_user,
            "active": 1,
            "can_open_shift": 0,
            "can_close_shift": 0,
            "display_name": "Validation Disallowed",
            "pin_hash": hash_pin("2345"),
            "default_cashier": 0,
        },
        {
            "user": disabled_user,
            "active": 1,
            "can_open_shift": 1,
            "can_close_shift": 1,
            "display_name": "Validation Disabled",
            "pin_hash": hash_pin("3456"),
            "default_cashier": 0,
        },
    ]

    _ensure_device(first_device_id, pos_profile, common_rows)
    _ensure_device(second_device_id, pos_profile, common_rows)
    frappe.db.commit()

    return ValidationContext(
        company=company,
        pos_profile=pos_profile,
        allowed_user=allowed_user,
        disallowed_user=disallowed_user,
        disabled_user=disabled_user,
        first_device_id=first_device_id,
        second_device_id=second_device_id,
        run_id=run_id,
    )


def _expect_validation_error(fn: Callable[[], object], substring: str) -> bool:
    try:
        fn()
    except frappe.ValidationError as exc:
        return substring.lower() in str(exc).lower()
    return False


def _close_shift_for_cleanup(ctx: ValidationContext, shift_id: str) -> None:
    from .api.shifts import close_shift_payload

    close_shift_payload(
        {
            "idempotency_key": f"{shift_id}-cleanup-close",
            "device_id": ctx.first_device_id,
            "staff_id": ctx.allowed_user,
            "shift_id": shift_id,
            "counted_cash_sen": 5000,
        }
    )


def _test_allowed_user_can_open_shift(ctx: ValidationContext) -> bool:
    from .api.shifts import open_shift_payload

    shift_id = f"{ctx.run_id}-SHIFT-OPEN"
    result = open_shift_payload(
        {
            "idempotency_key": f"{ctx.run_id}-open-ok",
            "device_id": ctx.first_device_id,
            "staff_id": ctx.allowed_user,
            "shift_id": shift_id,
            "opening_float_sen": 5000,
        }
    )
    if result.get("status") == "ok":
        _close_shift_for_cleanup(ctx, shift_id)
    return result.get("status") == "ok"


def _test_disallowed_user_cannot_open_shift(ctx: ValidationContext) -> bool:
    from .api.shifts import open_shift_payload

    return _expect_validation_error(
        lambda: open_shift_payload(
            {
                "idempotency_key": f"{ctx.run_id}-open-noauth",
                "device_id": ctx.first_device_id,
                "staff_id": ctx.disallowed_user,
                "shift_id": f"{ctx.run_id}-SHIFT-NOAUTH",
                "opening_float_sen": 5000,
            }
        ),
        "not authorized to open shifts",
    )


def _test_disabled_user_cannot_open_shift(ctx: ValidationContext) -> bool:
    from .api.shifts import open_shift_payload

    return _expect_validation_error(
        lambda: open_shift_payload(
            {
                "idempotency_key": f"{ctx.run_id}-open-disabled",
                "device_id": ctx.first_device_id,
                "staff_id": ctx.disabled_user,
                "shift_id": f"{ctx.run_id}-SHIFT-DISABLED",
                "opening_float_sen": 5000,
            }
        ),
        "disabled",
    )


def _test_unassigned_user_cannot_open_shift(ctx: ValidationContext) -> bool:
    from .api.shifts import open_shift_payload

    return _expect_validation_error(
        lambda: open_shift_payload(
            {
                "idempotency_key": f"{ctx.run_id}-open-unassigned",
                "device_id": ctx.first_device_id,
                "staff_id": "validation.unassigned@example.com",
                "shift_id": f"{ctx.run_id}-SHIFT-UNASSIGNED",
                "opening_float_sen": 5000,
            }
        ),
        "not assigned",
    )


def _test_replay_protection(ctx: ValidationContext) -> bool:
    from .api.shifts import open_shift_payload

    shift_id = f"{ctx.run_id}-SHIFT-REPLAY"
    payload = {
        "idempotency_key": f"{ctx.run_id}-open-replay",
        "device_id": ctx.first_device_id,
        "staff_id": ctx.allowed_user,
        "shift_id": shift_id,
        "opening_float_sen": 5000,
    }
    first = open_shift_payload(payload)
    second = open_shift_payload(payload)
    if first.get("status") == "ok":
        _close_shift_for_cleanup(ctx, shift_id)
    return first.get("status") == "ok" and second.get("status") == "duplicate"


def _test_disallowed_user_cannot_close_shift(ctx: ValidationContext) -> bool:
    from .api.shifts import close_shift_payload, open_shift_payload

    shift_id = f"{ctx.run_id}-SHIFT-CLOSE-DENY"
    opened = open_shift_payload(
        {
            "idempotency_key": f"{ctx.run_id}-open-close-deny",
            "device_id": ctx.first_device_id,
            "staff_id": ctx.allowed_user,
            "shift_id": shift_id,
            "opening_float_sen": 5000,
        }
    )
    if opened.get("status") != "ok":
        return False

    denied = _expect_validation_error(
        lambda: close_shift_payload(
            {
                "idempotency_key": f"{ctx.run_id}-close-deny",
                "device_id": ctx.first_device_id,
                "staff_id": ctx.disallowed_user,
                "shift_id": shift_id,
                "counted_cash_sen": 5000,
            }
        ),
        "not authorized to close shifts",
    )
    _close_shift_for_cleanup(ctx, shift_id)
    return denied


def _test_wrong_device_cannot_close_shift(ctx: ValidationContext) -> bool:
    from .api.shifts import close_shift_payload, open_shift_payload

    shift_id = f"{ctx.run_id}-SHIFT-WRONG-DEVICE"
    opened = open_shift_payload(
        {
            "idempotency_key": f"{ctx.run_id}-open-wrong-device",
            "device_id": ctx.first_device_id,
            "staff_id": ctx.allowed_user,
            "shift_id": shift_id,
            "opening_float_sen": 5000,
        }
    )
    if opened.get("status") != "ok":
        return False

    denied = _expect_validation_error(
        lambda: close_shift_payload(
            {
                "idempotency_key": f"{ctx.run_id}-close-wrong-device",
                "device_id": ctx.second_device_id,
                "staff_id": ctx.allowed_user,
                "shift_id": shift_id,
                "counted_cash_sen": 5000,
            }
        ),
        "does not belong to device",
    )
    _close_shift_for_cleanup(ctx, shift_id)
    return denied


def _test_close_replay_protection(ctx: ValidationContext) -> bool:
    from .api.shifts import close_shift_payload, open_shift_payload

    shift_id = f"{ctx.run_id}-SHIFT-CLOSE-REPLAY"
    opened = open_shift_payload(
        {
            "idempotency_key": f"{ctx.run_id}-open-close-replay",
            "device_id": ctx.first_device_id,
            "staff_id": ctx.allowed_user,
            "shift_id": shift_id,
            "opening_float_sen": 5000,
        }
    )
    if opened.get("status") != "ok":
        return False

    payload = {
        "idempotency_key": f"{ctx.run_id}-close-replay",
        "device_id": ctx.first_device_id,
        "staff_id": ctx.allowed_user,
        "shift_id": shift_id,
        "counted_cash_sen": 5000,
    }
    first = close_shift_payload(payload)
    second = close_shift_payload(payload)
    return first.get("status") == "ok" and second.get("status") == "duplicate"


def run_all_tests() -> dict[str, object]:
    ctx = _setup_context()
    checks: list[tuple[str, Callable[[ValidationContext], bool]]] = [
        ("allowed user can open shift", _test_allowed_user_can_open_shift),
        ("disallowed user cannot open shift", _test_disallowed_user_cannot_open_shift),
        ("disabled user cannot open shift", _test_disabled_user_cannot_open_shift),
        ("unassigned user cannot open shift", _test_unassigned_user_cannot_open_shift),
        ("replay protection works", _test_replay_protection),
        (
            "disallowed user cannot close shift",
            _test_disallowed_user_cannot_close_shift,
        ),
        ("wrong device cannot close shift", _test_wrong_device_cannot_close_shift),
        ("close replay protection works", _test_close_replay_protection),
    ]

    passed = 0
    results: list[dict[str, object]] = []
    _log("Shift sync live validation")
    for name, check in checks:
        try:
            ok = bool(check(ctx))
        except Exception:
            ok = False
            frappe.db.rollback()
            _log(f"FAIL {name}: unexpected exception")
            _log(frappe.get_traceback())
        else:
            frappe.db.commit()
        if ok:
            passed += 1
            _log(f"PASS {name}")
        else:
            _log(f"FAIL {name}")
        results.append({"name": name, "passed": ok})

    summary = {
        "status": "ok" if passed == len(checks) else "error",
        "passed": passed,
        "total": len(checks),
        "results": results,
        "notes": [
            "Manager approval live validation remains pending because shift APIs still accept missing manager_approval_token for backward compatibility.",
        ],
    }
    _log(f"Completed {passed}/{len(checks)} checks")
    return summary
