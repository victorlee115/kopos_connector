import hashlib
import re
import secrets
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import frappe
from frappe.utils import cint, cstr

from .crypto import decrypt_aes, encrypt_pin

DEFAULT_BASE_URL = "https://emerchant.maybank2u.com.my:8443/api/"
DEFAULT_ALLOWED_ORIGINS = frozenset({"https://emerchant.maybank2u.com.my:8443"})
DEFAULT_DEVICE_NAME = "Samsung Galaxy Tab A11 Small"
DEFAULT_DEVICE_OS = "Android"
# Compatibility aliases for support tooling that displays the defaults. Runtime
# provider requests use the immutable values persisted in Maybank Settings.
DEVICE_NAME = DEFAULT_DEVICE_NAME
DEVICE_OS = DEFAULT_DEVICE_OS
PROVIDER_DEVICE_ID_FIELD = "provider_device_id"
PROVIDER_DEVICE_NAME_FIELD = "provider_device_name"
PROVIDER_DEVICE_OS_FIELD = "provider_device_os"
PROVIDER_DEVICE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PROVIDER_DEVICE_METADATA_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9 ._()/-]{1,63}$"
)


def _site_cache_key(key: str) -> str:
    return f"{key}:{frappe.local.site}"


def _config_value(name: str, default: object = None) -> object:
    config = getattr(frappe, "conf", None)
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default) if config is not None else default


def _explicit_mock_mode_enabled() -> bool:
    """Require both an explicit opt-in and a test/developer execution context."""
    explicit_opt_in = bool(cint(_config_value("allow_maybank_mock", 0)))
    developer_context = bool(cint(_config_value("developer_mode", 0)))
    test_context = bool(cint(getattr(getattr(frappe, "flags", None), "in_test", 0)))
    return explicit_opt_in and (developer_context or test_context)


def _https_origin(value: str, *, config_entry: bool = False) -> str:
    try:
        parsed = urlsplit(cstr(value).strip())
        port = parsed.port
    except ValueError:
        frappe.throw(
            "Maybank API URL contains an invalid port",
            frappe.ValidationError,
        )

    if parsed.scheme.lower() != "https" or not parsed.hostname:
        label = "Maybank allowed origin" if config_entry else "Maybank API URL"
        frappe.throw(f"{label} must use HTTPS", frappe.ValidationError)
    if parsed.username or parsed.password:
        frappe.throw(
            "Maybank API URL must not contain embedded credentials",
            frappe.ValidationError,
        )

    hostname = parsed.hostname.lower().rstrip(".")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"https://{hostname}{f':{port}' if port not in (None, 443) else ''}"


def _allowed_origins() -> frozenset[str]:
    configured = _config_value("maybank_allowed_origins", ())
    if isinstance(configured, str):
        entries = [entry.strip() for entry in configured.split(",") if entry.strip()]
    elif isinstance(configured, (list, tuple, set, frozenset)):
        entries = [cstr(entry).strip() for entry in configured if cstr(entry).strip()]
    elif configured in (None, ""):
        entries = []
    else:
        frappe.throw(
            "maybank_allowed_origins must be a comma-separated string or list",
            frappe.ValidationError,
        )

    origins = set(DEFAULT_ALLOWED_ORIGINS)
    for entry in entries:
        origins.add(_https_origin(entry, config_entry=True))
    return frozenset(origins)


def validate_base_url(base_url: str, *, allow_mock: bool = False) -> str:
    """Normalize and validate the provider URL before creating an HTTP client."""
    value = cstr(base_url).strip() or DEFAULT_BASE_URL
    if value.lower().rstrip("/") == "mock:":
        if not allow_mock:
            frappe.throw(
                "Maybank mock mode is disabled outside an explicitly opted-in test or developer context",
                frappe.ValidationError,
            )
        return "mock://"

    parsed = urlsplit(value)
    origin = _https_origin(value)
    if origin not in _allowed_origins():
        frappe.throw(
            f"Maybank API origin {origin} is not allowlisted",
            frappe.ValidationError,
        )
    if parsed.query or parsed.fragment:
        frappe.throw(
            "Maybank API URL must not contain a query string or fragment",
            frappe.ValidationError,
        )

    path = parsed.path or "/"
    lowered_path = path.lower()
    path_segments = [segment for segment in path.split("/") if segment]
    if (
        "\\" in path
        or any(segment in {".", ".."} for segment in path_segments)
        or "%2e" in lowered_path
    ):
        frappe.throw(
            "Maybank API URL contains an unsafe path",
            frappe.ValidationError,
        )
    if not path.endswith("/"):
        path = f"{path}/"
    return f"{origin}{path}"


