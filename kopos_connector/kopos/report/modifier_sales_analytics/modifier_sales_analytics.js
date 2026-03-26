// Copyright (c) 2026, KoPOS and contributors
// For license information, please see license.txt

frappe.query_reports["Modifier Sales Analytics"] = {
    filters: [
        {
            fieldname: "from_date",
            label: __("From Date"),
            fieldtype: "Date",
            default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
            reqd: 1
        },
        {
            fieldname: "to_date",
            label: __("To Date"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
            reqd: 1
        },
        {
            fieldname: "modifier_group",
            label: __("Modifier Group"),
            fieldtype: "Link",
            options: "KoPOS Modifier Group"
        }
    ],

    onload: function(report) {
        report.page.add_inner_button(__("Refresh Stats"), function() {
            frappe.confirm(
                __("Refresh modifier stats for this date range?"),
                function() {
                    frappe.call({
                        method: "kopos_connector.api.modifiers.aggregate_modifier_stats_range",
                        args: {
                            from_date: frappe.query_report.get_filter_value("from_date"),
                            to_date: frappe.query_report.get_filter_value("to_date")
                        },
                        callback: function(r) {
                            if (r.message) {
                                frappe.msgprint(__("Stats refreshed: {0} records processed", [r.message]));
                                report.refresh();
                            }
                        }
                    });
                }
            );
        });
    }
};
