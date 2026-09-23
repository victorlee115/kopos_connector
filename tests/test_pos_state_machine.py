"""Small Hypothesis state machine for dangerous POS money identity.

This is a reliable in-memory model, not a giant formal Frappe model.
Real submit/cancel/stock still require the Frappe test site.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("hypothesis")

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule, run_state_machine_as_test
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class PosSaleMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.items: dict[str, int] = {}
        self.submitted = False
        self.captured_sen = 0
        self.refunded_sen = 0
        self.offline = False
        self.confirmed_ops: set[str] = set()
        self.effects = 0

    @initialize()
    def start(self) -> None:
        self.items = {}
        self.submitted = False
        self.captured_sen = 0
        self.refunded_sen = 0
        self.offline = False
        self.confirmed_ops = set()
        self.effects = 0

    @rule(item=st.sampled_from(["latte", "tea", "bun"]), qty=st.integers(min_value=1, max_value=5))
    def add_item(self, item: str, qty: int) -> None:
        if self.submitted:
            return
        self.items[item] = self.items.get(item, 0) + qty

    @rule(item=st.sampled_from(["latte", "tea", "bun"]))
    def remove_item(self, item: str) -> None:
        if self.submitted:
            return
        self.items.pop(item, None)

    @rule()
    def submit_sale(self) -> None:
        if not self.items or self.submitted:
            return
        self.submitted = True

    @rule(op_id=st.sampled_from(["pay-1", "pay-2"]), amount=st.integers(min_value=1, max_value=5000))
    def payment_success(self, op_id: str, amount: int) -> None:
        if not self.submitted:
            return
        if op_id in self.confirmed_ops:
            return
        self.confirmed_ops.add(op_id)
        self.captured_sen += amount
        self.effects += 1

    @rule(op_id=st.sampled_from(["pay-1", "pay-2"]))
    def duplicate_callback(self, op_id: str) -> None:
        if op_id in self.confirmed_ops:
            return
        return

    @rule(amount=st.integers(min_value=1, max_value=5000))
    def partial_refund(self, amount: int) -> None:
        if self.captured_sen <= 0:
            return
        refund = min(amount, self.captured_sen - self.refunded_sen)
        self.refunded_sen += refund

    @rule()
    def full_refund(self) -> None:
        self.refunded_sen = self.captured_sen

    @rule()
    def go_offline(self) -> None:
        self.offline = True

    @rule()
    def reconnect(self) -> None:
        self.offline = False

    @rule()
    def restart_terminal(self) -> None:
        # Confirmed operations must survive restart.
        self.offline = False

    @invariant()
    def refunds_never_exceed_capture(self) -> None:
        assert self.refunded_sen <= self.captured_sen

    @invariant()
    def duplicate_ops_do_not_multiply_effects(self) -> None:
        assert self.effects <= len(self.confirmed_ops)


def test_pos_sale_state_machine() -> None:
    run_state_machine_as_test(PosSaleMachine, settings=settings(max_examples=40, stateful_step_count=20))
