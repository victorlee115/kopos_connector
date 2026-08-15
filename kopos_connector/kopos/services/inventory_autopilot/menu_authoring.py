"""Small, deterministic CSV preflight for director recipe commissioning.

The importer deliberately does not create documents.  Standard Item, FB Recipe,
and BOM forms remain the authorities; this helper only turns a spreadsheet into
row-level errors before a director enters or imports the reviewed values.
"""

from __future__ import annotations

import csv
import io
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


CSV_HEADERS = (
    "recipe_code",
    "recipe_name",
    "sellable_item",
    "company",
    "recipe_type",
    "yield_qty",
    "yield_uom",
    "default_serving_qty",
    "default_serving_uom",
    "component_item",
    "component_type",
    "component_qty",
    "component_uom",
    "stock_qty",
    "stock_uom",
    "affects_stock",
    "affects_cogs",
)

MAX_CSV_BYTES = 512 * 1024
MAX_CSV_ROWS = 5_000
RECIPE_TYPES = {"Finished Drink", "Add-On", "Prep Batch", "Packaging Assembly"}
COMPONENT_TYPES = {"Ingredient", "Prep Item", "Packaging", "Tool Usage"}
CANONICAL_RECIPE_HASH = re.compile(r"^[0-9a-f]{64}$")
MAX_GUIDED_COMPONENTS = 100
MAX_GUIDED_MODIFIER_GROUPS = 30
MAX_GUIDED_MODIFIER_EFFECTS = 200


def csv_template() -> str:
    """Return the stable header row used by the director spreadsheet template."""

    return ",".join(CSV_HEADERS) + "\n"


def draft_recipe_code(*, sellable_item: str, company_abbr: str, version_no: int) -> str:
    """Return a short, stable recipe code for the required-first Draft flow."""

    suffix = f"-{company_abbr or 'COMPANY'}-v{version_no}"
    return f"{sellable_item[: max(1, 140 - len(suffix))]}{suffix}"


def validate_recipe_csv(csv_text: str) -> dict[str, Any]:
    """Validate a recipe/component CSV without reading or writing ERP state."""

    if not isinstance(csv_text, str):
        return {"valid": False, "recipes": [], "errors": [{"row": 0, "message": "CSV content must be text"}]}
    if len(csv_text.encode("utf-8")) > MAX_CSV_BYTES:
        return {"valid": False, "recipes": [], "errors": [{"row": 0, "message": "CSV is larger than 512 KB"}]}
    if not csv_text.strip():
        return {"valid": False, "recipes": [], "errors": [{"row": 0, "message": "CSV is empty"}]}

    reader = csv.DictReader(io.StringIO(csv_text, newline=""))
    headers = tuple((header or "").strip() for header in (reader.fieldnames or ()))
    if headers != CSV_HEADERS:
        return {
            "valid": False,
            "recipes": [],
            "errors": [{"row": 1, "message": "Header must exactly match the JiJi recipe template", "expected": list(CSV_HEADERS), "received": list(headers)}],
        }

    errors: list[dict[str, Any]] = []
    grouped: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    row_count = 0
    for row_number, raw_row in enumerate(reader, start=2):
        if row_count >= MAX_CSV_ROWS:
            errors.append({"row": row_number, "message": "CSV contains more than 5,000 data rows"})
            break
        row_count += 1
        row = {key: str(raw_row.get(key) or "").strip() for key in CSV_HEADERS}
        if not any(row.values()):
            continue
        recipe_code = row["recipe_code"]
        if not recipe_code:
            errors.append(_error(row_number, "recipe_code is required"))
            continue
        recipe = grouped.setdefault(recipe_code, {"recipe_code": recipe_code, "components": [], "row": row_number})
        for field in ("recipe_name", "sellable_item", "company", "yield_uom", "default_serving_uom"):
            _require_value(errors, row_number, row[field], field)
        _require_decimal(errors, row_number, row["yield_qty"], "yield_qty")
        _require_decimal(errors, row_number, row["default_serving_qty"], "default_serving_qty")
        if row["recipe_type"] not in RECIPE_TYPES:
            errors.append(_error(row_number, "recipe_type must be one of the template values"))
        for field in ("component_item", "component_uom"):
            _require_value(errors, row_number, row[field], field)
        if row["component_type"] not in COMPONENT_TYPES:
            errors.append(_error(row_number, "component_type must be one of the template values"))
        _require_decimal(errors, row_number, row["component_qty"], "component_qty")
        if row["stock_qty"]:
            _require_decimal(errors, row_number, row["stock_qty"], "stock_qty")
        if row["affects_stock"] not in {"", "0", "1", "false", "true", "False", "True"}:
            errors.append(_error(row_number, "affects_stock must be 0, 1, true or false"))
        if row["affects_cogs"] not in {"", "0", "1", "false", "true", "False", "True"}:
            errors.append(_error(row_number, "affects_cogs must be 0, 1, true or false"))

        recipe_fields = {field: row[field] for field in CSV_HEADERS[:9]}
        previous = recipe.get("recipe_fields")
        if previous is None:
            recipe["recipe_fields"] = recipe_fields
        elif previous != recipe_fields:
            errors.append(_error(row_number, "recipe-level fields must be identical on every component row"))
        recipe["components"].append({field: row[field] for field in CSV_HEADERS[9:]})

    for code, recipe in grouped.items():
        if not recipe["components"]:
            errors.append(_error(recipe["row"], f"recipe {code} has no component rows"))
        recipe.pop("row", None)
        recipe.update(recipe.pop("recipe_fields", {}))
    recipes = list(grouped.values())
    return {"valid": not errors and bool(recipes), "recipes": recipes, "errors": errors}


