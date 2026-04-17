# Copyright (c) 2026, KoPOS
# For license information, please see license.txt

app_name = "kopos_connector"
app_title = "KoPOS Connector"
app_publisher = "KoPOS"
app_description = "ERPNext connector for KoPOS mobile POS system with modifier and availability management"
app_icon = "octicon octicon-device-mobile"
app_color = "#F59E0B"
app_email = "support@kopos.my"
app_license = "GNU GPLv3"

# Required Frappe and ERPNext versions
# ------------------------------------
requires_frappe_version = ">=16.0.0,<17.0.0"
requires_erpnext_version = ">=16.0.0,<17.0.0"

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/kopos_connector/css/kopos_connector.css"
# app_include_js = "/assets/kopos_connector/js/kopos_connector.js"

# include js, css files in header of web template
# web_include_css = "/assets/kopos_connector/css/kopos_connector.css"
# web_include_js = "/assets/kopos_connector/js/kopos_connector.js"

# include custom scss in every website theme
# website_theme_scss = "kopos_connector/public/scss/website_theme.scss"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# POS Profile setup shortcut is injected via Client Script during migrate/install.
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

doctype_js = {
    "POS Invoice": "public/js/pos_invoice.js",
}

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this type
# website_generators = ["Web Page"]

# Installation
# ------------

before_install = "kopos_connector.install.install.before_install"
before_migrate = "kopos_connector.install.install.before_migrate"
after_install = "kopos_connector.install.install.after_install"
after_migrate = "kopos_connector.install.install.after_migrate"

# Uninstallation
# --------------

before_uninstall = "kopos_connector.uninstall.before_uninstall"
# after_uninstall = "kopos_connector.uninstall.after_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "kopos_connector.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document event behavior is implemented directly in the DocType controllers.

# Scheduled Tasks
# ---------------

scheduler_events = {
    "daily": [
        "kopos_connector.api.modifiers.aggregate_modifier_stats",
    ],
    "all": [
        "kopos_connector.tasks.poll_maybank.poll_pending_maybank_transactions",
    ],
}

doc_events = {
    "FB Booth Refill Request": {
        "validate": "kopos_connector.api.fb_refill.validate_fb_refill_request",
        "on_submit": "kopos_connector.api.fb_refill.on_submit_fb_refill_request",
    },
    "FB Return Event": {
        "validate": "kopos_connector.api.fb_returns.validate_fb_return_event",
        "on_submit": "kopos_connector.api.fb_returns.on_submit_fb_return_event",
    },
    "FB Remake Event": {
        "validate": "kopos_connector.api.fb_remakes.validate_fb_remake_event",
        "on_submit": "kopos_connector.api.fb_remakes.on_submit_fb_remake_event",
    },
    "FB Waste Event": {
        "validate": "kopos_connector.api.fb_waste.validate_fb_waste_event",
        "on_submit": "kopos_connector.api.fb_waste.on_submit_fb_waste_event",
    },
}

# Testing
# -------

# before_tests = "kopos_connector.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "kopos_connector.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "kopos_connector.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# --------------
before_request = ["kopos_connector.auth.enforce_device_api_restrictions"]
# after_request = ["kopos_connector.utils.after_request"]

# Job Events
# ----------
# on_job_change = "kopos_connector.utils.on_job_change"

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"kopos_connector.auth.auth"
# ]
