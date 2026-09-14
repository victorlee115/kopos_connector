"""A native runtime failure must never terminate a Frappe web/worker process.

The child receives only bounded JSON, a fixed operation and packaged code.
No ERP credentials, Frappe context, callback, file API or network API is exposed
to JavaScript. This adapter does not itself admit a new checkout version.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from jsonschema import Draft7Validator


MAX_INPUT_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 2_097_152
REQUEST_TIMEOUT_SECONDS = 3
_SLOTS = threading.BoundedSemaphore(2)
_OPERATIONS = {"quote", "basket_totals", "schedule", "occurrences", "identity"}


class PromotionRuntimeError(ValueError):
    """No valid price was returned; never substitute a partial/default quote."""


@lru_cache(maxsize=len(_OPERATIONS))
def _response_validator(operation):
    schema = json.loads(Path(__file__).with_name("response.schema.json").read_text())
    schema["properties"]["result"] = {"$ref": f"#/definitions/{operation}"}
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _finite_response_number(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Promotion response contains a non-finite number")
    return value


def _bounded(value, depth=0, budget=None):
    if budget is None:
        budget = [MAX_INPUT_BYTES]
    budget[0] -= 8 + (len(value.encode("utf-8")) if isinstance(value, str) else 0)
    if budget[0] < 0:
        raise PromotionRuntimeError("This offer is too large to calculate")
    if depth > 16:
        raise PromotionRuntimeError("Promotion input is nested too deeply")
    if isinstance(value, dict):
        if len(value) > 100 or any(not isinstance(key, str) for key in value):
            raise PromotionRuntimeError("Promotion input contains unsupported fields")
        for key, part in value.items():
            _bounded(key, depth + 1, budget)
            _bounded(part, depth + 1, budget)
    elif isinstance(value, list):
        if len(value) > 10_000:
            raise PromotionRuntimeError("Promotion input has too many rows")
        for part in value:
            _bounded(part, depth + 1, budget)
    elif isinstance(value, str) and len(value) > 10_000:
        raise PromotionRuntimeError("Promotion text is too long")


def calculate(operation: str, payload):
    if operation not in _OPERATIONS:
        raise PromotionRuntimeError("Unknown promotion operation")
    try:
        _bounded(payload)
        encoded = json.dumps({"operation": operation, "payload": payload}, allow_nan=False, separators=(",", ":")).encode()
    except PromotionRuntimeError:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise PromotionRuntimeError("Promotion input must contain finite JSON values") from error
    if len(encoded) > MAX_INPUT_BYTES:
        raise PromotionRuntimeError("This offer is too large to calculate")
    if not _SLOTS.acquire(blocking=False):
        raise PromotionRuntimeError("Promotion checks are busy. Try again shortly; no price has been accepted")
    try:
        directory = Path(__file__).resolve().parent
        # Anonymous files avoid communicate() buffering arbitrary native output
        # in the ERP parent. The private worker also applies an OS file-size cap.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            completed = subprocess.run(
                [sys.executable, "-I", str(directory / "worker.py")],
                input=encoded, stdout=output, stderr=errors,
                timeout=REQUEST_TIMEOUT_SECONDS, check=False,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                cwd=directory,
            )
            output.seek(0)
            response_bytes = output.read(MAX_OUTPUT_BYTES + 1)
            errors.seek(0)
            has_errors = bool(errors.read(1))
        if completed.returncode != 0:
            raise PromotionRuntimeError("The promotion check stopped unexpectedly. Try again; no price has been accepted")
        if len(response_bytes) > MAX_OUTPUT_BYTES or has_errors:
            raise PromotionRuntimeError("The promotion check returned an invalid response; no price has been accepted")
        try:
            response = json.loads(response_bytes, parse_float=_finite_response_number, parse_constant=_finite_response_number)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise PromotionRuntimeError("The promotion check response could not be read") from error
        manifest = json.loads((directory / "runtime.json").read_text())
        if not isinstance(response, dict) or response.get("artifact") != manifest["bundleSha256"]:
            raise PromotionRuntimeError("The promotion calculation version could not be verified")
        if hashlib.sha256((directory / "runtime.js").read_bytes()).hexdigest() != manifest["bundleSha256"]:
            raise PromotionRuntimeError("The packaged promotion calculation has changed")
        if not _response_validator(operation).is_valid(response):
            raise PromotionRuntimeError("The promotion check returned an invalid response; no price has been accepted")
        if response.get("ok") is not True:
            raise PromotionRuntimeError(str(response.get("message") or "Check the promotion details"))
        return response["result"]
    except subprocess.TimeoutExpired as error:
        raise PromotionRuntimeError("The promotion check took too long. Try a smaller offer; no price has been accepted") from error
    except OSError as error:
        raise PromotionRuntimeError("The promotion check could not start or load its package. Try again; no price has been accepted") from error
    finally:
        _SLOTS.release()
