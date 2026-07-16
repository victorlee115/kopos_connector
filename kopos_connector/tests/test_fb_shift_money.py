# pyright: reportMissingImports=false

from __future__ import annotations

from decimal import Decimal

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe


frappe.utils.now = lambda: "2026-07-14 13:40:36"

from kopos_connector.kopos.doctype.fb_shift.fb_shift import FBShift


def test_variance_uses_exact_sen_for_mixed_frappe_currency_types() -> None:
    shift = FBShift()
    shift.name = "FB-SHIFT-1"
    shift.counted_cash = Decimal("336.00")
    shift.expected_cash = 324.0

    shift.calculate_variance()

    assert shift.cash_variance == Decimal("12.00")
