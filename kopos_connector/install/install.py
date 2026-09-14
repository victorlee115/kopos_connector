# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt
# pyright: reportMissingImports=false

from importlib.resources import files

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from kopos_connector.kopos.install.fb_custom_fields import create_fb_custom_fields
from kopos_connector.patches.normalize_duplicate_device_api_users import (
    execute as normalize_duplicate_device_api_users,
)


KOPOS_DEVICE_API_ROLE = "KoPOS Device API"
JIJI_BUSINESS_MODULE_PROFILE = "JiJi Business"
CONFIGURATION_LIMIT = 500


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
        ensure_standard_pages()
        ensure_jiji_business_module_profile()
        ensure_kopos_custom_fields(skip_if_missing_doctypes=True)
        create_fb_custom_fields()
        ensure_sales_invoice_rate_precision()
        ensure_native_change_ledger_entries()
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
    ensure_standard_pages()
    ensure_jiji_business_module_profile()
    ensure_kopos_custom_fields(skip_if_missing_doctypes=False)
    create_fb_custom_fields()
    ensure_sales_invoice_rate_precision()
    ensure_native_change_ledger_entries()
    ensure_maybank_provider_device_id()
    ensure_operational_composite_indexes()


def ensure_sales_invoice_rate_precision() -> None:
    """Let native ERPNext retain fractional unit rates, not fractional totals.

    A sen-exact line amount divided by its quantity need not be a two-decimal
    rate. ERPNext rounds rates before multiplying, including on credit notes.
    Six rate decimals cover the POS's supported quantities (up to 100 per line)
    while the existing sen-equivalence checks remain the accounting authority.
    This is standard field metadata for all Sales Invoices, not a pricing hook
    or a rewrite of historical transactions. Amount/payment/tax precision and
    equal or higher site precision are deliberately left alone.
    """
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    for fieldname in ("rate", "net_rate", "base_rate", "base_net_rate"):
        if frappe.get_precision("Sales Invoice Item", fieldname) < 6:
            make_property_setter("Sales Invoice Item", fieldname, "precision", "6", "Int")


def ensure_native_change_ledger_entries() -> None:
    """Use native change entries instead of subtracting change per cash row.

    ERPNext's compact POS GL path subtracts invoice change from *each* matching
    payment row. The standard setting handles split cash correctly without
    merging source payment identities or maintaining custom GL calculations.
    This site-wide setting affects future retail invoices, not historical GL.
    """
    settings = frappe.get_single("POS Settings")
    if not settings.post_change_gl_entries:
        settings.post_change_gl_entries = 1
        settings.save(ignore_permissions=True)


def ensure_standard_pages() -> None:
    """Keep the director-facing standard pages present after upgrades.

    Frappe's page file sync is not guaranteed when a page is introduced in an
    already-installed app (for example, when an upgrade skips the initial
    module import).  These are durable navigation records, not a second page
    implementation, so creating them idempotently here keeps the routes
    available without asking directors to run a manual fixture import.
    """
    # Lightweight hook tests provide only the Frappe symbols needed for their
    # assertion and intentionally omit site configuration.  Real Frappe always
    # exposes ``conf``; skip page materialization in that reduced harness.
    if not hasattr(frappe, "conf"):
        return

    pages = (
        {
            "name": "jiji_home",
            "title": "JiJi Home",
            "roles": ("System Manager", "Company Director"),
        },
        {
            "name": "jiji_money",
            "title": "JiJi Money",
            "roles": ("System Manager", "Company Director"),
        },
        {
            "name": "jiji_supplies",
            "title": "JiJi Supplies",
            "roles": ("System Manager", "Company Director"),
        },
        {
            "name": "jiji_staff",
            "title": "JiJi Staff",
            "roles": ("System Manager", "Company Director"),
        },
        {
            "name": "jiji_promotions",
            "title": "Offers",
            "roles": ("System Manager", "Company Director"),
        },
        {
            "name": "jiji_menu_recipes",
            "title": "Menu",
            "roles": ("System Manager", "Company Director"),
        },
        {
            "name": "jiji_stock_autopilot",
            "title": "Stock",
            "roles": ("System Manager", "Company Director"),
        },
    )
    for page in pages:
        if frappe.db.exists("Page", page["name"]):
            if frappe.db.get_value("Page", page["name"], "title") != page["title"]:
                frappe.db.set_value(
                    "Page",
                    page["name"],
                    "title",
                    page["title"],
                    update_modified=False,
                )
            continue
        previous_developer_mode = getattr(frappe.conf, "developer_mode", 0)
        frappe.conf.developer_mode = 1
        try:
            frappe.get_doc(
                {
                    "doctype": "Page",
                    "name": page["name"],
                    "page_name": page["name"],
                    "title": page["title"],
                    "module": "KoPOS",
                    "standard": "Yes",
                    "roles": [{"role": role} for role in page["roles"]],
                }
            ).insert(ignore_permissions=True)
        finally:
            frappe.conf.developer_mode = previous_developer_mode


