import importlib
from decimal import Decimal
from types import SimpleNamespace

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.doctype.fb_order.fb_order import FBOrder
from kopos_connector.kopos.api.money_contract import (
    LEGACY_DECIMAL_MONEY_CONTRACT_VERSION,
    SEN_MONEY_CONTRACT_VERSION,
    MoneyContractValidationError,
    parse_legacy_decimal_sen,
    parse_positive_integer_quantity,
    parse_sen,
    parse_wire_money_sen,
    persisted_money_to_sen,
    require_money_contract_version,
    sen_to_decimal,
)


def test_live_contract_accepts_integer_sen_and_rejects_decimal_aliases() -> None:
    assert require_money_contract_version(
        {"money_contract_version": SEN_MONEY_CONTRACT_VERSION}
    ) == SEN_MONEY_CONTRACT_VERSION
    assert parse_wire_money_sen(
        {"amount_sen": "1250"},
        version=SEN_MONEY_CONTRACT_VERSION,
        sen_field="amount_sen",
        legacy_fields=("amount",),
    ) == 1250

    with pytest.raises(MoneyContractValidationError, match="remove legacy"):
        parse_wire_money_sen(
            {"amount_sen": 1250, "amount": 12.5},
            version=SEN_MONEY_CONTRACT_VERSION,
            sen_field="amount_sen",
            legacy_fields=("amount",),
        )
    with pytest.raises(MoneyContractValidationError, match="remove legacy"):
        parse_wire_money_sen(
            {"amount_sen": 1250, "amount": None},
            version=SEN_MONEY_CONTRACT_VERSION,
            sen_field="amount_sen",
            legacy_fields=("amount",),
        )


@pytest.mark.parametrize("value", [12.5, "12.50", True, None, "1e3", "1.0"])
def test_live_sen_parser_rejects_non_integer_wire_values(value: object) -> None:
    with pytest.raises(MoneyContractValidationError, match="integer number of sen"):
        parse_sen(value, "amount_sen")


def test_explicit_legacy_contract_converts_only_exact_decimal_currency() -> None:
    assert parse_wire_money_sen(
        {"amount": "12.50"},
        version=LEGACY_DECIMAL_MONEY_CONTRACT_VERSION,
        sen_field="amount_sen",
        legacy_fields=("amount",),
    ) == 1250
    assert parse_legacy_decimal_sen(-0.05, "rounding_adjustment") == -5

    with pytest.raises(MoneyContractValidationError, match="at most 2"):
        parse_legacy_decimal_sen("12.501", "amount")
    with pytest.raises(MoneyContractValidationError, match="not accepted"):
        parse_wire_money_sen(
            {"amount_sen": 1250},
            version=LEGACY_DECIMAL_MONEY_CONTRACT_VERSION,
            sen_field="amount_sen",
            legacy_fields=("amount",),
        )


def test_missing_or_unknown_contract_version_fails_closed() -> None:
    with pytest.raises(MoneyContractValidationError, match="money_contract_version"):
        require_money_contract_version({})
    with pytest.raises(MoneyContractValidationError, match="money_contract_version"):
        require_money_contract_version({"money_contract_version": "float_v2"})


def test_decimal_conversion_is_exact_at_the_erp_boundary() -> None:
    assert sen_to_decimal(1250) == Decimal("12.5")
    assert sen_to_decimal(-5) == Decimal("-0.05")
    assert persisted_money_to_sen(Decimal("12.50"), "grand_total") == 1250
    assert persisted_money_to_sen("-0.05", "rounding_adjustment") == -5

    with pytest.raises(MoneyContractValidationError, match="fractional sen"):
        persisted_money_to_sen("12.501", "grand_total")


def test_food_order_quantity_is_positive_and_integral() -> None:
    assert parse_positive_integer_quantity(2, "qty") == 2
    assert parse_positive_integer_quantity("2.000000", "qty") == 2
    for invalid in (0, -1, 1.5, "1.01", True, None):
        with pytest.raises(MoneyContractValidationError, match="positive integer"):
            parse_positive_integer_quantity(invalid, "qty")


def test_wire_and_persisted_money_reject_values_outside_js_safe_integer_range() -> None:
    with pytest.raises(MoneyContractValidationError, match="safe integer"):
        parse_sen(9_007_199_254_740_992, "amount_sen")
    with pytest.raises(MoneyContractValidationError, match="safe integer"):
        parse_legacy_decimal_sen("90071992547410.00", "amount")
    with pytest.raises(MoneyContractValidationError, match="safe integer"):
        persisted_money_to_sen("90071992547410.00", "grand_total")


def _make_money_only_fb_order() -> FBOrder:
    order = FBOrder()
    order.items = [
        SimpleNamespace(
            line_id="LINE-1",
            qty=Decimal("3.000000"),
            unit_price=Decimal("3.34"),
            modifier_total=Decimal("0.01"),
            discount_amount=Decimal("0.04"),
            line_total=Decimal("0.00"),
        )
    ]
    order.payments = [
        SimpleNamespace(
            payment_method="Cash",
            amount=Decimal("10.96"),
        )
    ]
    order.tax_total = Decimal("0.96")
    order.rounding_adjustment = Decimal("-0.01")
    order.get = lambda fieldname: getattr(order, fieldname, None)
    return order


def test_fb_order_calculates_and_validates_totals_in_integer_sen() -> None:
    order = _make_money_only_fb_order()

    order.calculate_totals()
    order.validate_order_totals()

    assert order.items[0].qty == 3
    assert order.items[0].unit_price == Decimal("3.34")
    assert order.items[0].modifier_total == Decimal("0.01")
    assert order.items[0].discount_amount == Decimal("0.04")
    assert order.items[0].line_total == Decimal("10.01")
    assert order.net_total == Decimal("10.01")
    assert order.tax_total == Decimal("0.96")
    assert order.rounding_adjustment == Decimal("-0.01")
    assert order.grand_total == Decimal("10.96")


def test_fb_order_keeps_fully_discounted_line_with_positive_order_total() -> None:
    order = _make_money_only_fb_order()
    order.items.insert(
        0,
        SimpleNamespace(
            line_id="LINE-FREE-1",
            qty=Decimal("1.000000"),
            unit_price=Decimal("12.00"),
            modifier_total=Decimal("0.00"),
            discount_amount=Decimal("12.00"),
            line_total=Decimal("99.99"),
        ),
    )

    order.calculate_totals()
    order.validate_order_totals()

    assert order.items[0].line_total == Decimal("0.00")
    assert order.net_total == Decimal("10.01")
    assert order.grand_total == Decimal("10.96")


def test_fb_order_rejects_fractional_sen_before_persisting_totals() -> None:
    order = _make_money_only_fb_order()
    order.items[0].unit_price = Decimal("3.341")

    with pytest.raises(frappe.ValidationError, match="fractional sen"):
        order.calculate_totals()


def test_fb_order_shift_cash_projection_accumulates_in_sen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = importlib.import_module(
        "kopos_connector.kopos.services.accounting.return_invoice_service"
    )
    refresh_calls: list[str] = []
    monkeypatch.setattr(
        service,
        "refresh_fb_shift_cash",
        lambda shift: refresh_calls.append(shift),
    )
    order = FBOrder()
    order.shift = "SHIFT-1"

    order.update_shift_expected_cash()

    assert refresh_calls == ["SHIFT-1"]
