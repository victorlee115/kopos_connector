from __future__ import annotations

from typing import Any

from kopos_connector.kopos.services.recipe.resolver import resolve_components


def apply_recipe_substitutions(
    recipe: Any, modifiers: list[Any]
) -> list[dict[str, object]]:
    return resolve_components(recipe, modifiers)
