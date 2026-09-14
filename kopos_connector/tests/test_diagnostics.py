# pyright: reportMissingImports=false

from __future__ import annotations

import json
import linecache
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from unittest.mock import Mock

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.utils import diagnostics


@pytest.fixture
def error_sink(monkeypatch: Any):
    captured = {}
    doc = Mock()
    def get_doc(values):
        captured.update(title=values["method"], message=values["error"])
        assert set(values) == {"doctype", "method", "error"}
        assert values["doctype"] == "Error Log"
        return doc
    monkeypatch.setattr(diagnostics.frappe, "get_doc", get_doc)
    legacy = Mock(side_effect=lambda *args, **kw: captured.update(
        message=kw.get("message", args[0] if args else ""),
        title=kw.get("title", args[1] if len(args) > 1 else "")))
    monkeypatch.setattr(diagnostics.frappe, "log_error", legacy)
    monkeypatch.setattr(diagnostics.frappe, "flags", Mock(read_only=False), raising=False)
    return captured, doc, legacy


def test_error_log_keeps_summary_and_frame_locations_without_source_or_locals(error_sink, monkeypatch):
    captured, doc, legacy = error_sink
    source_lookup = Mock(side_effect=AssertionError("Must not look up source lines"))
    try:
        raise ValueError("boom")  # unlabelled-source-marker
    except ValueError as error:
        private_local = "unlabelled-local-marker"
        with monkeypatch.context() as scoped:
            scoped.setattr(linecache, "getline", source_lookup)
            diagnostics.log_sanitized_error("KoPOS close_shift failed", error)
        assert private_local
    assert captured["title"] == "KoPOS close_shift failed"
    assert captured["message"].startswith("ValueError: boom\n")
    assert "test_diagnostics.py:" in captured["message"]
    assert "unlabelled" not in captured["message"]
    source_lookup.assert_not_called()
    legacy.assert_not_called()
    doc.insert.assert_called_once_with(ignore_permissions=True)


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


def test_error_log_never_uses_context_collecting_frappe_helpers(error_sink, monkeypatch):
    captured, _, legacy = error_sink
    getter = Mock(return_value="unlabelled-private-marker")
    monkeypatch.setattr(diagnostics.frappe, "get_traceback", getter, raising=False)
    diagnostics.log_sanitized_error("KoPOS submit failed", ValueError("failed"))
    assert captured["message"] == "ValueError: failed"
    getter.assert_not_called()
    legacy.assert_not_called()


def test_failed_savepoint_propagates_without_an_empty_fallback(monkeypatch: Any) -> None:
    failure = RuntimeError("savepoint unavailable")
    database = Mock()
    database.savepoint.side_effect = failure
    monkeypatch.setattr(diagnostics.frappe, "db", database)
    with pytest.raises(RuntimeError) as raised:
        diagnostics.make_savepoint("projection")
    assert raised.value is failure
    database.rollback.assert_not_called()
    database.before_commit.add.assert_called_once()


def test_empty_savepoint_never_rolls_back_the_parent_transaction(monkeypatch: Any) -> None:
    database = Mock()
    monkeypatch.setattr(diagnostics.frappe, "db", database)
    with pytest.raises(ValueError, match="savepoint name is required"):
        diagnostics.rollback_to_savepoint("", title="Projection rollback failed")
    database.rollback.assert_not_called()
    database.before_commit.add.assert_called_once()


def test_failed_named_rollback_propagates_without_logging_or_recovery(monkeypatch: Any) -> None:
    failure = RuntimeError("savepoint no longer exists")
    database = Mock()
    database.rollback.side_effect = failure
    monkeypatch.setattr(diagnostics.frappe, "db", database)
    logger = Mock()
    monkeypatch.setattr(diagnostics, "log_sanitized_error", logger)
    with pytest.raises(RuntimeError) as raised:
        diagnostics.rollback_to_savepoint("owned", title="Projection rollback failed")
    assert raised.value is failure
    database.rollback.assert_called_once_with(save_point="owned")
    database.before_commit.add.assert_called_once()
    logger.assert_not_called()


def test_successful_named_rollback_does_not_block_commit(monkeypatch: Any) -> None:
    database = Mock()
    monkeypatch.setattr(diagnostics.frappe, "db", database)
    diagnostics.rollback_to_savepoint("owned", title="Projection rollback failed")
    database.rollback.assert_called_once_with(save_point="owned")
    database.before_commit.add.assert_not_called()


class OpaqueDiagnostic:
    def __str__(self):
        return "unlabelled-private-marker"

    def __repr__(self):
        return "unlabelled-private-marker"

    def __json__(self):
        raise AssertionError("Diagnostic serialization must not invoke object hooks")

    def __iter__(self):
        raise AssertionError("Diagnostic serialization must not enumerate opaque values")


