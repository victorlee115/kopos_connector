"""Canonical modifier-selection bounds shared by ERP sale and catalog paths."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast


class ModifierBoundsError(ValueError):
    """A modifier contract cannot be represented safely."""


@dataclass(frozen=True)
class EffectiveModifierBounds:
    selection_type: Literal["single", "multiple"]
    is_required: bool
    min_selection: int
    max_selection: int


def resolve_effective_modifier_bounds(
    *,
    selection_type: object,
    group_is_required: object,
    group_min_selection: object,
    group_max_selection: object,
    recipe_required: object | None = None,
    override_min_selection: object | None = None,
    override_max_selection: object | None = None,
) -> EffectiveModifierBounds:
    """Resolve exactly the bounds enforced by FB Order.

    Recipe overrides intentionally retain precedence. A required single-select
    legacy group with a stored zero minimum has always been enforced as one by
    FB Order, so its canonical effective minimum is one without rewriting the
    published source row.
    """

    normalized_type = _selection_type(selection_type)
    raw_minimum = _integer(group_min_selection, "group min selection", default=0)
    raw_maximum = _integer(
        group_max_selection,
        "group max selection",
        default=1 if normalized_type == "single" else 0,
    )
    if raw_minimum < 0 or raw_maximum < 0:
        raise ModifierBoundsError("modifier selection bounds cannot be negative")
    if normalized_type == "single" and raw_maximum > 1:
        raise ModifierBoundsError(
            "single-select modifier group cannot allow more than one selection"
        )

    group_required = _flag(group_is_required, "group required")
    row_required = _flag(recipe_required, "recipe required")
    minimum_override = _optional_override(
        override_min_selection, "override min selection"
    )
    maximum_override = _optional_override(
        override_max_selection, "override max selection"
    )
    if minimum_override is not None:
        minimum = minimum_override
    elif row_required or group_required:
        minimum = 1 if normalized_type == "single" else (raw_minimum or 1)
    else:
        minimum = raw_minimum

    if maximum_override is not None:
        maximum = maximum_override
    elif normalized_type == "single":
        maximum = 1
    else:
        maximum = raw_maximum

    if minimum < 0 or maximum < 0:
        raise ModifierBoundsError("modifier selection bounds cannot be negative")
    if minimum > maximum:
        raise ModifierBoundsError(
            "modifier minimum selection cannot exceed maximum selection"
        )
    if normalized_type == "single" and maximum > 1:
        raise ModifierBoundsError(
            "single-select modifier group cannot allow more than one selection"
        )
    return EffectiveModifierBounds(
        selection_type=normalized_type,
        is_required=minimum > 0,
        min_selection=minimum,
        max_selection=maximum,
    )


def validate_published_modifier_bounds(
    *,
    selection_type: object,
    is_required: object,
    min_selection: object,
    max_selection: object,
) -> EffectiveModifierBounds:
    """Require a wire snapshot to already equal its canonical ERP meaning."""

    declared_required = _flag(is_required, "published required")
    declared_minimum = _integer(
        min_selection,
        "published min selection",
        default=0,
    )
    declared_maximum = _integer(
        max_selection,
        "published max selection",
        default=0,
    )
    if declared_required and declared_minimum < 1:
        raise ModifierBoundsError(
            "required modifier group must require at least one selection"
        )
    effective = resolve_effective_modifier_bounds(
        selection_type=selection_type,
        group_is_required=declared_required,
        group_min_selection=declared_minimum,
        group_max_selection=declared_maximum,
    )
    if (
        declared_required != effective.is_required
        or declared_minimum != effective.min_selection
        or declared_maximum != effective.max_selection
    ):
        raise ModifierBoundsError(
            "published modifier bounds do not match ERP order semantics"
        )
    return effective


def _selection_type(value: object) -> Literal["single", "multiple"]:
    normalized = str(value or "").strip().lower()
    if normalized not in {"single", "multiple"}:
        raise ModifierBoundsError("modifier selection type must be single or multiple")
    return cast(Literal["single", "multiple"], normalized)


def _optional_override(value: object, label: str) -> int | None:
    """Read a Frappe Int override whose persisted unset sentinel is zero.

    Frappe child-table Int fields are ``not null default 0``. The authoring
    contract has always treated zero as no override, so only a non-zero integer
    can alter the modifier group's canonical rule. A recipe that needs zero as
    a real selection bound must use a separate modifier group instead.
    """

    if value is None or value == "":
        return None
    return _integer(value, label) or None


def _flag(value: object, label: str) -> bool:
    parsed = _integer(value, label, default=0)
    if parsed not in {0, 1}:
        raise ModifierBoundsError(f"{label} must be 0 or 1")
    return bool(parsed)


def _integer(value: object, label: str, *, default: int | None = None) -> int:
    if value is None or value == "":
        if default is None:
            raise ModifierBoundsError(f"{label} is required")
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ModifierBoundsError(f"{label} must be an integer") from error
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise ModifierBoundsError(f"{label} must be an integer")
    return int(parsed)
