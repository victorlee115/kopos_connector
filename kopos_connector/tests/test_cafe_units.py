from __future__ import annotations

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.api.cafe_units import amount_of_phrase, amount_phrase, difference_phrase


def test_amounts_use_cafe_words_without_rounding() -> None:
    assert amount_phrase("250.000", "Gram") == "250 g"
    assert amount_phrase("1000", "Millilitre") == "1 L"
    assert amount_phrase("1500", "Millilitre") == "1,500 ml"
    assert amount_phrase("1", "Nos") == "1 item"
    assert amount_phrase("2", "Sleeve") == "2 sleeves"


def test_amounts_name_the_thing_without_erp_word_salad() -> None:
    assert amount_of_phrase("3060", "Millilitre", "Milk") == "3,060 ml of milk"
    assert amount_of_phrase("2", "Bag", "Coffee Beans") == "2 bags of coffee beans"
    assert amount_of_phrase("1", "Nos", "Cup") == "1 cup"
    assert amount_of_phrase("17", "Nos", "Cup") == "17 cups"


def test_differences_name_the_exact_amount_and_direction() -> None:
    assert difference_phrase("-18.000", "Gram") == "18 g less than expected"
    assert difference_phrase("2", "Carton") == "2 cartons more than expected"
    assert difference_phrase("0.00", "Millilitre") == "Matches the saved amount"
