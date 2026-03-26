from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from types import ModuleType, SimpleNamespace


def _raise(error):
    raise error


def install_fake_frappe_modules() -> None:
    frappe_module = sys.modules.get("frappe")
    utils_module = sys.modules.get("frappe.utils")
    password_module = sys.modules.get("frappe.utils.password")
    twofactor_module = sys.modules.get("frappe.twofactor")
    frappe_custom_module = sys.modules.get("frappe.custom")
    frappe_custom_doctype_module = sys.modules.get("frappe.custom.doctype")
    frappe_custom_field_module = sys.modules.get("frappe.custom.doctype.custom_field")
    frappe_custom_field_custom_field_module = sys.modules.get(
        "frappe.custom.doctype.custom_field.custom_field"
    )
    frappe_model_module = sys.modules.get("frappe.model")
    frappe_model_document_module = sys.modules.get("frappe.model.document")

    if frappe_module is None:
        frappe_module = ModuleType("frappe")
        sys.modules["frappe"] = frappe_module
    if utils_module is None:
        utils_module = ModuleType("frappe.utils")
        sys.modules["frappe.utils"] = utils_module
    if password_module is None:
        password_module = ModuleType("frappe.utils.password")
        sys.modules["frappe.utils.password"] = password_module
    if twofactor_module is None:
        twofactor_module = ModuleType("frappe.twofactor")
        sys.modules["frappe.twofactor"] = twofactor_module
    if frappe_custom_module is None:
        frappe_custom_module = ModuleType("frappe.custom")
        sys.modules["frappe.custom"] = frappe_custom_module
    if frappe_custom_doctype_module is None:
        frappe_custom_doctype_module = ModuleType("frappe.custom.doctype")
        sys.modules["frappe.custom.doctype"] = frappe_custom_doctype_module
    if frappe_custom_field_module is None:
        frappe_custom_field_module = ModuleType("frappe.custom.doctype.custom_field")
        sys.modules["frappe.custom.doctype.custom_field"] = frappe_custom_field_module
    if frappe_custom_field_custom_field_module is None:
        frappe_custom_field_custom_field_module = ModuleType(
            "frappe.custom.doctype.custom_field.custom_field"
        )
        sys.modules["frappe.custom.doctype.custom_field.custom_field"] = (
            frappe_custom_field_custom_field_module
        )
    if frappe_model_module is None:
        frappe_model_module = ModuleType("frappe.model")
        sys.modules["frappe.model"] = frappe_model_module
    if frappe_model_document_module is None:
        frappe_model_document_module = ModuleType("frappe.model.document")
        sys.modules["frappe.model.document"] = frappe_model_document_module

    class ValidationError(Exception):
        pass

    def cstr(value):
        return "" if value is None else str(value)

    def cint(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def flt(value, precision=None):
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        if precision is not None:
            return round(result, int(precision))
        return result

    def add_to_date(value=None, **kwargs):
        base = value or datetime(2026, 3, 13, 18, 5, 0)
        if isinstance(base, str):
            base = datetime.fromisoformat(base)
        return base + timedelta(**kwargs)

    def add_days(value, days):
        base = value
        if isinstance(base, str):
            base = date.fromisoformat(base[:10])
        elif isinstance(base, datetime):
            base = base.date()
        return base + timedelta(days=days)

    def getdate(value=None):
        if value is None:
            return date.today()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])

    def today_str():
        return "2026-03-13"

    setattr(utils_module, "cstr", cstr)
    setattr(utils_module, "cint", cint)
    setattr(utils_module, "flt", flt)
    setattr(utils_module, "add_to_date", add_to_date)
    setattr(utils_module, "add_days", add_days)
    setattr(utils_module, "getdate", getdate)
    setattr(utils_module, "today", today_str)
    setattr(
        utils_module,
        "get_datetime",
        lambda value=None: datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str) and value
        else (value or datetime(2026, 3, 13, 18, 5, 0)),
    )
    setattr(utils_module, "now_datetime", lambda: datetime(2026, 3, 13, 18, 5, 0))
    setattr(utils_module, "nowdate", lambda: "2026-03-13")
    setattr(utils_module, "get_url", lambda: "https://erp.example.com")

    class PermissionError(Exception):
        pass

    setattr(frappe_module, "_", lambda value: value)
    setattr(frappe_module, "ValidationError", ValidationError)
    setattr(frappe_module, "PermissionError", PermissionError)
    setattr(frappe_module, "whitelist", lambda *args, **kwargs: (lambda fn: fn))
    setattr(frappe_module, "sendmail", lambda *args, **kwargs: None)
    setattr(
        frappe_module,
        "throw",
        lambda message, exc=None: _raise((exc or ValidationError)(message)),
    )
    setattr(frappe_module, "parse_json", lambda value: json.loads(value))
    setattr(frappe_module, "session", SimpleNamespace(user="Administrator"))
    setattr(
        frappe_module,
        "local",
        SimpleNamespace(request=SimpleNamespace(path="/api/method/ping")),
    )
    setattr(frappe_module, "flags", SimpleNamespace())
    setattr(
        frappe_module,
        "db",
        SimpleNamespace(
            get_value=lambda *args, **kwargs: None,
            get_all=lambda *args, **kwargs: [],
            exists=lambda *args, **kwargs: False,
            has_column=lambda *args, **kwargs: False,
            get_single_value=lambda *args, **kwargs: "Asia/Kuala_Lumpur",
            set_value=lambda *args, **kwargs: None,
            sql=lambda *args, **kwargs: [],
            delete=lambda *args, **kwargs: None,
            commit=lambda: None,
            rollback=lambda: None,
            escape=lambda x: x,
        ),
    )
    setattr(
        frappe_module,
        "cache",
        lambda: SimpleNamespace(
            set_value=lambda *args, **kwargs: None,
            get_value=lambda *args, **kwargs: None,
            delete_value=lambda *args, **kwargs: None,
        ),
    )
    setattr(frappe_module, "generate_hash", lambda length=32: "token-123")
    setattr(frappe_module, "get_cached_doc", lambda *args, **kwargs: SimpleNamespace())
    setattr(frappe_module, "get_doc", lambda *args, **kwargs: SimpleNamespace())
    setattr(frappe_module, "new_doc", lambda *args, **kwargs: SimpleNamespace())
    setattr(frappe_module, "get_all", lambda *args, **kwargs: [])
    setattr(
        frappe_module,
        "logger",
        lambda *args, **kwargs: SimpleNamespace(info=lambda *a, **k: None),
    )
    setattr(
        frappe_module,
        "log_error",
        lambda *args, **kwargs: None,
    )
    setattr(frappe_module, "has_permission", lambda *args, **kwargs: True)
    setattr(frappe_module, "has_role", lambda *args, **kwargs: True)
    setattr(
        frappe_module,
        "qb",
        SimpleNamespace(
            from_=lambda *args, **kwargs: SimpleNamespace(
                inner_join=lambda *args, **kwargs: SimpleNamespace(
                    on=lambda *args, **kwargs: SimpleNamespace(
                        select=lambda *args, **kwargs: SimpleNamespace(
                            where=lambda *args, **kwargs: SimpleNamespace(
                                groupby=lambda *args, **kwargs: SimpleNamespace(
                                    run=lambda *a, **k: []
                                )
                            )
                        )
                    )
                )
            ),
            functions=SimpleNamespace(
                Count=lambda *a, **k: None,
                Sum=lambda *a, **k: None,
            ),
        ),
    )
    setattr(
        frappe_module,
        "defaults",
        SimpleNamespace(get_user_default=lambda *args, **kwargs: None),
    )
    setattr(
        frappe_module,
        "get_roles",
        lambda user=None: ["System Manager"] if user == "Administrator" else [],
    )
    setattr(frappe_module, "utils", utils_module)

    setattr(twofactor_module, "get_qr_svg_code", lambda value: b"svg-data")
    setattr(password_module, "get_decrypted_password", lambda *args, **kwargs: None)
    setattr(password_module, "set_encrypted_password", lambda *args, **kwargs: None)
    setattr(
        frappe_custom_field_custom_field_module,
        "create_custom_fields",
        lambda *args, **kwargs: None,
    )

    class Document:
        pass

    setattr(frappe_model_document_module, "Document", Document)
