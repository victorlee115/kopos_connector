# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.utils import diagnostics


def test_error_log_keeps_sanitized_exception_summary_before_traceback(
    monkeypatch: Any,
) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        diagnostics.frappe,
        "get_traceback",
        lambda: "Traceback (most recent call last):\n  File sample.py, line 1",
        raising=False,
    )
    monkeypatch.setattr(
        diagnostics.frappe,
        "log_error",
        lambda *, message, title: captured.update(message=message, title=title),
        raising=False,
    )

    diagnostics.log_sanitized_error("KoPOS close_shift failed", ValueError("boom"))

    assert captured["title"] == "KoPOS close_shift failed"
    assert captured["message"].startswith("ValueError: boom\nTraceback")


@pytest.mark.parametrize(
    "unsafe_message",
    [
        "credential=hunter2",
        "secret=hunter2",
        "session_id=deadbeef",
        "Cookie: sid=deadbeef",
        "Set-Cookie: system_user=yes; sid=deadbeef",
        '{"csrf_token":"deadbeef"}',
    ],
)
def test_error_summary_redacts_generic_credentials_and_session_material(
    unsafe_message: str,
) -> None:
    result = diagnostics.sanitized_error_message(RuntimeError(unsafe_message))

    assert result == "RuntimeError: [redacted]"
    assert "hunter2" not in result
    assert "deadbeef" not in result


def test_error_log_redacts_cookie_material_in_traceback(monkeypatch: Any) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        diagnostics.frappe,
        "get_traceback",
        lambda: "Traceback\nRequest Cookie: sid=deadbeef",
        raising=False,
    )
    monkeypatch.setattr(
        diagnostics.frappe,
        "log_error",
        lambda *, message, title: captured.update(message=message, title=title),
        raising=False,
    )

    diagnostics.log_sanitized_error("KoPOS submit failed", ValueError("failed"))

    assert captured["message"] == "ValueError: failed\n[redacted]"
    assert "deadbeef" not in captured["message"]
