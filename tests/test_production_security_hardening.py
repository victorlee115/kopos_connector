from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

maybank_client = importlib.import_module("kopos_connector.services.maybank.client")
projection_log_module = importlib.import_module(
    "kopos_connector.kopos.doctype.fb_projection_log.fb_projection_log"
)
provisioning = importlib.import_module("kopos_connector.api.provisioning")
install_module = importlib.import_module("kopos_connector.install.install")

ROOT = Path(__file__).resolve().parents[1]
VALID_DEVICE_ID = "0123456789abcdef0123456789abcdef"


def _doctype(relative_path: str) -> dict[str, object]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def test_cashier_role_cannot_read_device_doctype_directly() -> None:
    device = _doctype(
        "kopos_connector/kopos/doctype/kopos_device/kopos_device.json"
    )
    roles = {permission["role"] for permission in device["permissions"]}

    assert "POS User" not in roles
    assert "KoPOS Device API" not in roles
    assert any(
        permission["role"] == "System Manager"
        and permission.get("permlevel") == 1
        and permission.get("read") == 1
        and permission.get("write") == 1
        for permission in device["permissions"]
    )

    device_user = _doctype(
        "kopos_connector/kopos/doctype/kopos_device_user/kopos_device_user.json"
    )
    sensitive_fields = {
        "pin",
        "pin_hash",
        "pin_failed_attempts",
        "pin_last_failed_at",
        "pin_locked_until",
    }
    fields = {field["fieldname"]: field for field in device_user["fields"]}
    assert all(fields[fieldname]["permlevel"] == 1 for fieldname in sensitive_fields)


def test_device_scoped_config_endpoint_remains_available() -> None:
    device = SimpleNamespace(device_id="DEVICE-1", enabled=1)
    setup = {"device_id": "DEVICE-1", "config_version": 4}

    with (
        patch.object(provisioning, "get_device_doc", return_value=device),
        patch.object(provisioning, "require_device_api_access") as require_access,
        patch.object(provisioning, "serialize_device_config", return_value=setup),
    ):
        result = provisioning.get_device_config("DEVICE-1")

    require_access.assert_called_once_with(device)
    assert result == {
        "status": "ok",
        "device_id": "DEVICE-1",
        "config_version": 4,
        "setup": setup,
    }


def test_projection_logs_are_read_only_for_support_roles() -> None:
    projection_log = _doctype(
        "kopos_connector/kopos/doctype/fb_projection_log/fb_projection_log.json"
    )
    permissions = {
        permission["role"]: permission for permission in projection_log["permissions"]
    }

    for role in ("System Manager", "KoPOS Manager"):
        permission = permissions[role]
        assert permission["read"] == 1
        assert permission.get("create", 0) == 0
        assert permission.get("write", 0) == 0
        assert permission.get("delete", 0) == 0
        assert permission.get("share", 0) == 0


def test_projection_log_controller_rejects_direct_mutation() -> None:
    log = projection_log_module.FBProjectionLog()
    log.flags = SimpleNamespace(ignore_permissions=False)

    with pytest.raises(projection_log_module.frappe.PermissionError):
        log.before_save()


def test_projection_log_controller_allows_named_system_services() -> None:
    log = projection_log_module.FBProjectionLog()
    log.flags = {"ignore_permissions": True}

    log.before_insert()
    log.before_save()
    log.on_trash()


@pytest.mark.parametrize(
    "url",
    [
        "http://emerchant.maybank2u.com.my:8443/api/",
        "https://attacker.example/api/",
        "https://user:secret@emerchant.maybank2u.com.my:8443/api/",
        "https://emerchant.maybank2u.com.my:8443/api/?redirect=evil",
        "https://emerchant.maybank2u.com.my:8443/api/%2e%2e/",
    ],
)
def test_maybank_base_url_rejects_unsafe_or_unlisted_targets(url: str) -> None:
    with patch.object(
        maybank_client.frappe, "conf", SimpleNamespace(), create=True
    ):
        with pytest.raises(maybank_client.frappe.ValidationError):
            maybank_client.validate_base_url(url)


def test_maybank_base_url_normalizes_official_origin() -> None:
    with patch.object(
        maybank_client.frappe, "conf", SimpleNamespace(), create=True
    ):
        result = maybank_client.validate_base_url(
            "HTTPS://EMERCHANT.MAYBANK2U.COM.MY:8443/api"
        )

    assert result == maybank_client.DEFAULT_BASE_URL


def test_maybank_mock_requires_explicit_developer_or_test_opt_in() -> None:
    with (
        patch.object(
            maybank_client.frappe,
            "conf",
            SimpleNamespace(allow_maybank_mock=1, developer_mode=0),
            create=True,
        ),
        patch.object(maybank_client.frappe.flags, "in_test", 0, create=True),
    ):
        assert maybank_client._explicit_mock_mode_enabled() is False
        with pytest.raises(maybank_client.frappe.ValidationError):
            maybank_client.validate_base_url("mock://")

    with patch.object(
        maybank_client.frappe,
        "conf",
        SimpleNamespace(allow_maybank_mock=1, developer_mode=1),
        create=True,
    ):
        assert maybank_client._explicit_mock_mode_enabled() is True
        assert maybank_client.validate_base_url("mock://", allow_mock=True) == "mock://"


