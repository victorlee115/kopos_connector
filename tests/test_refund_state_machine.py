"""Stateful property test of the real refund allocator.

`test_pos_state_machine.py` next to this file models a toy basket (latte, tea,
bun) in plain Python and imports no connector code, so it cannot find a bug in
the connector. This one drives `refund_allocation.allocate_refund` itself.

Hypothesis generates a sale, then an arbitrary interleaving of:

  * partial refunds of any line, any quantity still refundable,
  * over-refund attempts (more units than remain),
  * exact replays of an earlier accepted request,
  * zero-value lines, where there is nothing to give back,

and checks after every step what a cashier and an auditor both rely on:

  * the running total never exceeds what the customer paid,
  * no line ever returns more than its pro-rata share of the units back so far,
  * a refused request is never recorded, so it cannot move money later,
  * once every unit is back, the refunds add up to exactly the sale total,
  * a refund on a cash-rounded sale is always payable in 5-sen coins.

The refused-request and replay rules matter because they are the retries a
tablet produces after a timeout. `_validate_existing_return_matches` stops a
replayed `return_id` upstream; this checks that even if one got through, the
allocator itself would not pay twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hypothesis import assume, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from kopos_connector.kopos.services.accounting.refund_allocation import (
    RefundableSale,
    RefundableSaleLine,
    RefundAllocationError,
    allocate_refund,
)


@st.composite
def sales(draw: st.DrawFn) -> RefundableSale:
    count = draw(st.integers(min_value=1, max_value=4))
    lines = tuple(
        RefundableSaleLine(
            line_id=f"L{index}",
            quantity=draw(st.integers(min_value=1, max_value=6)),
            line_total_sen=draw(st.integers(min_value=0, max_value=50_000)),
        )
        for index in range(count)
    )
    subtotal = sum(line.line_total_sen for line in lines)
    tax = 0 if subtotal == 0 else draw(st.integers(min_value=0, max_value=8_000))
    cash_unit = draw(st.sampled_from([1, 5]))
    if cash_unit == 5 and subtotal:
        # What the till actually does for a single cash tender (roundCashSen):
        # a remainder of 1-2 sen rounds down, 3-4 rounds up, so the total is
        # always payable in 5-sen coins.
        remainder = (subtotal + tax) % 5
        rounding = -remainder if remainder <= 2 else 5 - remainder
    else:
        rounding = 0
    total = subtotal + tax + rounding
    assume(total >= 0)
    return RefundableSale(
        lines=lines,
        tax_sen=tax,
        rounding_sen=rounding,
        total_sen=total,
        cash_unit_sen=cash_unit,
    )


class RefundLedger(RuleBasedStateMachine):
    """What the connector would have persisted after each accepted refund."""

    def __init__(self) -> None:
        super().__init__()
        self.sale: RefundableSale | None = None
        self.refunded: dict[str, int] = {}
        self.line_paid_back: dict[str, int] = {}
        self.paid_back = 0
        self.accepted_requests: list[list[dict[str, int]]] = []

    @initialize(sale=sales())
    def open_sale(self, sale: RefundableSale) -> None:
        self.sale = sale
        self.refunded = {line.line_id: 0 for line in sale.lines}
        self.line_paid_back = {line.line_id: 0 for line in sale.lines}

    # -- helpers ---------------------------------------------------------------

    def _before(self) -> list[dict[str, int]]:
        return [{"line_id": k, "quantity": v} for k, v in self.refunded.items() if v]

    def _remaining(self, line: RefundableSaleLine) -> int:
        return line.quantity - self.refunded[line.line_id]

    def _allocate(self, request: list[dict[str, int]]):
        assert self.sale is not None
        try:
            allocation = allocate_refund(self.sale, self._before(), request)
        except RefundAllocationError as error:
            return None, error.code
        return allocation, None

    def _record(self, request: list[dict[str, int]], allocation) -> None:
        assert self.sale is not None
        lines = {line.line_id: line for line in self.sale.lines}
        for entry in request:
            line = lines[entry["line_id"]]
            k = entry["quantity"]
            row = next(r for r in allocation.lines if r.line_id == line.line_id)
            self.refunded[line.line_id] += k
            self.line_paid_back[line.line_id] += row.net_sen
            # Pro rata, CUMULATIVELY and integer-exact: after r of n units are
            # back, the line has returned no more than r/n of its value.
            #
            # Deliberately not per refund. Floor positions defer a unit's
            # fraction to a later refund -- 2 sen over 3 units returns 0, then
            # 1, then 1 -- so a later single refund can exceed its own unit's
            # share while the running total never does. That is what lets the
            # last refund settle the sale exactly. Hypothesis found this; the
            # per-refund wording in invariants.yaml was wrong, not the code.
            returned = self.refunded[line.line_id]
            assert self.line_paid_back[line.line_id] * line.quantity <= line.line_total_sen * returned, (
                f"{line.line_id}: {returned}/{line.quantity} units returned "
                f"{self.line_paid_back[line.line_id]} of {line.line_total_sen} cumulatively"
            )
        if self.sale.cash_unit_sen == 5:
            # A cash drawer cannot pay out a 1-sen remainder.
            assert allocation.amount_sen % 5 == 0, (
                f"cash refund of {allocation.amount_sen} sen is not payable in 5-sen coins"
            )
        self.paid_back += allocation.amount_sen
        self.accepted_requests.append(request)

    # -- rules -----------------------------------------------------------------

    @precondition(lambda self: self.sale is not None)
    @rule(data=st.data())
    def refund_some(self, data: st.DataObject) -> None:
        assert self.sale is not None
        open_lines = [line for line in self.sale.lines if self._remaining(line) > 0]
        assume(open_lines)
        chosen = data.draw(
            st.lists(st.sampled_from(open_lines), min_size=1, max_size=len(open_lines), unique_by=lambda line: line.line_id)
        )
        request = [
            {"line_id": line.line_id, "quantity": data.draw(st.integers(1, self._remaining(line)))}
            for line in chosen
        ]
        allocation, code = self._allocate(request)
        if allocation is None:
            # The only legitimate refusal for units that are still refundable is
            # that they are worth nothing -- a free line, or a floor share of 0.
            # A refused return is not persisted, so the units stay refundable.
            assert code == "nothing_to_refund", code
            return
        assert allocation.amount_sen > 0
        self._record(request, allocation)

    @precondition(lambda self: self.sale is not None)
    @rule(data=st.data())
    def over_refund(self, data: st.DataObject) -> None:
        assert self.sale is not None
        line = data.draw(st.sampled_from(self.sale.lines))
        too_many = self._remaining(line) + data.draw(st.integers(1, 3))
        allocation, code = self._allocate([{"line_id": line.line_id, "quantity": too_many}])
        assert allocation is None, "an over-refund was paid"
        assert code == "exceeds_remaining", code

    @precondition(lambda self: self.accepted_requests)
    @rule(data=st.data())
    def replay_an_accepted_request(self, data: st.DataObject) -> None:
        """A tablet that timed out sends the same refund again."""
        request = data.draw(st.sampled_from(self.accepted_requests))
        assert self.sale is not None
        lines = {line.line_id: line for line in self.sale.lines}
        still_fits = all(
            entry["quantity"] <= self._remaining(lines[entry["line_id"]]) for entry in request
        )
        allocation, code = self._allocate(request)
        if not still_fits:
            assert allocation is None and code == "exceeds_remaining", code
        # If the units genuinely are still refundable, a second allocation is
        # legitimate at this layer; replay identity is enforced by return_id.
        elif allocation is not None:
            self._record(request, allocation)

    # -- invariants ------------------------------------------------------------

    @invariant()
    def never_pays_back_more_than_was_paid(self) -> None:
        if self.sale is not None:
            assert 0 <= self.paid_back <= self.sale.total_sen, (
                f"refunded {self.paid_back} of a {self.sale.total_sen} sale"
            )

    @invariant()
    def fully_returned_means_fully_refunded(self) -> None:
        if self.sale is None:
            return
        if all(self._remaining(line) == 0 for line in self.sale.lines):
            assert self.paid_back == self.sale.total_sen, (
                f"every unit is back but only {self.paid_back} of {self.sale.total_sen} was refunded"
            )


def test_refund_ledger_state_machine() -> None:
    run_state_machine_as_test(
        RefundLedger,
        settings=settings(max_examples=250, stateful_step_count=25, deadline=None),
    )
