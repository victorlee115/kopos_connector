"""Exact partial-refund money for a paid sale (owner rule of 2026-09-15).

A refund returns each returned unit's share of what the customer actually
paid. Units on one sale line share its paid line total equally; the sale's tax
and cash rounding follow the refunded share of the paid line totals. A partial
refund takes the floor of every share, so leftover sen stay with the final
refund and all refunds of a sale add up to exactly its total, line totals, tax
and rounding. Cash rounded to 5 sen is refunded in 5-sen steps with the same
final-remainder rule. Promotions are never re-evaluated.

Shares are cumulative: a refund is the difference between the refunded
position after it and before it, so the result depends only on how many units
of each line have been refunded. This mirrors the tablet's
``kopos/src/services/sale/refund-allocation.ts``; both are pinned by the shared
``contracts/fixtures/refund-allocation.v1.json`` vectors. The module is pure:
callers load the frozen sale and prior refunds under their own locks.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, NoReturn

REFUND_ALLOCATION_VERSION = "refund-allocation-v1"
MAX_SAFE_INTEGER = 2**53 - 1


class RefundAllocationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RefundableSaleLine:
    line_id: str
    quantity: int
    line_total_sen: int


@dataclass(frozen=True)
class RefundableSale:
    lines: tuple[RefundableSaleLine, ...]
    tax_sen: int
    rounding_sen: int
    total_sen: int
    cash_unit_sen: int


@dataclass(frozen=True)
class RefundAllocationLine:
    line_id: str
    quantity: int
    net_sen: int


@dataclass(frozen=True)
class RefundAllocation:
    lines: tuple[RefundAllocationLine, ...]
    net_sen: int
    tax_sen: int
    rounding_sen: int
    amount_sen: int
    is_final: bool
    version: str = REFUND_ALLOCATION_VERSION

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "lines": [
                {"lineId": line.line_id, "quantity": line.quantity, "netSen": line.net_sen}
                for line in self.lines
            ],
            "netSen": self.net_sen,
            "taxSen": self.tax_sen,
            "roundingSen": self.rounding_sen,
            "amountSen": self.amount_sen,
            "isFinal": self.is_final,
        }


def _fail(code: str, message: str) -> NoReturn:
    raise RefundAllocationError(code, message)


def _safe_integer(value: Any) -> int | None:
    """Whole numbers as JSON sees them; booleans and fractions are not numbers here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        number = int(value)
    else:
        return None
    return number if -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER else None


def _validate_sale(sale: RefundableSale) -> tuple[RefundableSale, int]:
    """Return the sale with plain integer amounts, and its subtotal."""
    if isinstance(sale.lines, (str, bytes)) or not isinstance(sale.lines, Iterable) or not sale.lines:
        _fail("invalid_sale", "sale has no lines")
    seen: set[str] = set()
    subtotal = 0
    normalized: list[RefundableSaleLine] = []
    for line in sale.lines:
        line_id = line.line_id
        if not isinstance(line_id, str) or not line_id.strip() or line_id != line_id.strip():
            _fail("invalid_sale", "sale line identity must be a non-blank trimmed string")
        if line_id in seen:
            _fail("invalid_sale", f"sale line {line_id} appears twice")
        seen.add(line_id)
        quantity = _safe_integer(line.quantity)
        if quantity is None or quantity <= 0:
            _fail("invalid_sale", f"sale line {line_id} quantity must be a positive whole number")
        line_total = _safe_integer(line.line_total_sen)
        if line_total is None or line_total < 0:
            _fail("invalid_sale", f"sale line {line_id} total must be non-negative whole sen")
        subtotal += line_total
        normalized.append(RefundableSaleLine(line_id, quantity, line_total))
    tax = _safe_integer(sale.tax_sen)
    rounding = _safe_integer(sale.rounding_sen)
    total = _safe_integer(sale.total_sen)
    if tax is None or tax < 0 or rounding is None or total is None or total < 0:
        _fail("invalid_sale", "sale tax, rounding and total must be whole sen")
    if _safe_integer(sale.cash_unit_sen) not in (1, 5):
        _fail("invalid_sale", "cash unit must be 1 or 5 sen")
    if subtotal + tax + rounding != total:
        _fail("invalid_sale", "sale total must equal its line totals plus tax and rounding")
    if subtotal == 0 and (tax != 0 or rounding != 0):
        _fail("invalid_sale", "a sale without paid lines cannot carry tax or rounding")
    return RefundableSale(tuple(normalized), tax, rounding, total, int(sale.cash_unit_sen)), subtotal


def _units_by_line(
    sale: RefundableSale,
    quantities: Iterable[Mapping[str, Any]],
    label: str,
    *,
    allow_zero: bool,
) -> dict[str, int]:
    if isinstance(quantities, (str, bytes, Mapping)) or not isinstance(quantities, Iterable):
        _fail("invalid_quantity", f"{label} must be a list")
    lines = {line.line_id: line for line in sale.lines}
    units: dict[str, int] = {}
    for entry in quantities:
        line_id = entry.get("line_id") if isinstance(entry, Mapping) else None
        line = lines.get(line_id) if isinstance(line_id, str) else None
        if line is None:
            _fail("unknown_line", f"{label} names line {line_id}, which is not on this sale")
        if line_id in units:
            _fail("duplicate_line", f"{label} names line {line_id} twice")
        quantity = _safe_integer(entry.get("quantity"))
        if quantity is None or quantity < (0 if allow_zero else 1):
            kind = "non-negative" if allow_zero else "positive"
            _fail("invalid_quantity", f"{label} quantity for line {line_id} must be a {kind} whole number")
        if quantity > line.quantity:
            _fail("exceeds_remaining", f"{label} for line {line_id} is more than the {line.quantity} sold")
        units[line_id] = quantity
    return units


