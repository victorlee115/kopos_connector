from __future__ import annotations

from typing import Any

import frappe


def get_shift(name: str) -> Any:
    return frappe.get_doc("FB Shift", name)
