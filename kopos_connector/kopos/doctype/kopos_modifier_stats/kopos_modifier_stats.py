# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class KoPOSModifierStats(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        date: DF.Date
        group_name: DF.Data | None
        modifier_group: DF.Link | None
        modifier_name: DF.Data | None
        modifier_option: DF.Link
        revenue: DF.Currency
        selection_count: DF.Int
    # end: auto-generated types

    pass