def _valid_provider_device_id(value: object) -> str:
    candidate = cstr(value).strip().lower()
    return candidate if PROVIDER_DEVICE_ID_PATTERN.fullmatch(candidate) else ""


def _valid_provider_metadata(value: object) -> str:
    candidate = " ".join(cstr(value).strip().split())
    return candidate if PROVIDER_DEVICE_METADATA_PATTERN.fullmatch(candidate) else ""


def _read_provider_device_id() -> str:
    getter = getattr(frappe.db, "get_single_value", None)
    if not callable(getter):
        return ""
    return _valid_provider_device_id(
        getter("Maybank Settings", PROVIDER_DEVICE_ID_FIELD)
    )


def _read_provider_metadata(fieldname: str) -> str:
    getter = getattr(frappe.db, "get_single_value", None)
    if not callable(getter):
        return ""
    return _valid_provider_metadata(getter("Maybank Settings", fieldname))


def _persist_single_value(fieldname: str, value: str) -> None:
    setter = getattr(frappe.db, "set_single_value", None)
    if callable(setter):
        setter("Maybank Settings", fieldname, value)
        return
    frappe.db.set_value(
        "Maybank Settings",
        "Maybank Settings",
        fieldname,
        value,
        update_modified=False,
    )


def _read_legacy_cached_device_id() -> str:
    try:
        cache = frappe.cache()
        value = cache.get(_site_cache_key("maybank_device_uid"))
    except Exception:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    return _valid_provider_device_id(value)


def ensure_stable_device_id() -> str:
    """Persist one provider identity in the Maybank Settings singleton.

    This is called by install/migrate hooks. Runtime requests only read the value,
    so a failed provider request cannot roll it back or silently rotate identity.
    """
    existing = _read_provider_device_id()
    if existing:
        return existing

    candidate = _read_legacy_cached_device_id() or secrets.token_hex(16)
    _persist_single_value(PROVIDER_DEVICE_ID_FIELD, candidate)

    persisted = _read_provider_device_id()
    if persisted != candidate:
        frappe.throw(
            "Maybank provider device identity could not be persisted; run bench migrate before enabling Maybank",
            frappe.ValidationError,
        )
    return persisted


def ensure_stable_device_metadata() -> tuple[str, str]:
    """Persist provider-visible model/OS labels once during install or migrate."""
    configured_defaults = {
        PROVIDER_DEVICE_NAME_FIELD: _config_value(
            "maybank_provider_device_name", DEFAULT_DEVICE_NAME
        ),
        PROVIDER_DEVICE_OS_FIELD: _config_value(
            "maybank_provider_device_os", DEFAULT_DEVICE_OS
        ),
    }
    persisted_values: dict[str, str] = {}
    for fieldname, configured in configured_defaults.items():
        existing = _read_provider_metadata(fieldname)
        if existing:
            persisted_values[fieldname] = existing
            continue
        candidate = _valid_provider_metadata(configured)
        if not candidate:
            frappe.throw(
                f"{fieldname} must be 2-64 safe printable characters",
                frappe.ValidationError,
            )
        _persist_single_value(fieldname, candidate)
        persisted = _read_provider_metadata(fieldname)
        if persisted != candidate:
            frappe.throw(
                f"{fieldname} could not be persisted; run bench migrate before enabling Maybank",
                frappe.ValidationError,
            )
        persisted_values[fieldname] = persisted
    return (
        persisted_values[PROVIDER_DEVICE_NAME_FIELD],
        persisted_values[PROVIDER_DEVICE_OS_FIELD],
    )


def _stable_device_id() -> str:
    device_id = _read_provider_device_id()
    if not device_id:
        frappe.throw(
            "Maybank provider device identity is missing; run bench migrate before enabling Maybank",
            frappe.ValidationError,
        )
    return device_id


