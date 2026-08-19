# pyright: reportMissingImports=false

"""Return-to-stock reversal is not a supported capability.

JiJi does not restock a refund. `process_return_payload` rejects a refund that
declares an inventory evaluation, normalises any legacy `return_to_stock: true`
intent to false, and posts the commercial refund without touching stock; the
end-to-end refund test patches this module to fail if the cashier path ever
reaches it.

The previous implementation here derived per-unit component quantities with
float division::

    qty_per_unit = component_qty / total_qty
    return_qty = qty_per_unit * qty_returned

That is not sound authority for a Stock Ledger Entry, it had no production
caller, and it carried no reversal identity, so a replay could post the same
receipt twice. Rather than leave working-looking float stock maths one call away
from being wired up, the entry point fails closed.

If restocking is ever brought into scope it must be rebuilt to:

- compile quantities from the frozen ``stock_qty_decimal`` on the resolved sale,
  using ``Decimal`` throughout, exactly as the recipe compiler and guided
  transfers already do;
- carry a unique reversal identity (a ``custom_kopos_inventory_command_id`` on
  the Stock Entry) so a retry adopts the existing document instead of posting a
  second receipt;
- be gated by an explicit director-approved policy, not inferred from a refund.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _


def create_reversal_stock_entry(fb_return_event: Any) -> str | None:
    """Refuse to reverse stock for a refund.

    Kept as the module's public entry point so the commercial refund test can
    continue to assert that the cashier path never calls it.
    """

    frappe.throw(
        _(
            "Refunds do not return stock. Restocking a refund is not a supported "
            "JiJi capability; record the stock movement as an assigned inventory "
            "task instead."
        ),
        frappe.ValidationError,
    )
    return None
