"""Shared role boundary for outlet POS users and restored-data preflight."""

AUTHORIZED_DIRECTOR_ROLE = "Company Director"
TECHNICAL_ADMIN_ROLE = "System Manager"
DEVICE_API_ROLE = "KoPOS Device API"

LEGACY_MANAGER_ROLES = frozenset(
    {
        "Accounts Manager",
        "Item Manager",
        "KoPOS Manager",
        "Manufacturing Manager",
        "POS Manager",
        "Purchase Manager",
        "Sales Manager",
        "Stock Manager",
        "Warehouse Manager",
    }
)

SENSITIVE_BUSINESS_DOCTYPES = frozenset(
    {
        "Account",
        "Bin",
        "BOM",
        "FB Booth Refill Request",
        "FB Availability Hold",
        "FB Inventory Availability Rule",
        "FB Inventory Count Observation",
        "FB Inventory Count Task",
        "FB Inventory Count Task Line",
        "FB Inventory Exception",
        "FB Inventory Plan",
        "FB Inventory Plan Line",
        "FB Inventory Policy",
        "FB Order",
        "FB Projection Log",
        "FB Remake Event",
        "FB Recipe",
        "FB Recipe Modifier Effect",
        "FB Resolved Sale",
        "FB Return Event",
        "FB Shift",
        "FB Stock Override Log",
        "FB Waste Event",
        "GL Entry",
        "Item",
        "KoPOS Device",
        "Material Request",
        "Purchase Order",
        "Purchase Receipt",
        "Stock Entry",
        "Stock Ledger Entry",
        "Stock Reconciliation",
        "Supplier Quotation",
        "Warehouse",
    }
)

DEVICE_API_AUDITED_DOCTYPES = frozenset(
    {"KoPOS Promotion", "KoPOS Promotion Snapshot", "Maybank QR Transaction"}
)
