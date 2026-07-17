# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt
# pyright: reportMissingImports=false

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from kopos_connector.kopos.install.fb_custom_fields import create_fb_custom_fields
from kopos_connector.patches.normalize_duplicate_device_api_users import (
    execute as normalize_duplicate_device_api_users,
)


KOPOS_DEVICE_API_ROLE = "KoPOS Device API"


def before_install():
    """
    Pre-installation checks
    Called before app installation
    """
    # Check if ERPNext is installed
    installed_apps = frappe.get_installed_apps()

    if "erpnext" not in installed_apps:
        frappe.throw(
            _("ERPNext must be installed before installing KoPOS Connector"),
            title=_("Missing Dependency"),
        )

    # Check Frappe version
    frappe_version = get_major_version(frappe.__version__)
    if frappe_version < 16:
        frappe.throw(
            _("KoPOS Connector requires Frappe v16 or newer"),
            title=_("Version Mismatch"),
        )

    frappe.logger().info("KoPOS Connector: Pre-installation checks passed")
    return True


def before_migrate():
    """Fail closed when a migration would strand devices by changing API users."""
    normalize_duplicate_device_api_users()


def after_install():
    """
    Post-installation hook to create custom fields
    Called automatically after app installation
    """
    try:
        ensure_kopos_module_defs()
        ensure_kopos_custom_fields(skip_if_missing_doctypes=True)
        create_fb_custom_fields()
        ensure_maybank_provider_device_id()
        ensure_operational_composite_indexes()
    except Exception as e:
        frappe.log_error(
            title="KoPOS Connector: Post-install setup failed",
            message=frappe.get_traceback(),
        )
        frappe.throw(_("KoPOS Connector post-install setup failed: {0}").format(str(e)))


def after_migrate():
    """Ensure active KoPOS custom fields exist after DocTypes are synced."""
    ensure_kopos_module_defs()
    ensure_kopos_custom_fields(skip_if_missing_doctypes=False)
    create_fb_custom_fields()
    ensure_maybank_provider_device_id()
    ensure_operational_composite_indexes()


def ensure_operational_composite_indexes() -> None:
    """Keep device-scoped mutation/reset gates constant-footprint at high volume."""
    index_specs = (
        (
            "KoPOS Device Safe Reset",
            ["device_id", "status"],
            "idx_kopos_safe_reset_device_status",
        ),
        (
            "Maybank QR Transaction",
            ["device_id", "status"],
            "idx_kopos_maybank_device_status",
        ),
        (
            "Maybank QR Transaction",
            ["device_id", "manual_reconciliation_status"],
            "idx_kopos_maybank_device_reconciliation",
        ),
        (
            "Manual QR Reconciliation",
            ["device_id", "status"],
            "idx_kopos_manual_qr_device_status",
        ),
        (
            "FB Order",
            ["shift", "status"],
            "idx_kopos_fb_order_shift_status",
        ),
        (
            "FB Order",
            ["shift", "docstatus", "automatic_qr_state"],
            "idx_kopos_fb_order_shift_qr_state",
        ),
        (
            "FB Order",
            ["device_id", "docstatus", "automatic_qr_state"],
            "idx_kopos_fb_order_device_qr_state",
        ),
        (
            "FB Projection Log",
            ["source_doctype", "source_name", "state", "projection_type"],
            "idx_kopos_projection_source_state",
        ),
        (
            "FB Projection Log",
            ["state", "next_retry_at", "lease_expires_at"],
            "idx_kopos_projection_retry_due",
        ),
        (
            "Maybank QR Transaction",
            ["status", "expires_at", "last_polled_at"],
            "idx_kopos_maybank_poll_due",
        ),
        (
            "Maybank QR Transaction",
            ["device_id", "created_at"],
            "idx_kopos_maybank_device_created",
        ),
        (
            "Maybank QR Transaction",
            ["fb_order", "fb_order_payment", "status", "maybank_status"],
            "idx_kopos_maybank_order_payment_status",
        ),
        (
            "Maybank QR Transaction",
            ["duplicate_payment_status", "paid_at"],
            "idx_kopos_maybank_duplicate_refund",
        ),
        (
            "Maybank QR Transaction",
            ["device_id", "duplicate_payment_status", "status"],
            "idx_kopos_maybank_device_duplicate_status",
        ),
        (
            "FB Resolved Sale",
            ["fb_order"],
            "idx_kopos_resolved_sale_order",
        ),
    )
    for doctype, fields, index_name in index_specs:
        if frappe.db.exists("DocType", doctype):
            frappe.db.add_index(doctype, fields, index_name=index_name)