def test_http_adapter_never_retries_post_requests() -> None:
    session = maybank_client._create_session()
    retries = session.adapters["https://"].max_retries

    assert "POST" not in retries.allowed_methods
    assert {"GET", "HEAD", "OPTIONS"}.issubset(retries.allowed_methods)


def test_qr_creation_does_not_replay_after_unauthorized_response() -> None:
    client = maybank_client.MaybankClient(
        username="merchant",
        encrypted_pin="encrypted",
        user_type="merchant",
        outlet_id="OUTLET-1",
        base_url=maybank_client.DEFAULT_BASE_URL,
        provider_device_id=VALID_DEVICE_ID,
        provider_device_name=maybank_client.DEFAULT_DEVICE_NAME,
        provider_device_os=maybank_client.DEFAULT_DEVICE_OS,
    )
    response = Mock(status_code=401)
    response.raise_for_status.side_effect = requests.HTTPError("unauthorized")
    client.session = Mock()
    client.session.post.return_value = response
    client._get_jwt = Mock(return_value="stale-token")
    client._clear_auth_cache = Mock()

    with pytest.raises(requests.HTTPError, match="unauthorized"):
        client.generate_qr("10.00")

    client.session.post.assert_called_once()
    client._clear_auth_cache.assert_called_once_with()
    client._get_jwt.assert_called_once_with()


def test_migrate_preserves_legacy_provider_device_identity() -> None:
    with (
        patch.object(
            maybank_client,
            "_read_provider_device_id",
            side_effect=["", VALID_DEVICE_ID],
        ),
        patch.object(
            maybank_client,
            "_read_legacy_cached_device_id",
            return_value=VALID_DEVICE_ID,
        ),
        patch.object(
            maybank_client.frappe.db,
            "set_single_value",
            create=True,
        ) as set_single_value,
    ):
        result = maybank_client.ensure_stable_device_id()

    assert result == VALID_DEVICE_ID
    set_single_value.assert_called_once_with(
        "Maybank Settings", "provider_device_id", VALID_DEVICE_ID
    )


def test_provider_metadata_defaults_to_small_tab_a11_without_stale_os_version() -> None:
    assert maybank_client.DEFAULT_DEVICE_NAME == "Samsung Galaxy Tab A11 Small"
    assert maybank_client.DEFAULT_DEVICE_OS == "Android"
    assert "Android 11" not in maybank_client.DEFAULT_DEVICE_OS


def test_provider_login_uses_persisted_device_metadata() -> None:
    client = maybank_client.MaybankClient(
        username="merchant",
        encrypted_pin="encrypted",
        user_type="merchant",
        outlet_id="OUTLET-1",
        base_url=maybank_client.DEFAULT_BASE_URL,
        provider_device_id=VALID_DEVICE_ID,
        provider_device_name="Samsung Galaxy Tab A11 Small SM-X130",
        provider_device_os="Android 16",
    )
    response = Mock(status_code=200)
    response.json.return_value = {"status": "QR000", "access_token": "jwt-token"}
    client.session = Mock()
    client.session.post.return_value = response
    client._jwt_cache_key = Mock(return_value="jwt-cache-key")
    client._cache_get = Mock(return_value="")
    client._cache_set = Mock()

    with patch.object(maybank_client, "encrypt_pin", return_value="provider-pin"):
        assert client._get_jwt() == "jwt-token"

    request = client.session.post.call_args.kwargs["json"]
    assert request["device_uniqueid"] == VALID_DEVICE_ID
    assert request["device_name"] == "Samsung Galaxy Tab A11 Small SM-X130"
    assert request["device_os"] == "Android 16"


def test_migrate_persists_configured_provider_metadata_once() -> None:
    persisted: dict[str, str] = {}

    def read_metadata(fieldname: str) -> str:
        return persisted.get(fieldname, "")

    def persist(fieldname: str, value: str) -> None:
        persisted[fieldname] = value

    config = SimpleNamespace(
        maybank_provider_device_name="Samsung Galaxy Tab A11 Small SM-X130",
        maybank_provider_device_os="Android 16",
    )
    with (
        patch.object(maybank_client.frappe, "conf", config, create=True),
        patch.object(
            maybank_client,
            "_read_provider_metadata",
            side_effect=read_metadata,
        ),
        patch.object(
            maybank_client,
            "_persist_single_value",
            side_effect=persist,
        ),
    ):
        result = maybank_client.ensure_stable_device_metadata()

    assert result == ("Samsung Galaxy Tab A11 Small SM-X130", "Android 16")
    assert persisted == {
        "provider_device_name": "Samsung Galaxy Tab A11 Small SM-X130",
        "provider_device_os": "Android 16",
    }


def test_after_migrate_initializes_provider_identity() -> None:
    with (
        patch.object(install_module, "ensure_kopos_module_defs"),
        patch.object(install_module, "ensure_kopos_custom_fields"),
        patch.object(install_module, "create_fb_custom_fields"),
        patch.object(
            install_module,
            "ensure_maybank_provider_device_id",
            return_value=VALID_DEVICE_ID,
        ) as ensure_identity,
    ):
        install_module.after_migrate()

    ensure_identity.assert_called_once_with()


def test_connector_version_is_consistent() -> None:
    package = importlib.import_module("kopos_connector")
    pyproject_source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert package.__version__ == "1.0.10"
    assert 'version = { attr = "kopos_connector.__version__" }' in pyproject_source
    assert '"requests>=2.31.0,<3.0.0"' in pyproject_source
