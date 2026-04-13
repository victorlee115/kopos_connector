import importlib
import unittest
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
install_module = importlib.import_module("kopos_connector.install.install")


class InstallHookTests(unittest.TestCase):
    def test_before_migrate_normalizes_duplicate_device_api_users(self):
        with patch.object(
            install_module, "normalize_duplicate_device_api_users"
        ) as normalize:
            install_module.before_migrate()

        normalize.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
