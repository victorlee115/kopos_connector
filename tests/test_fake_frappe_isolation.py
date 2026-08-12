from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from tests.fake_frappe import install_fake_frappe_modules


def test_fake_installer_never_mutates_an_existing_real_frappe_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_frappe = ModuleType("frappe")
    real_local = SimpleNamespace(conf={"site": "test.localhost"})
    real_db = object()
    real_cache = object()
    real_frappe.local = real_local
    real_frappe.db = real_db
    real_frappe.cache = real_cache
    monkeypatch.setitem(sys.modules, "frappe", real_frappe)

    with pytest.raises(pytest.skip.Exception):
        install_fake_frappe_modules()

    assert real_frappe.local is real_local
    assert real_frappe.db is real_db
    assert real_frappe.cache is real_cache
