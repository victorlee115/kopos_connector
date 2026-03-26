# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class KoPOSInvoiceItemModifier(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        base_price: DF.Currency
        display_order: DF.Int
        is_default: DF.Check
        modifier_group: DF.Link | None
        modifier_group_name: DF.Data
        modifier_name: DF.Data
        modifier_option: DF.Link | None
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        price_adjustment: DF.Currency
    # end: auto-generated types

    pass