def build_guided_recipe_preview(
    *,
    yield_qty: Any,
    yield_uom: str,
    default_serving_qty: Any,
    default_serving_uom: str,
    components: list[Mapping[str, Any]],
    item_details: Mapping[str, Mapping[str, Any]],
    modifier_effects: list[Mapping[str, Any]] | None = None,
    prepared_components: Mapping[str, Mapping[str, Any]] | None = None,
    yield_conversion_factor: Any = 1,
    serving_conversion_factor: Any = 1,
    require_prepared_bom: bool = True,
) -> dict[str, Any]:
    """Build the director-only recipe preview from ERP-resolved values.

    The browser supplies measured entered quantities, but it never supplies a
    stock conversion or valuation.  The API resolves those values from ERP and
    passes this small pure function the resulting snapshot.  Quantities stay
    Decimal strings so the preview and the published recipe use the same
    arithmetic without introducing a second quantity authority.
    """

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    prepared_lookup = prepared_components or {}
    if not components:
        errors.append(_guided_error("components", "Add at least one measured component row"))
    if len(components) > MAX_GUIDED_COMPONENTS:
        errors.append(
            _guided_error(
                "components", f"A recipe can contain at most {MAX_GUIDED_COMPONENTS} rows"
            )
        )
    if len(modifier_effects or []) > MAX_GUIDED_MODIFIER_EFFECTS:
        errors.append(
            _guided_error(
                "modifier_effects",
                f"A recipe can show at most {MAX_GUIDED_MODIFIER_EFFECTS} modifier effects",
            )
        )

    try:
        parsed_yield = _guided_positive_decimal(yield_qty, "yield_qty")
        parsed_serving = _guided_positive_decimal(
            default_serving_qty, "default_serving_qty"
        )
        parsed_yield_factor = _guided_positive_decimal(
            yield_conversion_factor, "yield_conversion_factor"
        )
        parsed_serving_factor = _guided_positive_decimal(
            serving_conversion_factor, "serving_conversion_factor"
        )
    except ValueError as error:
        errors.append(_guided_error("yield", str(error)))
        parsed_yield = Decimal("1")
        parsed_serving = Decimal("1")
        parsed_yield_factor = Decimal("1")
        parsed_serving_factor = Decimal("1")

    yield_name = str(yield_uom or "").strip()
    serving_name = str(default_serving_uom or "").strip()
    if not yield_name:
        errors.append(_guided_error("yield_uom", "Yield UOM is required"))
    if not serving_name:
        errors.append(_guided_error("default_serving_uom", "Serving UOM is required"))
    output_ratio = (
        parsed_serving * parsed_serving_factor
    ) / (parsed_yield * parsed_yield_factor)
    preview_components: list[dict[str, Any]] = []
    total_cost = Decimal("0")
    cost_complete = True

    for row_number, raw_row in enumerate(components, start=1):
        if not isinstance(raw_row, Mapping):
            errors.append(_guided_error(f"components[{row_number}]", "Component row must be an object"))
            continue
        item = str(raw_row.get("item") or "").strip()
        component_type = str(raw_row.get("component_type") or "Ingredient").strip()
        entered_uom = str(raw_row.get("uom") or "").strip()
        if not item:
            errors.append(_guided_error(f"components[{row_number}].item", "Item is required"))
            continue
        if not entered_uom:
            errors.append(_guided_error(f"components[{row_number}].uom", "UOM is required"))
            continue
        try:
            entered_qty = _guided_positive_decimal(
                raw_row.get("qty"), f"components[{row_number}].qty"
            )
            loss_factor = _guided_non_negative_decimal(
                raw_row.get("loss_factor_pct", 0),
                f"components[{row_number}].loss_factor_pct",
            )
        except ValueError as error:
            errors.append(_guided_error(f"components[{row_number}]", str(error)))
            continue
        if loss_factor > Decimal("100"):
            errors.append(
                _guided_error(
                    f"components[{row_number}].loss_factor_pct",
                    "Loss factor cannot exceed 100%",
                )
            )
            continue

        detail = item_details.get(item)
        if not detail:
            errors.append(_guided_error(f"components[{row_number}].item", f"Item {item} was not found"))
            continue
        stock_uom = str(detail.get("stock_uom") or "").strip()
        affects_stock = _guided_truthy(raw_row.get("affects_stock", True))
        affects_cogs = _guided_truthy(raw_row.get("affects_cogs", True))
        conversion = _guided_conversion_factor(detail, entered_uom, stock_uom)
        if affects_stock and conversion is None:
            errors.append(
                _guided_error(
                    f"components[{row_number}].uom",
                    f"No ERP conversion from {entered_uom} to {stock_uom or 'the stock UOM'} for {item}",
                )
            )
            continue
        conversion = conversion or Decimal("1")
        batch_qty = entered_qty * conversion
        if affects_stock:
            batch_qty *= Decimal("1") + (loss_factor / Decimal("100"))
        serving_qty = batch_qty * output_ratio
        valuation = _guided_decimal_or_none(detail.get("valuation_rate"))
        row_cost = valuation * batch_qty if affects_cogs and valuation is not None else None
        if affects_cogs:
            if row_cost is None:
                cost_complete = False
                warnings.append(
                    _guided_error(
                        f"components[{row_number}].cost",
                        f"Current valuation is missing for {item}; promotion publication will remain blocked",
                    )
                )
            else:
                total_cost += row_cost

        prepared = prepared_lookup.get(item)
        if component_type == "Prep Item":
            if not prepared or not _guided_truthy(prepared.get("ready")):
                missing_bom = _guided_error(
                    f"components[{row_number}].prepared_bom",
                    f"Prepared Item {item} needs one active, submitted BOM before publishing",
                )
                (errors if require_prepared_bom else warnings).append(missing_bom)
            elif not str(prepared.get("instructions") or "").strip():
                warnings.append(
                    _guided_error(
                        f"components[{row_number}].prepared_bom",
                        f"Add short preparation instructions to the BOM for {item}",
                    )
                )

        preview_components.append(
            {
                "item": item,
                "component_type": component_type,
                "entered_qty": _guided_decimal_text(entered_qty),
                "entered_uom": entered_uom,
                "stock_qty_per_batch": _guided_decimal_text(batch_qty),
                "stock_qty_per_serving": _guided_decimal_text(serving_qty),
                "stock_uom": stock_uom,
                "conversion_factor": _guided_decimal_text(conversion),
                "loss_factor_pct": _guided_decimal_text(loss_factor),
                "valuation_rate": _guided_decimal_text(valuation) if valuation is not None else None,
                "cost_per_batch": _guided_decimal_text(row_cost) if row_cost is not None else None,
                "affects_stock": affects_stock,
                "affects_cogs": affects_cogs,
                "prepared_bom": prepared,
            }
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "yield": {
            "qty": _guided_decimal_text(parsed_yield),
            "uom": yield_name,
        },
        "serving": {
            "qty": _guided_decimal_text(parsed_serving),
            "uom": serving_name,
            "output_ratio": _guided_decimal_text(output_ratio),
        },
        "components": preview_components,
        "modifier_effects": [dict(row) for row in (modifier_effects or [])[:MAX_GUIDED_MODIFIER_EFFECTS]],
        "cost_per_batch": _guided_decimal_text(total_cost) if cost_complete else None,
        "cost_per_serving": _guided_decimal_text(total_cost * output_ratio) if cost_complete else None,
        "cost_status": "complete" if cost_complete else "missing",
        "prepared_component_count": sum(
            1 for row in preview_components if row["component_type"] == "Prep Item"
        ),
    }


