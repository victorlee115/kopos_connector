from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from importlib import import_module
from math import isfinite
from typing import Any

frappe = import_module("frappe")
BaseDocument = import_module("frappe.model.document").Document
frappe_utils = import_module("frappe.utils")

flt = frappe_utils.flt
now_datetime = frappe_utils.now_datetime
DocumentLike = Any

from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    parse_positive_integer_quantity,
    persisted_money_to_sen,
    sen_to_decimal,
)
from kopos_connector.kopos.doctype.fb_modifier_group.fb_modifier_group import (
    filter_visible_allowed_modifier_groups,
)
from kopos_connector.kopos.services.accounting.sales_invoice_service import (
    create_sales_invoice,
)
from kopos_connector.kopos.services.accounting.maybank_payment_service import (
    register_qr_payment_settlement,
)
from kopos_connector.kopos.services.inventory.stock_issue_service import (
    create_ingredient_stock_entry,
)
from kopos_connector.kopos.services.inventory.warning_service import (
    detect_stock_shortfall,
    log_stock_shortfall,
    require_advisory_shortfall_policy,
)
from kopos_connector.kopos.services.projection.log_service import (
    create_projection_log,
    update_projection_state,
)
from kopos_connector.kopos.services.recipe.modifier_bounds import (
    EffectiveModifierBounds,
    ModifierBoundsError,
    resolve_effective_modifier_bounds,
)


def cstr(value: Any) -> str:
    return str(frappe_utils.cstr(value))


def _optional_money_value(value: Any) -> Any:
    return 0 if value is None or value == "" else value


def _money_sen(value: Any, fieldname: str) -> int:
    try:
        return persisted_money_to_sen(value, fieldname)
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error


def _positive_integer_qty(value: Any, fieldname: str) -> int:
    try:
        return parse_positive_integer_quantity(value, fieldname)
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error


RESOLVED_COMPONENT_FIELDS = (
    "item",
    "source_type",
    "qty",
    "uom",
    "stock_qty",
    "stock_uom",
    "warehouse",
    "source_reference",
    "affects_stock",
    "affects_cogs",
    "remarks",
)


