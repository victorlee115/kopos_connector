"""In-memory multi-terminal harness.

Prefer the real Frappe test environment for stock/accounting proof. This
scaffold exists so concurrent-terminal scenarios can be expressed as tests
before a Bench is available.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StockBook:
    qty: int
    policy: str = "reject_negative"

    def take(self, amount: int = 1) -> bool:
        if amount <= 0:
            raise ValueError("amount must be positive")
        if self.qty >= amount:
            self.qty -= amount
            return True
        if self.policy == "allow_negative":
            self.qty -= amount
            return True
        return False


@dataclass
class Terminal:
    name: str
    book: StockBook
    online: bool = True
    seen_ops: set[str] = field(default_factory=set)
    sales: int = 0

    def go_offline(self) -> None:
        self.online = False

    def go_online(self) -> None:
        self.online = True

    def restart(self) -> None:
        self.online = True

    def sell(self, op_id: str, qty: int = 1) -> bool:
        if op_id in self.seen_ops:
            return True
        if not self.book.take(qty):
            return False
        self.seen_ops.add(op_id)
        self.sales += 1
        return True

    def pay(self, op_id: str) -> bool:
        if op_id in self.seen_ops:
            return True
        self.seen_ops.add(op_id)
        return True

    def refund(self, op_id: str) -> bool:
        if op_id in self.seen_ops:
            return True
        self.seen_ops.add(op_id)
        return True

    def return_item(self, qty: int = 1) -> None:
        self.book.qty += qty

    def sync(self) -> None:
        self.online = True
