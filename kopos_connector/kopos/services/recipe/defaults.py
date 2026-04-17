from __future__ import annotations

from typing import Any

from kopos_connector.kopos.services.recipe.resolver import apply_defaults


def resolve_default_modifiers(modifier_groups: list[Any]) -> list[Any]:
    return apply_defaults(modifier_groups)
