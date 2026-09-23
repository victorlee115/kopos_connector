"""production(input) == reference(input), over randomly generated sales.

Phase 30's oracle pattern. `tests/reference/refund_reference.py` re-derives the
refund rule with exact rationals instead of integer division, so this test fails
if the two implementations ever disagree about a floor, a share, or the 5-sen
cash step.

This complements `test_refund_allocation_properties.py` rather than repeating it.
Those tests assert *properties* the allocator must satisfy; this one asserts the
*exact number*, which catches a wrong-but-self-consistent answer that still
satisfies every property.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from kopos_connector.kopos.services.accounting.refund_allocation import (
    RefundableSale,
    RefundableSaleLine,
    RefundAllocationError,
    allocate_refund,
    refund_position,
)

from .reference.refund_reference import reference_allocate, reference_position


@st.composite
def sales(draw: st.DrawFn) -> RefundableSale:
    count = draw(st.integers(min_value=1, max_value=4))
    lines = tuple(
        RefundableSaleLine(
            line_id=f"L{index}",
            quantity=draw(st.integers(min_value=1, max_value=5)),
            line_total_sen=draw(st.integers(min_value=0, max_value=100_000)),
        )
        for index in range(count)
    )
    subtotal = sum(line.line_total_sen for line in lines)
    tax = 0 if subtotal == 0 else draw(st.integers(min_value=0, max_value=20_000))
    rounding = 0 if subtotal == 0 else draw(st.integers(min_value=-4, max_value=4))
    total = subtotal + tax + rounding
    assume(total >= 0)
    return RefundableSale(
        lines=lines,
        tax_sen=tax,
        rounding_sen=rounding,
        total_sen=total,
        cash_unit_sen=draw(st.sampled_from([1, 5])),
    )


def _as_tuples(sale: RefundableSale) -> list[tuple[str, int, int]]:
    return [(line.line_id, line.quantity, line.line_total_sen) for line in sale.lines]


@given(sale=sales(), data=st.data())
@settings(max_examples=300, deadline=None)
def test_position_matches_the_reference(sale: RefundableSale, data: st.DataObject) -> None:
    """Every reachable position agrees, not just the endpoints."""
    units = {
        line.line_id: data.draw(st.integers(min_value=0, max_value=line.quantity))
        for line in sale.lines
    }

    produced = refund_position(
        sale, [{"line_id": k, "quantity": v} for k, v in units.items()]
    )
    expected = reference_position(
        _as_tuples(sale), sale.tax_sen, sale.rounding_sen, sale.cash_unit_sen, units
    )

    assert produced["net_sen"] == expected.net_sen
    assert produced["tax_sen"] == expected.tax_sen
    assert produced["rounding_sen"] == expected.rounding_sen
    assert produced["amount_sen"] == expected.amount_sen


@given(sale=sales(), data=st.data())
@settings(max_examples=300, deadline=None)
def test_allocation_matches_the_reference(sale: RefundableSale, data: st.DataObject) -> None:
    """A refund after an arbitrary prior refund agrees on every field.

    Both implementations are asked the same question from the same starting
    position, including the refusal cases: where production raises, the reference
    must return None, and vice versa.
    """
    before = {
        line.line_id: data.draw(st.integers(min_value=0, max_value=line.quantity))
        for line in sale.lines
    }
    request = {
        line.line_id: data.draw(
            st.integers(min_value=0, max_value=line.quantity - before[line.line_id])
        )
        for line in sale.lines
    }
    request = {k: v for k, v in request.items() if v > 0}
    assume(request)

    expected = reference_allocate(
        _as_tuples(sale),
        sale.tax_sen,
        sale.rounding_sen,
        sale.cash_unit_sen,
        sale.total_sen,
        before,
        request,
    )

    try:
        produced = allocate_refund(
            sale,
            [{"line_id": k, "quantity": v} for k, v in before.items()],
            [{"line_id": k, "quantity": v} for k, v in request.items()],
        )
    except RefundAllocationError as error:
        assert expected is None, (
            f"production refused with {error.code} but the reference allocated "
            f"{expected.amount_sen if expected else None} sen"
        )
        return

    assert expected is not None, (
        f"production allocated {produced.amount_sen} sen but the reference refused"
    )
    assert produced.net_sen == expected.net_sen
    assert produced.tax_sen == expected.tax_sen
    assert produced.rounding_sen == expected.rounding_sen
    assert produced.amount_sen == expected.amount_sen
    assert produced.is_final == expected.is_final
    for row in produced.lines:
        assert row.net_sen == expected.line_net[row.line_id]
