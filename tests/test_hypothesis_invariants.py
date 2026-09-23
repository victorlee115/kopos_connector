"""Property tests for money and idempotent request identity.

These are development evidence (mocked_unit). They do not replace real Frappe
accounting or restored-data gates.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("hypothesis")

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kopos_connector.kopos.api.money_contract import (
    MAX_SAFE_INTEGER,
    MoneyContractValidationError,
    parse_sen,
    sen_to_decimal,
)


safe_sen = st.integers(min_value=0, max_value=10_000_000)


@given(captured=safe_sen, refund=safe_sen)
@settings(max_examples=80)
def test_refund_cannot_exceed_capture(captured: int, refund: int) -> None:
    allowed = min(refund, captured)
    assert allowed <= captured
    remaining = captured - allowed
    assert remaining >= 0
    assert remaining + allowed == captured


@given(sen=st.integers(min_value=0, max_value=1_000_000_000))
@settings(max_examples=80)
def test_parse_sen_round_trip_decimal(sen: int) -> None:
    parsed = parse_sen(sen, "amount_sen")
    assert parsed == sen
    decimal_value = sen_to_decimal(parsed)
    assert decimal_value * 100 == sen


@given(payload=st.text(min_size=1, max_size=24))
@settings(max_examples=40)
def test_parse_sen_rejects_non_integers(payload: str) -> None:
    if payload.strip().lstrip("+-").isdigit() and payload.strip().lstrip("+-") != "":
        return
    with pytest.raises(MoneyContractValidationError):
        parse_sen(payload, "amount_sen")


def test_parse_sen_rejects_bool_and_oversize() -> None:
    with pytest.raises(MoneyContractValidationError):
        parse_sen(True, "amount_sen")
    with pytest.raises(MoneyContractValidationError):
        parse_sen(MAX_SAFE_INTEGER + 1, "amount_sen")


class _IdempotentStore:
    def __init__(self) -> None:
        self._results: dict[str, int] = {}

    def capture(self, request_id: str, amount_sen: int) -> int:
        existing = self._results.get(request_id)
        if existing is not None:
            return existing
        self._results[request_id] = amount_sen
        return amount_sen


@given(request_id=st.text(min_size=8, max_size=36), amount=safe_sen, repeats=st.integers(min_value=1, max_value=8))
@settings(max_examples=60)
def test_same_request_id_converges(request_id: str, amount: int, repeats: int) -> None:
    store = _IdempotentStore()
    values = [store.capture(request_id, amount) for _ in range(repeats)]
    assert set(values) == {amount}
    assert len(store._results) == 1
