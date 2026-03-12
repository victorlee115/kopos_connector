from __future__ import annotations

import hashlib
import secrets

import frappe
from frappe import _


DEFAULT_COST = 16384


def assert_pin_format(pin: str) -> None:
    if not pin or not pin.isdigit() or len(pin) != 4:
        frappe.throw(_("PIN must be exactly 4 digits"), frappe.ValidationError)


def hash_pin(pin: str, cost: int = DEFAULT_COST) -> str:
    assert_pin_format(pin)
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(
        pin.encode("utf-8"), salt=salt.encode("utf-8"), n=cost, r=8, p=1, dklen=32
    ).hex()
    return f"scrypt${cost}${salt}${key}"
