"""Partial-refund money parity with the tablet and exhaustive owner-rule invariants."""
from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import pytest

from kopos_connector.kopos.services.accounting.refund_allocation import (
    REFUND_ALLOCATION_VERSION,
    RefundAllocationError,
    RefundableSale,
    RefundableSaleLine,
    allocate_refund,
    refund_position,
)


def _pos_root() -> Path:
    return Path(os.environ.get("KOPOS_TYPESCRIPT_ROOT", Path(__file__).resolve().parents[2] / "JiJiPOS/kopos/src")).resolve().parents[1]


def _sale(fixture: dict) -> RefundableSale:
    return RefundableSale(
        lines=tuple(
            RefundableSaleLine(line["lineId"], line["quantity"], line["lineTotalSen"])
            for line in fixture["lines"]
        ),
        tax_sen=fixture["taxSen"],
        rounding_sen=fixture["roundingSen"],
        total_sen=fixture["totalSen"],
        cash_unit_sen=fixture["cashUnitSen"],
    )


def _units(entries: list[dict]) -> list[dict]:
    return [{"line_id": entry.get("lineId"), "quantity": entry.get("quantity")} for entry in entries]


FIXTURES = json.loads((_pos_root() / "contracts/fixtures/refund-allocation.v1.json").read_text())


def test_fixture_version_matches_the_erp_rule():
    assert FIXTURES["version"] == REFUND_ALLOCATION_VERSION
    assert len(FIXTURES["cases"]) >= 40


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=[case["name"] for case in FIXTURES["cases"]])
def test_shared_vectors_match_the_tablet(case):
    sale = _sale(case["sale"])
    if "error" in case:
        with pytest.raises(RefundAllocationError) as error:
            allocate_refund(sale, _units(case["refundedBefore"]), _units(case["request"]))
        assert error.value.code == case["error"]
    else:
        allocation = allocate_refund(sale, _units(case["refundedBefore"]), _units(case["request"]))
        assert allocation.to_wire() == case["expected"]


def _cash_rounding(total: int) -> int:
    remainder = total % 5
    return -remainder if remainder <= 2 else 5 - remainder


def _sales():
    for quantities in ((1,), (2,), (3,), (1, 2), (2, 2)):
        for totals in itertools.product((0, 1, 7, 100, 1003), repeat=len(quantities)):
            subtotal = sum(totals)
            for tax in sorted({0, subtotal * 6 // 100, 3 if subtotal else 0}):
                for cash in (False, True):
                    rounding = _cash_rounding(subtotal + tax) if cash else 0
                    yield RefundableSale(
                        lines=tuple(RefundableSaleLine(f"line-{index}", quantity, total) for index, (quantity, total) in enumerate(zip(quantities, totals))),
                        tax_sen=tax,
                        rounding_sen=rounding,
                        total_sen=subtotal + tax + rounding,
                        cash_unit_sen=5 if cash else 1,
                    )


def _unit_orders(sale: RefundableSale):
    units = [line.line_id for line in sale.lines for _ in range(line.quantity)]
    return set(itertools.permutations(units))


def test_every_one_unit_refund_order_conserves_the_sale_exactly():
    checked = 0
    for sale in _sales():
        subtotal = sum(line.line_total_sen for line in sale.lines)
        for order in _unit_orders(sale):
            refunded: dict[str, int] = {}
            carried: dict[str, int] = {}
            sums = {"amount": 0, "net": 0, "tax": 0, "rounding": 0}
            finals = 0
            for line_id in [*order, None]:
                request = dict(carried)
                if line_id is not None:
                    request[line_id] = request.get(line_id, 0) + 1
                if not request:
                    continue
                try:
                    allocation = allocate_refund(
                        sale,
                        [{"line_id": key, "quantity": value} for key, value in refunded.items()],
                        [{"line_id": key, "quantity": value} for key, value in request.items()],
                    )
                except RefundAllocationError as error:
                    assert error.code == "nothing_to_refund"
                    carried = request
                    continue
                carried = {}
                for key, value in request.items():
                    refunded[key] = refunded.get(key, 0) + value
                assert allocation.amount_sen > 0
                assert allocation.amount_sen == allocation.net_sen + allocation.tax_sen + allocation.rounding_sen
                if sale.cash_unit_sen == 5:
                    assert allocation.amount_sen % 5 == 0
                position = refund_position(sale, [{"line_id": key, "quantity": value} for key, value in refunded.items()])
                assert position["amount_sen"] * subtotal <= position["net_sen"] * sale.total_sen
                sums["amount"] += allocation.amount_sen
                sums["net"] += allocation.net_sen
                sums["tax"] += allocation.tax_sen
                sums["rounding"] += allocation.rounding_sen
                finals += allocation.is_final
                checked += 1
            assert sums["amount"] == sale.total_sen
            if sale.total_sen > 0:
                assert sums == {"amount": sale.total_sen, "net": subtotal, "tax": sale.tax_sen, "rounding": sale.rounding_sen}
                assert finals == 1
    assert checked > 1000


def test_request_identities_and_quantities_must_belong_to_the_sale():
    sale = RefundableSale((RefundableSaleLine("latte", 2, 2000),), 0, 0, 2000, 1)
    cases = {
        "unknown_line": ([], [{"line_id": "mocha", "quantity": 1}]),
        "duplicate_line": ([], [{"line_id": "latte", "quantity": 1}, {"line_id": "latte", "quantity": 1}]),
        "invalid_quantity": ([], [{"line_id": "latte", "quantity": True}]),
        "exceeds_remaining": ([{"line_id": "latte", "quantity": 2}], [{"line_id": "latte", "quantity": 1}]),
    }
    for code, (before, request) in cases.items():
        with pytest.raises(RefundAllocationError) as error:
            allocate_refund(sale, before, request)
        assert error.value.code == code
