import frappe


def execute():
    script_name = "KoPOS POS Invoice Modifier Display"
    if frappe.db.exists("Client Script", script_name):
        frappe.delete_doc("Client Script", script_name, ignore_permissions=True)
        frappe.db.commit()