def ensure_maybank_provider_device_id() -> str:
    """Create or migrate durable Maybank provider identity after schema sync."""
    from kopos_connector.services.maybank.client import (
        ensure_stable_device_id,
        ensure_stable_device_metadata,
    )

    device_id = ensure_stable_device_id()
    ensure_stable_device_metadata()
    return device_id


def ensure_kopos_module_defs() -> None:
    if frappe.db.exists("Module Def", "KoPOS"):
        return

    frappe.get_doc(
        {
            "doctype": "Module Def",
            "module_name": "KoPOS",
            "app_name": "kopos_connector",
        }
    ).insert(ignore_permissions=True)


def ensure_kopos_custom_fields(skip_if_missing_doctypes: bool) -> None:
    missing_doctypes = get_missing_kopos_doctypes()
    if missing_doctypes:
        message = "KoPOS Connector: Skipping custom field creation until DocTypes exist: {0}".format(
            ", ".join(missing_doctypes)
        )
        if skip_if_missing_doctypes:
            frappe.logger().warning(message)
            return
        frappe.throw(_(message))

    create_kopos_custom_fields()
    ensure_kopos_roles()
    ensure_kopos_client_scripts()
    frappe.logger().info("KoPOS Connector: Custom fields created successfully")


def create_kopos_custom_fields():
    """
    Create custom fields for active Item, POS Profile, and Sales Invoice flows.

    Legacy ERP point-of-sale document fields are intentionally not installed.
    Existing fields remain untouched for explicit migration tooling.
    """
    custom_fields = {
        "Item": [
            {
                "fieldname": "kopos_availability_section",
                "fieldtype": "Section Break",
                "label": "KoPOS Availability",
                "insert_after": "disabled",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_kopos_availability_mode",
                "label": "KoPOS Availability Mode",
                "fieldtype": "Select",
                "options": "auto\nforce_available\nforce_unavailable",
                "default": "auto",
                "insert_after": "kopos_availability_section",
                "description": "Controls item availability in KoPOS:<br>"
                "• Auto - Use stock level (if tracking enabled)<br>"
                "• Force Available - Always show as available<br>"
                "• Force Unavailable - Always show as sold out",
            },
            {
                "fieldname": "custom_kopos_track_stock",
                "label": "KoPOS Track Stock",
                "fieldtype": "Check",
                "default": 0,
                "insert_after": "custom_kopos_availability_mode",
                "description": "Enable stock-based availability checking in KoPOS",
            },
            {
                "fieldname": "custom_kopos_min_qty",
                "label": "KoPOS Min Qty",
                "fieldtype": "Float",
                "default": 1,
                "insert_after": "custom_kopos_track_stock",
                "depends_on": "eval:doc.custom_kopos_track_stock==1",
                "description": "Minimum quantity required for item to be available",
            },
            {
                "fieldname": "custom_kopos_is_prep_item",
                "label": "KoPOS Prep Item",
                "fieldtype": "Check",
                "default": 0,
                "insert_after": "custom_kopos_min_qty",
                "description": "Print a cup sticker for this item after successful checkout",
            },
            {
                "fieldname": "kopos_modifiers_section",
                "fieldtype": "Section Break",
                "label": "KoPOS Modifiers",
                "insert_after": "custom_kopos_is_prep_item",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_kopos_modifier_groups",
                "label": "KoPOS Modifier Sets",
                "fieldtype": "Table",
                "options": "KoPOS Item Modifier Group",
                "insert_after": "kopos_modifiers_section",
                "description": "Link modifier groups to this item for customization options "
                "(size, milk type, add-ons, etc.)",
            },
        ],
        "POS Profile": [
            {
                "fieldname": "kopos_sst_section",
                "fieldtype": "Section Break",
                "label": "KoPOS SST Configuration",
                "insert_after": "warehouse",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_kopos_enable_sst",
                "label": "Enable SST",
                "fieldtype": "Check",
                "default": 1,
                "insert_after": "kopos_sst_section",
                "description": "Enable SST (Sales and Service Tax) for this POS profile",
            },
            {
                "fieldname": "custom_kopos_sst_rate",
                "label": "SST Rate (%)",
                "fieldtype": "Float",
                "default": 8,
                "insert_after": "custom_kopos_enable_sst",
                "depends_on": "eval:doc.custom_kopos_enable_sst==1",
                "description": "SST percentage rate (default: 8% for Malaysia)",
                "precision": 2,
            },
        ],
        "Sales Invoice": [
            {
                "fieldname": "custom_kopos_refund_idempotency_key",
                "label": "KoPOS Refund Idempotency Key",
                "fieldtype": "Data",
                "insert_after": "against_sales_invoice",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "unique": 1,
                "search_index": 1,
            },
            {
                "fieldname": "custom_kopos_device_id",
                "label": "KoPOS Device ID",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_refund_idempotency_key",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "search_index": 1,
            },
            {
                "fieldname": "custom_kopos_pricing_mode",
                "label": "KoPOS Pricing Mode",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_device_id",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_snapshot_version",
                "label": "KoPOS Promotion Snapshot Version",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_pricing_mode",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "search_index": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_snapshot_hash",
                "label": "KoPOS Promotion Snapshot Hash",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_promotion_snapshot_version",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "search_index": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_reconciliation_status",
                "label": "KoPOS Promotion Reconciliation Status",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_promotion_snapshot_hash",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_payload",
                "label": "KoPOS Promotion Payload",
                "fieldtype": "Long Text",
                "insert_after": "custom_kopos_promotion_reconciliation_status",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
        ],
        "Sales Invoice Item": [
            {
                "fieldname": "custom_kopos_modifiers",
                "label": "KoPOS Modifiers JSON",
                "fieldtype": "Long Text",
                "insert_after": "pricing_rules",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 0,
            },
            {
                "fieldname": "custom_kopos_modifier_total",
                "label": "KoPOS Modifier Total",
                "fieldtype": "Currency",
                "insert_after": "custom_kopos_modifiers",
                "read_only": 1,
                "precision": "2",
            },
            {
                "fieldname": "custom_kopos_has_modifiers",
                "label": "Has Modifiers",
                "fieldtype": "Check",
                "insert_after": "custom_kopos_modifier_total",
                "read_only": 1,
                "search_index": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_allocation",
                "label": "KoPOS Promotion Allocation",
                "fieldtype": "Long Text",
                "insert_after": "custom_kopos_has_modifiers",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
        ],
    }

    previous_in_patch = getattr(frappe.flags, "in_patch", False)
    previous_in_install = getattr(frappe.flags, "in_install", False)
    try:
        frappe.flags.in_patch = True
        frappe.flags.in_install = True
        create_custom_fields(custom_fields, update=True)
        frappe.db.commit()
    finally:
        frappe.flags.in_patch = previous_in_patch
        frappe.flags.in_install = previous_in_install


def ensure_kopos_client_scripts() -> None:
    ensure_kopos_roles()
    ensure_kopos_device_provisioning_script()
    ensure_pos_profile_provisioning_script()
    remove_legacy_modifier_group_parent_option_script()


def ensure_kopos_roles() -> None:
    if not frappe.db.exists("Role", KOPOS_DEVICE_API_ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": KOPOS_DEVICE_API_ROLE}).insert(
            ignore_permissions=True
        )


def ensure_kopos_device_provisioning_script() -> None:
    script_name = "KoPOS Device Provisioning Shortcut"
    script_body = """
const koposEscapeHtml = (value) => String(value || "")
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/\"/g, "&quot;")
  .replace(/'/g, "&#39;");

async function koposShowProvisioningQr(payload) {
  const preview = payload.setup_preview || {};
  const message = `
    <div style="text-align:center;padding:8px 0;">
      <img
        src="data:image/svg+xml;base64,${payload.provisioning_qr_svg}"
        alt="${koposEscapeHtml(__("KoPOS provisioning QR"))}"
        style="width:280px;height:280px;max-width:100%;border-radius:16px;border:1px solid var(--border-color);background:#fff;padding:12px;"
      />
      <div style="margin-top:16px;color:var(--text-muted);line-height:1.7;text-align:left;display:inline-block;">
        <div><strong>${koposEscapeHtml(__("Device"))}:</strong> ${koposEscapeHtml(preview.device || "-")}</div>
        <div><strong>${koposEscapeHtml(__("POS Profile"))}:</strong> ${koposEscapeHtml(preview.pos_profile || "-")}</div>
        <div><strong>${koposEscapeHtml(__("Provisioning User"))}:</strong> ${koposEscapeHtml(preview.provisioning_user || frappe.session.user || "-")}</div>
        <div><strong>${koposEscapeHtml(__("Expires At"))}:</strong> ${koposEscapeHtml(frappe.datetime.str_to_user(payload.expires_at))}</div>
      </div>
      <div style="margin-top:12px;font-size:12px;color:var(--text-muted);">${koposEscapeHtml(__("The one-time setup link is hidden after creation. Scan the QR or use Copy Link only while this dialog is open."))}</div>
    </div>
  `;

  frappe.msgprint({
    title: __("KoPOS Setup QR"),
    message,
    wide: true,
    primary_action: {
      label: __("Copy Link"),
      action: async () => {
        try {
          await navigator.clipboard.writeText(payload.provisioning_link);
          frappe.show_alert({ message: __("Provisioning link copied"), indicator: "green" });
        } catch (error) {
          frappe.msgprint(__("Copy failed. Scan the QR while this dialog is open."));
        }
      },
    },
  });
}

frappe.ui.form.on("KoPOS Device", {
  refresh(frm) {
    if (frm.is_new()) {
      return;
    }

    if (!(frappe.user_roles || []).includes("System Manager")) {
      return;
    }

    frm.add_custom_button(__("Generate KoPOS Setup QR"), async () => {
      frappe.dom.freeze(__("Generating KoPOS setup QR..."));
      try {
        const response = await frappe.call({
          method: "kopos_connector.api.create_device_provisioning_qr",
          args: {
            device: frm.doc.name,
            erpnext_url: window.location.origin,
          },
        });
        await koposShowProvisioningQr(response.message || response);
      } catch (error) {
        frappe.msgprint({
          title: __("Provisioning failed"),
          message: error?.message || __("Failed to generate provisioning QR"),
          indicator: "red",
        });
      } finally {
        frappe.dom.unfreeze();
      }
    }, __("KoPOS"));

    frm.add_custom_button(__("Advanced Provisioning"), () => {
      frappe.route_options = {
        device: frm.doc.name,
      };
      frappe.set_route("kopos_provisioning");
    }, __("KoPOS"));
  },
});
""".strip()

    existing_name = frappe.db.exists("Client Script", script_name)
    if existing_name:
        doc = frappe.get_doc("Client Script", existing_name)
        doc.dt = "KoPOS Device"
        doc.view = "Form"
        doc.enabled = 1
        doc.script = script_body
        doc.save(ignore_permissions=True)
    else:
        frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": script_name,
                "dt": "KoPOS Device",
                "view": "Form",
                "enabled": 1,
                "script": script_body,
            }
        ).insert(ignore_permissions=True)


