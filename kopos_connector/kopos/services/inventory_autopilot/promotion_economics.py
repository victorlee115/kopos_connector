"""Small, explainable promotion economics calculator.

This is intentionally reporting-only in v1. It does not estimate causal lift or
trigger purchasing; a director-entered scenario can only create a Review First
planning suggestion.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
import hashlib
import json
from collections.abc import Mapping
from typing import Any, Iterable


class PromotionEconomicsError(ValueError):
    pass


ALLOWED_SCENARIO_LABELS = frozenset({"low", "base", "high"})
MAX_SCENARIO_UNITS = 1_000_000


def normalize_scenarios(value: Any) -> dict[str, int]:
    """Validate the small director-entered planning scenario contract.

    Scenarios are deliberately bounded and are never used as an accounting or
    purchasing authority.  Keeping this validation next to the calculator
    prevents another caller from accidentally accepting arbitrary labels,
    fractional quantities, or unbounded values.
    """

    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise PromotionEconomicsError("scenarios must be an object")
    unknown = sorted(
        str(label)
        for label in value
        if str(label) not in ALLOWED_SCENARIO_LABELS
    )
    if unknown:
        raise PromotionEconomicsError(
            "scenarios may only contain low, base and high"
        )
    normalized: dict[str, int] = {}
    for label, raw_units in value.items():
        if isinstance(raw_units, bool):
            raise PromotionEconomicsError(
                f"scenarios.{label} must be a positive whole-number unit count"
            )
        try:
            units = Decimal(str(raw_units))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise PromotionEconomicsError(
                f"scenarios.{label} must be a positive whole-number unit count"
            ) from error
        if (
            not units.is_finite()
            or units != units.to_integral_value()
            or units <= 0
            or units > MAX_SCENARIO_UNITS
        ):
            raise PromotionEconomicsError(
                f"scenarios.{label} must be between 1 and {MAX_SCENARIO_UNITS} whole units"
            )
        normalized[str(label)] = int(units)
    return normalized


def economics_source_hash(source: Any) -> str:
    """Return a stable identity for the exact inputs used by an economics check."""

    payload = json.dumps(
        source,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def calculate_promotion_economics(
    *,
    items: Iterable[dict[str, Any]],
    scenarios: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = list(items)
    if not rows:
        raise PromotionEconomicsError("at least one promotion Item is required")
    revenue = Decimal("0")
    tax = Decimal("0")
    cogs = Decimal("0")
    baseline_revenue = Decimal("0")
    baseline_tax = Decimal("0")
    baseline_cogs = Decimal("0")
    ingredient_demand: dict[str, Decimal] = {}
    group_totals: dict[str, dict[str, Decimal]] = {}
    prepared_components: dict[str, dict[str, Any]] = {}
    risk_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        units = _positive(row.get("units"), f"items[{index}].units")
        price = _sen(row.get("promoted_price_sen"), f"items[{index}].promoted_price_sen")
        baseline_price = _sen(row.get("baseline_price_sen"), f"items[{index}].baseline_price_sen")
        net_price = _net_revenue_sen(row, price, f"items[{index}]")
        baseline_net_price = _baseline_net_revenue_sen(
            row, baseline_price, f"items[{index}]"
        )
        row_tax = _tax_sen(row, price, net_price, f"items[{index}]")
        baseline_row_tax = _baseline_tax_sen(
            row, baseline_price, baseline_net_price, f"items[{index}]"
        )
        cost_value = row.get("cogs_sen")
        if cost_value in (None, ""):
            raise PromotionEconomicsError(f"items[{index}].cogs_sen is required; stale or missing cost blocks publication")
        cost = _sen(cost_value, f"items[{index}].cogs_sen")
        revenue += units * net_price
        tax += units * row_tax
        cogs += units * cost
        baseline_revenue += units * baseline_net_price
        baseline_tax += units * baseline_row_tax
        baseline_cogs += units * cost
        group = str(row.get("item_group") or "Unassigned").strip() or "Unassigned"
        group_total = group_totals.setdefault(
            group,
            {"net_revenue": Decimal("0"), "cogs": Decimal("0"), "units": Decimal("0")},
        )
        group_total["net_revenue"] += units * net_price
        group_total["cogs"] += units * cost
        group_total["units"] += units
        for component in row.get("components", []) or []:
            if not isinstance(component, dict):
                raise PromotionEconomicsError(f"items[{index}].components contains an invalid row")
            item = str(component.get("item") or "").strip()
            qty = _positive(component.get("qty"), f"items[{index}].components.qty")
            if not item:
                raise PromotionEconomicsError(f"items[{index}].components.item is required")
            ingredient_demand[item] = ingredient_demand.get(item, Decimal("0")) + units * qty
            prepared = component.get("prepared")
            if isinstance(prepared, Mapping):
                prepared_row = prepared_components.setdefault(
                    item,
                    {
                        "item": item,
                        "bom": str(prepared.get("bom") or ""),
                        "batch_qty": prepared.get("batch_qty"),
                        "min_ready_qty": prepared.get("min_ready_qty"),
                        "lead_minutes": prepared.get("lead_minutes"),
                        "demand": Decimal("0"),
                        "per_unit_demand": Decimal("0"),
                    },
                )
                prepared_row["demand"] += units * qty
                prepared_row["per_unit_demand"] += qty
            inventory = component.get("inventory")
            if isinstance(inventory, Mapping):
                risk_rows.append(
                    {
                        "item": item,
                        "demand": units * qty,
                        "inventory": inventory,
                    }
                )
    gross_profit = revenue - cogs
    baseline_profit = baseline_revenue - baseline_cogs
    margin = _ratio(gross_profit, revenue)
    baseline_margin = _ratio(baseline_profit, baseline_revenue)
    scenarios_result: list[dict[str, int]] = []
    for label, raw_units in (scenarios or {}).items():
        units = _positive(raw_units, f"scenarios.{label}")
        scenario_revenue = sum(
            units * _net_revenue_sen(
                row,
                _sen(row.get("promoted_price_sen"), "promoted_price_sen"),
                "promoted_price_sen",
            )
            for row in rows
        )
        scenario_tax = sum(
            units
            * _tax_sen(
                row,
                _sen(row.get("promoted_price_sen"), "promoted_price_sen"),
                _net_revenue_sen(
                    row,
                    _sen(row.get("promoted_price_sen"), "promoted_price_sen"),
                    "promoted_price_sen",
                ),
                "promoted_price_sen",
            )
            for row in rows
        )
        scenario_cogs = sum(
            units * _sen(row.get("cogs_sen"), "cogs_sen") for row in rows
        )
        scenarios_result.append({
            "label": str(label),
            "units": _int(units),
            "revenue_sen": _int(scenario_revenue),
            "net_revenue_sen": _int(scenario_revenue),
            "tax_sen": _int(scenario_tax),
            "cogs_sen": _int(scenario_cogs),
            "gross_profit_sen": _int(scenario_revenue - scenario_cogs),
        })
    break_even_units = (baseline_profit - gross_profit) / (gross_profit / sum(_positive(row.get("units"), "units") for row in rows)) if gross_profit > 0 else None
    batch_impact = _batch_preparation_impact(prepared_components, scenarios or {})
    risk_summary = _runout_waste_risk(risk_rows, ingredient_demand)
    group_rows = [
        {
            "item_group": group,
            "units": _int(values["units"]),
            "net_revenue_sen": _int(values["net_revenue"]),
            "cogs_sen": _int(values["cogs"]),
            "gross_profit_sen": _int(values["net_revenue"] - values["cogs"]),
        }
        for group, values in sorted(group_totals.items())
    ]
    worst_group = (
        max(group_rows, key=lambda row: (row["cogs_sen"], row["item_group"]))
        if group_rows
        else None
    )
    return {
        "revenue_sen": _int(revenue),
        "net_revenue_sen": _int(revenue),
        "tax_sen": _int(tax),
        "cogs_sen": _int(cogs),
        "gross_profit_sen": _int(gross_profit),
        "margin_percent": _percent(margin),
        "baseline_gross_profit_sen": _int(baseline_profit),
        "baseline_margin_percent": _percent(baseline_margin),
        "margin_change_percent": _percent(margin - baseline_margin),
        "break_even_additional_units": None if break_even_units is None else max(0, _int(break_even_units)),
        "ingredient_demand": {key: _plain_decimal(value) for key, value in sorted(ingredient_demand.items())},
        "batch_preparation_impact": batch_impact,
        "runout_waste_risk": risk_summary,
        "item_groups": group_rows,
        "worst_affected_item_group": worst_group,
        "actual_results": {
            "status": "not_available",
            "reason": "promotion_actual_attribution_is_not_part_of_this_calculation",
        },
        "scenarios": scenarios_result,
        "planning_mode": "Review First",
    }


def summarize_actual_promotion_results(
    *,
    records: Iterable[Mapping[str, Any]],
    promotion_id: str,
) -> dict[str, Any]:
    """Summarize immutable sale evidence already attributed to a promotion.

    FB Order promotion payloads are the only current attribution authority. The
    summary reports only the promoted share of each attributed line's net
    revenue and discount evidence together with COGS prepared from submitted
    Material Issue valuation. It never
    fills a missing cost with zero or with a selling price. A promotion can
    therefore have attributed sales while its actual profit remains visibly
    unavailable until every post-cutover ingredient issue has authoritative ERP
    valuation evidence.
    """

    record_rows = [record for record in records if isinstance(record, Mapping)]
    target = str(promotion_id or "").strip()
    if not target:
        return {"status": "not_available", "reason": "promotion_id_required"}
    order_count = 0
    promoted_units = 0
    net_revenue = Decimal("0")
    tax = Decimal("0")
    discount = Decimal("0")
    matched_evidence = 0
    revenue_complete = True
    revenue_reasons: list[str] = []
    tax_complete = True
    tax_reasons: list[str] = []
    for record in record_rows:
        payload = record.get("promotion_payload") if isinstance(record, Mapping) else None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                continue
        if not isinstance(payload, Mapping):
            continue
        applied = [
            row
            for row in (payload.get("applied_promotions") or [])
            if isinstance(row, Mapping) and str(row.get("promotion_id") or "").strip() == target
        ]
        if not applied:
            continue
        order_count += 1
        matched_evidence += 1
        revenue_payload = record.get("actual_revenue_payload") or payload
        if record.get("actual_revenue_reason"):
            revenue_complete = False
            reason = str(record.get("actual_revenue_reason")).strip()
            if reason and reason not in revenue_reasons:
                revenue_reasons.append(reason)
        else:
            revenue_evidence = _promoted_revenue_from_payload(
                revenue_payload,
                target,
            )
            if revenue_evidence["status"] != "available":
                revenue_complete = False
                reason = str(revenue_evidence.get("reason") or "promoted line revenue evidence is unavailable")
                if reason not in revenue_reasons:
                    revenue_reasons.append(reason)
            else:
                net_revenue += revenue_evidence["net_revenue"]
                promoted_units += int(revenue_evidence["promoted_units"])
                raw_tax_rate = record.get("tax_rate")
                if raw_tax_rate in (None, ""):
                    tax_complete = False
                    reason = "post-promotion tax-rate evidence is missing"
                    if reason not in tax_reasons:
                        tax_reasons.append(reason)
                else:
                    try:
                        tax_rate = _tax_rate(raw_tax_rate, "actual promotion tax rate")
                    except PromotionEconomicsError as error:
                        tax_complete = False
                        reason = str(error)
                        if reason not in tax_reasons:
                            tax_reasons.append(reason)
                    else:
                        tax += revenue_evidence["net_revenue"] * tax_rate
        discount += sum(
            (_optional_sen(row.get("amount_sen")) for row in applied),
            Decimal("0"),
        )
    cost_total = Decimal("0")
    cost_reasons: list[str] = []
    cost_complete = True
    for record in record_rows:
        if not isinstance(record, Mapping):
            continue
        payload = record.get("promotion_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                continue
        if not isinstance(payload, Mapping):
            continue
        applied = [
            row
            for row in (payload.get("applied_promotions") or [])
            if isinstance(row, Mapping) and str(row.get("promotion_id") or "").strip() == target
        ]
        if not applied:
            continue
        actual_status = str(record.get("actual_cogs_status") or "").strip()
        actual_value = record.get("actual_cogs_sen")
        if actual_status != "available" or actual_value in (None, ""):
            cost_complete = False
            reason = str(
                record.get("actual_cogs_reason")
                or "post-cutover ingredient consumption or ERP valuation is unavailable"
            ).strip()
            if reason and reason not in cost_reasons:
                cost_reasons.append(reason)
            continue
        try:
            cost_total += _sen(actual_value, "actual promotion cogs")
        except PromotionEconomicsError as error:
            cost_complete = False
            reason = str(error)
            if reason not in cost_reasons:
                cost_reasons.append(reason)

    if not matched_evidence:
        return {
            "status": "not_available",
            "reason": "no_post_promotion_orders_with_attribution",
        }
    metrics_complete = revenue_complete and cost_complete
    actual_status = "available" if metrics_complete else "not_available"
    actual_reason = None
    gross_profit: int | None = None
    margin: str | None = None
    if metrics_complete:
        gross_profit_value = net_revenue - cost_total
        gross_profit = _int(gross_profit_value)
        margin = _percent(_ratio(gross_profit_value, net_revenue))
    else:
        actual_reasons = revenue_reasons + cost_reasons
        actual_reason = "; ".join(actual_reasons) or "actual promotion economics evidence is incomplete"
    tax_reason = "; ".join(tax_reasons) if not tax_complete else None
    return {
        "status": actual_status,
        "attribution_status": "available",
        "attribution": "order_level_net_revenue",
        "order_count": order_count,
        "promoted_units": promoted_units or None,
        "net_revenue_sen": _int(net_revenue) if revenue_complete else None,
        "tax_sen": _int(tax) if revenue_complete and tax_complete else None,
        "tax_status": "available" if revenue_complete and tax_complete else "not_available",
        "tax_reason": tax_reason,
        "discount_sen": _int(discount),
        "cogs_sen": None if not metrics_complete else _int(cost_total),
        "gross_profit_sen": gross_profit,
        "margin_percent": margin,
        "cogs_source": (
            "submitted Material Issue Stock Entry valuation"
            if metrics_complete
            else None
        ),
        "reason": actual_reason,
        "note": (
            "Actual COGS is allocated from submitted post-cutover Material Issue valuation."
            if metrics_complete
            else "Actual COGS, gross profit and margin are unavailable until the listed ERP evidence is complete."
        ),
    }


def _promoted_revenue_from_payload(
    payload: Any,
    promotion_id: str,
) -> dict[str, Any]:
    """Return only the exact promoted share of each attributed order line."""

    if not isinstance(payload, Mapping):
        return {
            "status": "not_available",
            "reason": "promotion line revenue evidence is missing",
        }
    net_revenue = Decimal("0")
    promoted_units = Decimal("0")
    matched_line = False
    for item in payload.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        target_allocations = [
            allocation
            for allocation in (item.get("promotion_allocations") or [])
            if isinstance(allocation, Mapping)
            and str(allocation.get("promotion_id") or "").strip() == promotion_id
        ]
        if not target_allocations:
            continue
        matched_line = True
        line_id = str(item.get("line_id") or "").strip()
        if not line_id:
            return {
                "status": "not_available",
                "reason": "promoted line revenue evidence is missing its line identity",
            }
        for fieldname in ("qty", "unit_price_sen", "line_total_sen"):
            if item.get(fieldname) in (None, ""):
                return {
                    "status": "not_available",
                    "reason": f"promoted line {line_id} revenue evidence is missing {fieldname}",
                }
        try:
            quantity = _whole_positive_quantity(item.get("qty"), f"promoted line {line_id} quantity")
            unit_price = _sen(item.get("unit_price_sen"), f"promoted line {line_id} unit price")
            line_total = _sen(item.get("line_total_sen"), f"promoted line {line_id} total")
        except PromotionEconomicsError as error:
            return {"status": "not_available", "reason": str(error)}
        allocation_quantity = Decimal("0")
        for allocation in target_allocations:
            try:
                allocation_quantity += _whole_positive_quantity(
                    allocation.get("quantity"),
                    f"promotion allocation on line {line_id}",
                )
            except PromotionEconomicsError as error:
                return {"status": "not_available", "reason": str(error)}
        if allocation_quantity > quantity:
            return {
                "status": "not_available",
                "reason": f"promotion quantity exceeds line quantity for {line_id}",
            }
        # Validate the unit price even though line_total is the amount authority;
        # it proves that the payload contains a complete sale-time price snapshot.
        if unit_price < 0 or line_total < 0:
            return {
                "status": "not_available",
                "reason": f"promoted line {line_id} revenue evidence is invalid",
            }
        net_revenue += line_total * allocation_quantity / quantity
        promoted_units += allocation_quantity
    if not matched_line:
        return {
            "status": "not_available",
            "reason": "promotion line revenue evidence is missing",
        }
    return {
        "status": "available",
        "net_revenue": net_revenue,
        "promoted_units": _int(promoted_units),
    }


def _whole_positive_quantity(value: Any, label: str) -> Decimal:
    quantity = _positive_quantity(value, label)
    if quantity != quantity.to_integral_value():
        raise PromotionEconomicsError(f"{label} must be a positive whole-number quantity")
    return quantity


def _tax_rate(value: Any, label: str) -> Decimal:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PromotionEconomicsError(f"{label} is missing or invalid") from error
    if not rate.is_finite() or rate < 0 or rate > 1:
        raise PromotionEconomicsError(f"{label} is missing or invalid")
    return rate


def calculate_actual_cogs_from_stock_entries(
    *,
    resolved_sales: Iterable[Mapping[str, Any]],
    stock_entries: Mapping[str, Mapping[str, Any]],
    promoted_line_quantities: Mapping[str, Any],
) -> dict[str, Any]:
    """Allocate submitted Material Issue valuation to promoted resolved sales.

    ``FB Resolved Sale`` freezes the ingredient vector, while ERPNext's submitted
    Material Issue rows own the actual valuation. A single order can contain
    promoted and non-promoted lines and the worker groups their ingredients into
    one Stock Entry, so each submitted row is allocated by frozen component
    quantity. Arithmetic remains Decimal until final sen rounding. Missing,
    invalid, or mismatched valuation evidence fails closed.

    Inputs are plain mappings so this invariant can be tested without a live
    Frappe site. The API adapter converts ERP currency fields to Decimal sen
    values before calling this function; the response is rounded to integer sen.
    """

    sales = [sale for sale in resolved_sales if isinstance(sale, Mapping)]
    promoted: dict[str, Decimal] = {}
    for line_id, raw_quantity in promoted_line_quantities.items():
        line = str(line_id or "").strip()
        if not line:
            return {"status": "not_available", "reason": "promotion line identity is missing"}
        try:
            quantity = _positive_quantity(raw_quantity, f"promoted quantity {line}")
        except PromotionEconomicsError as error:
            return {"status": "not_available", "reason": str(error)}
        promoted[line] = promoted.get(line, Decimal("0")) + quantity

    if not promoted:
        return {"status": "not_available", "reason": "promoted resolved-sale lines are missing"}

    sales_by_line = {
        str(sale.get("line_id") or "").strip(): sale
        for sale in sales
        if str(sale.get("line_id") or "").strip()
    }
    missing_lines = sorted(set(promoted) - set(sales_by_line))
    if missing_lines:
        return {
            "status": "not_available",
            "reason": "resolved ingredient consumption is missing for line(s): "
            + ", ".join(missing_lines[:8]),
        }

    expected_by_entry: dict[str, dict[tuple[str, str, str], Decimal]] = {}
    promoted_by_entry: dict[str, dict[tuple[str, str, str], Decimal]] = {}
    promoted_component_found = False
    for sale in sales:
        entry_name = str(sale.get("stock_entry") or "").strip()
        line_id = str(sale.get("line_id") or "").strip()
        if not entry_name:
            if line_id in promoted:
                return {
                    "status": "not_available",
                    "reason": "post-cutover ingredient Stock Entry is not posted",
                }
            continue
        expected = expected_by_entry.setdefault(entry_name, {})
        promoted_expected = promoted_by_entry.setdefault(entry_name, {})
        try:
            sale_qty = _positive_quantity(
                sale.get("qty"), f"resolved sale {line_id} quantity"
            )
        except PromotionEconomicsError as error:
            return {"status": "not_available", "reason": str(error)}
        promoted_qty = promoted.get(line_id, Decimal("0"))
        if promoted_qty > sale_qty:
            return {
                "status": "not_available",
                "reason": f"promotion quantity exceeds resolved sale quantity for line {line_id}",
            }
        promotion_ratio = promoted_qty / sale_qty if promoted_qty else Decimal("0")
        for component in sale.get("components") or []:
            if not isinstance(component, Mapping):
                return {"status": "not_available", "reason": "resolved component evidence is invalid"}
            if not int(component.get("affects_cogs") or 0):
                continue
            if not int(component.get("affects_stock") or 0):
                return {
                    "status": "not_available",
                    "reason": "a COGS component has no submitted stock consumption",
                }
            item = str(component.get("item") or "").strip()
            warehouse = str(component.get("warehouse") or sale.get("warehouse") or "").strip()
            stock_uom = str(component.get("stock_uom") or component.get("uom") or "").strip()
            if not item or not warehouse or not stock_uom:
                return {
                    "status": "not_available",
                    "reason": "resolved COGS component is missing Item, warehouse or stock UOM",
                }
            try:
                quantity = _positive_quantity(
                    component.get("stock_qty_decimal")
                    or component.get("stock_qty")
                    or component.get("qty_decimal")
                    or component.get("qty"),
                    f"resolved component {item} quantity",
                )
            except PromotionEconomicsError as error:
                return {"status": "not_available", "reason": str(error)}
            key = (item, warehouse, stock_uom)
            expected[key] = expected.get(key, Decimal("0")) + quantity
            if promotion_ratio:
                promoted_component_found = True
                promoted_expected[key] = promoted_expected.get(key, Decimal("0")) + quantity * promotion_ratio

    if not promoted_component_found:
        return {
            "status": "not_available",
            "reason": "post-cutover COGS component evidence is missing",
        }

    total_cogs_sen = Decimal("0")
    for entry_name, expected_rows in expected_by_entry.items():
        entry = stock_entries.get(entry_name)
        promoted_rows = promoted_by_entry.get(entry_name, {})
        if not isinstance(entry, Mapping):
            if promoted_rows:
                return {
                    "status": "not_available",
                    "reason": f"submitted Stock Entry {entry_name} is unavailable",
                }
            continue
        if int(entry.get("docstatus") or 0) != 1 or str(
            entry.get("stock_entry_type") or entry.get("purpose") or ""
        ).strip() != "Material Issue":
            if promoted_rows:
                return {
                    "status": "not_available",
                    "reason": f"Stock Entry {entry_name} is not a submitted Material Issue",
                }
            continue
        actual_rows: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        for row in entry.get("items") or []:
            if not isinstance(row, Mapping):
                continue
            key = (
                str(row.get("item_code") or "").strip(),
                str(row.get("s_warehouse") or "").strip(),
                str(row.get("stock_uom") or row.get("uom") or "").strip(),
            )
            if key in actual_rows:
                return {
                    "status": "not_available",
                    "reason": f"Stock Entry {entry_name} contains duplicate valuation rows",
                }
            actual_rows[key] = row
        for key, expected_quantity in expected_rows.items():
            row = actual_rows.get(key)
            promoted_quantity = promoted_rows.get(key, Decimal("0"))
            if row is None:
                if promoted_quantity:
                    return {
                        "status": "not_available",
                        "reason": f"Stock Entry {entry_name} is missing valuation for {key[0]}",
                    }
                continue
            try:
                actual_quantity = _positive_quantity(
                    row.get("qty") or row.get("transfer_qty"),
                    f"Stock Entry {entry_name} {key[0]} quantity",
                )
            except PromotionEconomicsError as error:
                if promoted_quantity:
                    return {"status": "not_available", "reason": str(error)}
                continue
            if actual_quantity != expected_quantity and promoted_quantity:
                return {
                    "status": "not_available",
                    "reason": f"Stock Entry {entry_name} quantity does not match frozen consumption for {key[0]}",
                }
            if not promoted_quantity:
                continue
            raw_amount = row.get("basic_amount_sen")
            if raw_amount in (None, ""):
                raw_rate = row.get("basic_rate_sen")
                if raw_rate in (None, ""):
                    return {
                        "status": "not_available",
                        "reason": f"Stock Entry {entry_name} has no authoritative valuation for {key[0]}",
                    }
                try:
                    actual_amount = actual_quantity * _decimal_sen(
                        raw_rate, f"Stock Entry {entry_name} {key[0]} valuation"
                    )
                except PromotionEconomicsError as error:
                    return {"status": "not_available", "reason": str(error)}
            else:
                try:
                    actual_amount = _decimal_sen(
                        raw_amount, f"Stock Entry {entry_name} {key[0]} valuation"
                    )
                except PromotionEconomicsError as error:
                    return {"status": "not_available", "reason": str(error)}
                if actual_amount == 0 and row.get("basic_rate_sen") in (None, ""):
                    return {
                        "status": "not_available",
                        "reason": f"Stock Entry {entry_name} has no authoritative valuation for {key[0]}",
                    }
            total_cogs_sen += actual_amount * promoted_quantity / actual_quantity

    return {
        "status": "available",
        "cogs_sen": _int(total_cogs_sen),
        "source": "submitted Material Issue Stock Entry valuation",
    }


def _net_revenue_sen(row: Mapping[str, Any], gross_price: Decimal, label: str) -> Decimal:
    value = row.get("net_revenue_sen")
    if value not in (None, ""):
        return _sen(value, f"{label}.net_revenue_sen")
    return gross_price


def _baseline_net_revenue_sen(row: Mapping[str, Any], gross_price: Decimal, label: str) -> Decimal:
    value = row.get("baseline_net_revenue_sen")
    if value not in (None, ""):
        return _sen(value, f"{label}.baseline_net_revenue_sen")
    return gross_price


def _tax_sen(row: Mapping[str, Any], gross_price: Decimal, net_price: Decimal, label: str) -> Decimal:
    value = row.get("tax_sen")
    if value not in (None, ""):
        return _sen(value, f"{label}.tax_sen")
    return max(Decimal("0"), gross_price - net_price)


def _baseline_tax_sen(row: Mapping[str, Any], gross_price: Decimal, net_price: Decimal, label: str) -> Decimal:
    value = row.get("baseline_tax_sen")
    if value not in (None, ""):
        return _sen(value, f"{label}.baseline_tax_sen")
    return max(Decimal("0"), gross_price - net_price)


def _batch_preparation_impact(
    rows: Mapping[str, Mapping[str, Any]], scenarios: Mapping[str, Any]
) -> dict[str, Any]:
    if not rows:
        return {"status": "not_applicable", "components": []}
    components: list[dict[str, Any]] = []
    for item, value in sorted(rows.items()):
        raw_batch = value.get("batch_qty")
        if raw_batch in (None, ""):
            components.append({"item": item, "status": "not_available", "reason": "prepared_batch_qty_missing"})
            continue
        batch_qty = _positive_quantity(raw_batch, f"prepared.{item}.batch_qty")
        demand = value.get("demand") or Decimal("0")
        per_unit_demand = value.get("per_unit_demand") or Decimal("0")
        scenario_batches = {
            str(label): _int(
                (_positive_quantity(raw_units, f"scenarios.{label}") * per_unit_demand / batch_qty)
                .to_integral_value(rounding=ROUND_CEILING)
            )
            for label, raw_units in scenarios.items()
        }
        components.append(
            {
                "item": item,
                "status": "available",
                "bom": str(value.get("bom") or ""),
                "base_demand": _plain_decimal(demand),
                "batch_qty": _plain_decimal(batch_qty),
                "batches_required": _int((demand / batch_qty).to_integral_value(rounding=ROUND_CEILING)),
                "scenario_batches": scenario_batches,
                "min_ready_qty": value.get("min_ready_qty"),
                "lead_minutes": value.get("lead_minutes"),
            }
        )
    return {"status": "available", "components": components}


def _runout_waste_risk(
    rows: Iterable[Mapping[str, Any]], ingredient_demand: Mapping[str, Decimal]
) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for row in rows:
        inventory = row.get("inventory")
        if not isinstance(inventory, Mapping):
            continue
        item = str(row.get("item") or "").strip()
        demand = ingredient_demand.get(item, Decimal("0"))
        raw_stock = inventory.get("usable_stock")
        if raw_stock in (None, ""):
            values.append({"item": item, "status": "not_available", "reason": "usable_stock_missing"})
            continue
        stock = _non_negative_quantity(raw_stock, f"inventory.{item}.usable_stock")
        runout = "risk" if stock < demand else "no_immediate_runout"
        waste = "not_available"
        waste_reason = "shelf_life_or_expiry_evidence_missing"
        days_to_expiry = inventory.get("days_to_expiry")
        daily_demand = inventory.get("daily_forecast_demand")
        if days_to_expiry not in (None, "") and daily_demand not in (None, ""):
            expected_use = _non_negative_quantity(daily_demand, f"inventory.{item}.daily_forecast_demand") * _non_negative_quantity(days_to_expiry, f"inventory.{item}.days_to_expiry")
            waste = "risk" if stock > expected_use else "no_waste_evidence"
            waste_reason = None
        values.append({"item": item, "runout": runout, "waste": waste, "reason": waste_reason})
    if not values:
        return {"status": "not_available", "reason": "stock_and_expiry_evidence_missing", "items": []}
    return {"status": "available", "items": values}


def _positive_quantity(value: Any, label: str) -> Decimal:
    result = _non_negative_quantity(value, label)
    if result <= 0:
        raise PromotionEconomicsError(f"{label} must be greater than zero")
    return result


def _non_negative_quantity(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PromotionEconomicsError(f"{label} must be a finite decimal") from error
    if not result.is_finite() or result < 0:
        raise PromotionEconomicsError(f"{label} must be a finite decimal")
    return result


def _optional_sen(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return _sen(value, "actual promotion amount")


def _sen(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PromotionEconomicsError(f"{label} must be an integer sen value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise PromotionEconomicsError(f"{label} must be an integer sen value") from error
    if not result.is_finite() or result != result.to_integral_value() or result < 0:
        raise PromotionEconomicsError(f"{label} must be a non-negative integer sen value")
    return result


def _decimal_sen(value: Any, label: str) -> Decimal:
    """Parse a non-negative sen amount without rounding before aggregation."""

    if isinstance(value, bool) or value is None:
        raise PromotionEconomicsError(f"{label} must be a finite sen amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise PromotionEconomicsError(f"{label} must be a finite sen amount") from error
    if not result.is_finite() or result < 0:
        raise PromotionEconomicsError(f"{label} must be a finite sen amount")
    return result


def _positive(value: Any, label: str) -> Decimal:
    result = _sen(value, label)
    if result <= 0:
        raise PromotionEconomicsError(f"{label} must be greater than zero")
    return result


def _int(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator if denominator else Decimal("0")


def _percent(value: Decimal) -> str:
    return str((value * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _plain_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
