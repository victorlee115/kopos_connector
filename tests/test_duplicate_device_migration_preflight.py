from __future__ import annotations

from unittest.mock import patch

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.patches import normalize_duplicate_device_api_users


DUPLICATE_ROWS = [
    {
        "name": "KOPOS-DEVICE-NEW",
        "device_id": "DEVICE-NEW",
        "api_user": "shared@kopos.local",
        "last_seen_at": "2026-07-12 10:00:00",
        "modified": "2026-07-12 10:00:00",
    },
    {
        "name": "KOPOS-DEVICE-OLD",
        "device_id": "DEVICE-OLD",
        "api_user": "shared@kopos.local",
        "last_seen_at": "2026-07-11 10:00:00",
        "modified": "2026-07-11 10:00:00",
    },
]


def test_migration_preflight_reports_duplicates_without_mutating_devices() -> None:
    with (
        patch.object(normalize_duplicate_device_api_users.frappe.db, "exists", return_value=True),
        patch.object(normalize_duplicate_device_api_users.frappe, "get_all", return_value=DUPLICATE_ROWS),
        patch.object(normalize_duplicate_device_api_users.frappe.db, "set_value") as set_value,
    ):
        with pytest.raises(
            normalize_duplicate_device_api_users.frappe.ValidationError,
            match="verified backup",
        ):
            normalize_duplicate_device_api_users.execute()

    set_value.assert_not_called()


def test_explicit_post_backup_remediation_clears_only_older_mapping_without_commit() -> None:
    with (
        patch.object(normalize_duplicate_device_api_users.frappe.db, "exists", return_value=True),
        patch.object(normalize_duplicate_device_api_users.frappe, "get_all", return_value=DUPLICATE_ROWS),
        patch.object(normalize_duplicate_device_api_users.frappe.db, "set_value") as set_value,
        patch.object(normalize_duplicate_device_api_users.frappe.db, "commit") as commit,
        patch.object(normalize_duplicate_device_api_users.frappe, "log_error"),
    ):
        normalize_duplicate_device_api_users.execute(
            allow_clear=True,
            backup_verified=True,
        )

    set_value.assert_called_once_with(
        "KoPOS Device",
        "KOPOS-DEVICE-OLD",
        "api_user",
        None,
        update_modified=False,
    )
    commit.assert_not_called()


def test_allow_clear_without_verified_backup_still_fails_closed() -> None:
    with (
        patch.object(normalize_duplicate_device_api_users.frappe.db, "exists", return_value=True),
        patch.object(normalize_duplicate_device_api_users.frappe, "get_all", return_value=DUPLICATE_ROWS),
        patch.object(normalize_duplicate_device_api_users.frappe.db, "set_value") as set_value,
    ):
        with pytest.raises(
            normalize_duplicate_device_api_users.frappe.ValidationError,
            match="backup_verified=True",
        ):
            normalize_duplicate_device_api_users.execute(allow_clear=True)

    set_value.assert_not_called()
