from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import NamedTuple

import frappe
from frappe import _


DEFAULT_COST = 16_384
MIN_COST = 256
MAX_COST = 16_384

_COST_PATTERN = re.compile(r"[0-9]+")
_SALT_PATTERN = re.compile(r"[0-9a-fA-F]{32}")
_KEY_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


class ParsedPinHash(NamedTuple):
    cost: int
    salt: str
    key: bytes


def assert_pin_format(pin: str) -> None:
    if not pin or not pin.isdigit() or len(pin) != 4:
        frappe.throw(_("PIN must be exactly 4 digits"), frappe.ValidationError)


def _is_supported_cost(cost: int) -> bool:
    return MIN_COST <= cost <= MAX_COST and not cost & (cost - 1)


def hash_pin(pin: str, cost: int = DEFAULT_COST) -> str:
    assert_pin_format(pin)
    if (
        isinstance(cost, bool)
        or not isinstance(cost, int)
        or not _is_supported_cost(cost)
    ):
        frappe.throw(
            _("KDF cost must be a power of two between {0} and {1}").format(
                MIN_COST, MAX_COST
            ),
            frappe.ValidationError,
        )
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


def is_supported_pin_hash(encoded_hash: object) -> bool:
    return _parse_pin_hash(encoded_hash) is not None


def _parse_pin_hash(encoded_hash: object) -> ParsedPinHash | None:
    try:
        algorithm, raw_cost, salt, raw_key = str(encoded_hash or "").split("$", 3)
        if (
            algorithm != "scrypt"
            or _COST_PATTERN.fullmatch(raw_cost) is None
            or _SALT_PATTERN.fullmatch(salt) is None
            or _KEY_PATTERN.fullmatch(raw_key) is None
        ):
            return None
        cost = int(raw_cost)
        key = bytes.fromhex(raw_key)
    except (TypeError, ValueError):
        return None
    if not _is_supported_cost(cost) or len(key) != 32:
        return None
    return ParsedPinHash(cost=cost, salt=salt, key=key)