def ensure_jiji_business_module_profile() -> None:
    """Give ordinary directors the JiJi-only desk without weakening security.

    Module Profile is presentation only. Direct record permissions remain the
    security boundary. Users who also hold System Manager are deliberately not
    changed because support and business access must use separate accounts.
    """

    if not frappe.db.exists("DocType", "Module Profile"):
        return
    module_names = [
        str(value)
        for value in frappe.get_all(
            "Module Def",
            pluck="name",
            order_by="name asc",
            limit_page_length=CONFIGURATION_LIMIT + 1,
        )
    ]
    if len(module_names) > CONFIGURATION_LIMIT:
        frappe.throw(_("JiJi could not safely simplify the desk navigation"))
    blocked = [name for name in module_names if name != "KoPOS"]

    profile = (
        frappe.get_doc("Module Profile", JIJI_BUSINESS_MODULE_PROFILE)
        if frappe.db.exists("Module Profile", JIJI_BUSINESS_MODULE_PROFILE)
        else frappe.get_doc(
            {
                "doctype": "Module Profile",
                "module_profile_name": JIJI_BUSINESS_MODULE_PROFILE,
            }
        )
    )
    profile_meta = frappe.get_meta("Module Profile")
    block_field = profile_meta.get_field("block_modules")
    if not block_field or not getattr(block_field, "options", None):
        frappe.throw(_("JiJi could not safely simplify the desk navigation"))
    child_meta = frappe.get_meta(block_field.options)
    module_field = next(
        (
            field.fieldname
            for field in child_meta.fields
            if field.fieldtype == "Link" and field.options == "Module Def"
        ),
        "module",
    )
    desired_modules = set(blocked)
    existing_modules = {
        str(
            row.get(module_field)
            if hasattr(row, "get")
            else getattr(row, module_field, "")
        ).strip()
        for row in (profile.get("block_modules") or [])
    }
    profile_is_new = getattr(profile, "is_new", lambda: True)()
    if profile_is_new or existing_modules != desired_modules:
        profile.set("block_modules", [{module_field: name} for name in blocked])
    if profile_is_new:
        profile.insert(ignore_permissions=True)
    elif existing_modules != desired_modules:
        profile.save(ignore_permissions=True)

    directors = set(
        frappe.get_all(
            "Has Role",
            filters={"parenttype": "User", "role": "Company Director"},
            pluck="parent",
            limit_page_length=CONFIGURATION_LIMIT + 1,
        )
    )
    system_managers = set(
        frappe.get_all(
            "Has Role",
            filters={"parenttype": "User", "role": "System Manager"},
            pluck="parent",
            limit_page_length=CONFIGURATION_LIMIT + 1,
        )
    )
    if len(directors) > CONFIGURATION_LIMIT or len(system_managers) > CONFIGURATION_LIMIT:
        frappe.throw(_("JiJi could not safely assign the business navigation"))
    # Frappe's built-in Administrator has every role by design; it is not an
    # ordinary director/support account that can be separated this way.
    directors.discard("Administrator")
    system_managers.discard("Administrator")
    mixed_accounts = sorted(directors & system_managers)
    if mixed_accounts:
        frappe.log_error(
            title="JiJi business and support accounts must be separated",
            message=", ".join(mixed_accounts),
        )
    for user in sorted(directors - system_managers):
        if frappe.db.exists("User", user):
            # Frappe's stock default-workspace redirect opens the legacy
            # workspace slug, which is not the JiJi page route on v16. The
            # role-aware home-page hook owns the landing route; clear an old
            # default so it cannot override that supported hook.
            frappe.db.set_value(
                "User",
                user,
                {
                    "module_profile": JIJI_BUSINESS_MODULE_PROFILE,
                    "default_workspace": None,
                },
                update_modified=False,
            )


