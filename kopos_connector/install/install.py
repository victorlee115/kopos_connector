# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


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


def after_install():
    """
    Post-installation hook to create custom fields
    Called automatically after app installation
    """
    try:
        ensure_kopos_custom_fields(skip_if_missing_doctypes=True)
    except Exception as e:
        frappe.log_error(
            title="KoPOS Connector: Failed to create custom fields",
            message=frappe.get_traceback(),
        )
        frappe.throw(_("Failed to create custom fields: {0}").format(str(e)))


def after_migrate():
    """Ensure KoPOS custom fields exist after DocTypes are synced."""
    ensure_kopos_custom_fields(skip_if_missing_doctypes=False)


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
    Create custom fields for Item and POS Profile DocTypes
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
                "fieldname": "kopos_modifier_groups",
                "label": "KoPOS Modifier Groups",
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
        "POS Invoice": [
            {
                "fieldname": "custom_kopos_idempotency_key",
                "label": "KoPOS Idempotency Key",
                "fieldtype": "Data",
                "insert_after": "remarks",
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
                "insert_after": "custom_kopos_idempotency_key",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_refund_reason_code",
                "label": "KoPOS Refund Reason Code",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_device_id",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_refund_reason",
                "label": "KoPOS Refund Reason",
                "fieldtype": "Small Text",
                "insert_after": "custom_kopos_refund_reason_code",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_snapshot_version",
                "label": "KoPOS Promotion Snapshot Version",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_refund_reason",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_pricing_mode",
                "label": "KoPOS Pricing Mode",
                "fieldtype": "Select",
                "options": "legacy_client\nmanual_only\nonline_snapshot\noffline_snapshot\nserver_validated",
                "insert_after": "custom_kopos_promotion_snapshot_version",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_reconciliation_status",
                "label": "KoPOS Promotion Reconciliation Status",
                "fieldtype": "Select",
                "options": "not_applicable\npending\nmatched\nreview_required",
                "insert_after": "custom_kopos_pricing_mode",
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
            {
                "fieldname": "custom_kopos_promotion_review_status",
                "label": "KoPOS Promotion Review Status",
                "fieldtype": "Select",
                "options": "not_required\npending_review\napproved_override\nrejected",
                "insert_after": "custom_kopos_promotion_payload",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_review_decision",
                "label": "KoPOS Promotion Review Decision",
                "fieldtype": "Select",
                "options": "\napproved_override\nrejected",
                "insert_after": "custom_kopos_promotion_review_status",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_reviewed_by",
                "label": "KoPOS Promotion Reviewed By",
                "fieldtype": "Link",
                "options": "User",
                "insert_after": "custom_kopos_promotion_review_decision",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_reviewed_at",
                "label": "KoPOS Promotion Reviewed At",
                "fieldtype": "Datetime",
                "insert_after": "custom_kopos_promotion_reviewed_by",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_promotion_review_notes",
                "label": "KoPOS Promotion Review Notes",
                "fieldtype": "Small Text",
                "insert_after": "custom_kopos_promotion_reviewed_at",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
        ],
        "POS Opening Entry": [
            {
                "fieldname": "custom_kopos_idempotency_key",
                "label": "KoPOS Idempotency Key",
                "fieldtype": "Data",
                "insert_after": "remarks",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "unique": 1,
                "search_index": 1,
            },
            {
                "fieldname": "custom_kopos_shift_id",
                "label": "KoPOS Shift ID",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_idempotency_key",
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
                "insert_after": "custom_kopos_shift_id",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "search_index": 1,
            },
        ],
        "POS Closing Entry": [
            {
                "fieldname": "custom_kopos_idempotency_key",
                "label": "KoPOS Idempotency Key",
                "fieldtype": "Data",
                "insert_after": "remarks",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "unique": 1,
                "search_index": 1,
            },
            {
                "fieldname": "custom_kopos_shift_id",
                "label": "KoPOS Shift ID",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_idempotency_key",
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
                "insert_after": "custom_kopos_shift_id",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "search_index": 1,
            },
        ],
        "POS Invoice Item": [
            {
                "fieldname": "custom_kopos_promotion_allocation",
                "label": "KoPOS Promotion Allocation",
                "fieldtype": "Long Text",
                "insert_after": "pricing_rules",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_kopos_modifiers",
                "label": "KoPOS Modifiers JSON",
                "fieldtype": "Long Text",
                "insert_after": "custom_kopos_promotion_allocation",
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
                "fieldname": "custom_kopos_modifiers_table",
                "label": "KoPOS Modifiers",
                "fieldtype": "Table",
                "options": "KoPOS Invoice Item Modifier",
                "insert_after": "custom_kopos_has_modifiers",
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
    ensure_pos_invoice_modifier_script()


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
      <div style="margin-top:12px;word-break:break-all;font-size:12px;color:var(--text-muted);"><code>${koposEscapeHtml(payload.provisioning_link)}</code></div>
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
          frappe.msgprint(payload.provisioning_link);
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


def ensure_pos_invoice_modifier_script() -> None:
    script_name = "KoPOS POS Invoice Modifier Display"
    script_body = """
/**
 * KoPOS Modifier Display for POS Invoice
 * Shows modifier badges and expandable details
 */

const koposEscapeHtml = (value) => String(value || "")
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;")
  .replace(/'/g, "&#39;");

frappe.ui.form.on("POS Invoice Item", {
    custom_kopos_has_modifiers: function(frm, cdt, cdn) {
        try {
            const row = frappe.get_doc(cdt, cdn);
            
            if (!row) {
                console.warn(`Row not found: ${cdt}/${cdn}`);
                return;
            }
            
            ModifierBadgeManager.toggle(frm, row);
            
        } catch (error) {
            frappe.show_alert({
                message: __("Error updating modifier display"),
                indicator: "red"
            }, 5);
            console.error("Modifier handler error:", error);
        }
    },
    
    items_remove: function(frm, cdt, cdn) {
        ModifierBadgeManager.cleanup(cdn);
    }
});


frappe.ui.form.on("POS Invoice", {
    refresh: function(frm) {
        if (frm.doc.docstatus === 1) {
            const modifierCount = frm.doc.items.reduce((sum, item) => 
                sum + (item.custom_kopos_has_modifiers || 0), 0);
            
            if (modifierCount > 0) {
                frm.add_custom_button(__("Modifier Summary"), () => {
                    show_modifier_summary(frm);
                }, __("View"));
            }
        }
        
        frm.doc.items.forEach(item => {
            if (item.custom_kopos_has_modifiers) {
                ModifierBadgeManager.show(frm, item);
            }
        });
    }
});


const ModifierBadgeManager = {
    _cache: new Map(),
    
    toggle: function(frm, row) {
        const hasModifiers = Boolean(row.custom_kopos_has_modifiers);
        const rowName = row.name;
        
        if (hasModifiers) {
            this._show(frm, row);
            this._cache.set(rowName, true);
        } else {
            this._hide(frm, row);
            this._cache.delete(rowName);
        }
    },
    
    show: function(frm, row) {
        this._show(frm, row);
    },
    
    _show: function(frm, row) {
        const $row = this._getRowElement(row.name);
        if (!$row) return;
        
        this._hide(frm, row);
        
        const badge = this._createBadge(row);
        $row.find(".col-name").append(badge);
    },
    
    _hide: function(frm, row) {
        const $row = this._getRowElement(row.name);
        if ($row) {
            $row.find(".modifier-badge").remove();
        }
    },
    
    _getRowElement: function(frm, rowName) {
        const $grid = frm.fields_dict.items?.grid;
        return $grid?.wrapper?.find(`[data-name="${koposEscapeHtml(rowName)}"]`);
    },
    
    _createBadge: function(frm, row) {
        const count = this._getModifierCount(row);
        return $(`
            <span class="modifier-badge label label-info" 
                  style="margin-left: 8px; cursor: pointer"
                  onclick="show_item_modifiers('${koposEscapeHtml(row.name)}')">
                <i class="fa fa-plus-circle"></i> ${count} ${__("modifiers")}
            </span>
        `);
    },

    _getModifierCount: function(row) {
        try {
            const snapshot = JSON.parse(row.custom_kopos_modifiers || "{}");
            return snapshot.count || snapshot.modifiers?.length || 0;
        } catch {
            return 0;
        }
    },

    cleanup: function(rowName) {
        this._cache.delete(rowName);
    }
};


window.show_item_modifiers = function(itemName) {
    const item = cur_frm.doc.items.find(i => i.name === itemName);
    if (!item) return;
    
    let snapshot = {};
    try {
        snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
    } catch (e) {
        console.warn("Invalid modifier JSON for item:", itemName);
        return;
    }
    
    const table = $(`
        <table class="table table-bordered">
            <thead>
                <tr>
                    <th>${__("Item")}</th>
                    <th>${__("Modifiers")}</th>
                    <th class="text-right">${__("Total")}</th>
                </tr>
            </thead>
            <tbody>
    `);
    
    snapshot.modifiers.forEach(mod => {
        table.find("tbody").append($(`
            <tr>
                <td>${koposEscapeHtml(mod.name) || "-"}</td>
                <td>${koposEscapeHtml(mod.group_name) || "-"}</td>
                <td class="text-right">${format_currency(mod.price || 0)}</td>
            </tr>
        `));
    });
    
    table.append("</tbody></table>");
    
    frappe.msgprint({
        title: __("Modifiers for {0}").format(item.item_name || item.item_code),
        message: table,
        wide: true
    });
};


function format_currency(amount) {
    return new Intl.NumberFormat(frappe.boot.user.lang || "en-MY", {
        style: "currency",
        currency: frappe.boot.currency || "MYR"
    }).format(amount);
}


function show_modifier_summary(frm) {
    const items = frm.doc.items.filter(i => i.custom_kopos_has_modifiers);
    
    const table = $(`
        <table class="table table-bordered">
            <thead>
                <tr>
                    <th>${__("Item")}</th>
                    <th>${__("Modifiers")}</th>
                    <th class="text-right">${__("Total")}</th>
                </tr>
            </thead>
            <tbody>
    `);
    
    items.forEach(item => {
        let snapshot = {};
        try {
            snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
        } catch {
            return;
        }
        
        snapshot.modifiers.forEach(mod => {
            table.find("tbody").append($(`
                <tr>
                    <td>${koposEscapeHtml(item.item_name)}</td>
                    <td>${koposEscapeHtml(mod.name)} (${koposEscapeHtml(mod.group_name)})</td>
                    <td class="text-right">${format_currency(mod.price || 0)}</td>
                </tr>
            `));
        });
    });
    
    table.append("</tbody></table>");
    
    const totalModifiers = items.reduce((sum, item) => {
        try {
            const snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
            return sum + (snapshot.total || 0);
        } catch {
            return sum;
        }
    }, 0);
    
    frappe.msgprint({
        title: __("Modifier Summary"),
        message: table,
        wide: true
    });
}
""".strip()

    existing_name = frappe.db.exists("Client Script", script_name)
    if existing_name:
        doc = frappe.get_doc("Client Script", existing_name)
        doc.dt = "POS Invoice"
        doc.view = "Form"
        doc.enabled = 1
        doc.script = script_body
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": script_name,
                "dt": "POS Invoice",
                "view": "Form",
                "enabled": 1,
                "script": script_body,
            }
        )
        doc.insert(ignore_permissions=True)

    frappe.db.commit()


def get_missing_kopos_doctypes() -> list[str]:
    required_doctypes = [
        "KoPOS Modifier Group",
        "KoPOS Modifier Option",
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
