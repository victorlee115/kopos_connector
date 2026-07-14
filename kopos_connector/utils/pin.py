from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import NamedTuple

import frappe
from frappe import _


DEFAULT_COST = 16_384
MIN_COST = 256
MAX_COST = 1_048_576


class ParsedPinHash(NamedTuple):
    cost: int
    salt: str
    key: bytes


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


def verify_pin(pin: str, encoded_hash: str) -> bool:
    """Verify a raw PIN using scrypt and a constant-time key comparison."""
    parsed = _parse_pin_hash(encoded_hash)
    if parsed is None:
        return False

    try:
        candidate = hashlib.scrypt(
            str(pin or "").encode("utf-8"),
            salt=parsed.salt.encode("utf-8"),
            n=parsed.cost,
            r=8,
            p=1,
            dklen=len(parsed.key),
        )
    except (MemoryError, ValueError):
        return False
    format_valid = bool(pin and pin.isdigit() and len(pin) == 4)
    return bool(format_valid and hmac.compare_digest(candidate, parsed.key))


def pin_hash_needs_upgrade(encoded_hash: str) -> bool:
    parsed = _parse_pin_hash(encoded_hash)
    return parsed is None or parsed.cost < DEFAULT_COST


def _parse_pin_hash(encoded_hash: str) -> ParsedPinHash | None:
    try:
        algorithm, raw_cost, salt, raw_key = str(encoded_hash or "").split("$", 3)
        cost = int(raw_cost)
        key = bytes.fromhex(raw_key)
    except (TypeError, ValueError):
        return None
    if algorithm != "scrypt" or not (8 <= len(salt) <= 128) or len(key) != 32:
        return None
    if cost < MIN_COST or cost > MAX_COST or cost & (cost - 1):
        return None
    return ParsedPinHash(cost=cost, salt=salt, key=key)
