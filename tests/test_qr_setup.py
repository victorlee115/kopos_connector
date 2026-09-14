from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe
from kopos_connector.api import qr_setup
from kopos_connector.services.static_qr_commissioning import inspect_paynet_static_qr


QR = '00020201021126410014A0000006150001010650166402091234567895204999953034585802MY5909QRCSDNBHD6005BANGI6304184A'


@pytest.fixture
def setup(monkeypatch):
    profile = SimpleNamespace(name='Counter', company='Company', currency='MYR', modified='2026-09-14 01:00:00',
        custom_kopos_static_qr_enabled=0, custom_kopos_static_qr_payload='', custom_kopos_automatic_qr_enabled=0,
        check_permission=Mock(), save=Mock())
    profile.save.return_value = profile
    monkeypatch.setattr(frappe, 'get_roles', lambda: ['Accounts Manager'])
    monkeypatch.setattr(frappe, 'get_doc', lambda *args: profile)
    monkeypatch.setattr(frappe.db, 'get_all', Mock(return_value=[]), raising=False)
    monkeypatch.setattr(frappe, 'get_all', Mock(return_value=[]), raising=False)
    monkeypatch.setattr(frappe.db, 'get_value', lambda doctype, *args, **kwargs: 'Bank' if doctype == 'Mode of Payment' else 'QR Bank')
    monkeypatch.setattr(frappe.db, 'savepoint', Mock())
    monkeypatch.setattr(frappe.db, 'rollback', Mock())
    monkeypatch.setattr(frappe.db, 'commit', Mock())
    monkeypatch.setattr(qr_setup, 'get_payment_readiness_payload', lambda *args: {
        'cash': {'ready': True}, 'static_qr': {'ready': True}, 'automatic_qr': {'ready': False},
    })
    return profile


@pytest.mark.parametrize('operation', ['preview', 'apply'])
def test_setup_requires_native_document_permission(setup, operation):
    setup.check_permission.side_effect = frappe.PermissionError('Restricted profile')
    with pytest.raises(frappe.PermissionError):
        if operation == 'preview':
            qr_setup.get_qr_setup_preview(setup.name)
        else:
            qr_setup.apply_qr_configuration(setup.name, {'expected_modified': setup.modified})
    setup.save.assert_not_called()
    frappe.db.commit.assert_not_called()


def test_preview_derives_merchant_but_does_not_save_or_commit(setup):
    result = qr_setup.get_qr_setup_preview(setup.name, {'custom_kopos_static_qr_payload': QR, 'custom_kopos_static_qr_enabled': 1})
    assert result['static_qr_merchant']['merchant_name'] == 'QRCSDNBHD'
    assert result['requires_merchant_confirmation'] is True
    assert result['profile_modified'] == setup.modified
    assert result['changes_are_company_wide'] is False
    setup.save.assert_not_called()
    frappe.db.commit.assert_not_called()


def test_new_qr_requires_confirmation_of_its_exact_payload(setup):
    with pytest.raises(frappe.ValidationError, match='merchant'):
        qr_setup.apply_qr_configuration(setup.name, {
            'expected_modified': setup.modified, 'custom_kopos_static_qr_payload': QR,
            'custom_kopos_static_qr_enabled': 1, 'custom_kopos_manual_qr_suspense_account': 'QR Suspense',
        })
    setup.save.assert_not_called()
    frappe.db.commit.assert_not_called()


def test_confirmed_qr_is_derived_and_saved_using_native_permissions_without_commit(setup):
    result = qr_setup.apply_qr_configuration(setup.name, {
        'expected_modified': setup.modified, 'custom_kopos_static_qr_payload': QR,
        'confirmed_static_qr_sha256': inspect_paynet_static_qr(QR)['payload_sha256'],
        'custom_kopos_static_qr_enabled': 1, 'custom_kopos_manual_qr_suspense_account': 'QR Suspense',
    })
    assert result['static_qr']['ready'] is True
    assert setup.custom_kopos_static_qr_payload_sha256 == inspect_paynet_static_qr(QR)['payload_sha256']
    assert setup.custom_kopos_static_qr_merchant_id == '123456789'
    assert setup.custom_kopos_static_qr_commissioned_at
    setup.check_permission.assert_called_with('write')
    setup.save.assert_called_once_with()
    frappe.db.commit.assert_not_called()


def test_response_failure_is_inside_the_savepoint(setup, monkeypatch):
    monkeypatch.setattr(qr_setup, '_mode_of_payment_mapping', Mock(side_effect=RuntimeError('mapping unavailable')))
    with pytest.raises(RuntimeError, match='mapping unavailable'):
        qr_setup.apply_qr_configuration(setup.name, {'expected_modified': setup.modified})
    setup.save.assert_called_once_with()
    frappe.db.rollback.assert_called_once()
    frappe.db.commit.assert_not_called()


def test_mapping_uses_native_default_account_and_propagates_lookup_failure(setup, monkeypatch):
    lookup = Mock(side_effect=['Mapped QR Bank', 'Bank'])
    monkeypatch.setattr(frappe.db, 'get_value', lookup)
    assert qr_setup._mode_of_payment_mapping(setup)['account'] == 'Mapped QR Bank'
    assert lookup.call_args_list[0].args == ('Mode of Payment Account', {'parent': 'DuitNow QR', 'company': 'Company'}, 'default_account')
    lookup.side_effect = RuntimeError('mapping unavailable')
    with pytest.raises(RuntimeError, match='mapping unavailable'):
        qr_setup._mode_of_payment_mapping(setup)


def test_apply_requires_a_preview_revision_and_rejects_unknown_fields(setup):
    for config in ({}, {'expected_modified': setup.modified, 'company': 'Other Company'}):
        with pytest.raises(frappe.ValidationError):
            qr_setup.apply_qr_configuration(setup.name, config)
    setup.save.assert_not_called()