def _guided_conversion_factor(
    detail: Mapping[str, Any], entered_uom: str, stock_uom: str
) -> Decimal | None:
    if entered_uom == stock_uom:
        return Decimal("1")
    conversions = detail.get("conversion_factors") or {}
    raw_value = conversions.get(entered_uom)
    return _guided_decimal_or_none(raw_value)


def _guided_positive_decimal(value: Any, label: str) -> Decimal:
    parsed = _guided_decimal_or_none(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{label} must be a finite number greater than zero")
    return parsed


def _guided_non_negative_decimal(value: Any, label: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    parsed = _guided_decimal_or_none(value)
    if parsed is None or parsed < 0:
        raise ValueError(f"{label} must be a finite number greater than or equal to zero")
    return parsed


def _guided_decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _guided_decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _guided_truthy(value: Any) -> bool:
    return value in (True, 1, "1", "true", "True")


def _guided_error(field: str, message: str) -> dict[str, str]:
    return {"field": field, "message": message}


def summarize_menu_authoring(
    *,
    item_rows: list[Mapping[str, Any]],
    recipe_rows: list[Mapping[str, Any]],
    bom_count: int,
    modifier_count: int,
    promotion_count: int,
    company: str | None,
    company_selection_required: bool,
    item_fields_ready: bool,
    recipe_schema_ready: bool,
) -> dict[str, Any]:
    """Return one honest, non-financial commissioning checklist.

    Ingredient, packaging, and prep Items are not saleable menu Items merely
    because they carry stock.  The checklist therefore evaluates only enabled
    ``is_sales_item`` rows.  Each of those rows needs either a published,
    canonical recipe for the selected company or an explicit approved
    exclusion with a reason.
    """

    selected_company = str(company or "").strip() or None
    saleable_items = [
        row
        for row in item_rows
        if _truthy(row.get("is_sales_item")) and not _truthy(row.get("disabled"))
    ]
    active_recipes = [
        row
        for row in recipe_rows
        if str(row.get("status") or "").strip() == "Active"
        and (not selected_company or str(row.get("company") or "").strip() == selected_company)
        and _is_canonical_hash(row.get("canonical_hash"))
    ]
    recipe_items = {
        str(row.get("sellable_item") or "").strip()
        for row in active_recipes
        if str(row.get("sellable_item") or "").strip()
    }

    unclassified = [
        row
        for row in saleable_items
        if str(row.get("custom_fb_item_role") or "").strip() != "Sellable Drink"
    ]
    exclusions = [
        row for row in saleable_items if _truthy(row.get("custom_fb_inventory_excluded"))
    ]
    invalid_exclusions = [
        row
        for row in exclusions
        if not str(row.get("custom_fb_inventory_exclusion_reason") or "").strip()
    ]
    covered_items = {
        str(row.get("name") or row.get("item_code") or "").strip()
        for row in saleable_items
        if str(row.get("name") or row.get("item_code") or "").strip() in recipe_items
        and not _truthy(row.get("custom_fb_inventory_excluded"))
    }
    missing_recipe = [
        row
        for row in saleable_items
        if not _truthy(row.get("custom_fb_inventory_excluded"))
        and str(row.get("name") or row.get("item_code") or "").strip() not in recipe_items
    ]
    ready = bool(
        selected_company
        and not company_selection_required
        and item_fields_ready
        and recipe_schema_ready
        and saleable_items
        and not missing_recipe
        and not unclassified
        and not invalid_exclusions
    )
    return {
        "selected_company": selected_company,
        "company_selection_required": company_selection_required,
        "item_fields_ready": item_fields_ready,
        "recipe_schema_ready": recipe_schema_ready,
        "saleable_items": len(saleable_items),
        "items_ready": len(covered_items),
        "items_missing_recipe": len(missing_recipe),
        "published_recipes": len(active_recipes),
        "draft_recipes": sum(
            1
            for row in recipe_rows
            if str(row.get("status") or "").strip() == "Draft"
            and (not selected_company or str(row.get("company") or "").strip() == selected_company)
        ),
        "boms": int(bom_count or 0),
        "modifier_groups": int(modifier_count or 0),
        "active_promotions": int(promotion_count or 0),
        "unclassified_items": len(unclassified),
        "approved_exclusions": len(exclusions) - len(invalid_exclusions),
        "invalid_exclusions": len(invalid_exclusions),
        "missing_recipe_items": _item_names(missing_recipe),
        "unclassified_item_names": _item_names(unclassified),
        "ready": ready,
    }


def _truthy(value: Any) -> bool:
    return value in (True, 1, "1", "true", "True")


def _is_canonical_hash(value: Any) -> bool:
    return bool(CANONICAL_RECIPE_HASH.fullmatch(str(value or "").strip().lower()))


def _item_names(rows: list[Mapping[str, Any]], *, limit: int = 12) -> list[str]:
    return sorted(
        {
            str(row.get("item_name") or row.get("item_code") or row.get("name") or "").strip()
            for row in rows
            if str(row.get("item_name") or row.get("item_code") or row.get("name") or "").strip()
        }
    )[:limit]


def _require_value(errors: list[dict[str, Any]], row: int, value: str, field: str) -> None:
    if not value:
        errors.append(_error(row, f"{field} is required"))


def _require_decimal(errors: list[dict[str, Any]], row: int, value: str, field: str) -> None:
    if not value:
        errors.append(_error(row, f"{field} is required"))
        return
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        errors.append(_error(row, f"{field} must be a number greater than zero"))
        return
    if not parsed.is_finite() or parsed <= 0:
        errors.append(_error(row, f"{field} must be a number greater than zero"))


def _error(row: int, message: str) -> dict[str, Any]:
    return {"row": row, "message": message}
