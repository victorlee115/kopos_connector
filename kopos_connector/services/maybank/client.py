from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit

import requests
from jsonschema import Draft7Validator
from requests.adapters import HTTPAdapter
from urllib3.exceptions import HTTPError as Urllib3HTTPError
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

import frappe
from frappe.utils import cint, cstr

from .crypto import decrypt_aes, encrypt_pin

DEFAULT_BASE_URL = "https://emerchant.maybank2u.com.my:8443/api/"
DEFAULT_ALLOWED_ORIGINS = frozenset({"https://emerchant.maybank2u.com.my:8443"})
DEFAULT_DEVICE_NAME = "Samsung Galaxy Tab A11 Small"
DEFAULT_DEVICE_OS = "Android"
# Compatibility aliases for support tooling that displays the defaults. Runtime
# provider requests use the immutable values persisted in a branch-scoped
# Maybank QRPayBiz Account.  The singleton remains available only for the
# compatibility migration path.
DEVICE_NAME = DEFAULT_DEVICE_NAME
DEVICE_OS = DEFAULT_DEVICE_OS
PROVIDER_DEVICE_ID_FIELD = "provider_device_id"
PROVIDER_DEVICE_NAME_FIELD = "provider_device_name"
PROVIDER_DEVICE_OS_FIELD = "provider_device_os"
PROVIDER_DEVICE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PROVIDER_DEVICE_METADATA_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9 ._()/-]{1,63}$"
)
MAYBANK_MOCK_PAYMENT_MODE_AUTO_PAID = "auto_paid"
MAYBANK_MOCK_PAYMENT_MODE_MANUAL = "manual"
MAYBANK_MOCK_PAYMENT_MODES = frozenset(
    {
        MAYBANK_MOCK_PAYMENT_MODE_AUTO_PAID,
        MAYBANK_MOCK_PAYMENT_MODE_MANUAL,
    }
)
TLS_PEER_CERTIFICATE_SHA256_ATTR = "_kopos_tls_peer_certificate_sha256"
MAX_PROVIDER_RESPONSE_BYTES = 1_048_576

# Validate consumed response fields, allowing additive provider fields/items.
_AUTH_RESPONSE_VALIDATORS = {
    "login": Draft7Validator({
        "type": "object", "required": ["status", "access_token"],
        "properties": {"status": {"const": "QR000"}},
    }),
    "outlet token": Draft7Validator({
        "type": "object", "required": ["status", "data"],
        "properties": {"status": {"const": "QR000"}, "data": {
            "type": "array", "minItems": 1, "items": [{
                "type": "object", "required": ["outletaccesstoken"],
                "properties": {"outletaccesstoken": {
                    "type": "object", "required": ["access_token"],
                    "properties": {"access_token": {"type": "string", "minLength": 1}},
                }},
            }],
        }},
    }),
}
TLS_VERIFIED_ATTR = "_kopos_tls_verified"


def _peer_certificate_der(response: requests.Response) -> bytes | None:
    """Read the live peer certificate before urllib3 releases the connection."""

    raw_response = getattr(response, "raw", None)
    connection = getattr(raw_response, "connection", None) or getattr(
        raw_response, "_connection", None
    )
    sock = getattr(connection, "sock", None)
    get_peer_certificate = getattr(sock, "getpeercert", None)
    if not callable(get_peer_certificate):
        return None
    try:
        certificate = get_peer_certificate(binary_form=True)
    except Exception:
        return None
    return bytes(certificate) if certificate else None