OPERATIONAL_INDEX_SPECS = (
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
        "Manual QR Reconciliation",
        [
            "device_id",
            "claim_role",
            "finance_resolution_status",
            "status",
        ],
        "idx_kopos_manual_qr_device_finance_status",
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
        "FB Order",
        ["booth_warehouse", "company", "status", "stock_status", "sale_datetime"],
        "idx_kopos_fb_order_warehouse_stock_status",
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
        "FB Projection Log",
        ["projection_type", "state", "source_doctype", "last_attempt_at", "source_name"],
        "idx_kopos_projection_stock_completed",
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


def ensure_operational_composite_indexes() -> None:
    """Keep device-scoped mutation/reset gates constant-footprint at high volume."""
    for doctype, fields, index_name in OPERATIONAL_INDEX_SPECS:
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
                "hidden": 1,
                "read_only": 1,
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
                "hidden": 1,
                "read_only": 1,
                "description": "Enable stock-based availability checking in KoPOS",
            },
            {
                "fieldname": "custom_kopos_min_qty",
                "label": "KoPOS Min Qty",
                "fieldtype": "Float",
                "default": 1,
                "insert_after": "custom_kopos_track_stock",
                "hidden": 1,
                "read_only": 1,
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
            {
                "fieldname": "kopos_qr_payments_section",
                "fieldtype": "Section Break",
                "label": "KoPOS QR Payments",
                "insert_after": "custom_kopos_sst_rate",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_kopos_static_qr_enabled",
                "label": "Enable Static QR",
                "fieldtype": "Check",
                "default": 0,
                "insert_after": "kopos_qr_payments_section",
            },
            {
                "fieldname": "custom_kopos_static_qr_payload",
                "label": "Static QR Payload",
                "fieldtype": "Long Text",
                "read_only": 1,
                "insert_after": "custom_kopos_static_qr_enabled",
                "depends_on": "eval:doc.custom_kopos_static_qr_enabled==1",
            },
            {
                "fieldname": "custom_kopos_static_qr_payload_sha256",
                "label": "Static QR Payload SHA-256",
                "fieldtype": "Data",
                "read_only": 1,
                "hidden": 1,
                "insert_after": "custom_kopos_static_qr_payload",
            },
            {
                "fieldname": "custom_kopos_static_qr_merchant_id",
                "label": "Verified Merchant ID",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "custom_kopos_static_qr_payload_sha256",
            },
            {
                "fieldname": "custom_kopos_static_qr_acquirer_id",
                "label": "Verified Acquirer ID",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "custom_kopos_static_qr_merchant_id",
            },
            {
                "fieldname": "custom_kopos_static_qr_merchant_name",
                "label": "Verified Merchant Name",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "custom_kopos_static_qr_acquirer_id",
            },
            {
                "fieldname": "custom_kopos_static_qr_version",
                "label": "Static QR Version",
                "fieldtype": "Data",
                "read_only": 1,
                "hidden": 1,
                "insert_after": "custom_kopos_static_qr_merchant_name",
            },
            {
                "fieldname": "custom_kopos_static_qr_commissioned_at",
                "label": "Static QR Commissioned At",
                "fieldtype": "Datetime",
                "read_only": 1,
                "insert_after": "custom_kopos_static_qr_version",
            },
            {
                "fieldname": "custom_kopos_manual_qr_suspense_account",
                "label": "Manual QR Suspense Account",
                "fieldtype": "Link",
                "options": "Account",
                "insert_after": "custom_kopos_static_qr_commissioned_at",
            },
            {
                "fieldname": "custom_kopos_automatic_qr_enabled",
                "label": "Enable Automatic QR",
                "fieldtype": "Check",
                "default": 0,
                "insert_after": "custom_kopos_manual_qr_suspense_account",
            },
            {
                "fieldname": "custom_kopos_maybank_qrpaybiz_account",
                "label": "Maybank QRPayBiz Account",
                "fieldtype": "Link",
                "options": "Maybank QRPayBiz Account",
                "insert_after": "custom_kopos_automatic_qr_enabled",
                "depends_on": "eval:doc.custom_kopos_automatic_qr_enabled==1",
            },
            {
                "fieldname": "custom_kopos_maybank_outlet_id",
                "label": "Maybank Outlet ID",
                "fieldtype": "Data",
                "insert_after": "custom_kopos_maybank_qrpaybiz_account",
                "depends_on": "eval:doc.custom_kopos_automatic_qr_enabled==1",
            },
            {
                "fieldname": "custom_kopos_qr_clearing_account",
                "label": "QR Clearing Account",
                "fieldtype": "Link",
                "options": "Account",
                "insert_after": "custom_kopos_maybank_outlet_id",
                "description": "Company-wide DuitNow QR Mode of Payment account; changing it affects this company.",
            },
            {
                "fieldname": "custom_kopos_qr_settlement_bank_account",
                "label": "Settlement Bank Account",
                "fieldtype": "Link",
                "options": "Bank Account",
                "insert_after": "custom_kopos_qr_clearing_account",
            },
            {
                "fieldname": "custom_kopos_qr_configuration_status",
                "label": "QR Configuration Status",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "custom_kopos_qr_settlement_bank_account",
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
    if not frappe.db.exists("Role", "Company Director"):
        frappe.get_doc({"doctype": "Role", "role_name": "Company Director"}).insert(
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

    _ensure_client_script(script_name, "KoPOS Device", script_body)


def ensure_pos_profile_provisioning_script() -> None:
    script_name = "KoPOS POS Profile Provisioning Shortcut"
    script_body = files("kopos_connector").joinpath("public/js/pos_profile_setup.js").read_text(encoding="utf-8")
    _ensure_client_script(script_name, "POS Profile", script_body)
    frappe.db.commit()


def _ensure_client_script(script_name: str, doctype: str, script_body: str) -> None:
    """Keep native validation for changes without resaving unchanged scripts."""
    values = {"dt": doctype, "view": "Form", "enabled": 1, "script": script_body}
    existing_name = frappe.db.exists("Client Script", script_name)
    if existing_name:
        doc = frappe.get_doc("Client Script", existing_name)
        changed = False
        for field, value in values.items():
            if getattr(doc, field, None) != value:
                setattr(doc, field, value)
                changed = True
        if changed:
            doc.save(ignore_permissions=True)
    else:
        frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": script_name,
                **values,
            }
        ).insert(ignore_permissions=True)


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