def test_payload_opaque_values_and_keys_never_reach_object_serialization() -> None:
    value = {"details": OpaqueDiagnostic(), OpaqueDiagnostic(): "private-value"}
    encoded = diagnostics.redacted_json(value)
    assert "unlabelled-private-marker" not in encoded
    assert "private-value" not in encoded
    assert json.loads(encoded)["details"] == "[unsupported]"


def test_payload_does_not_walk_custom_mapping_implementations() -> None:
    class HostileMapping(Mapping):
        def __getitem__(self, key):
            raise AssertionError("Must not call arbitrary mapping methods")

        def __iter__(self):
            raise AssertionError("Must not call arbitrary mapping methods")

        def __len__(self):
            raise AssertionError("Must not call arbitrary mapping methods")

    assert json.loads(diagnostics.redacted_json(HostileMapping())) == "[unsupported]"


@pytest.mark.parametrize("shape", ["list", "dict"])
def test_payload_cycles_are_bounded_without_losing_root_recovery_fields(shape: str) -> None:
    cycle: Any = [] if shape == "list" else {}
    if shape == "list":
        cycle.append(cycle)
    else:
        cycle["again"] = cycle
    payload = {"provider_result": cycle, "resolution": "provider_transaction_absent", "provider_replay_blocked": True}
    encoded = diagnostics.redacted_json(payload)
    parsed = json.loads(encoded)
    assert parsed["resolution"] == payload["resolution"]
    assert parsed["provider_replay_blocked"] is True
    assert "[omitted]" in encoded
    assert len(encoded) < 1000


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), Decimal("12.30"), b"private-bytes", 1 << 10000])
def test_payload_unsupported_primitives_are_never_coerced(value: object) -> None:
    assert json.loads(diagnostics.redacted_json({"value": value})) == {"value": "[unsupported]"}


def test_payload_large_values_have_explicit_omission_and_bounded_output() -> None:
    large = {str(i): ["x" * 501] * 1000 for i in range(1000)}
    payload = {"provider_result": large, "resolution": "late_provider_result_after_release", "display_authorized": False}
    encoded = diagnostics.redacted_json(payload)
    parsed = json.loads(encoded)
    assert parsed["resolution"] == payload["resolution"]
    assert parsed["display_authorized"] is False
    assert "[omitted]" in encoded
    assert len(encoded) < 10000


def test_payload_deep_values_do_not_use_the_python_recursion_limit() -> None:
    value: Any = "private-leaf"
    for _ in range(2000):
        value = [value]
    encoded = diagnostics.redacted_json(value)
    assert "private-leaf" not in encoded
    assert "[omitted]" in encoded
    assert len(encoded) < 100


def test_payload_json_primitives_and_secret_masking_remain_compatible() -> None:
    payload = {"status": "QR000", "data": ({"transaction_refno": "ref-1", "amount_sen": 1230, "token": "private"},),
               "allowed": False, "empty": None, "fraction": 1.25, "note": "kedai 咖啡"}
    before = repr(payload)
    parsed = json.loads(diagnostics.redacted_json(payload))
    assert parsed == {**payload, "data": [{"transaction_refno": "ref-1", "amount_sen": 1230, "token": "[redacted]"}]}
    assert repr(payload) == before


def test_payload_failed_serialization_returns_fixed_json_not_object_text(monkeypatch: Any) -> None:
    monkeypatch.setattr(diagnostics, "redacted_payload", Mock(side_effect=RuntimeError("private-marker")))
    encoded = diagnostics.redacted_json(OpaqueDiagnostic())
    assert json.loads(encoded) == "[diagnostic unavailable]"
    assert "private-marker" not in encoded


def test_error_summary_never_calls_exception_str_or_opaque_arg_hooks():
    class HostileError(Exception):
        def __str__(self):
            raise AssertionError("Do not render custom exceptions")
    assert diagnostics.sanitized_error_message(HostileError("boom")) == "HostileError: boom"
    assert diagnostics.sanitized_error_message(HostileError(OpaqueDiagnostic())) == "HostileError: [unsupported]"
    assert diagnostics.sanitized_error_message(OpaqueDiagnostic()) == "OpaqueDiagnostic: [unsupported]"
    assert diagnostics.sanitized_error_message(ValueError("boom", OpaqueDiagnostic())) == "ValueError: boom"


def test_error_summary_ignores_overridden_exception_args():
    class HostileArgs(Exception):
        @property
        def args(self):
            raise AssertionError("Use native exception args")
    assert diagnostics.sanitized_error_message(HostileArgs("boom")) == "HostileArgs: boom"


