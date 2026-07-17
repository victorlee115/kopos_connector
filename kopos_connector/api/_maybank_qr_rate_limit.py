# pyright: reportMissingImports=false

"""Atomic device/outlet rate limiting for Maybank QR generation."""

from __future__ import annotations

import hashlib

import frappe
from frappe.utils import cint, cstr

from kopos_connector.utils.diagnostics import log_sanitized_error

from ._maybank_qr_contract import (
    DEFAULT_QR_PER_OUTLET_PER_MINUTE,
    MAX_QR_PER_MINUTE,
    MAYBANK_PROVIDER,
    PREFLIGHT_REASON_RATE_LIMIT,
    PREFLIGHT_REASON_RATE_LIMITER_UNAVAILABLE,
    QR_RATE_LIMIT_SCRIPT,
    QR_RATE_LIMIT_WINDOW_SECONDS,
    MaybankQrPreflightRejection,
)

def _config_positive_int(name: str, default: int, maximum: int) -> int:
    config = getattr(frappe, "conf", None)
    getter = getattr(config, "get", None)
    value = getter(name, default) if callable(getter) else getattr(config, name, default)
    parsed = cint(value)
    if parsed < 1 or parsed > maximum:
        frappe.throw(
            f"{name} must be between 1 and {maximum}",
            frappe.ValidationError,
        )
    return parsed


def _qr_rate_limit_key(scope: str, value: str) -> str:
    site = cstr(getattr(getattr(frappe, "local", None), "site", None)).strip()
    digest = hashlib.sha256(
        f"{site}\0{MAYBANK_PROVIDER}\0{scope}\0{value}".encode("utf-8")
    ).hexdigest()
    return f"kopos:maybank-qr-rate:{scope}:{digest}"


def _check_rate_limit(device_id: str, outlet_id: str) -> None:
    device_limit = _config_positive_int(
        "maybank_qr_per_device_per_minute",
        MAX_QR_PER_MINUTE,
        1_000,
    )
    outlet_limit = _config_positive_int(
        "maybank_qr_per_outlet_per_minute",
        DEFAULT_QR_PER_OUTLET_PER_MINUTE,
        10_000,
    )
    try:
        cache = frappe.cache()
        make_key = getattr(cache, "make_key", None)
        eval_script = getattr(cache, "eval", None)
        if not callable(make_key) or not callable(eval_script):
            raise RuntimeError("Redis atomic scripting is unavailable")
        result = eval_script(
            QR_RATE_LIMIT_SCRIPT,
            2,
            make_key(_qr_rate_limit_key("device", device_id)),
            make_key(_qr_rate_limit_key("outlet", outlet_id)),
            QR_RATE_LIMIT_WINDOW_SECONDS,
            device_limit,
            outlet_limit,
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("Redis returned invalid Maybank QR rate-limit state")
        device_count = int(result[0])
        outlet_count = int(result[1])
    except Exception as error:
        log_sanitized_error("Maybank QR rate limiter unavailable", error)
        raise MaybankQrPreflightRejection(
            "Automatic QR is temporarily unavailable; try again shortly",
            PREFLIGHT_REASON_RATE_LIMITER_UNAVAILABLE,
        )

    if device_count > device_limit or outlet_count > outlet_limit:
        raise MaybankQrPreflightRejection(
            "Automatic QR request limit exceeded; try again shortly",
            PREFLIGHT_REASON_RATE_LIMIT,
        )
