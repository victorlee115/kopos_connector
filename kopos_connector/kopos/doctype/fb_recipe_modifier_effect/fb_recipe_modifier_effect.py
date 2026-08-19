from frappe.model.document import Document


class FBRecipeModifierEffect(Document):
    """Read-only snapshot row; values are materialized when a recipe is published."""