def _record_value(record: Any, fieldname: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(fieldname)
    return getattr(record, fieldname, None)


def _optional_text(value: Any) -> str | None:
    text = cstr(value).strip()
    return text or None


def _canonical_resolved_component(component: Any) -> dict[str, Any]:
    """Strip Frappe child-row metadata from one immutable recipe snapshot."""

    return {
        "item": cstr(_record_value(component, "item")).strip(),
        "source_type": cstr(_record_value(component, "source_type")).strip(),
        "qty": flt(_record_value(component, "qty")),
        "uom": cstr(_record_value(component, "uom")).strip(),
        "stock_qty": flt(_record_value(component, "stock_qty")),
        "stock_uom": _optional_text(_record_value(component, "stock_uom")),
        "warehouse": _optional_text(_record_value(component, "warehouse")),
        "source_reference": _optional_text(
            _record_value(component, "source_reference")
        ),
        "affects_stock": int(_record_value(component, "affects_stock") or 0),
        "affects_cogs": int(_record_value(component, "affects_cogs") or 0),
        "remarks": _optional_text(_record_value(component, "remarks")),
    }


def _canonical_resolved_components(components: list[Any]) -> list[dict[str, Any]]:
    return [_canonical_resolved_component(component) for component in components]


def _resolution_hash(
    *,
    recipe: str,
    recipe_version: Any,
    selected_modifiers: list[dict[str, Any]],
    resolved_components: list[dict[str, Any]],
) -> str:
    payload = {
        "recipe": recipe,
        "recipe_version": recipe_version,
        "selected_modifiers": selected_modifiers,
        "resolved_components": resolved_components,
    }
    serialized_payload = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()


PREPARED_SALE_IMMUTABLE_FIELDS = (
    "order_id",
    "display_number",
    "order_type",
    "catalog_version",
    "external_idempotency_key",
    "request_fingerprint",
    "accepted_sale_fingerprint",
    "source",
    "sale_datetime",
    "device_id",
    "shift",
    "staff_id",
    "event_project",
    "booth_warehouse",
    "company",
    "currency",
    "customer",
    "net_total",
    "tax_total",
    "tax_rate",
    "rounding_adjustment",
    "grand_total",
    "pricing_mode",
    "promotion_snapshot_version",
    "promotion_snapshot_hash",
    "promotion_reconciliation_status",
    "promotion_payload_json",
    "notes",
)

PREPARED_LINE_IMMUTABLE_FIELDS = (
    "line_id",
    "backend_line_uuid",
    "item",
    "item_name_snapshot",
    "qty",
    "uom",
    "unit_price",
    "modifier_total",
    "discount_amount",
    "line_total",
    "recipe",
    "recipe_version",
    "is_recipe_managed",
    "promotion_allocations_json",
    "remarks",
)

PREPARED_PAYMENT_IMMUTABLE_FIELDS = (
    "source_payment_id",
    "payment_method",
    "payment_channel_code",
    "amount",
    "tendered_amount",
    "change_amount",
)


PREPARED_SALE_MONEY_FIELDS = {
    "net_total",
    "tax_total",
    "rounding_adjustment",
    "grand_total",
    "unit_price",
    "modifier_total",
    "discount_amount",
    "line_total",
    "amount",
    "tendered_amount",
    "change_amount",
}


def _prepared_immutable_value(record: Any, fieldname: str) -> Any:
    value = _record_value(record, fieldname)
    if fieldname in PREPARED_SALE_MONEY_FIELDS:
        return _money_sen(_optional_money_value(value), fieldname)
    if fieldname == "qty":
        return _positive_integer_qty(value, "Prepared FB Order line qty")
    if fieldname == "is_recipe_managed":
        return int(value or 0)
    if fieldname == "tax_rate":
        # Frappe Float fields persist an omitted optional rate as zero. Treat
        # only those two representations as the same immutable business value;
        # rounding here could conceal a mutation before MariaDB DECIMAL storage.
        if value is None or value == "":
            return Decimal("0")
        try:
            tax_rate = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            frappe.throw(
                "Prepared Automatic QR tax_rate is invalid",
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw must raise") from error
        if not tax_rate.is_finite():
            frappe.throw(
                "Prepared Automatic QR tax_rate must be finite",
                frappe.ValidationError,
            )
        return Decimal("0") if tax_rate == 0 else tax_rate.normalize()
    if fieldname in {"promotion_payload_json", "promotion_allocations_json"}:
        text = cstr(value).strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            frappe.throw(
                f"Prepared Automatic QR {fieldname} is invalid JSON",
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw must raise")
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    if fieldname == "sale_datetime" and value not in (None, ""):
        return frappe_utils.get_datetime(value).isoformat()
    return cstr(value)


def _prepared_sale_immutable_snapshot(order: Any) -> dict[str, Any]:
    return {
        "order": {
            fieldname: _prepared_immutable_value(order, fieldname)
            for fieldname in PREPARED_SALE_IMMUTABLE_FIELDS
        },
        "items": [
            {
                fieldname: _prepared_immutable_value(line, fieldname)
                for fieldname in PREPARED_LINE_IMMUTABLE_FIELDS
            }
            for line in list(getattr(order, "items", None) or [])
        ],
        "payments": [
            {
                fieldname: _prepared_immutable_value(payment, fieldname)
                for fieldname in PREPARED_PAYMENT_IMMUTABLE_FIELDS
            }
            for payment in list(getattr(order, "payments", None) or [])
        ],
    }


def _prepared_sale_changed_paths(
    before: dict[str, Any],
    current: dict[str, Any],
) -> list[str]:
    """Return immutable field paths only, without exposing sale values."""

    changed_paths: list[str] = []
    for fieldname in PREPARED_SALE_IMMUTABLE_FIELDS:
        if before["order"].get(fieldname) != current["order"].get(fieldname):
            changed_paths.append(f"order.{fieldname}")

    for section, fieldnames in (
        ("items", PREPARED_LINE_IMMUTABLE_FIELDS),
        ("payments", PREPARED_PAYMENT_IMMUTABLE_FIELDS),
    ):
        before_rows = before[section]
        current_rows = current[section]
        if len(before_rows) != len(current_rows):
            changed_paths.append(f"{section}.length")
        for row_index, (before_row, current_row) in enumerate(
            zip(before_rows, current_rows),
            start=1,
        ):
            for fieldname in fieldnames:
                if before_row.get(fieldname) != current_row.get(fieldname):
                    changed_paths.append(f"{section}[{row_index}].{fieldname}")
    return changed_paths


def _is_static_qr_winner_transition(
    before: Any,
    current: Any,
    changed_paths: list[str],
) -> bool:
    """Allow only the audited Maybank-prepared -> static-winner transition.

    Payment channel is part of the accepted fingerprint so arbitrary edits stay
    forbidden.  The versioned confirmation service is the sole caller that sets
    the explicit winner marker and exact manual evidence before this save.
    """

    if changed_paths != ["payments[1].payment_channel_code"]:
        return False
    if cstr(getattr(before, "automatic_qr_winner_channel", None)).strip():
        return False
    if cstr(getattr(current, "automatic_qr_winner_channel", None)).strip() != (
        "static_qr"
    ):
        return False
    if cstr(getattr(current, "automatic_qr_state", None)).strip() != (
        "manual_pending_reconciliation"
    ):
        return False
    if cstr(getattr(before, "automatic_qr_state", None)).strip() not in {
        "prepared",
        "provider_pending",
        "provider_ambiguous",
        "provider_rejected",
        "provider_paid",
        "manual_pending_reconciliation",
    }:
        return False
    before_payments = list(getattr(before, "payments", None) or [])
    current_payments = list(getattr(current, "payments", None) or [])
    if len(before_payments) != 1 or len(current_payments) != 1:
        return False
    before_payment = before_payments[0]
    current_payment = current_payments[0]
    payment_name = cstr(getattr(current_payment, "name", None)).strip()
    if not payment_name or payment_name != cstr(
        getattr(current, "automatic_qr_payment", None)
    ).strip():
        return False
    if cstr(getattr(before_payment, "name", None)).strip() != payment_name:
        return False
    if cstr(getattr(before_payment, "payment_channel_code", None)).strip().lower() not in {
        "maybank",
        "maybank qr",
        "maybank-qr",
        "maybank_qr",
    }:
        return False
    if cstr(getattr(current_payment, "payment_channel_code", None)).strip() != (
        "static_qr"
    ):
        return False
    return bool(
        int(getattr(current_payment, "is_manual_confirmation", 0) or 0)
        and cstr(
            getattr(current_payment, "manual_confirmation_evidence_json", None)
        ).strip()
        and cstr(
            getattr(current_payment, "reconciliation_idempotency_key", None)
        ).strip()
        and cstr(getattr(current_payment, "external_transaction_id", None))
        .strip()
        .startswith("static-")
    )


class FBOrder(BaseDocument):
    def get_selected_modifier_rows(self, line) -> list[Any]:
        persisted_rows = list(line.get("selected_modifiers") or [])
        if persisted_rows:
            return persisted_rows

        transient_rows = getattr(line, "_selected_modifiers_payload", None)
        if not transient_rows:
            resolved_sale_name = cstr(getattr(line, "resolved_sale", None)).strip()
            if not resolved_sale_name:
                return []
            resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
            if cstr(getattr(resolved_sale, "fb_order", None)).strip() != cstr(
                self.name
            ).strip():
                frappe.throw(
                    "Prepared resolved sale belongs to another FB Order",
                    frappe.ValidationError,
                )
            return list(getattr(resolved_sale, "selected_modifiers", None) or [])

        return list(transient_rows)

    def validate(self):
        self.validate_required_fields()
        self.calculate_totals()
        self.validate_order_totals()
        self.validate_idempotency_key_uniqueness()
        self.validate_prepared_sale_immutability()

    def validate_prepared_sale_immutability(self) -> None:
        if not cstr(getattr(self, "accepted_sale_fingerprint", None)).strip():
            return
        get_before_save = getattr(self, "get_doc_before_save", None)
        before = get_before_save() if callable(get_before_save) else None
        if before is None or not cstr(
            getattr(before, "accepted_sale_fingerprint", None)
        ).strip():
            return
        before_snapshot = _prepared_sale_immutable_snapshot(before)
        current_snapshot = _prepared_sale_immutable_snapshot(self)
        changed_paths = _prepared_sale_changed_paths(
            before_snapshot,
            current_snapshot,
        )
        if changed_paths and not _is_static_qr_winner_transition(
            before,
            self,
            changed_paths,
        ):
            displayed_paths = changed_paths[:12]
            undisplayed_count = len(changed_paths) - len(displayed_paths)
            suffix = (
                f" (and {undisplayed_count} more)" if undisplayed_count else ""
            )
            frappe.throw(
                "Prepared Automatic QR immutable sale snapshot cannot be changed: "
                + ", ".join(displayed_paths)
                + suffix,
                frappe.ValidationError,
            )

    def before_submit(self):
        if cstr(getattr(self, "accepted_sale_fingerprint", None)).strip():
            line_resolutions = self.validate_prepared_resolved_sales()
        else:
            line_resolutions = self.build_line_resolutions()
            self.validate_stock_availability(line_resolutions)
        if cstr(getattr(self, "accepted_sale_fingerprint", None)).strip():
            self.mark_prepared_resolved_sales_submitted(line_resolutions)
        else:
            self.create_resolved_sales(line_resolutions)
        register_qr_payment_settlement(self)

    def on_submit(self):
        resolved_sales = self.get_resolved_sales()
        invoice_log = self.create_projection_entry("Sales Invoice")
        stock_log = self.create_projection_entry("Stock Issue")
        shift_log = self.create_projection_entry("FB Shift")

        invoice_error = None
        try:
            self.sales_invoice = create_sales_invoice(self)
        except Exception as error:
            self.sales_invoice = None
            invoice_error = error
        if self.sales_invoice:
            self.invoice_status = "Posted"
            update_projection_state(
                invoice_log,
                "Succeeded",
                "Sales Invoice",
                self.sales_invoice,
                None,
            )
        else:
            self.invoice_status = "Failed"
            update_projection_state(
                invoice_log,
                "Failed",
                "Sales Invoice",
                None,
                str(invoice_error) if invoice_error else "Sales Invoice projection failed",
            )

        stock_error = None
        stock_projection_required = self.requires_stock_projection(resolved_sales)
        if stock_projection_required:
            try:
                self.ingredient_stock_entry = create_ingredient_stock_entry(
                    self, resolved_sales
                )
            except Exception as error:
                self.ingredient_stock_entry = None
                stock_error = error
        else:
            self.ingredient_stock_entry = None
        if self.ingredient_stock_entry:
            self.stock_status = "Posted"
            update_projection_state(
                stock_log,
                "Succeeded",
                "Stock Entry",
                self.ingredient_stock_entry,
                None,
            )
        elif not stock_projection_required:
            # A no-op stock projection is terminal success, not retryable work.
            self.stock_status = "Posted"
            update_projection_state(
                stock_log,
                "Succeeded",
                "Stock Entry",
                None,
                None,
            )
        else:
            self.stock_status = "Failed"
            update_projection_state(
                stock_log,
                "Failed",
                "Stock Entry",
                None,
                str(stock_error) if stock_error else "Stock issue projection failed",
            )

        self.status = "Submitted"
        self.db_set("status", self.status, update_modified=False)
        self.db_set("invoice_status", self.invoice_status, update_modified=False)
        self.db_set("stock_status", self.stock_status, update_modified=False)
        if self.sales_invoice:
            self.db_set("sales_invoice", self.sales_invoice, update_modified=False)
        if self.ingredient_stock_entry:
            self.db_set(
                "ingredient_stock_entry",
                self.ingredient_stock_entry,
                update_modified=False,
            )
        try:
            self.update_shift_expected_cash()
            update_projection_state(
                shift_log,
                "Succeeded",
                "FB Shift",
                self.shift,
                None,
            )
        except Exception as error:
            update_projection_state(
                shift_log,
                "Failed",
                "FB Shift",
                self.shift,
                "FB Shift projection failed for FB Order {0}: {1}".format(
                    self.name,
                    error,
                ),
            )

    def create_projection_entry(self, projection_type: str) -> str:
        return create_projection_log(
            source_doctype="FB Order",
            source_name=self.name,
            projection_type=projection_type,
            idempotency_key=f"{self.external_idempotency_key}:{projection_type}",
            payload_hash=self.build_projection_hash(projection_type),
        )

    def build_projection_hash(self, projection_type: str) -> str:
        payload = {
            "order": self.name,
            "projection_type": projection_type,
            "status": self.status,
            "invoice_status": self.invoice_status,
            "stock_status": self.stock_status,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def get_resolved_sales(self) -> list[DocumentLike]:
        resolved_sales = []
        for line in self.items:
            if getattr(line, "resolved_sale", None):
                resolved_sales.append(
                frappe.get_doc("FB Resolved Sale", line.resolved_sale)
                )
        return resolved_sales

    def requires_stock_projection(self, resolved_sales: list[DocumentLike]) -> bool:
        for resolved_sale in resolved_sales:
            for component in list(getattr(resolved_sale, "resolved_components", None) or []):
                if not int(getattr(component, "affects_stock", 0) or 0):
                    continue
                item = getattr(component, "item", None)
                warehouse = getattr(component, "warehouse", None) or getattr(
                    resolved_sale,
                    "booth_warehouse",
                    None,
                )
                qty = flt(
                    getattr(component, "stock_qty", None)
                    or getattr(component, "qty", None)
                    or 0
                )
                stock_uom = getattr(component, "stock_uom", None) or getattr(
                    component,
                    "uom",
                    None,
                )
                if not item or not warehouse or not stock_uom or not isfinite(qty) or qty <= 0:
                    frappe.throw(
                        "Stock-affecting resolved component requires item, warehouse, stock UOM, and positive stock quantity",
                        frappe.ValidationError,
                    )
                return True
        return False

    def validate_prepared_resolved_sales(self) -> list[dict[str, Any]]:
        line_resolutions: list[dict[str, Any]] = []
        for line_index, line in enumerate(self.items, start=1):
            resolved_sale_name = cstr(getattr(line, "resolved_sale", None)).strip()
            if not resolved_sale_name:
                frappe.throw(
                    "Prepared Automatic QR line {0} has no resolved sale snapshot".format(
                        line_index
                    ),
                    frappe.ValidationError,
                )
            resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
            expected_identity = {
                "fb_order": cstr(self.name),
                "fb_order_line": cstr(line.name),
                "backend_line_uuid": cstr(line.backend_line_uuid),
                "sellable_item": cstr(line.item),
                "booth_warehouse": cstr(self.booth_warehouse),
                "recipe": cstr(line.recipe),
                "recipe_version": cstr(line.recipe_version),
            }
            for fieldname, expected_value in expected_identity.items():
                if cstr(getattr(resolved_sale, fieldname, None)) != expected_value:
                    frappe.throw(
                        "Prepared resolved sale {0} {1} does not match".format(
                            resolved_sale_name,
                            fieldname,
                        ),
                        frappe.ValidationError,
                    )
            if _positive_integer_qty(
                getattr(resolved_sale, "qty", None),
                "Prepared resolved sale qty",
            ) != _positive_integer_qty(line.qty, "FB Order line qty"):
                frappe.throw(
                    "Prepared resolved sale {0} qty does not match".format(
                        resolved_sale_name
                    ),
                    frappe.ValidationError,
                )
            if cstr(getattr(resolved_sale, "status", None)).strip() not in {
                "Prepared",
                "Submitted",
            }:
                frappe.throw(
                    "Prepared resolved sale {0} has invalid status".format(
                        resolved_sale_name
                    ),
                    frappe.ValidationError,
                )
            if not cstr(getattr(resolved_sale, "resolution_hash", None)).strip():
                frappe.throw(
                    "Prepared resolved sale {0} has no resolution hash".format(
                        resolved_sale_name
                    ),
                    frappe.ValidationError,
                )
            resolved_components = _canonical_resolved_components(
                list(getattr(resolved_sale, "resolved_components", None) or [])
            )
            if not resolved_components:
                frappe.throw(
                    "Prepared resolved sale {0} has no resolved components".format(
                        resolved_sale_name
                    ),
                    frappe.ValidationError,
                )
            try:
                line_snapshot_value = json.loads(
                    cstr(getattr(line, "resolved_components_snapshot", None))
                )
            except (TypeError, ValueError):
                line_snapshot_value = None
            if not isinstance(line_snapshot_value, list) or (
                _canonical_resolved_components(line_snapshot_value)
                != resolved_components
            ):
                frappe.throw(
                    "Prepared resolved sale {0} component snapshot does not match".format(
                        resolved_sale_name
                    ),
                    frappe.ValidationError,
                )
            selected_modifier_hash_rows = [
                {
                    "modifier_group": cstr(
                        _record_value(modifier, "modifier_group")
                    ),
                    "modifier": cstr(_record_value(modifier, "modifier")),
                    "price_adjustment_sen": _money_sen(
                        _optional_money_value(
                            _record_value(modifier, "price_adjustment")
                        ),
                        "Prepared selected modifier price_adjustment",
                    ),
                }
                for modifier in list(
                    getattr(resolved_sale, "selected_modifiers", None) or []
                )
            ]
            persisted_resolution_hash = cstr(
                getattr(resolved_sale, "resolution_hash", None)
            ).strip()
            recomputed_resolution_hash = _resolution_hash(
                recipe=cstr(getattr(resolved_sale, "recipe", None)),
                recipe_version=getattr(resolved_sale, "recipe_version", None),
                selected_modifiers=selected_modifier_hash_rows,
                resolved_components=resolved_components,
            )
            if persisted_resolution_hash != recomputed_resolution_hash:
                frappe.throw(
                    "Prepared resolved sale {0} resolution hash does not match".format(
                        resolved_sale_name
                    ),
                    frappe.ValidationError,
                )
            line_resolutions.append(
                {
                    "line": line,
                    "resolved_sale": resolved_sale,
                    "resolved_components": resolved_components,
                }
            )
        return line_resolutions

    def mark_prepared_resolved_sales_submitted(
        self,
        line_resolutions: list[dict[str, Any]],
    ) -> None:
        for line_resolution in line_resolutions:
            resolved_sale = line_resolution["resolved_sale"]
            if cstr(getattr(resolved_sale, "status", None)).strip() == "Submitted":
                continue
            frappe.db.set_value(
                "FB Resolved Sale",
                resolved_sale.name,
                "status",
                "Submitted",
                update_modified=False,
            )

    def update_shift_expected_cash(self):
        if not self.shift:
            return
        from kopos_connector.kopos.services.accounting.return_invoice_service import (
            refresh_fb_shift_cash,
        )

        refresh_fb_shift_cash(self.shift)

    def calculate_totals(self):
        net_total_sen = 0
        for line_index, line in enumerate(self.get("items") or [], start=1):
            prefix = f"FB Order item {line_index}"
            unit_price_sen = _money_sen(
                _optional_money_value(getattr(line, "unit_price", None)),
                f"{prefix} unit_price",
            )
            modifier_total_sen = _money_sen(
                _optional_money_value(getattr(line, "modifier_total", None)),
                f"{prefix} modifier_total",
            )
            discount_amount_sen = _money_sen(
                _optional_money_value(getattr(line, "discount_amount", None)),
                f"{prefix} discount_amount",
            )
            qty = _positive_integer_qty(getattr(line, "qty", None), f"{prefix} qty")
            computed_line_total_sen = (
                (unit_price_sen + modifier_total_sen) * qty
            ) - discount_amount_sen
            if computed_line_total_sen < 0:
                frappe.throw(
                    f"{prefix} line_total must be 0 or greater",
                    frappe.ValidationError,
                )
            line.qty = qty
            line.unit_price = sen_to_decimal(unit_price_sen)
            line.modifier_total = sen_to_decimal(modifier_total_sen)
            line.discount_amount = sen_to_decimal(discount_amount_sen)
            line.line_total = sen_to_decimal(computed_line_total_sen)
            net_total_sen += computed_line_total_sen

        tax_total_sen = _money_sen(
            _optional_money_value(getattr(self, "tax_total", None)),
            "FB Order tax_total",
        )
        rounding_adjustment_sen = _money_sen(
            _optional_money_value(getattr(self, "rounding_adjustment", None)),
            "FB Order rounding_adjustment",
        )
        if tax_total_sen < 0:
            frappe.throw(
                "FB Order tax_total must be non-negative",
                frappe.ValidationError,
            )
        grand_total_sen = net_total_sen + tax_total_sen + rounding_adjustment_sen
        if grand_total_sen <= 0:
            frappe.throw(
                "FB Order grand_total must be greater than 0",
                frappe.ValidationError,
            )
        self.net_total = sen_to_decimal(net_total_sen)
        self.tax_total = sen_to_decimal(tax_total_sen)
        self.rounding_adjustment = sen_to_decimal(rounding_adjustment_sen)
        self.grand_total = sen_to_decimal(grand_total_sen)

    def validate_required_fields(self):
        required_order_fields = {
            "order_id": self.order_id,
            "external_idempotency_key": self.external_idempotency_key,
            "source": self.source,
            "device_id": self.device_id,
            "shift": self.shift,
            "staff_id": self.staff_id,
            "booth_warehouse": self.booth_warehouse,
            "company": self.company,
            "currency": self.currency,
        }
        missing_order_fields = [
            fieldname for fieldname, value in required_order_fields.items() if not value
        ]
        if missing_order_fields:
            frappe.throw(
                "FB Order is missing required fields: {0}".format(
                    ", ".join(missing_order_fields)
                ),
                frappe.ValidationError,
            )

        if not self.get("items"):
            frappe.throw(
                "FB Order must contain at least one item", frappe.ValidationError
            )

        for line_index, line in enumerate(self.items, start=1):
            required_line_fields = {
                "line_id": line.line_id,
                "item": line.item,
                "qty": line.qty,
                "uom": line.uom,
            }
            missing_line_fields = [
                fieldname
                for fieldname, value in required_line_fields.items()
                if not value
            ]
            if missing_line_fields:
                frappe.throw(
                    "Order line {0} is missing required fields: {1}".format(
                        self.describe_line(line_index, line),
                        ", ".join(missing_line_fields),
                    ),
                    frappe.ValidationError,
                )

            line_label = self.describe_line(line_index, line)
            _positive_integer_qty(line.qty, f"Order line {line_label} qty")
            unit_price_sen = _money_sen(
                _optional_money_value(getattr(line, "unit_price", None)),
                f"Order line {line_label} unit_price",
            )
            modifier_total_sen = _money_sen(
                _optional_money_value(getattr(line, "modifier_total", None)),
                f"Order line {line_label} modifier_total",
            )
            discount_amount_sen = _money_sen(
                _optional_money_value(getattr(line, "discount_amount", None)),
                f"Order line {line_label} discount_amount",
            )
            if unit_price_sen < 0 or discount_amount_sen < 0:
                frappe.throw(
                    f"Order line {line_label} unit price and discount must be non-negative",
                    frappe.ValidationError,
                )

        for payment_index, payment in enumerate(self.get("payments") or [], start=1):
            if not payment.payment_method:
                frappe.throw(
                    "Payment row {0} is missing payment_method".format(payment_index),
                    frappe.ValidationError,
                )
            payment_amount_sen = _money_sen(
                getattr(payment, "amount", None),
                f"Payment row {payment_index} amount",
            )
            if payment_amount_sen <= 0:
                frappe.throw(
                    "Payment row {0} must have amount greater than 0".format(
                        payment_index
                    ),
                    frappe.ValidationError,
                )
            payment.amount = sen_to_decimal(payment_amount_sen)
            for fieldname in ("tendered_amount", "change_amount"):
                field_value = getattr(payment, fieldname, None)
                if field_value is None or field_value == "":
                    continue
                field_value_sen = _money_sen(
                    field_value,
                    f"Payment row {payment_index} {fieldname}",
                )
                if field_value_sen < 0:
                    frappe.throw(
                        f"Payment row {payment_index} {fieldname} must be non-negative",
                        frappe.ValidationError,
                    )
                setattr(payment, fieldname, sen_to_decimal(field_value_sen))

    def validate_order_totals(self):
        expected_net_total_sen = sum(
            _money_sen(line.line_total, f"FB Order item {index} line_total")
            for index, line in enumerate(self.items, start=1)
        )
        net_total_sen = _money_sen(self.net_total, "FB Order net_total")
        if net_total_sen != expected_net_total_sen:
            frappe.throw(
                "FB Order net_total {0} does not match summed line totals {1}".format(
                    sen_to_decimal(net_total_sen),
                    sen_to_decimal(expected_net_total_sen),
                ),
                frappe.ValidationError,
            )

        tax_total_sen = _money_sen(self.tax_total, "FB Order tax_total")
        rounding_adjustment_sen = _money_sen(
            _optional_money_value(getattr(self, "rounding_adjustment", None)),
            "FB Order rounding_adjustment",
        )
        expected_grand_total_sen = (
            expected_net_total_sen + tax_total_sen + rounding_adjustment_sen
        )
        grand_total_sen = _money_sen(self.grand_total, "FB Order grand_total")
        if grand_total_sen != expected_grand_total_sen:
            frappe.throw(
                "FB Order grand_total {0} does not match net_total plus tax_total plus rounding_adjustment {1}".format(
                    sen_to_decimal(grand_total_sen),
                    sen_to_decimal(expected_grand_total_sen),
                ),
                frappe.ValidationError,
            )

        payment_rows = list(self.get("payments") or [])
        payment_total_sen = sum(
            _money_sen(payment.amount, f"FB Order payment {index} amount")
            for index, payment in enumerate(payment_rows, start=1)
        )
        if payment_rows and payment_total_sen != grand_total_sen:
            frappe.throw(
                "FB Order payment total {0} does not match grand_total {1}".format(
                    sen_to_decimal(payment_total_sen),
                    sen_to_decimal(grand_total_sen),
                ),
                frappe.ValidationError,
            )

    def validate_idempotency_key_uniqueness(self):
        duplicate_name = frappe.db.get_value(
            "FB Order",
            {
                "external_idempotency_key": self.external_idempotency_key,
                "name": ["!=", self.name or ""],
            },
            "name",
        )
        if duplicate_name:
            frappe.throw(
                "Idempotency key {0} is already used by FB Order {1}".format(
                    self.external_idempotency_key, duplicate_name
                ),
                frappe.ValidationError,
            )

    def build_line_resolutions(self) -> list[dict[str, Any]]:
        line_resolutions = []
        for line_index, line in enumerate(self.items, start=1):
            recipe_doc = self.resolve_recipe_for_line(line_index, line)
            selected_modifiers = self.validate_modifier_selections(
                line_index=line_index,
                line=line,
                recipe_doc=recipe_doc,
            )
            resolved_components = self.resolve_components_for_line(
                line_index=line_index,
                line=line,
                recipe_doc=recipe_doc,
                selected_modifiers=selected_modifiers,
            )
            line_resolutions.append(
                {
                    "line": line,
                    "line_index": line_index,
                    "recipe_doc": recipe_doc,
                    "selected_modifiers": selected_modifiers,
                    "resolved_components": resolved_components,
                }
            )
        return line_resolutions

    def resolve_recipe_for_line(self, line_index: int, line) -> DocumentLike:
        has_explicit_sale_recipe = bool(line.recipe and line.recipe_version)
        if line.recipe:
            recipe_doc = frappe.get_cached_doc("FB Recipe", line.recipe)
        else:
            recipe_doc = self.find_default_recipe_for_item(line.item)
            line.recipe = recipe_doc.name

        if has_explicit_sale_recipe and int(recipe_doc.version_no) != int(
            line.recipe_version
        ):
            frappe.throw(
                "Order line {0} recipe {1} version changed: sale used {2}, ERP has {3}".format(
                    self.describe_line(line_index, line),
                    recipe_doc.name,
                    line.recipe_version,
                    recipe_doc.version_no,
                ),
                frappe.ValidationError,
            )

        if recipe_doc.status != "Active" and not has_explicit_sale_recipe:
            frappe.throw(
                "Order line {0} references inactive recipe {1}".format(
                    self.describe_line(line_index, line), recipe_doc.name
                ),
                frappe.ValidationError,
            )

        if recipe_doc.sellable_item != line.item:
            frappe.throw(
                "Order line {0} recipe {1} does not match sellable item {2}".format(
                    self.describe_line(line_index, line), recipe_doc.name, line.item
                ),
                frappe.ValidationError,
            )

        if recipe_doc.company and recipe_doc.company != self.company:
            frappe.throw(
                "Order line {0} recipe {1} belongs to company {2}, expected {3}".format(
                    self.describe_line(line_index, line),
                    recipe_doc.name,
                    recipe_doc.company,
                    self.company,
                ),
                frappe.ValidationError,
            )

        if not self.recipe_is_effective(
            recipe_doc, getattr(self, "sale_datetime", None)
        ):
            frappe.throw(
                "Order line {0} recipe {1} is not effective at the original sale time".format(
                    self.describe_line(line_index, line), recipe_doc.name
                ),
                frappe.ValidationError,
            )

        if not recipe_doc.components:
            frappe.throw(
                "Order line {0} recipe {1} has no components to resolve".format(
                    self.describe_line(line_index, line), recipe_doc.name
                ),
                frappe.ValidationError,
            )

        item_is_stock = frappe.db.get_value("Item", line.item, "is_stock_item")
        if item_is_stock and int(line.is_recipe_managed or 1):
            frappe.throw(
                "Order line {0} item {1} is recipe-managed and must be configured as a non-stock item".format(
                    self.describe_line(line_index, line), line.item
                ),
                frappe.ValidationError,
            )

        line.is_recipe_managed = 1
        line.recipe_version = recipe_doc.version_no
        line.item_name_snapshot = line.item_name_snapshot or recipe_doc.recipe_name
        return recipe_doc

    def find_default_recipe_for_item(self, item_code: str) -> DocumentLike:
        candidate_names = frappe.get_all(
            "FB Recipe",
            filters={
                "sellable_item": item_code,
                "company": self.company,
            },
            pluck="name",
            order_by="version_no desc",
        )
        effective_candidates = []
        for candidate_name in candidate_names:
            recipe_doc = frappe.get_cached_doc("FB Recipe", candidate_name)
            is_historical_version = bool(
                recipe_doc.status != "Active" and recipe_doc.effective_to
            )
            if (
                recipe_doc.status == "Active" or is_historical_version
            ) and self.recipe_is_effective(
                recipe_doc, getattr(self, "sale_datetime", None)
            ):
                effective_candidates.append(recipe_doc)

        if not effective_candidates:
            frappe.throw(
                "No FB Recipe effective at the original sale time was found for item {0} in company {1}".format(
                    item_code, self.company
                ),
                frappe.ValidationError,
            )

        if len(effective_candidates) > 1:
            frappe.throw(
                "Multiple active FB Recipes were found for item {0}: {1}".format(
                    item_code, ", ".join(recipe.name for recipe in effective_candidates)
                ),
                frappe.ValidationError,
            )

        return effective_candidates[0]

    def recipe_is_effective(
        self, recipe_doc: DocumentLike, at_time: Any | None = None
    ) -> bool:
        submit_time = frappe_utils.get_datetime(
            at_time or getattr(self, "sale_datetime", None) or now_datetime()
        )
        if recipe_doc.effective_from and submit_time < recipe_doc.effective_from:
            return False
        if recipe_doc.effective_to and submit_time > recipe_doc.effective_to:
            return False
        return True

    def validate_modifier_selections(
        self, line_index: int, line, recipe_doc: DocumentLike
    ) -> list[dict[str, Any]]:
        allowed_group_rows = recipe_doc.get("allowed_modifier_groups") or []
        all_allowed_group_map = {
            row.modifier_group: row for row in allowed_group_rows if row.modifier_group
        }
        selected_modifiers = []
        selections_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        selected_rows = self.get_selected_modifier_rows(line)
        selected_modifier_names = {
            cstr(selected_row.modifier)
            for selected_row in selected_rows
            if cstr(selected_row.modifier)
        }
        visible_group_rows = filter_visible_allowed_modifier_groups(
            allowed_group_rows, selected_modifier_names
        )
        visible_group_map = {
            group_name: row
            for row in visible_group_rows
            if (group_name := cstr(getattr(row, "modifier_group", None)))
        }

        for selected_row in selected_rows:
            if not selected_row.modifier_group or not selected_row.modifier:
                frappe.throw(
                    "Order line {0} has a selected modifier row without modifier_group or modifier".format(
                        self.describe_line(line_index, line)
                    ),
                    frappe.ValidationError,
                )

            if selected_row.modifier_group not in all_allowed_group_map:
                frappe.throw(
                    "Order line {0} selected modifier group {1} is not allowed by recipe {2}".format(
                        self.describe_line(line_index, line),
                        selected_row.modifier_group,
                        recipe_doc.name,
                    ),
                    frappe.ValidationError,
                )

            modifier_group_doc = frappe.get_cached_doc(
                "FB Modifier Group", selected_row.modifier_group
            )
            modifier_doc = frappe.get_cached_doc("FB Modifier", selected_row.modifier)

            has_explicit_sale_recipe = bool(
                getattr(line, "recipe", None)
                and getattr(line, "recipe_version", None)
                and getattr(self, "request_fingerprint", None)
            )

            if not int(modifier_group_doc.active) and not has_explicit_sale_recipe:
                frappe.throw(
                    "Order line {0} selected inactive modifier group {1}".format(
                        self.describe_line(line_index, line), modifier_group_doc.name
                    ),
                    frappe.ValidationError,
                )

            if not int(modifier_doc.active) and not has_explicit_sale_recipe:
                frappe.throw(
                    "Order line {0} selected inactive modifier {1}".format(
                        self.describe_line(line_index, line), modifier_doc.name
                    ),
                    frappe.ValidationError,
                )

            if modifier_doc.modifier_group != selected_row.modifier_group:
                frappe.throw(
                    "Order line {0} modifier {1} does not belong to modifier group {2}".format(
                        self.describe_line(line_index, line),
                        modifier_doc.name,
                        selected_row.modifier_group,
                    ),
                    frappe.ValidationError,
                )

            submitted_price_adjustment = getattr(
                selected_row, "price_adjustment", None
            )
            if submitted_price_adjustment in (None, ""):
                submitted_price_adjustment = modifier_doc.price_adjustment
            selected_row.price_adjustment = sen_to_decimal(
                _money_sen(
                    _optional_money_value(submitted_price_adjustment),
                    f"FB Modifier {modifier_doc.name} sale price_adjustment",
                )
            )
            selected_row.instruction_text = (
                selected_row.instruction_text or modifier_doc.instruction_text
            )
            selected_row.sort_order = (
                selected_row.sort_order or modifier_doc.display_order or 0
            )
            selected_row.affects_stock = int(modifier_doc.affects_stock)
            selected_row.affects_recipe = int(modifier_doc.affects_recipe)

            normalized_modifier = {
                "row": selected_row,
                "group_row": all_allowed_group_map[selected_row.modifier_group],
                "group_doc": modifier_group_doc,
                "modifier_doc": modifier_doc,
            }
            selections_by_group[selected_row.modifier_group].append(normalized_modifier)
            selected_modifiers.append(normalized_modifier)

        for group_name, group_row in visible_group_map.items():
            group_doc = frappe.get_cached_doc("FB Modifier Group", group_name)
            selected_count = len(selections_by_group.get(group_name, []))
            bounds = self.resolve_modifier_bounds(group_doc, group_row)
            min_selection = bounds.min_selection
            max_selection = bounds.max_selection

            if bounds.selection_type == "single" and selected_count > 1:
                frappe.throw(
                    "Order line {0} modifier group {1} allows only one selection".format(
                        self.describe_line(line_index, line), group_name
                    ),
                    frappe.ValidationError,
                )

            if min_selection and selected_count < min_selection:
                frappe.throw(
                    "Order line {0} modifier group {1} requires at least {2} selection(s)".format(
                        self.describe_line(line_index, line), group_name, min_selection
                    ),
                    frappe.ValidationError,
                )

            if max_selection and selected_count > max_selection:
                frappe.throw(
                    "Order line {0} modifier group {1} allows at most {2} selection(s)".format(
                        self.describe_line(line_index, line), group_name, max_selection
                    ),
                    frappe.ValidationError,
                )

        return selected_modifiers

    def resolve_modifier_bounds(
        self, group_doc: DocumentLike, group_row: DocumentLike
    ) -> EffectiveModifierBounds:
        group_name = cstr(getattr(group_doc, "name", None)).strip() or "(unnamed)"
        try:
            return resolve_effective_modifier_bounds(
                selection_type=getattr(group_doc, "selection_type", None),
                group_is_required=getattr(group_doc, "is_required", None),
                group_min_selection=getattr(group_doc, "min_selection", None),
                group_max_selection=getattr(group_doc, "max_selection", None),
                recipe_required=getattr(group_row, "required", None),
                override_min_selection=getattr(
                    group_row, "override_min_selection", None
                ),
                override_max_selection=getattr(
                    group_row, "override_max_selection", None
                ),
            )
        except ModifierBoundsError as error:
            frappe.throw(
                f"Modifier group {group_name} has invalid selection rules: {error}",
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw must raise") from error

    def resolve_min_selection(self, group_doc: DocumentLike, group_row) -> int:
        return self.resolve_modifier_bounds(group_doc, group_row).min_selection

    def resolve_max_selection(self, group_doc: DocumentLike, group_row) -> int:
        return self.resolve_modifier_bounds(group_doc, group_row).max_selection

    def resolve_components_for_line(
        self,
        line_index: int,
        line,
        recipe_doc: DocumentLike,
        selected_modifiers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        default_serving_qty = flt(recipe_doc.default_serving_qty)
        if not isfinite(default_serving_qty) or default_serving_qty <= 0:
            frappe.throw(
                "Order line {0} recipe {1} requires default_serving_qty greater than 0".format(
                    self.describe_line(line_index, line), recipe_doc.name
                ),
                frappe.ValidationError,
            )
        scale_factor = flt(line.qty) / default_serving_qty
        resolved_components = []

        for component_index, component_row in enumerate(recipe_doc.components, start=1):
            resolved_components.append(
                {
                    "item": component_row.item,
                    "source_type": "Base Recipe",
                    "qty": flt(component_row.qty) * scale_factor,
                    "uom": component_row.uom,
                    "stock_qty": flt(component_row.stock_qty or component_row.qty)
                    * scale_factor,
                    "stock_uom": component_row.stock_uom or component_row.uom,
                    "warehouse": self.booth_warehouse,
                    "source_reference": component_row.substitution_key
                    or "component-{0}".format(component_index),
                    "affects_stock": int(component_row.affects_stock),
                    "affects_cogs": int(component_row.affects_cogs),
                    "remarks": component_row.remarks,
                }
            )

        for selected_modifier in selected_modifiers:
            modifier_doc = selected_modifier["modifier_doc"]
            if not int(modifier_doc.affects_recipe):
                continue

            if modifier_doc.kind == "Add":
                resolved_components.append(
                    self.build_modifier_component(modifier_doc, line_index, line)
                )
                continue

            matched_components = self.find_matching_components(
                resolved_components=resolved_components,
                modifier_doc=modifier_doc,
            )

            if not matched_components:
                frappe.throw(
                    "Order line {0} modifier {1} could not resolve a target recipe component".format(
                        self.describe_line(line_index, line), modifier_doc.name
                    ),
                    frappe.ValidationError,
                )

            if modifier_doc.kind == "Remove":
                for component in matched_components:
                    resolved_components.remove(component)
                continue

            if modifier_doc.kind == "Replace":
                if not modifier_doc.new_item:
                    frappe.throw(
                        "Order line {0} modifier {1} requires new_item for replacement".format(
                            self.describe_line(line_index, line), modifier_doc.name
                        ),
                        frappe.ValidationError,
                    )
                for component in matched_components:
                    component["item"] = modifier_doc.new_item
                    component["source_type"] = "Modifier Replace"
                    component["source_reference"] = modifier_doc.name
                    if modifier_doc.qty_delta:
                        component["qty"] = flt(component["qty"]) + flt(
                            modifier_doc.qty_delta
                        )
                        component["stock_qty"] = flt(component["stock_qty"]) + flt(
                            modifier_doc.qty_delta
                        )
                continue

            if modifier_doc.kind == "Scale":
                scale_percent = flt(modifier_doc.scale_percent)
                if scale_percent <= 0:
                    frappe.throw(
                        "Order line {0} modifier {1} requires scale_percent greater than 0".format(
                            self.describe_line(line_index, line), modifier_doc.name
                        ),
                        frappe.ValidationError,
                    )
                scale_multiplier = scale_percent / 100
                for component in matched_components:
                    component["qty"] = flt(component["qty"]) * scale_multiplier
                    component["stock_qty"] = (
                        flt(component["stock_qty"]) * scale_multiplier
                    )
                    component["source_type"] = "Modifier Scale"
                    component["source_reference"] = modifier_doc.name
                continue

            frappe.throw(
                "Order line {0} modifier {1} has unsupported recipe effect kind {2}".format(
                    self.describe_line(line_index, line),
                    modifier_doc.name,
                    modifier_doc.kind,
                ),
                frappe.ValidationError,
            )

        if not resolved_components:
            frappe.throw(
                "Order line {0} resolved to zero components".format(
                    self.describe_line(line_index, line)
                ),
                frappe.ValidationError,
            )

        self.validate_resolved_components(
            line_index=line_index,
            line=line,
            resolved_components=resolved_components,
        )
        return resolved_components

    def validate_resolved_components(
        self,
        *,
        line_index: int,
        line: DocumentLike,
        resolved_components: list[dict[str, Any]],
    ) -> None:
        item_stock_metadata: dict[str, tuple[str, int]] = {}
        for component_index, component in enumerate(resolved_components, start=1):
            label = "Order line {0} resolved component {1}".format(
                self.describe_line(line_index, line),
                component_index,
            )
            item_code = cstr(component.get("item")).strip()
            uom = cstr(component.get("uom")).strip()
            qty = flt(component.get("qty"))
            if not item_code or not uom or not isfinite(qty) or qty <= 0:
                frappe.throw(
                    f"{label} requires item, UOM, and positive quantity",
                    frappe.ValidationError,
                )
            if not int(component.get("affects_stock") or 0):
                continue

            stock_qty = flt(component.get("stock_qty"))
            stock_uom = cstr(component.get("stock_uom")).strip()
            warehouse = cstr(component.get("warehouse") or self.booth_warehouse).strip()
            if not stock_uom or not warehouse or not isfinite(stock_qty) or stock_qty <= 0:
                frappe.throw(
                    f"{label} affects stock and requires warehouse, Stock UOM, and positive Stock Qty",
                    frappe.ValidationError,
                )

            if item_code not in item_stock_metadata:
                item_values = frappe.db.get_value(
                    "Item",
                    item_code,
                    ["stock_uom", "is_stock_item"],
                    as_dict=True,
                )
                if not item_values:
                    frappe.throw(
                        f"{label} Item {item_code} was not found",
                        frappe.ValidationError,
                    )
                if isinstance(item_values, dict):
                    item_stock_metadata[item_code] = (
                        cstr(item_values.get("stock_uom")).strip(),
                        int(item_values.get("is_stock_item") or 0),
                    )
                else:
                    item_stock_metadata[item_code] = (
                        cstr(getattr(item_values, "stock_uom", None)).strip(),
                        int(getattr(item_values, "is_stock_item", 0) or 0),
                    )
            item_stock_uom, is_stock_item = item_stock_metadata[item_code]
            if not is_stock_item:
                frappe.throw(
                    f"{label} Item {item_code} must be a stock Item",
                    frappe.ValidationError,
                )
            if stock_uom != item_stock_uom:
                frappe.throw(
                    f"{label} Stock UOM {stock_uom} does not match Item stock UOM {item_stock_uom or '(missing)'}",
                    frappe.ValidationError,
                )

    def build_modifier_component(
        self, modifier_doc: DocumentLike, line_index: int, line
    ) -> dict[str, Any]:
        target_item = modifier_doc.new_item or modifier_doc.target_item
        if not target_item:
            frappe.throw(
                "Order line {0} modifier {1} requires target_item or new_item for Add resolution".format(
                    self.describe_line(line_index, line), modifier_doc.name
                ),
                frappe.ValidationError,
            )
        if not modifier_doc.qty_uom:
            frappe.throw(
                "Order line {0} modifier {1} requires qty_uom for Add resolution".format(
                    self.describe_line(line_index, line), modifier_doc.name
                ),
                frappe.ValidationError,
            )
        if flt(modifier_doc.qty_delta) <= 0:
            frappe.throw(
                "Order line {0} modifier {1} requires qty_delta greater than 0 for Add resolution".format(
                    self.describe_line(line_index, line), modifier_doc.name
                ),
                frappe.ValidationError,
            )

        return {
            "item": target_item,
            "source_type": "Modifier Add",
            "qty": flt(modifier_doc.qty_delta),
            "uom": modifier_doc.qty_uom,
            "stock_qty": flt(modifier_doc.qty_delta),
            "stock_uom": modifier_doc.qty_uom,
            "warehouse": self.booth_warehouse,
            "source_reference": modifier_doc.name,
            "affects_stock": 1 if int(modifier_doc.affects_stock or 0) else 0,
            "affects_cogs": 1,
            "remarks": modifier_doc.instruction_text,
        }

    def find_matching_components(
        self, resolved_components: list[dict[str, Any]], modifier_doc: DocumentLike
    ) -> list[dict[str, Any]]:
        if modifier_doc.target_substitution_key:
            return [
                component
                for component in resolved_components
                if component.get("source_reference")
                == modifier_doc.target_substitution_key
            ]
        if modifier_doc.target_item:
            return [
                component
                for component in resolved_components
                if component.get("item") == modifier_doc.target_item
            ]
        return []

    def validate_stock_availability(self, line_resolutions: list[dict[str, Any]]):
        components: list[dict[str, Any]] = []

        for line_resolution in line_resolutions:
            for component in line_resolution["resolved_components"]:
                normalized_component = dict(component)
                normalized_component["warehouse"] = (
                    normalized_component.get("warehouse") or self.booth_warehouse
                )
                components.append(normalized_component)

        shortfalls = detect_stock_shortfall(components)
        if shortfalls:
            require_advisory_shortfall_policy(shortfalls)
            log_stock_shortfall(self, shortfalls, timestamp=now_datetime())

    def create_resolved_sales(self, line_resolutions: list[dict[str, Any]]):
        for line_resolution in line_resolutions:
            line = line_resolution["line"]
            recipe_doc = line_resolution["recipe_doc"]
            selected_modifiers = line_resolution["selected_modifiers"]
            resolved_components = line_resolution["resolved_components"]
            resolved_sale_id = line.backend_line_uuid or "{0}-{1}".format(
                self.order_id, line.line_id
            )

            existing_resolved_sale = frappe.db.get_value(
                "FB Resolved Sale", {"resolved_sale_id": resolved_sale_id}, "name"
            )
            resolution_hash = self.build_resolution_hash(
                recipe_doc=recipe_doc,
                selected_modifiers=selected_modifiers,
                resolved_components=resolved_components,
            )
            if existing_resolved_sale:
                existing = frappe.get_doc(
                    "FB Resolved Sale",
                    existing_resolved_sale,
                )
                expected_identity = {
                    "fb_order": cstr(self.name),
                    "fb_order_line": cstr(line.name),
                    "backend_line_uuid": cstr(line.backend_line_uuid),
                    "sellable_item": cstr(line.item),
                    "booth_warehouse": cstr(self.booth_warehouse),
                    "recipe": cstr(recipe_doc.name),
                    "recipe_version": cstr(recipe_doc.version_no),
                    "resolution_hash": resolution_hash,
                }
                for fieldname, expected_value in expected_identity.items():
                    if cstr(getattr(existing, fieldname, None)) != expected_value:
                        frappe.throw(
                            "Prepared resolved sale {0} {1} does not match".format(
                                existing_resolved_sale,
                                fieldname,
                            ),
                            frappe.ValidationError,
                        )
                if _positive_integer_qty(
                    getattr(existing, "qty", None),
                    "Prepared resolved sale qty",
                ) != _positive_integer_qty(line.qty, "FB Order line qty"):
                    frappe.throw(
                        "Prepared resolved sale {0} qty does not match".format(
                            existing_resolved_sale
                        ),
                        frappe.ValidationError,
                    )
                line.resolved_sale = existing_resolved_sale
                line.resolved_components_snapshot = json.dumps(
                    resolved_components,
                    sort_keys=True,
                    default=str,
                )
                continue

            resolved_sale = frappe.new_doc("FB Resolved Sale")
            resolved_sale.resolved_sale_id = resolved_sale_id
            resolved_sale.fb_order = self.name
            resolved_sale.fb_order_line = line.name
            resolved_sale.backend_line_uuid = line.backend_line_uuid
            resolved_sale.sellable_item = line.item
            resolved_sale.qty = line.qty
            resolved_sale.booth_warehouse = self.booth_warehouse
            resolved_sale.recipe = recipe_doc.name
            resolved_sale.recipe_version = recipe_doc.version_no
            resolved_sale.status = (
                "Prepared"
                if cstr(
                    getattr(self, "accepted_sale_fingerprint", None)
                ).strip()
                else "Submitted"
            )
            resolved_sale.event_project = self.event_project
            resolved_sale.resolution_hash = resolution_hash

            for selected_modifier in selected_modifiers:
                selected_row = selected_modifier["row"]
                resolved_sale.append(
                    "selected_modifiers",
                    {
                        "modifier_group": selected_row.modifier_group,
                        "modifier": selected_row.modifier,
                        "price_adjustment": selected_row.price_adjustment,
                        "instruction_text": selected_row.instruction_text,
                        "sort_order": selected_row.sort_order,
                        "affects_stock": selected_row.affects_stock,
                        "affects_recipe": selected_row.affects_recipe,
                    },
                )

            for component in resolved_components:
                resolved_sale.append("resolved_components", component)

            resolved_sale.insert(ignore_permissions=True)
            line.resolved_sale = resolved_sale.name
            line.resolved_components_snapshot = json.dumps(
                resolved_components, sort_keys=True, default=str
            )

    def build_resolution_hash(
        self,
        recipe_doc: DocumentLike,
        selected_modifiers: list[dict[str, Any]],
        resolved_components: list[dict[str, Any]],
    ) -> str:
        return _resolution_hash(
            recipe=recipe_doc.name,
            recipe_version=recipe_doc.version_no,
            selected_modifiers=[
                {
                    "modifier_group": entry["row"].modifier_group,
                    "modifier": entry["row"].modifier,
                    "price_adjustment_sen": _money_sen(
                        _optional_money_value(entry["row"].price_adjustment),
                        "FB selected modifier price_adjustment",
                    ),
                }
                for entry in selected_modifiers
            ],
            resolved_components=_canonical_resolved_components(resolved_components),
        )

    def run_projection_service(
        self, candidate_paths: list[str], projection_label: str
    ) -> Any:
        last_lookup_error = None
        callable_service = None
        selected_path = None

        for candidate_path in candidate_paths:
            try:
                callable_service = frappe.get_attr(candidate_path)
                selected_path = candidate_path
                break
            except Exception as error:
                last_lookup_error = error

        if callable_service is None:
            raise frappe.ValidationError(
                "Unable to resolve {0} projection service. Tried: {1}. Last error: {2}".format(
                    projection_label,
                    ", ".join(candidate_paths),
                    last_lookup_error,
                )
            )

        try:
            return callable_service(self)
        except Exception as error:
            if projection_label == "invoice":
                self.invoice_status = "Failed"
            elif projection_label == "stock":
                self.stock_status = "Failed"
            raise frappe.ValidationError(
                "{0} projection service {1} failed for FB Order {2}: {3}".format(
                    projection_label.capitalize(), selected_path, self.name, error
                )
            ) from error

    def extract_target_name(self, projection_result: Any) -> str | None:
        if isinstance(projection_result, str):
            return projection_result
        if isinstance(projection_result, dict):
            for key in (
                "name",
                "target_name",
                "sales_invoice",
                "stock_entry",
                "ingredient_stock_entry",
            ):
                value = projection_result.get(key)
                if value:
                    return value
        return None

    def describe_line(self, line_index: int, line) -> str:
        return line.line_id or line.backend_line_uuid or str(line_index)
