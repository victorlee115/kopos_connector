from __future__ import annotations

import json
from pathlib import Path


CONNECTOR_ROOT = Path(__file__).resolve().parents[2]


def test_item_group_count_schedule_fields_are_idempotent_install_inputs():
    source = (CONNECTOR_ROOT / "kopos/install/fb_custom_fields.py").read_text()
    assert '"Item Group": [' in source
    assert '"custom_kopos_count_frequency"' in source
    assert '"custom_kopos_count_weekdays"' in source
    assert "create_custom_fields(custom_fields)" in source
    assert '("Item Group", "custom_kopos_count_frequency")' in source
    assert '("Item Group", "custom_kopos_count_weekdays")' in source


def test_count_task_persists_schedule_identity_without_financial_fields():
    metadata = json.loads(
        (CONNECTOR_ROOT / "kopos/doctype/fb_inventory_count_task/fb_inventory_count_task.json").read_text()
    )
    fields = {field["fieldname"]: field for field in metadata["fields"]}
    assert fields["stock_group"]["options"] == "Item Group"
    assert fields["schedule_key"]["unique"] == 1
    assert fields["schedule_key"]["read_only"] == 1
    assert fields["schedule_frequency"]["hidden"] == 1
    assert fields["schedule_period"]["hidden"] == 1
    assert not {"valuation", "cogs", "rate", "amount"}.intersection(fields)


def test_scheduler_is_registered_hourly_and_uses_protected_timezone():
    hooks_source = (CONNECTOR_ROOT / "hooks.py").read_text()
    scheduler_source = (
        CONNECTOR_ROOT / "kopos/services/inventory_autopilot/count_scheduler.py"
    ).read_text()
    assert "count_scheduler.schedule_inventory_count_tasks" in hooks_source
    assert 'ZoneInfo("Asia/Kuala_Lumpur")' in scheduler_source
    assert '"inventory_count_before_cutover"' in scheduler_source
    assert '"inventory_count_opening_reconciliation"' in scheduler_source
