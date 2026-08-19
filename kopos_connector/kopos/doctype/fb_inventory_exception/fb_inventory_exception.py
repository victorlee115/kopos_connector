from __future__ import annotations

from frappe.model.document import Document


class FBInventoryException(Document):
    """Durable, human-actionable inventory exception record."""
