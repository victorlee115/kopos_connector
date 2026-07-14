import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class FBProjectionLog(Document):
    """System-managed projection evidence.

    Projection services always use ``ignore_permissions=True`` at their named
    mutation boundaries. Desk/API document writes must not be able to forge or
    rewrite synchronization evidence, even for users who can inspect the log.
    """

    def before_insert(self) -> None:
        self._require_system_mutation()

    def before_save(self) -> None:
        self._require_system_mutation()

    def on_trash(self) -> None:
        self._require_system_mutation()

    def _require_system_mutation(self) -> None:
        flags = getattr(self, "flags", None)
        if isinstance(flags, dict):
            ignore_permissions = flags.get("ignore_permissions", 0)
        else:
            ignore_permissions = getattr(flags, "ignore_permissions", 0)
        if cint(ignore_permissions):
            return
        frappe.throw(
            _(
                "FB Projection Log is system-managed and cannot be changed directly; use the supported projection retry workflow"
            ),
            frappe.PermissionError,
        )
