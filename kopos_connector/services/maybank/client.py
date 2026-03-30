import hashlib
import secrets

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import frappe
from frappe.utils import cstr

from .crypto import decrypt_aes, encrypt_pin

DEFAULT_BASE_URL = "https://emerchant.maybank2u.com.my:8443/api/"
DEVICE_NAME = "Samsung Galaxy A11"
DEVICE_OS = "Android 11"


def _site_cache_key(key: str) -> str:
    return f"{key}:{frappe.local.site}"


def _stable_device_id() -> str:
    cache = frappe.cache()
    cache_key = _site_cache_key("maybank_device_uid")
    uid = cache.get(cache_key)
    if uid:
        return uid.decode() if isinstance(uid, bytes) else uid
    uid = secrets.token_hex(16)
    cache.setex(cache_key, 86400 * 365, uid)
    return uid


def _create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=None,
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
    ):
        self.username = username
        self.encrypted_pin = encrypted_pin
        self.user_type = user_type
        self.outlet_id = outlet_id
        self.base_url = base_url or DEFAULT_BASE_URL
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
                "device_name": DEVICE_NAME,
                "device_os": DEVICE_OS,
                "device_uniqueid": _stable_device_id(),
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
        if self.base_url.startswith("mock://"):
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
            self._clear_auth_cache()
            token = self._get_jwt(force_refresh=True)
            resp = self.session.post(
                self.base_url + endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        resp.raise_for_status()
        return resp.json()

    def check_status(self, transaction_refno: str) -> dict:
        if self.base_url.startswith("mock://"):
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
        return {
            "status": "QR000",
            "data": [
                {
                    "transaction_refno": transaction_refno,
                    "sale_amount": "0.00",
                    "status": 1,
                }
            ],
        }
