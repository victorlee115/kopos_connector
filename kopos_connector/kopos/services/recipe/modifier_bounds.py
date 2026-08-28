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
    raw_minimum = _integer(group_min_selection, "minimum number of customer choices", default=0)
    raw_maximum = _integer(
        group_max_selection,
        "maximum number of customer choices",
        default=1 if normalized_type == "single" else 0,
    )
    if raw_minimum < 0 or raw_maximum < 0:
        raise ModifierBoundsError("the minimum and maximum number of customer choices cannot be negative")
    if normalized_type == "single" and raw_maximum > 1:
        raise ModifierBoundsError(
            "a single-choice group cannot allow more than one choice"
        )

    group_required = _flag(group_is_required, "whether the customer choice group is required")
    row_required = _flag(recipe_required, "whether this recipe requires a customer choice")
    minimum_override = _optional_override(
        override_min_selection, "recipe minimum number of customer choices"
    )
    maximum_override = _optional_override(
        override_max_selection, "recipe maximum number of customer choices"
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
        raise ModifierBoundsError("the minimum and maximum number of customer choices cannot be negative")
    if minimum > maximum:
        raise ModifierBoundsError(
            "the minimum number of customer choices cannot be larger than the maximum"
        )
    if normalized_type == "single" and maximum > 1:
        raise ModifierBoundsError(
            "a single-choice group cannot allow more than one choice"
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
            "a required customer choice group must require at least one choice"
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
            "the saved customer choice limits do not match the limits used for orders"
        )
    return effective


def _selection_type(value: object) -> Literal["single", "multiple"]:
    normalized = str(value or "").strip().lower()
    if normalized not in {"single", "multiple"}:
        raise ModifierBoundsError("customer choice type must be Single or Multiple")
    return cast(Literal["single", "multiple"], normalized)


def _optional_override(value: object, label: str) -> int | None:
    """Read an override column that stores "unset" as zero rather than NULL.

    Frappe emits Int columns as ``not null default 0``, so an authoring row that
    never set an override is indistinguishable from one pinned to zero. Zero is
    therefore read as absent, matching how FB Recipe has always validated these
    fields.
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
