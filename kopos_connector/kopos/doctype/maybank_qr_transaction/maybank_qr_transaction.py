import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class MaybankQRTransaction(Document):
    """Provider and accounting evidence that only named server services may mutate."""

    def onload(self) -> None:
        from kopos_connector.api.maybank_qr_simulation import (
            get_maybank_qr_simulation_capability,
        )

        self.set_onload(
            "maybank_qr_simulation",
            get_maybank_qr_simulation_capability(self),
        )

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
                "Maybank QR Transaction is server-controlled and cannot be changed directly; use the supported Maybank polling, reconciliation, refund, or test-simulation workflow"
            ),
            frappe.PermissionError,
        )