def test_error_summary_omits_oversized_text_before_regex_processing(monkeypatch):
    native_search = diagnostics.SENSITIVE_TEXT_PATTERN.search
    def bounded_search(value):
        assert len(value) <= 500
        return native_search(value)
    pattern = Mock(side_effect=bounded_search)
    monkeypatch.setattr(diagnostics, "SENSITIVE_TEXT_PATTERN", Mock(search=pattern))
    assert diagnostics.sanitized_error_message(RuntimeError("x " * 10000)) == "RuntimeError: [omitted]"


@pytest.mark.parametrize("title", ["Cookie: sid=private", "password=private", "x" * 10000, OpaqueDiagnostic()])
def test_log_title_is_sanitized_and_bounded_even_without_an_exception(error_sink, title):
    captured, _, legacy = error_sink
    diagnostics.log_sanitized_error(title)
    assert captured["title"] in ("[redacted]", "[omitted]", "[unsupported]")
    assert captured["message"] == captured["title"]
    legacy.assert_not_called()


def test_log_uses_active_exception_when_omitted_but_never_its_chain(error_sink):
    captured, _, _ = error_sink
    try:
        try:
            raise RuntimeError("unlabelled-chain-marker")
        except RuntimeError:
            raise ValueError("boom")
    except ValueError:
        diagnostics.log_sanitized_error("KoPOS operation failed")
    assert captured["message"].startswith("ValueError: boom")
    assert "unlabelled-chain-marker" not in captured["message"]


def test_log_limits_frame_count_and_keeps_the_summary(error_sink):
    captured, _, _ = error_sink
    def recurse(depth):
        if depth:
            recurse(depth - 1)
        else:
            raise ValueError("boom")
    try:
        recurse(100)
    except ValueError as error:
        diagnostics.log_sanitized_error("KoPOS operation failed", error)
    assert captured["message"].startswith("ValueError: boom")
    assert 1 <= captured["message"].count("test_diagnostics.py:") <= 8
    assert len(captured["message"]) < 4000


def test_log_preserves_native_read_only_deferred_insertion(error_sink, monkeypatch):
    _, doc, legacy = error_sink
    monkeypatch.setattr(diagnostics.frappe, "flags", Mock(read_only=True))
    diagnostics.log_sanitized_error("KoPOS operation failed", ValueError("boom"))
    doc.deferred_insert.assert_called_once_with()
    doc.insert.assert_not_called()
    legacy.assert_not_called()


def test_log_storage_typeerror_is_never_retried(error_sink, monkeypatch):
    _, doc, legacy = error_sink
    doc.insert.side_effect = TypeError("unlabelled-storage-marker")
    legacy.side_effect = TypeError("unlabelled-storage-marker")
    logger = Mock()
    with monkeypatch.context() as scoped:
        scoped.setattr(diagnostics.logging, "getLogger", Mock(return_value=logger))
        diagnostics.log_sanitized_error("KoPOS operation failed", ValueError("boom"))
    doc.insert.assert_called_once_with(ignore_permissions=True)
    legacy.assert_not_called()
    logger.warning.assert_called_once()
    assert "unlabelled-storage-marker" not in str(logger.warning.call_args)


@pytest.mark.parametrize("operation", ["log", "json"])
def test_diagnostic_sink_failures_cannot_mask_original_operation(error_sink, monkeypatch, capsys, operation):
    _, doc, legacy = error_sink
    doc.insert.side_effect = RuntimeError("unlabelled-storage-marker")
    legacy.side_effect = RuntimeError("unlabelled-storage-marker")
    with monkeypatch.context() as scoped:
        scoped.setattr(diagnostics.logging, "getLogger", Mock(side_effect=RuntimeError("unlabelled-handler-marker")))
        if operation == "json":
            scoped.setattr(diagnostics, "redacted_payload", Mock(side_effect=ValueError("unlabelled-encoder-marker")))
            assert diagnostics.redacted_json({}) == '"[diagnostic unavailable]"'
        else:
            diagnostics.log_sanitized_error("KoPOS operation failed", ValueError("boom"))
    assert capsys.readouterr().err == "KoPOS diagnostic recording failed\n"


def test_log_does_not_escape_when_both_fallback_sinks_are_unavailable(error_sink, monkeypatch):
    _, doc, legacy = error_sink
    doc.insert.side_effect = RuntimeError("storage unavailable")
    legacy.side_effect = RuntimeError("storage unavailable")
    with monkeypatch.context() as scoped:
        scoped.setattr(diagnostics.logging, "getLogger", Mock(side_effect=RuntimeError("handler unavailable")))
        scoped.setattr("sys.stderr", Mock(write=Mock(side_effect=OSError("stderr unavailable"))))
        diagnostics.log_sanitized_error("KoPOS operation failed", ValueError("boom"))
