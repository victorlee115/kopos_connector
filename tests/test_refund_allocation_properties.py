"""Property tests for the refund allocator's money invariants.

These call the real `allocate_refund`. That distinction matters: a property test
that computes both sides itself proves only that arithmetic is arithmetic.

The invariant under test is the one recorded as `unconfirmed.refund_limit` in
docs/domain/invariants.yaml:

    cumulative successful refunds <= the captured amount

The allocator does not enforce this with an explicit comparison. It enforces it
structurally: a refund's amount is the *difference between two positions*
(`end_amount - start_amount`), where a position is a floor-share of the sale over
refunded units, and refunded units are capped per line at the quantity sold.
These tests are what turns that reading into evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


@st.composite
def refundable_sales(draw: st.DrawFn) -> RefundableSale:
    """A sale that satisfies `_validate_sale`, so failures are about money."""
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
    if subtotal == 0:
        # The validator refuses tax or rounding on a sale with no paid lines.
        tax = 0
        rounding = 0
    else:
        tax = draw(st.integers(min_value=0, max_value=20_000))
        rounding = draw(st.integers(min_value=-4, max_value=4))
    total = subtotal + tax + rounding
    assume(total >= 0)
    return RefundableSale(
        lines=lines,
        tax_sen=tax,
        rounding_sen=rounding,
        total_sen=total,
        cash_unit_sen=draw(st.sampled_from([1, 5])),
    )


def _refund_everything(sale: RefundableSale) -> list[list[dict[str, int]]]:
    """One request per line, refunding every unit, in line order."""
    return [
        [{"line_id": line.line_id, "quantity": line.quantity}]
        for line in sale.lines
        if line.quantity > 0
    ]


@given(sale=refundable_sales())
@settings(max_examples=200, deadline=None)
def test_cumulative_refund_never_exceeds_the_sale_total(sale: RefundableSale) -> None:
    """The invariant: refunding every unit, one line at a time, never overshoots."""
    refunded: list[dict[str, int]] = []
    cumulative = 0

    for request in _refund_everything(sale):
        try:
            allocation = allocate_refund(sale, refunded, request)
        except RefundAllocationError as error:
            # `nothing_to_refund` is legitimate: a zero-value line floor-shares to
            # nothing. Any other refusal on a request we built from the sale's own
            # quantities would be a defect in the allocator, not in this test.
            # Assert on the code, not the message: messages are for humans.
            assert error.code == "nothing_to_refund", (error.code, str(error))
            refunded = _merge(refunded, request)
            continue

        assert allocation.amount_sen > 0, "an accepted allocation must move money"
        cumulative += allocation.amount_sen
        assert cumulative <= sale.total_sen, (
            f"cumulative refund {cumulative} exceeded the sale total {sale.total_sen}"
        )
        refunded = _merge(refunded, request)

    assert cumulative <= sale.total_sen


@given(sale=refundable_sales())
@settings(max_examples=200, deadline=None)
def test_refunding_every_unit_returns_exactly_the_sale_total(sale: RefundableSale) -> None:
    """Fully refunded means fully refunded: no sen kept back, none conjured."""
    every_unit = [
        {"line_id": line.line_id, "quantity": line.quantity} for line in sale.lines
    ]
    position = refund_position(sale, every_unit)
    assert position["amount_sen"] == sale.total_sen, (
        f"refunding every unit settled at {position['amount_sen']}, "
        f"but the sale total is {sale.total_sen}"
    )


@given(sale=refundable_sales())
@settings(max_examples=200, deadline=None)
def test_a_partial_refund_never_returns_more_than_its_share(sale: RefundableSale) -> None:
    """Each single-line refund is bounded by the whole sale, and is positive."""
    for line in sale.lines:
        request = [{"line_id": line.line_id, "quantity": line.quantity}]
        try:
            allocation = allocate_refund(sale, [], request)
        except RefundAllocationError:
            continue  # a zero-value line has nothing to return
        assert 0 < allocation.amount_sen <= sale.total_sen
        assert allocation.net_sen <= line.line_total_sen


@given(sale=refundable_sales())
@settings(max_examples=300, deadline=None)
def test_a_partial_refund_never_exceeds_its_pro_rata_share(sale: RefundableSale) -> None:
    """The property that actually discriminates.

    The cumulative-total tests above cannot catch over-refunding on their own:
    an allocation is `end_position - start_position`, so the sequence telescopes
    to the sale total whatever the share function does. Rounding *up* instead of
    down survives them. It does not survive this.

    A refund of k of n units must never return more than k/n of the line, i.e.
        net_sen * n <= line_total * k
    which is integer-exact and fails immediately under a ceiling share.
    """
    for line in sale.lines:
        if line.quantity < 2 or line.line_total_sen == 0:
            continue  # a single unit or a free line has no partial share to test
        for quantity in range(1, line.quantity):
            request = [{"line_id": line.line_id, "quantity": quantity}]
            try:
                allocation = allocate_refund(sale, [], request)
            except RefundAllocationError as error:
                assert error.code == "nothing_to_refund", (error.code, str(error))
                continue

            line_net = next(
                row.net_sen for row in allocation.lines if row.line_id == line.line_id
            )
            assert line_net * line.quantity <= line.line_total_sen * quantity, (
                f"refunding {quantity} of {line.quantity} returned {line_net} sen, "
                f"more than the pro-rata share of {line.line_total_sen}"
            )
            # And the whole allocation stays inside the sale.
            assert allocation.amount_sen <= sale.total_sen


@given(sale=refundable_sales())
@settings(max_examples=100, deadline=None)
def test_refunding_the_same_units_twice_is_refused(sale: RefundableSale) -> None:
    """Replay must not pay twice: the second identical request is refused."""
    line = sale.lines[0]
    request = [{"line_id": line.line_id, "quantity": line.quantity}]
    try:
        allocate_refund(sale, [], request)
    except RefundAllocationError:
        return  # nothing to refund on this line; the replay case does not arise

    with pytest.raises(RefundAllocationError) as caught:
        allocate_refund(sale, request, request)
    assert caught.value.code in {"exceeds_remaining", "nothing_to_refund"}, caught.value.code


def _merge(
    refunded: list[dict[str, int]], request: list[dict[str, int]]
) -> list[dict[str, int]]:
    """Accumulate refunded units per line, as the caller would persist them."""
    totals: dict[str, int] = {entry["line_id"]: entry["quantity"] for entry in refunded}
    for entry in request:
        totals[entry["line_id"]] = totals.get(entry["line_id"], 0) + entry["quantity"]
    return [{"line_id": line_id, "quantity": qty} for line_id, qty in totals.items()]
