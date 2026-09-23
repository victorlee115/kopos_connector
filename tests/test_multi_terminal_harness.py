from __future__ import annotations

from tests.harness.multi_terminal import StockBook, Terminal


def test_last_unit_concurrent_sale_rejects_second_terminal() -> None:
    book = StockBook(qty=1, policy="reject_negative")
    terminal_a = Terminal("A", book)
    terminal_b = Terminal("B", book)
    assert terminal_a.sell("A-sale-1") is True
    assert terminal_b.sell("B-sale-1") is False
    assert book.qty == 0


def test_duplicate_payment_event_is_idempotent() -> None:
    book = StockBook(qty=5)
    terminal = Terminal("A", book)
    assert terminal.pay("pay-1") is True
    assert terminal.pay("pay-1") is True
    assert len(terminal.seen_ops) == 1


def test_offline_replay_converges() -> None:
    book = StockBook(qty=3)
    terminal = Terminal("A", book)
    terminal.go_offline()
    assert terminal.sell("sale-offline") is True
    terminal.restart()
    terminal.sync()
    assert terminal.sell("sale-offline") is True
    assert terminal.sales == 1
    assert book.qty == 2
