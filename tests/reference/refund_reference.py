"""A deliberately simple, slow reference for refund allocation.

Phase 30 asks for an oracle: a second implementation that is obvious enough to
read in one sitting, so property tests can assert
`production(input) == reference(input)` rather than asserting arithmetic against
itself.

This one is written from the *rule*, as recorded in docs/domain/invariants.yaml:

    A position is the cumulative money owed if exactly `units[line]` units of each
    line have been returned. A line's share is the floor of its pro-rata part,
    and exactly the whole line when every unit is returned. Tax and rounding take
    the same floor share of the subtotal. When the sale settles in 5-sen cash
    units, a partial position is floored to a multiple of 5 and the difference is
    absorbed by rounding. An allocation is the difference between two positions.

and it uses different primitives to get there: exact rationals from
`fractions.Fraction` with an explicit `math.floor`, rather than Python's integer
`//`. If the two ever disagree, one of them is wrong about flooring.

HONEST LIMIT ON INDEPENDENCE
----------------------------
This file and the code it checks were written by the same author in the same
session, so it cannot catch a misunderstanding of the *requirement* - only a
mistake in expressing it. A reference that could catch the former would have to
come from someone who had not read `refund_allocation.py`. What it does give is a
second derivation of the composition: positions, their difference, and the cash
rounding step are each rebuilt here rather than reused.

It is not used in production and is not imported by the connector.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Mapping, NamedTuple, Sequence


class ReferencePosition(NamedTuple):
    line_net: dict[str, int]
    net_sen: int
    tax_sen: int
    rounding_sen: int
    amount_sen: int


class ReferenceAllocation(NamedTuple):
    line_net: dict[str, int]
    net_sen: int
    tax_sen: int
    rounding_sen: int
    amount_sen: int
    is_final: bool


def exact_floor_share(amount: int, part: int, whole: int) -> int:
    """`amount * part / whole`, floored, via exact rationals.

    Deliberately not `amount * part // whole`. The whole point of this file is to
    reach the same number by a different route. `whole == 0` cannot arise for a
    line (a zero-quantity line is refused upstream) but is defined here as zero so
    the function is total.
    """
    if whole == 0:
        return 0
    if part == whole:
        return amount
    return math.floor(Fraction(amount) * Fraction(part, whole))


def reference_position(
    lines: Sequence[tuple[str, int, int]],
    tax_sen: int,
    rounding_sen: int,
    cash_unit_sen: int,
    units: Mapping[str, int],
) -> ReferencePosition:
    """Cumulative refunded money for `units` returned per line.

    `lines` is (line_id, quantity, line_total_sen), in sale order.
    """
    line_net: dict[str, int] = {}
    for line_id, quantity, line_total_sen in lines:
        returned = units.get(line_id, 0)
        line_net[line_id] = exact_floor_share(line_total_sen, returned, quantity)

    net = sum(line_net.values())
    subtotal = sum(line_total_sen for _id, _qty, line_total_sen in lines)

    # A sale with no paid lines carries no tax and no rounding, so every position
    # on it is zero. Guarding here also keeps the shares below well defined.
    if subtotal == 0:
        return ReferencePosition(line_net, 0, 0, 0, 0)

    tax = exact_floor_share(tax_sen, net, subtotal)
    rounding = exact_floor_share(rounding_sen, net, subtotal)
    amount = net + tax + rounding

    # Cash that settles in 5-sen units cannot pay out a 1-sen remainder. A fully
    # refunded sale is exempt: it must settle at exactly the sale total.
    if cash_unit_sen == 5 and net != subtotal:
        remainder = amount - (amount // 5) * 5
        amount = amount - remainder
        rounding = amount - net - tax

    return ReferencePosition(line_net, net, tax, rounding, amount)


def reference_allocate(
    lines: Sequence[tuple[str, int, int]],
    tax_sen: int,
    rounding_sen: int,
    cash_unit_sen: int,
    total_sen: int,
    refunded_before: Mapping[str, int],
    request: Mapping[str, int],
) -> ReferenceAllocation | None:
    """The refund for `request`, given `refunded_before` already returned.

    Returns None where the production allocator refuses, so a caller can compare
    refusals as well as amounts. No exception types are modelled: this is an
    oracle for the arithmetic, not for the error contract.
    """
    by_id = {line_id: (quantity, total) for line_id, quantity, total in lines}

    # Refuse exactly where the production allocator refuses on quantity.
    for line_id, quantity in request.items():
        if line_id not in by_id or quantity < 1:
            return None
        if refunded_before.get(line_id, 0) + quantity > by_id[line_id][0]:
            return None
    if not request:
        return None

    after = dict(refunded_before)
    for line_id, quantity in request.items():
        after[line_id] = after.get(line_id, 0) + quantity

    start = reference_position(lines, tax_sen, rounding_sen, cash_unit_sen, refunded_before)
    end = reference_position(lines, tax_sen, rounding_sen, cash_unit_sen, after)

    amount = end.amount_sen - start.amount_sen
    if amount <= 0:
        return None

    return ReferenceAllocation(
        line_net={
            line_id: end.line_net[line_id] - start.line_net[line_id]
            for line_id in request
        },
        net_sen=end.net_sen - start.net_sen,
        tax_sen=end.tax_sen - start.tax_sen,
        rounding_sen=end.rounding_sen - start.rounding_sen,
        amount_sen=amount,
        is_final=end.amount_sen == total_sen,
    )
