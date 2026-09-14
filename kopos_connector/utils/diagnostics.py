# pyright: reportMissingImports=false

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import traceback
from itertools import islice
from typing import Any

import frappe


SENSITIVE_KEY_TOKENS = (
    "api_key",
    "api_secret",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "csrf",
    "encrypted_pin",
    "gcm_token",
    "password",
    "pin",
    "pin_hash",
    "provisioning_token",
    "qr_code",
    "qr_data",
    "qrstring",
    "raw_response",
    "receipt_file_hash",
    "secret",
    "session",
    "sid",
    "token",
)

MAX_ERROR_MESSAGE_LENGTH = 240
MAX_PAYLOAD_TEXT_LENGTH = 500
MAX_PAYLOAD_DEPTH = 8
MAX_PAYLOAD_NODES = 128
MAX_PAYLOAD_ITEMS = 32
DIAGNOSTIC_OMITTED = "[omitted]"
DIAGNOSTIC_UNSUPPORTED = "[unsupported]"
SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|authorization|bearer\s+\S+|cookie|credential|csrf|encrypted[_-]?pin|pin[_-]?hash|password|provisioning[_-]?token|qr[_-]?(data|code|string)|raw[_-]?response|secret|session(?:[_-]?id)?|set[_-]?cookie|\bsid\b|token)"
)


def sanitized_error_message(error: BaseException | object) -> str:
    """Bound and redact plain error text without invoking object rendering hooks."""
    if isinstance(error, BaseException):
        # Native args bypass overridden __str__, repr and args properties.
        # Extra arguments may contain request/response objects; never render them.
        args = BaseException.args.__get__(error)
        raw = args[0] if args else ""
    else:
        raw = error
    error_type = _redact_text(type.__getattribute__(type(error), "__name__"))[:80]
    cleaned = " ".join(_redact_text(raw).split()) if type(raw) is str else DIAGNOSTIC_UNSUPPORTED
    if len(cleaned) > MAX_ERROR_MESSAGE_LENGTH:
        cleaned = f"{cleaned[: MAX_ERROR_MESSAGE_LENGTH - 3]}..."
    return f"{error_type}: {cleaned}" if cleaned else error_type


def redacted_payload(value: Any) -> Any:
    """Copy bounded JSON data, never invoking opaque object serialization hooks."""
    return _redact_payload(value, depth=0, budget=MAX_PAYLOAD_NODES)


def _redact_payload(value: Any, *, depth: int, budget: int) -> Any:
    if depth > MAX_PAYLOAD_DEPTH or budget < 1:
        return DIAGNOSTIC_OMITTED
    if isinstance(value, (dict, list, tuple)):
        if budget < 2:
            return DIAGNOSTIC_OMITTED
        # Bypass overridden iteration methods; arbitrary Mapping/Iterable
        # implementations are not diagnostic data. Keep copies bounded too.
        iterator = (
            dict.items(value) if isinstance(value, dict) else
            list.__iter__(value) if isinstance(value, list) else tuple.__iter__(value)
        )
        limit = min(MAX_PAYLOAD_ITEMS, budget - 1)
        entries = list(islice(iterator, limit + 1))
        omitted = len(entries) > limit
        entries = entries[:limit]
        # Share the budget across siblings so a large provider result cannot
        # consume the allowance for later server-authored recovery fields.
        child_budget = (budget - 1) // max(1, len(entries))
        if isinstance(value, dict):
            result = {}
            for key, item in entries:
                if type(key) is not str or len(key) > MAX_PAYLOAD_TEXT_LENGTH:
                    result["[omitted key]"] = DIAGNOSTIC_OMITTED
                else:
                    result[key] = "[redacted]" if _is_sensitive_key(key) else _redact_payload(
                        item, depth=depth + 1, budget=child_budget
                    )
            if omitted:
                result["[omitted entries]"] = DIAGNOSTIC_OMITTED
            return result
        result = [_redact_payload(item, depth=depth + 1, budget=child_budget) for item in entries]
        return result + [DIAGNOSTIC_OMITTED] if omitted else result
    if type(value) is str:
        return _redact_text(value)
    if value is None or type(value) is bool:
        return value
    if type(value) is int and value.bit_length() <= 256:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return DIAGNOSTIC_UNSUPPORTED


