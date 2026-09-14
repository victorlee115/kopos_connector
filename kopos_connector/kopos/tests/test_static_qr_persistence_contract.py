"""Native static-QR commissioning; all synthetic changes roll back, no provider calls."""
import binascii
import hashlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api import devices
from kopos_connector.services import static_qr_commissioning as static_qr


VALID_QR = (
    "00020201021126410014A000000615000101065016640209123456789"
    "5204999953034585802MY5909QRCSDNBHD6005BANGI6304184A"
)


def _checksum(prefix):
    body = prefix + "6304"
    return f"{body}{binascii.crc_hqx(body.encode('ascii'), 0xFFFF):04X}"


def _invalid_payloads():
    yield "format order", _checksum(VALID_QR[6:-8] + VALID_QR[:6])
    for value in ("", "0", "0x01A", "0305AB", "0301A0301B", "0300"):
        yield "additional " + value, _checksum(VALID_QR[:-8] + f"62{len(value):02d}{value}")
    yield "empty unknown", _checksum(VALID_QR[:-8] + "8100")


class TestStaticQrPersistenceContract(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        name = frappe.db.get_value("KoPOS Device", {"device_id": "SMOKE-TAB-A001"}, "name")
        self.assertTrue(name, "Requires the isolated canonical smoke fixture")
        self.device = frappe.get_doc("KoPOS Device", name)
        self.profile = frappe.get_doc("POS Profile", self.device.pos_profile)

    def tearDown(self):
        frappe.db.rollback()

    def _save_device(self, payload):
        self.device.static_qr_payload = payload
        self.device.flags.notify_update = False
        self.device.save(ignore_permissions=True)
        return self.device.reload()

    def test_native_device_save_read_preserves_exact_payload_and_commissioning(self):
        payload = _checksum(VALID_QR[:-8] + "62120702T10302S18101Z")
        saved = self._save_device(payload)
        metadata = static_qr.commissioned_static_qr_config(saved, expected_company=self.profile.company)
        self.assertEqual(metadata["static_qr_payload"], payload)
        self.assertEqual(metadata["static_qr_payload_sha256"], hashlib.sha256(payload.encode('ascii')).hexdigest())
        self.assertEqual(metadata["static_qr_merchant_id"], "123456789")
        self.assertEqual(metadata["static_qr_acquirer_id"], "501664")
        self.assertEqual(metadata["static_qr_company"], self.profile.company)
        self.assertTrue(metadata["static_qr_commissioned_at"])
        retained = (saved.static_qr_payload_sha256, saved.static_qr_commissioned_at)
        self.assertEqual((self._save_device(payload).static_qr_payload_sha256,
                          self.device.static_qr_commissioned_at), retained)

    def test_native_device_save_rejects_bad_layout_without_replacing_commissioning(self):
        self._save_device(VALID_QR)
        fields = ["static_qr_payload", "static_qr_payload_sha256", "static_qr_merchant_id",
                  "static_qr_commissioned_at", "static_qr_company", "config_version"]
        before = frappe.db.get_value("KoPOS Device", self.device.name, fields)
        for label, payload in _invalid_payloads():
            with self.subTest(layout=label):
                frappe.db.savepoint("static_qr_layout")
                try:
                    with self.assertRaises(frappe.ValidationError):
                        self._save_device(payload)
                    self.assertEqual(frappe.db.get_value("KoPOS Device", self.device.name, fields), before)
                finally:
                    frappe.db.rollback(save_point="static_qr_layout")
                    self.device.reload()
                    frappe.clear_messages()

    def test_native_profile_serialization_omits_invalid_payable_payload(self):
        # A native save/read supplies production-shaped metadata. Direct DB
        # replacement then models a retained bad import, not approved authoring.
        self.profile.custom_kopos_static_qr_payload = VALID_QR
        self.profile.custom_kopos_static_qr_payload_sha256 = hashlib.sha256(VALID_QR.encode('ascii')).hexdigest()
        self.profile.custom_kopos_static_qr_merchant_id = "123456789"
        self.profile.custom_kopos_static_qr_acquirer_id = "501664"
        self.profile.custom_kopos_static_qr_merchant_name = "QRCSDNBHD"
        self.profile.custom_kopos_static_qr_version = "02"
        self.profile.custom_kopos_static_qr_commissioned_at = frappe.utils.now_datetime()
        self.profile.flags.notify_update = False
        self.profile.save(ignore_permissions=True)
        self.device.reload()
        good = devices.serialize_device_config(self.device)
        self.assertEqual(good["static_qr_payload"], VALID_QR)
        self.assertEqual(good["static_qr_configuration_status"], "commissioned")
        for label, payload in _invalid_payloads():
            with self.subTest(layout=label):
                frappe.db.set_value("POS Profile", self.profile.name, {
                    "custom_kopos_static_qr_payload": payload,
                    "custom_kopos_static_qr_payload_sha256": hashlib.sha256(payload.encode('ascii')).hexdigest(),
                }, update_modified=False)
                # Do not create non-transactional Error Logs for this projection
                # test; the diagnostic helper has its own native persistence suite.
                with patch.object(devices, "log_sanitized_error") as diagnostic:
                    result = devices.serialize_device_config(self.device)
                self.assertIsNone(result["static_qr_payload"])
                self.assertEqual(result["static_qr_configuration_status"], "invalid")
                self.assertEqual(
                    {key: value for key, value in result.items() if not key.startswith("static_qr_")},
                    {key: value for key, value in good.items() if not key.startswith("static_qr_")},
                )
                diagnostic.assert_called_once()
                frappe.clear_messages()
