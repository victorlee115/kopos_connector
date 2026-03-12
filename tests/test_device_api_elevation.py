import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.api.devices import elevate_device_api_user


class DeviceApiElevationTests(unittest.TestCase):
    def test_elevates_kopos_device_api_users_temporarily(self):
        switched_users: list[str] = []

        def fake_set_user(user: str) -> None:
            switched_users.append(user)

        with (
            patch(
                "kopos_connector.api.devices.frappe.session",
                SimpleNamespace(user="device-a@kopos.local"),
            ),
            patch(
                "kopos_connector.api.devices.frappe.set_user",
                side_effect=fake_set_user,
                create=True,
            ),
            patch(
                "kopos_connector.api.devices.get_session_roles",
                return_value={"KoPOS Device API"},
            ),
        ):
            with elevate_device_api_user():
                self.assertEqual(switched_users, ["Administrator"])

        self.assertEqual(switched_users, ["Administrator", "device-a@kopos.local"])

    def test_skips_elevation_for_system_manager(self):
        with (
            patch(
                "kopos_connector.api.devices.frappe.session",
                SimpleNamespace(user="Administrator"),
            ),
            patch(
                "kopos_connector.api.devices.frappe.set_user", create=True
            ) as set_user_mock,
            patch(
                "kopos_connector.api.devices.get_session_roles",
                return_value={"System Manager"},
            ),
        ):
            with elevate_device_api_user():
                pass

        set_user_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