class _TlsEvidenceAdapter(HTTPAdapter):
    """Bound provider bodies and attach verified peer-certificate evidence."""

    def send(
        self,
        request: Any,
        stream: bool = False,
        timeout: Any = None,
        verify: bool | str = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        response = super().send(
            request,
            stream=stream,
            timeout=timeout,
            verify=verify,
            cert=cert,
            proxies=proxies,
        )
        certificate = _peer_certificate_der(response)
        certificate_sha256 = (
            hashlib.sha256(certificate).hexdigest() if certificate else ""
        )
        setattr(
            response,
            TLS_PEER_CERTIFICATE_SHA256_ATTR,
            certificate_sha256,
        )
        setattr(
            response,
            TLS_VERIFIED_ATTR,
            bool(verify is not False and certificate_sha256),
        )
        try:
            body = None
            read_failed = False
            try:
                # Bound wire payload before decompression, regardless of Content-
                # Length or chunked framing. urllib3 owns both transport parsing
                # and bounded decompression; do not implement a second decoder.
                encoded = response.raw.read(MAX_PROVIDER_RESPONSE_BYTES + 1, decode_content=False)
                if len(encoded) <= MAX_PROVIDER_RESPONSE_BYTES:
                    # A short read can precede urllib3's premature-EOF check.
                    # Force that check before accepting a complete body.
                    encoded += response.raw.read(1, decode_content=False)
                if len(encoded) <= MAX_PROVIDER_RESPONSE_BYTES:
                    with HTTPResponse(
                        body=BytesIO(encoded), preload_content=False,
                        headers={"content-encoding": response.headers.get("Content-Encoding", "")},
                    ) as decoded:
                        body = decoded.read(MAX_PROVIDER_RESPONSE_BYTES + 1, decode_content=True)
            except Urllib3HTTPError:
                read_failed = True
            # Raise outside the parser exception handler, without raw context.
            if read_failed:
                raise requests.RequestException("Maybank response could not be read")
            if body is None or len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                raise requests.RequestException("Maybank response exceeds byte limit")
            # Cache the bounded decoded body before Requests' Session.send can
            # eagerly read content (including during redirect handling). These
            # are Requests' own response cache fields, covered by real TLS tests.
            response._content = body
            response._content_consumed = True
            return response
        finally:
            response.close()


def _exact_body_bytes(value: Any, fieldname: str) -> bytes:
    if isinstance(value, bytes):
        body = value
    elif isinstance(value, str):
        body = value.encode("utf-8")
    else:
        frappe.throw(
            f"Maybank {fieldname} bytes are unavailable",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise")
    if not body:
        frappe.throw(
            f"Maybank {fieldname} bytes are empty",
            frappe.ValidationError,
        )
    return body


def _status_transport_evidence(
    response: requests.Response,
    transaction_refno: str,
) -> dict[str, Any]:
    request = getattr(response, "request", None)
    request_body = _exact_body_bytes(
        getattr(request, "body", None),
        "status request body",
    )
    try:
        request_payload = json.loads(request_body)
    except (TypeError, ValueError, UnicodeDecodeError):
        frappe.throw(
            "Maybank status request body is not exact JSON",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise")
    if request_payload != {"transaction_refno": transaction_refno}:
        frappe.throw(
            "Maybank status request body does not match the durable transaction reference",
            frappe.ValidationError,
        )

    response_body = _exact_body_bytes(response.content, "status response body")
    certificate_sha256 = cstr(
        getattr(response, TLS_PEER_CERTIFICATE_SHA256_ATTR, None)
    ).strip()
    tls_verified = getattr(response, TLS_VERIFIED_ATTR, None) is True
    if (
        not re.fullmatch(r"[a-f0-9]{64}", certificate_sha256)
        or not tls_verified
    ):
        frappe.throw(
            "Maybank TLS peer-certificate evidence is unavailable",
            frappe.ValidationError,
        )

    return {
        "request_body_sha256": hashlib.sha256(request_body).hexdigest(),
        "response_body_sha256": hashlib.sha256(response_body).hexdigest(),
        "tls_peer_certificate_sha256": certificate_sha256,
        "tls_verified": True,
        "observed_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }


def _site_cache_key(key: str) -> str:
    return f"{key}:{frappe.local.site}"


def _json_response(response: requests.Response, operation: str) -> dict[str, Any]:
    try:
        data = response.json()
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        frappe.throw(f"Maybank {operation} response is invalid", frappe.ValidationError)
    return data


def _auth_response(response: requests.Response, operation: str) -> dict[str, Any]:
    data = _json_response(response, operation)
    # is_valid avoids secret-bearing validation errors. Raise outside the JSON
    # exception handler so raw response text is not retained as exception context.
    if not _AUTH_RESPONSE_VALIDATORS[operation].is_valid(data):
        frappe.throw(f"Maybank {operation} response is invalid", frappe.ValidationError)
    return data


def _bearer_token(value: object, operation: str) -> str:
    # RFC 6750 section 2.1: opaque Bearer syntax, not a JWT/signature validator.
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value):
        return value
    frappe.throw(f"Maybank {operation} access token is invalid", frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")


def _config_value(name: str, default: object = None) -> object:
    config = getattr(frappe, "conf", None)
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default) if config is not None else default


def _explicit_mock_mode_enabled() -> bool:
    """Require both an explicit opt-in and a test/developer execution context."""
    explicit_opt_in = bool(cint(_config_value("allow_maybank_mock", 0)))
    developer_context = bool(cint(_config_value("developer_mode", 0)))
    test_context = bool(cint(getattr(frappe, "in_test", 0))) or bool(
        cint(getattr(getattr(frappe, "flags", None), "in_test", 0))
    )
    return explicit_opt_in and (developer_context or test_context)


def _mock_payment_mode() -> str:
    value = cstr(
        _config_value(
            "maybank_mock_payment_mode",
            MAYBANK_MOCK_PAYMENT_MODE_AUTO_PAID,
        )
    ).strip().lower()
    if value not in MAYBANK_MOCK_PAYMENT_MODES:
        frappe.throw(
            "maybank_mock_payment_mode must be auto_paid or manual",
            frappe.ValidationError,
        )
    return value


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
    # Validate the configured origin once; never forward credentials or replay
    # a provider POST via redirects, even to the same origin. Requests owns the
    # rejection (TooManyRedirects), before sending any redirected request.
    session.max_redirects = 0
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        # Never replay provider POST requests at the transport layer. Status
        # queries that use POST perform their one explicit auth refresh below.
        allowed_methods=Retry.DEFAULT_ALLOWED_METHODS,
    )
    adapter = _TlsEvidenceAdapter(
        max_retries=retry,
        pool_connections=5,
        pool_maxsize=10,
    )
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
        account_revision: tuple[str, str] | None = None,
    ) -> None:
        self.username = username
        self.encrypted_pin = encrypted_pin
        self.user_type = user_type
        self.outlet_id = outlet_id
        self.account_revision = account_revision
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
        # Frappe saves modified and the encrypted PIN in one DB transaction.
        # A late response from an old client can populate only its old revision.
        raw = json.dumps([
            self.account_revision, self.username, self.user_type,
            self.outlet_id, self.base_url.rstrip("/"),
        ], separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _jwt_cache_key(self) -> str:
        return _site_cache_key(f"maybank_jwt:v3:{self._auth_scope()}")

    def _outlet_token_cache_key(self) -> str:
        return _site_cache_key(f"maybank_outlet_token:v3:{self._auth_scope()}")

    def _cache_get(self, key: str) -> str:
        if self.account_revision is None:
            return ""
        value = frappe.cache().get(key)
        if isinstance(value, bytes):
            return value.decode()
        return value or ""

    def _cache_set(self, key: str, value: str, ttl_seconds: int) -> None:
        if self.account_revision is not None:
            frappe.cache().setex(key, ttl_seconds, value)

    def _cache_delete(self, key: str) -> None:
        if self.account_revision is not None:
            frappe.cache().delete(key)

    def _clear_auth_cache(self) -> None:
        self._cache_delete(self._jwt_cache_key())
        self._cache_delete(self._outlet_token_cache_key())

    @classmethod
    def from_settings(cls) -> "MaybankClient":
        """Uncached compatibility constructor; use a saved account for token reuse."""
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

    @classmethod
    def from_account_doc(
        cls,
        account_doc: Any,
        outlet_id: str,
        *,
        require_enabled: bool = False,
    ) -> "MaybankClient":
        """Build a client from one immutable credential principal."""

        if require_enabled and not int(getattr(account_doc, "enabled", 0) or 0):
            frappe.throw("Maybank QRPayBiz Account is disabled", frappe.ValidationError)
        resolved_outlet = cstr(outlet_id).strip()
        if not resolved_outlet:
            frappe.throw("Maybank outlet ID is required on the POS Profile", frappe.ValidationError)
        encrypted_pin = account_doc.get_password("encrypted_pin") or ""
        account_name = cstr(getattr(account_doc, "name", None)).strip()
        modified = cstr(getattr(account_doc, "modified", None)).strip()
        return cls(
            username=cstr(getattr(account_doc, "username", None)),
            encrypted_pin=encrypted_pin,
            user_type=cstr(getattr(account_doc, "user_type", None)) or "merchant",
            outlet_id=resolved_outlet,
            base_url=cstr(getattr(account_doc, "base_url", None)) or DEFAULT_BASE_URL,
            provider_device_id=cstr(getattr(account_doc, PROVIDER_DEVICE_ID_FIELD, None)),
            provider_device_name=cstr(getattr(account_doc, PROVIDER_DEVICE_NAME_FIELD, None)),
            provider_device_os=cstr(getattr(account_doc, PROVIDER_DEVICE_OS_FIELD, None)),
            allow_mock=_explicit_mock_mode_enabled(),
            account_revision=(account_name, modified) if account_name and modified else None,
        )

    @classmethod
    def from_device(cls, device_id: str) -> "MaybankClient":
        from kopos_connector.services.maybank.branch_config import (
            profile_for_device,
            require_provider_binding,
        )

        profile = profile_for_device(device_id)
        account, outlet = require_provider_binding(profile, for_new_payment=True)
        return cls.from_account_doc(account, outlet, require_enabled=True)

    @classmethod
    def from_transaction(cls, transaction_doc: Any) -> "MaybankClient":
        """Recover a transaction using its persisted account/outlet snapshot."""

        if isinstance(transaction_doc, dict):
            account_name = cstr(transaction_doc.get("maybank_qrpaybiz_account")).strip()
            outlet = cstr(transaction_doc.get("outlet_id")).strip()
        else:
            account_name = cstr(getattr(transaction_doc, "maybank_qrpaybiz_account", None)).strip()
            outlet = cstr(getattr(transaction_doc, "outlet_id", None)).strip()
        if not account_name or not outlet:
            frappe.throw(
                "Maybank QR transaction provider snapshot is missing",
                frappe.ValidationError,
            )
        account = frappe.get_doc("Maybank QRPayBiz Account", account_name)
        return cls.from_account_doc(account, outlet, require_enabled=False)

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
        data = _auth_response(resp, "login")
        token = _bearer_token(data["access_token"], "login")
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
        data = _auth_response(resp, "outlet token")
        encrypted = data["data"][0]["outletaccesstoken"]["access_token"]
        try:
            plaintext = decrypt_aes(encrypted, jwt)
        except ValueError:
            plaintext = None
        outlet_token = _bearer_token(plaintext, "outlet token")
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
        return _json_response(resp, "QR creation")

    def check_status(self, transaction_refno: str) -> dict:
        if self.base_url == "mock://":
            return self._mock_check_status(transaction_refno)

        return _json_response(self._request_status(transaction_refno), "status")

    def _request_status(self, transaction_refno: str) -> requests.Response:
        """One owner for status POST and its single, read-only auth refresh."""
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
        return resp

    def check_status_with_transport_evidence(
        self,
        transaction_refno: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read provider status and return hashes of that exact HTTPS exchange.

        This method does not update a Maybank QR Transaction or any accounting
        document. It exists only for an explicitly invoked production-
        acceptance capture. Raw bodies, certificates, and credentials are
        never returned or persisted.
        """

        if self.base_url == "mock://":
            frappe.throw(
                "Maybank UAT transport evidence cannot use mock mode",
                frappe.ValidationError,
            )

        resp = self._request_status(transaction_refno)
        result = _json_response(resp, "status")
        transport_evidence = _status_transport_evidence(
            resp,
            transaction_refno,
        )
        return result, transport_evidence

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
                    "status": (
                        2
                        if _mock_payment_mode()
                        == MAYBANK_MOCK_PAYMENT_MODE_MANUAL
                        else 1
                    ),
                }
            ],
        }
