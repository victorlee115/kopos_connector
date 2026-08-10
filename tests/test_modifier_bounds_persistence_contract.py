from __future__ import annotations

import pytest

from kopos_connector.kopos.services.recipe.modifier_bounds import (
    EffectiveModifierBounds,
    resolve_effective_modifier_bounds,
)


@pytest.mark.parametrize("persisted_unset", [None, "", 0, "0"])
def test_frappe_int_zero_and_blank_are_the_same_unset_override(
    persisted_unset: object,
) -> None:
    bounds = resolve_effective_modifier_bounds(
        selection_type="Single",
        group_is_required=1,
        group_min_selection=0,
        group_max_selection=1,
        recipe_required=0,
        override_min_selection=persisted_unset,
        override_max_selection=persisted_unset,
    )

    assert bounds == EffectiveModifierBounds(
        selection_type="single",
        is_required=True,
        min_selection=1,
        max_selection=1,
    )


def test_nonzero_recipe_override_remains_explicit() -> None:
    bounds = resolve_effective_modifier_bounds(
        selection_type="Multiple",
        group_is_required=0,
        group_min_selection=0,
        group_max_selection=3,
        recipe_required=0,
        override_min_selection=0,
        override_max_selection=2,
    )

    assert bounds == EffectiveModifierBounds(
        selection_type="multiple",
        is_required=False,
        min_selection=0,
        max_selection=2,
    )