def _floor_share(amount: int, part: int, whole: int) -> int:
    # Python's floor division is a mathematical floor for negative rounding too,
    # so a partial refund never returns more than its exact share.
    return amount if part == whole else amount * part // whole


def _position(sale: RefundableSale, subtotal: int, units: Mapping[str, int]) -> tuple[dict[str, int], int, int, int, int]:
    line_net = {
        line.line_id: _floor_share(line.line_total_sen, units.get(line.line_id, 0), line.quantity)
        for line in sale.lines
    }
    net = sum(line_net.values())
    if subtotal == 0:
        return line_net, 0, 0, 0, 0
    tax = _floor_share(sale.tax_sen, net, subtotal)
    rounding = _floor_share(sale.rounding_sen, net, subtotal)
    amount = net + tax + rounding
    if sale.cash_unit_sen == 5 and net != subtotal:
        amount -= amount % 5
        rounding = amount - net - tax
    return line_net, net, tax, rounding, amount


def refund_position(sale: RefundableSale, refunded_units: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """The cumulative refunded money for a given number of refunded units per line."""
    sale, subtotal = _validate_sale(sale)
    _line_net, net, tax, rounding, amount = _position(
        sale, subtotal, _units_by_line(sale, refunded_units, "refunded units", allow_zero=True)
    )
    return {"net_sen": net, "tax_sen": tax, "rounding_sen": rounding, "amount_sen": amount}


def allocate_refund(
    sale: RefundableSale,
    refunded_before: Iterable[Mapping[str, Any]],
    request: Iterable[Mapping[str, Any]],
) -> RefundAllocation:
    sale, subtotal = _validate_sale(sale)
    before = _units_by_line(sale, refunded_before, "refunded units", allow_zero=True)
    requested = _units_by_line(sale, request, "refund request", allow_zero=False)
    if not requested:
        _fail("invalid_quantity", "refund request must name at least one line")
    lines = {line.line_id: line for line in sale.lines}
    after = dict(before)
    for line_id, quantity in requested.items():
        total = before.get(line_id, 0) + quantity
        if total > lines[line_id].quantity:
            remaining = lines[line_id].quantity - before.get(line_id, 0)
            _fail("exceeds_remaining", f"refund request for line {line_id} is more than the {remaining} still refundable")
        after[line_id] = total
    start_lines, start_net, start_tax, start_rounding, start_amount = _position(sale, subtotal, before)
    end_lines, end_net, end_tax, end_rounding, end_amount = _position(sale, subtotal, after)
    amount = end_amount - start_amount
    if amount <= 0:
        _fail("nothing_to_refund", "these items have no paid amount left to refund")
    return RefundAllocation(
        lines=tuple(
            RefundAllocationLine(
                line_id=line.line_id,
                quantity=requested[line.line_id],
                net_sen=end_lines[line.line_id] - start_lines[line.line_id],
            )
            for line in sale.lines
            if line.line_id in requested
        ),
        net_sen=end_net - start_net,
        tax_sen=end_tax - start_tax,
        rounding_sen=end_rounding - start_rounding,
        amount_sen=amount,
        is_final=end_amount == sale.total_sen,
    )


def parse_stored_allocation(value: Any) -> RefundAllocation | None:
    """Read an allocation persisted with its refund; None for a legacy full refund."""
    if value in (None, ""):
        return None
    data = json.loads(value) if isinstance(value, (str, bytes)) else value
    if not isinstance(data, Mapping) or data.get("version") != REFUND_ALLOCATION_VERSION:
        _fail("invalid_sale", "stored refund allocation has an unsupported version")
    lines = data.get("lines")
    numbers = [data.get(key) for key in ("netSen", "taxSen", "roundingSen", "amountSen")]
    if not isinstance(lines, list) or not lines or any(_safe_integer(number) is None for number in numbers) or not isinstance(data.get("isFinal"), bool):
        _fail("invalid_sale", "stored refund allocation is incomplete")
    parsed_lines = []
    for line in lines:
        quantity = _safe_integer(line.get("quantity")) if isinstance(line, Mapping) else None
        net = _safe_integer(line.get("netSen")) if isinstance(line, Mapping) else None
        line_id = line.get("lineId") if isinstance(line, Mapping) else None
        if not isinstance(line_id, str) or not line_id or quantity is None or quantity <= 0 or net is None or net < 0:
            _fail("invalid_sale", "stored refund allocation has an invalid line")
        parsed_lines.append(RefundAllocationLine(line_id, quantity, net))
    net_sen, tax_sen, rounding_sen, amount_sen = (_safe_integer(number) for number in numbers)
    if sum(line.net_sen for line in parsed_lines) != net_sen or net_sen + tax_sen + rounding_sen != amount_sen or amount_sen <= 0:
        _fail("invalid_sale", "stored refund allocation does not add up")
    return RefundAllocation(tuple(parsed_lines), net_sen, tax_sen, rounding_sen, amount_sen, data["isFinal"])


def serialize_allocation(allocation: RefundAllocation) -> str:
    return json.dumps(allocation.to_wire(), sort_keys=True, separators=(",", ":"))
