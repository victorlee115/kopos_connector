# pyright: reportMissingImports=false

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
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
SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|authorization|bearer\s+\S+|cookie|credential|csrf|encrypted[_-]?pin|pin[_-]?hash|password|provisioning[_-]?token|qr[_-]?(data|code|string)|raw[_-]?response|secret|session(?:[_-]?id)?|set[_-]?cookie|\bsid\b|token)"
)


def sanitized_error_message(error: BaseException | object) -> str:
    """Return a short support-safe error message without raw credentials or QR data."""
    if isinstance(error, BaseException):
        raw = str(error)
        error_type = error.__class__.__name__
    else:
        raw = str(error)
        error_type = type(error).__name__
    cleaned = _redact_text(" ".join(raw.split()))
    if len(cleaned) > MAX_ERROR_MESSAGE_LENGTH:
        cleaned = f"{cleaned[: MAX_ERROR_MESSAGE_LENGTH - 3]}..."
    return f"{error_type}: {cleaned}" if cleaned else error_type


def redacted_payload(value: Any) -> Any:
    """Recursively redact provider/API payloads before persisting support diagnostics."""
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]" if _is_sensitive_key(str(key)) else redacted_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redacted_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redacted_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def redacted_json(value: Any) -> str:
    redacted = redacted_payload(value)
    try:
        return str(frappe.as_json(redacted))
    except Exception as error:
        logging.getLogger(__name__).debug(
            "Frappe JSON serialization failed for redacted payload: %s",
            sanitized_error_message(error),
        )
        return _safe_payload_text(redacted)


def log_sanitized_error(title: str, error: BaseException | None = None) -> None:
    """Log sanitized diagnostics without allowing logging failures to mask root causes."""
    message = sanitized_error_message(error) if error is not None else title
    traceback_getter = getattr(frappe, "get_traceback", None)
    if callable(traceback_getter):
        try:
            safe_traceback = _redact_text(str(traceback_getter()))
            if safe_traceback and safe_traceback != message:
                # Keep the sanitized exception type/message before the bounded
                # traceback. Long tracebacks are intentionally truncated, so
                # storing only their leading frames can otherwise discard the
                # one line support needs to identify the failure class.
                message = f"{message}\n{safe_traceback}"
        except Exception as traceback_error:
            logging.getLogger(__name__).debug(
                "Frappe traceback collection failed: %s",
                sanitized_error_message(traceback_error),
            )

    try:
        frappe.log_error(message=message, title=title)
    except TypeError:
        try:
            frappe.log_error(message, title)
        except Exception as log_error:
            logging.getLogger(__name__).warning(
                "Frappe error logging failed for %s: %s",
                title,
                sanitized_error_message(log_error),
            )
    except Exception as log_error:
        logging.getLogger(__name__).warning(
            "Frappe error logging failed for %s: %s",
            title,
            sanitized_error_message(log_error),
        )


def make_savepoint(prefix: str) -> str:
    name = f"{prefix}_{frappe.generate_hash(length=8)}"
    try:
        frappe.db.savepoint(name)
    except Exception as error:
        logging.getLogger(__name__).debug(
            "Savepoint creation failed for %s: %s",
            prefix,
            sanitized_error_message(error),
        )
        return ""
    return name


def rollback_to_savepoint(savepoint: str, *, title: str) -> None:
    try:
        if savepoint:
            frappe.db.rollback(save_point=savepoint)
        else:
            frappe.db.rollback()
    except Exception as error:
        log_sanitized_error(title, error)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    normalized = normalized.replace(" ", "_")
    return any(token in normalized for token in SENSITIVE_KEY_TOKENS)


def _redact_text(value: str) -> str:
    if SENSITIVE_TEXT_PATTERN.search(value):
        return "[redacted]"
    if len(value) > MAX_PAYLOAD_TEXT_LENGTH:
        return f"{value[: MAX_PAYLOAD_TEXT_LENGTH - 3]}..."
    return value


def _safe_payload_text(value: Any) -> str:
    text = str(value)
    return text if len(text) <= MAX_PAYLOAD_TEXT_LENGTH else f"{text[:497]}..."