def _create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        # Never replay provider POST requests at the transport layer. Status
        # queries that use POST perform their one explicit auth refresh below.
        allowed_methods=Retry.DEFAULT_ALLOWED_METHODS,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=10)
    session.mount("https://", adapter)
    return session


class MaybankClient:
    def __init__(
        self,
        username: str,
        encrypted_pin: str,
        user_type: str,
        outlet_id: str,
        base_url: str,
        provider_device_id: str,
        provider_device_name: str,
        provider_device_os: str,
        *,
        allow_mock: bool = False,
    ) -> None:
        self.username = username
        self.encrypted_pin = encrypted_pin
        self.user_type = user_type
        self.outlet_id = outlet_id
        self.base_url = validate_base_url(base_url, allow_mock=allow_mock)
        self.provider_device_id = _valid_provider_device_id(provider_device_id)
        self.provider_device_name = _valid_provider_metadata(provider_device_name)
        self.provider_device_os = _valid_provider_metadata(provider_device_os)
        if not self.provider_device_id and self.base_url != "mock://":
            frappe.throw(
                "Maybank provider device identity is invalid",
                frappe.ValidationError,
            )
        if self.base_url != "mock://" and (
            not self.provider_device_name or not self.provider_device_os
        ):
            frappe.throw(
                "Maybank provider device metadata is invalid",
                frappe.ValidationError,
            )
        self.session = _create_session()

    def _auth_scope(self) -> str:
        raw = "|".join(
            [self.username, self.user_type, self.outlet_id, self.base_url.rstrip("/")]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _jwt_cache_key(self) -> str:
        return _site_cache_key(f"maybank_jwt:{self._auth_scope()}")

    def _outlet_token_cache_key(self) -> str:
        return _site_cache_key(f"maybank_outlet_token:{self._auth_scope()}")

    def _cache_get(self, key: str) -> str:
        value = frappe.cache().get(key)
        if isinstance(value, bytes):
            return value.decode()
        return value or ""

    def _cache_set(self, key: str, value: str, ttl_seconds: int) -> None:
        frappe.cache().setex(key, ttl_seconds, value)

    def _cache_delete(self, key: str) -> None:
        frappe.cache().delete(key)

    def _clear_auth_cache(self) -> None:
        self._cache_delete(self._jwt_cache_key())
        self._cache_delete(self._outlet_token_cache_key())

    @classmethod
    def from_settings(cls) -> "MaybankClient":
        s = frappe.get_single("Maybank Settings")
        if not s.enabled:
            frappe.throw("Maybank QRPayBiz is not enabled")
        return cls(
            username=cstr(s.username),
            encrypted_pin=s.get_password("encrypted_pin") or "",
            user_type=cstr(s.user_type) or "merchant",
            outlet_id=cstr(s.outlet_id),
            base_url=cstr(s.base_url) or DEFAULT_BASE_URL,
            provider_device_id=_stable_device_id(),
            provider_device_name=cstr(getattr(s, PROVIDER_DEVICE_NAME_FIELD, None)),
            provider_device_os=cstr(getattr(s, PROVIDER_DEVICE_OS_FIELD, None)),
            allow_mock=_explicit_mock_mode_enabled(),
        )

    def _get_jwt(self, force_refresh: bool = False) -> str:
        cache_key = self._jwt_cache_key()
        if not force_refresh:
            token = self._cache_get(cache_key)
            if token:
                return token

        endpoint = (
            "v1/mobile/cashier/login"
            if self.user_type == "cashier"
            else "v1/mobile/merchant/login"
        )
        encrypted_pin = encrypt_pin(self.encrypted_pin, self.username)
        resp = self.session.post(
            self.base_url + endpoint,
            json={
                "user_name": self.username,
                "pin": encrypted_pin,
                "device_name": self.provider_device_name,
                "device_os": self.provider_device_os,
                "device_uniqueid": self.provider_device_id,
                "gcm_token": "",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "QR000":
            frappe.throw(f"Maybank login failed: {data.get('text', 'Unknown error')}")

        token = cstr(data["access_token"])
        if not token:
            frappe.throw("Maybank login returned empty access token")
        self._cache_set(cache_key, token, 3540)
        return token

    def _get_outlet_token(self, force_refresh: bool = False) -> str:
        if self.user_type != "corporate":
            return self._get_jwt(force_refresh=force_refresh)

        cache_key = self._outlet_token_cache_key()
        if not force_refresh:
            token = self._cache_get(cache_key)
            if token:
                return token

        jwt = self._get_jwt(force_refresh=force_refresh)
        payload = {"outlet_id": self.outlet_id}
        resp = self.session.post(
            self.base_url + "v1/mobile/merchant/sslv2/outletaccesstoken",
            json=payload,
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=15,
        )
        if resp.status_code == 401 and not force_refresh:
            self._clear_auth_cache()
            jwt = self._get_jwt(force_refresh=True)
            resp = self.session.post(
                self.base_url + "v1/mobile/merchant/sslv2/outletaccesstoken",
                json=payload,
                headers={"Authorization": f"Bearer {jwt}"},
                timeout=15,
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "QR000":
            frappe.throw(
                f"Maybank outlet token request failed: {data.get('text', 'Unknown error')}"
            )
        token_data = data.get("data")
        if not token_data or not isinstance(token_data, list) or len(token_data) == 0:
            frappe.throw("Maybank returned empty outlet access token response")
        outlet_info = token_data[0].get("outletaccesstoken", {})
        encrypted = outlet_info.get("access_token", "")
        if not encrypted:
            frappe.throw("Maybank returned empty encrypted outlet token")
        outlet_token = decrypt_aes(encrypted, jwt)
        self._cache_set(cache_key, outlet_token, 3540)
        return outlet_token

    def generate_qr(self, amount_rm: str) -> dict:
        if self.base_url == "mock://":
            return self._mock_generate_qr(amount_rm)

        token = self._get_jwt()
        if self.user_type == "corporate":
            endpoint = "v1/mobile/merchant/cpDynamicQRCodeInitTransaction"
        elif self.user_type == "cashier":
            endpoint = "v1/mobile/cashier/initTransaction"
        else:
            endpoint = "v1/mobile/merchant/initTransaction"

        payload = {"outlet_id": self.outlet_id, "sale_amount": amount_rm}
        resp = self.session.post(
            self.base_url + endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 401:
            # QR creation is not provider-idempotent. Clear the stale token for
            # the next independently reconciled attempt, but never replay this
            # POST because the provider may have accepted the first request.
            self._clear_auth_cache()
        resp.raise_for_status()
        return resp.json()

    def check_status(self, transaction_refno: str) -> dict:
        if self.base_url == "mock://":
            return self._mock_check_status(transaction_refno)

        token = self._get_outlet_token()
        endpoint = (
            "v1/mobile/cashier/transactionById"
            if self.user_type == "cashier"
            else "v1/mobile/merchant/transactionById"
        )
        payload = {"transaction_refno": transaction_refno}
        resp = self.session.post(
            self.base_url + endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 401:
            self._clear_auth_cache()
            token = self._get_outlet_token(force_refresh=True)
            resp = self.session.post(
                self.base_url + endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        resp.raise_for_status()
        return resp.json()

    def _mock_generate_qr(self, amount_rm: str) -> dict:
        refno = f"MOCK-TXN-{secrets.token_hex(8).upper()}"
        qr_data = f"00020101021226580013com.kopos.mock0110{refno}5204581253034585405{amount_rm}5802MY5910KoPOS Mock6010Kuala Lumpur6304"
        return {
            "status": "QR000",
            "data": [
                {
                    "transaction_refno": refno,
                    "qr_data": qr_data,
                    "qr_code": qr_data,
                }
            ],
        }

    def _mock_check_status(self, transaction_refno: str) -> dict:
        sale_amount = frappe.db.get_value(
            "Maybank QR Transaction",
            {"transaction_refno": transaction_refno},
            "sale_amount",
        ) or "0.00"
        return {
            "status": "QR000",
            "data": [
                {
                    "transaction_refno": transaction_refno,
                    "sale_amount": cstr(sale_amount),
                    "status": 1,
                }
            ],
        }
