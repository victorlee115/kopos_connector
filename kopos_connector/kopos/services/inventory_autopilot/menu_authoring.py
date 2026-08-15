"""Small, deterministic CSV preflight for director recipe commissioning.

The importer deliberately does not create documents.  Standard Item, FB Recipe,
and BOM forms remain the authorities; this helper only turns a spreadsheet into
row-level errors before a director enters or imports the reviewed values.
"""

from __future__ import annotations

import csv
import io
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any


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


def csv_template() -> str:
    """Return the stable header row used by the director spreadsheet template."""

    return ",".join(CSV_HEADERS) + "\n"


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
