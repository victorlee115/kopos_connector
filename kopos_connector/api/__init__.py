from __future__ import annotations

from typing import Any


def get_catalog(*args: Any, **kwargs: Any):
    from kopos.api import get_catalog as impl

    return impl(*args, **kwargs)


def get_tax_rate(*args: Any, **kwargs: Any):
    from kopos.api import get_tax_rate as impl

    return impl(*args, **kwargs)


def get_item_modifiers(*args: Any, **kwargs: Any):
    from kopos.api import get_item_modifiers as impl

    return impl(*args, **kwargs)


def get_refund_reasons(*args: Any, **kwargs: Any):
    from kopos.api import get_refund_reasons as impl

    return impl(*args, **kwargs)


def submit_order(*args: Any, **kwargs: Any):
    from kopos.api import submit_order as impl

    return impl(*args, **kwargs)


def process_refund(*args: Any, **kwargs: Any):
    from kopos.api import process_refund as impl

    return impl(*args, **kwargs)


__all__ = [
    "get_catalog",
    "get_item_modifiers",
    "get_refund_reasons",
    "get_tax_rate",
    "process_refund",
    "submit_order",
]
