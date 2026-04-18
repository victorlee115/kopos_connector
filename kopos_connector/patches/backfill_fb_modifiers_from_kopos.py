from __future__ import annotations

from kopos_connector.api.modifier_migration import backfill_kopos_modifiers_to_fb


def execute() -> None:
    backfill_kopos_modifiers_to_fb(dry_run=False)