def redacted_json(value: Any) -> str:
    try:
        # The general Frappe encoder invokes __json__, iterates objects and
        # stringifies exceptions. Those hooks run after redaction and can leak
        # new private data. Standard JSON only sees the bounded primitive copy.
        return json.dumps(
            redacted_payload(value), sort_keys=True, allow_nan=False, separators=(",", ":")
        )
    except Exception:
        _diagnostic_fallback()
        return '"[diagnostic unavailable]"'


def log_sanitized_error(title: str, error: BaseException | None = None) -> None:
    """Store only sanitized fields in native Error Log, without implicit telemetry."""
    try:
        title = " ".join(_redact_text(title).split())[:140] if type(title) is str else DIAGNOSTIC_UNSUPPORTED
        title = title or "KoPOS operation failed"
        error = error if error is not None else sys.exc_info()[1]
        message = sanitized_error_message(error) if error is not None else title
        if isinstance(error, BaseException):
            frames = traceback.StackSummary.extract(
                traceback.walk_tb(BaseException.__traceback__.__get__(error)),
                limit=8, lookup_lines=False, capture_locals=False,
            )
            # Do not format FrameSummary: it would fetch source text. No locals,
            # exception chain, request context or absolute paths are collected.
            for frame in frames:
                filename = _redact_text(os.path.basename(frame.filename))[:140]
                function = _redact_text(frame.name)[:80]
                message += f"\n  {filename}:{frame.lineno} in {function}"
        # frappe.log_error adds metadata and separately captures the active
        # exception/request in Sentry. Native document insertion does neither.
        doc = frappe.get_doc({"doctype": "Error Log", "method": title, "error": message})
        if frappe.flags.read_only:
            doc.deferred_insert()
        else:
            doc.insert(ignore_permissions=True)
    except Exception:
        # Never retry a possibly completed insert (including after TypeError).
        _diagnostic_fallback()


def _diagnostic_fallback() -> None:
    message = "KoPOS diagnostic recording failed"
    try:
        logging.getLogger(__name__).warning(message)
    except Exception:
        try:
            sys.stderr.write(message + "\n")
        except Exception:
            # Neither existing process sink is usable; preserve the caller's
            # outcome instead of raising a second, diagnostic-only exception.
            pass


def make_savepoint(prefix: str) -> str:
    """Establish a native boundary or fail; an empty fallback is never safe."""
    name = f"{prefix}_{frappe.generate_hash(length=8)}"
    try:
        frappe.db.savepoint(name)
    except Exception:
        frappe.db.before_commit.add(_reject_unsafe_transaction)
        raise
    return name


def rollback_to_savepoint(savepoint: str, *, title: str) -> None:
    try:
        if not savepoint:
            raise ValueError(f"{title}: a savepoint name is required")
        frappe.db.rollback(save_point=savepoint)
    except Exception:
        # Outer projection handlers can catch errors and keep working. Native
        # before_commit must still reject a transaction whose rollback failed.
        frappe.db.before_commit.add(_reject_unsafe_transaction)
        raise


def _reject_unsafe_transaction() -> None:
    # Frappe consumes callbacks before invoking them. Re-arm so catching this
    # error and committing again cannot bypass it. A native full rollback owns
    # resetting the callback queue; a named rollback must not clear this fence.
    frappe.db.before_commit.add(_reject_unsafe_transaction)
    raise RuntimeError("Transaction savepoint failed; full rollback is required before retry")


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    normalized = normalized.replace(" ", "_")
    return any(token in normalized for token in SENSITIVE_KEY_TOKENS)


def _redact_text(value: str) -> str:
    if len(value) > MAX_PAYLOAD_TEXT_LENGTH:
        return DIAGNOSTIC_OMITTED
    if SENSITIVE_TEXT_PATTERN.search(value):
        return "[redacted]"
    return value
