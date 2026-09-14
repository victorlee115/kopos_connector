"""Optional receipt configuration uses native Company and linked Address data."""
from types import SimpleNamespace
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()
from kopos_connector.api import devices


def test_receipt_header_uses_linked_native_address():
    profile = SimpleNamespace(company="Company A", company_address="Address A")
    address = SimpleNamespace(
        links=[SimpleNamespace(link_doctype="Company", link_name="Company A")],
        address_line1="12 Test Street", address_line2="Level 2\nUnit 3",
        city="Kuala Lumpur", pincode="50000", state="", country="Malaysia",
    )
    with patch.object(devices.frappe.db, "get_value", return_value="Test Cafe"), \
            patch.object(devices.frappe, "get_doc", return_value=address):
        assert devices._receipt_header(profile) == {
            "business_name": "Test Cafe",
            "address_lines": ["12 Test Street", "Level 2", "Unit 3", "Kuala Lumpur 50000", "Malaysia"],
        }


def test_receipt_header_never_leaks_a_foreign_company_address():
    profile = SimpleNamespace(company="Company A", company_address="Foreign Address")
    address = SimpleNamespace(links=[SimpleNamespace(link_doctype="Company", link_name="Company B")])
    with patch.object(devices.frappe.db, "get_value", return_value="Test Cafe"), \
            patch.object(devices.frappe, "get_doc", return_value=address), \
            patch.object(devices, "log_sanitized_error") as log:
        assert devices._receipt_header(profile) == {"business_name": "Test Cafe", "address_lines": []}
        log.assert_called_once()


def test_invalid_or_missing_optional_details_do_not_disable_configuration():
    profile = SimpleNamespace(company="Company A", company_address=None)
    with patch.object(devices.frappe.db, "get_value", return_value="Bad\x1b@"), \
            patch.object(devices, "log_sanitized_error") as log:
        assert devices._receipt_header(profile) == {"business_name": "Sales receipt", "address_lines": []}
        log.assert_called_once()
    with patch.object(devices.frappe.db, "get_value", side_effect=RuntimeError("lookup unavailable")), \
            patch.object(devices, "log_sanitized_error") as log:
        assert devices._receipt_header(profile) == {"business_name": "Company A", "address_lines": []}
        log.assert_called_once()
