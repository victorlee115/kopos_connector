from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any

frappe = import_module("frappe")
from kopos_connector.utils.diagnostics import log_sanitized_error


def get_available_stock(item_code: str, warehouse: str) -> Decimal:
    """Read a finite diagnostic quantity without creating a sale gate."""

    try:
        qty = frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
        )
        parsed = _finite_decimal(qty)
    except Exception as error:
        _log_diagnostic(
            f"Inventory shortfall stock read failed for {item_code} at {warehouse}",
            error,
        )
        return Decimal("0")
    if parsed is None:
        _log_diagnostic(
            f"Inventory shortfall stock value was invalid for {item_code} at {warehouse}",
            ValueError("actual_qty is not a finite decimal"),
        )
        return Decimal("0")
    return parsed


def detect_stock_shortfall(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    required_stock_by_bin: dict[tuple[str, str], Decimal] = defaultdict(Decimal)

    for component in components:
        if not isinstance(component, dict):
            _log_diagnostic(
                "Inventory shortfall component was not an object",
                ValueError("component is not a mapping"),
            )
            continue
        try:
            affects_stock = int(component.get("affects_stock") or 0)
        except (TypeError, ValueError):
            _log_diagnostic(
                "Inventory shortfall component had an invalid stock flag",
                ValueError("affects_stock is not an integer"),
            )
            continue
        if not affects_stock:
            continue

        warehouse = component.get("warehouse")
        item_code = component.get("item")
        needed = _finite_decimal(
            component.get("stock_qty_decimal")
            or component.get("stock_qty")
            or component.get("qty_decimal")
            or component.get("qty")
        )
        if not warehouse or not item_code or needed is None or needed <= 0:
            if warehouse and item_code and needed is None:
                _log_diagnostic(
                    f"Inventory shortfall component quantity was invalid for {item_code} at {warehouse}",
                    ValueError("component quantity is not a finite decimal"),
                )
            continue

        required_stock_by_bin[(str(item_code), str(warehouse))] += needed

    shortfalls: list[dict[str, Any]] = []
    for (item_code, warehouse), required_qty in required_stock_by_bin.items():
        available_qty = get_available_stock(item_code, warehouse)
        if available_qty + Decimal("0.0001") < required_qty:
            shortfalls.append(
                {
                    "item": item_code,
                    "item_code": item_code,
                    "warehouse": warehouse,
                    "available": available_qty,
                    "available_qty": available_qty,
                    "needed": required_qty,
                    "required_qty": required_qty,
                    "shortfall_qty": required_qty - available_qty,
                }
            )
    return shortfalls


def _finite_decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return parsed if parsed.is_finite() else None


def _log_diagnostic(message: str, error: Exception) -> None:
    try:
        log_sanitized_error(message, error)
    except Exception:
        # This is an optional diagnostic path. Logging failure must not turn
        # malformed stock evidence into a commercial order failure.
        return


def require_advisory_shortfall_policy(
    shortfalls: list[dict[str, Any]],
) -> None:
    """Retain the old callable without making it a commercial gate.

    Inventory stock is optional during sale registration.  Shortfalls are
    recorded as inventory exceptions by :func:`record_stock_shortfall_exceptions`
    and must never reject an FB Order because a site setting or Item type is
    unsuitable for negative stock.
    """

    del shortfalls


def log_stock_shortfall(
    fb_order: Any,
    shortfalls: list[dict[str, Any]],
    timestamp: Any | None = None,
) -> list[str]:
    """Compatibility alias for the exception-based diagnostic."""

    return record_stock_shortfall_exceptions(
        fb_order,
        shortfalls,
        timestamp=timestamp,
    )


def record_stock_shortfall_exceptions(
    fb_order: Any,
    shortfalls: list[dict[str, Any]],
    timestamp: Any | None = None,
) -> list[str]:
    """Record one durable inventory exception per Item/warehouse shortfall.

    Exception creation is best effort.  A missing or unhealthy optional
    diagnostic surface must not turn commercial order registration into an
    inventory failure.
    """

    if not shortfalls:
        return []

    exception_names: list[str] = []
    order_reference = _value(fb_order, "order_id") or _value(fb_order, "name")
    source_name = str(_value(fb_order, "name") or "").strip() or None
    try:
        if not frappe.db.exists("DocType", "FB Inventory Exception"):
            return []
        exceptions = import_module(
            "kopos_connector.kopos.services.inventory_autopilot.exceptions"
        )
    except Exception as error:
        _log_diagnostic(
            f"Inventory shortfall exception surface unavailable for FB Order {order_reference}",
            error,
        )
        return []

    for shortfall in shortfalls:
        item_code = str(shortfall.get("item_code") or shortfall.get("item") or "").strip()
        warehouse = str(shortfall.get("warehouse") or "").strip()
        if not item_code or not warehouse:
            continue
        try:
            exception_name = exceptions.upsert_inventory_exception(
                reason_code="inventory_stock_shortfall",
                summary=f"{item_code} is below the required stock level at {warehouse}",
                next_action=(
                    "Review the stock count and create or approve the required replenishment"
                ),
                severity="Warning",
                company=str(_value(fb_order, "company") or "").strip() or None,
                warehouse=warehouse,
                item=item_code,
                source_doctype="FB Order",
                source_name=source_name,
            )
        except Exception as error:
            _log_diagnostic(
                f"Inventory shortfall exception failed for FB Order {order_reference}",
                error,
            )
            continue
        if exception_name:
            exception_names.append(str(exception_name))

    del timestamp
    return exception_names


def _value(doc: Any, fieldname: str) -> Any:
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)

    getter = getattr(doc, "get", None)
    if callable(getter):
        return getter(fieldname)

    return None


def _is_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    try:
        return int(value or 0) == 1
    except (TypeError, ValueError):
        return False
