from __future__ import annotations

from types import SimpleNamespace

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()


def _api(monkeypatch):
    import kopos_connector.api as api

    monkeypatch.setattr(
        api.frappe,
        "local",
        SimpleNamespace(response={}),
        raising=False,
    )
    return api


def test_every_written_failure_has_a_versioned_reason(monkeypatch) -> None:
    api = _api(monkeypatch)

    api._write_response(
        {"status": "error", "message": "diagnostic only"},
        http_status_code=500,
    )

    assert api.frappe.local.response["error_contract_version"] == "kopos.operational-error.v1"
    assert api.frappe.local.response["error_code"] == "server_unavailable"


def test_custom_domain_reason_is_preserved(monkeypatch) -> None:
    api = _api(monkeypatch)

    class SetupError(api.frappe.ValidationError):
        error_code = "store_setup_incomplete"

    payload = api._validation_error_payload(SetupError("private setup detail"))
    api._write_response(payload, http_status_code=400)

    assert api.frappe.local.response["error_code"] == "store_setup_incomplete"
    assert api.frappe.local.response["error_contract_version"] == "kopos.operational-error.v1"


def test_generic_validation_uses_safe_reason_without_parsing_message(monkeypatch) -> None:
    api = _api(monkeypatch)

    payload = api._validation_error_payload(
        api.frappe.ValidationError("wording may change without changing tablet copy")
    )

    assert payload["error_code"] == "request_rejected"
    assert payload["error_contract_version"] == "kopos.operational-error.v1"