def ensure_pos_profile_provisioning_script() -> None:
    script_name = "KoPOS POS Profile Provisioning Shortcut"
    script_body = """
frappe.ui.form.on(\"POS Profile\", {
  refresh(frm) {
    if (frm.is_new()) {
      return;
    }

    if (!(frappe.user_roles || []).includes(\"System Manager\")) {
      return;
    }

    frm.add_custom_button(__(\"Generate KoPOS Setup QR\"), () => {
      frappe.route_options = {
        pos_profile: frm.doc.name,
        company: frm.doc.company || undefined,
        warehouse: frm.doc.warehouse || undefined,
        currency: frm.doc.currency || undefined,
      };
      frappe.set_route(\"kopos_provisioning\");
    }, __(\"KoPOS\"));
  },
});
""".strip()

    existing_name = frappe.db.exists("Client Script", script_name)
    if existing_name:
        doc = frappe.get_doc("Client Script", existing_name)
        doc.dt = "POS Profile"
        doc.view = "Form"
        doc.enabled = 1
        doc.script = script_body
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": script_name,
                "dt": "POS Profile",
                "view": "Form",
                "enabled": 1,
                "script": script_body,
            }
        )
        doc.insert(ignore_permissions=True)

    frappe.db.commit()


def remove_legacy_modifier_group_parent_option_script() -> None:
    script_name = "KoPOS Modifier"
    script_name = f"{script_name} Group Parent Option Picker"
    existing_name = frappe.db.exists("Client Script", script_name)
    if not existing_name:
        return

    frappe.delete_doc("Client Script", existing_name, ignore_permissions=True)
    frappe.db.commit()


def get_missing_kopos_doctypes() -> list[str]:
    required_doctypes = [
        "KoPOS Item Modifier Group",
        "KoPOS Promotion",
        "KoPOS Promotion Item",
        "KoPOS Promotion Item Group",
        "KoPOS Promotion POS Profile",
        "KoPOS Promotion Snapshot",
    ]
    return [
        doctype
        for doctype in required_doctypes
        if not frappe.db.exists("DocType", doctype)
    ]


def get_major_version(version: str | None) -> int:
    if not version:
        return 0

    current = []
    for char in str(version):
        if char.isdigit():
            current.append(char)
            continue
        if current:
            break

    return int("".join(current) or 0)
