from decimal import Decimal

import pytest

from kopos_connector.kopos.services.inventory_autopilot.preparation import (
    preparation_thresholds,
)


def test_preparation_thresholds_default_to_bom_quantity():
    assert preparation_thresholds(bom_quantity="12") == (Decimal("12"), Decimal("12"))


def test_preparation_thresholds_accept_explicit_batch_and_ready_levels():
    assert preparation_thresholds(
        bom_quantity="12",
        configured_batch_qty="24",
        configured_min_ready_qty="8",
    ) == (Decimal("24"), Decimal("8"))


@pytest.mark.parametrize("value", ["-1", "not-a-number", "NaN", "Infinity"])
def test_preparation_thresholds_reject_invalid_bom_quantity(value):
    with pytest.raises(ValueError):
        preparation_thresholds(bom_quantity=value)

