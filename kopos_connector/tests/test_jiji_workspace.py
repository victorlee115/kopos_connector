"""The sidebar should answer "what do I do here", not "what tables exist".

Before this workspace the sidebar listed KoPOS Modifier Group, KoPOS Promotion
Snapshot, Projection Support Queue -- ten destinations, all weighted equally,
none of them a thing a café owner set out to do.

JiJi names six destinations after the business. Raw records and support tools
belong to the separate System Manager workspace and never appear here.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "kopos" / "workspace" / "jiji" / "jiji.json"
INSTALL = ROOT / "install" / "install.py"

DESTINATIONS = ("Home", "Money", "Menu", "Stock", "Supplies", "Staff")


def _workspace() -> dict:
    return json.loads(WORKSPACE.read_text(encoding="utf-8"))


def test_six_destinations_named_after_the_business() -> None:
    shortcuts = _workspace()["shortcuts"]
    assert tuple(row["label"] for row in shortcuts) == DESTINATIONS
    # Every one of them opens a JiJi page, not a table.
    assert {row["type"] for row in shortcuts} == {"Page"}
    assert all(row["link_to"].startswith("jiji_") for row in shortcuts)


def test_every_destination_is_a_page_that_exists() -> None:
    install = INSTALL.read_text(encoding="utf-8")
    for row in _workspace()["shortcuts"]:
        page = row["link_to"]
        assert f'"name": "{page}"' in install, f"{page} is linked but never installed"
        assert (ROOT / "kopos" / "page" / page / f"{page}.js").exists(), f"{page} has no script"


def test_business_workspace_contains_no_raw_or_support_routes() -> None:
    workspace = _workspace()
    assert workspace["links"] == []
    content = json.loads(workspace["content"])
    headers = [block["data"]["text"] for block in content if block["type"] == "header"]
    assert headers == ["Run your café"]
    assert {block["type"] for block in content} <= {"header", "paragraph", "shortcut"}


def test_a_director_can_open_it() -> None:
    roles = {row["role"] for row in _workspace()["roles"]}
    assert "Company Director" in roles
    assert _workspace()["public"] == 1


def test_it_sorts_above_the_support_cockpit() -> None:
    support = json.loads((ROOT / "kopos" / "workspace" / "kopos_support" / "kopos_support.json").read_text("utf-8"))
    # A director's first sidebar entry should be the café, not the cockpit.
    assert _workspace()["sequence_id"] < support["sequence_id"]


def test_support_workspace_is_for_system_managers_only() -> None:
    support = json.loads((ROOT / "kopos" / "workspace" / "kopos_support" / "kopos_support.json").read_text("utf-8"))
    assert {row["role"] for row in support["roles"]} == {"System Manager"}


def test_existing_workspace_migration_is_idempotent_and_preserves_custom_blocks() -> None:
    from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

    install_fake_frappe_modules()
    from kopos_connector.patches.simplify_jiji_business_workspace import simplify_content

    old_layout = [
        {"type": "header", "data": {"text": "Your café", "col": 12}},
        {"type": "header", "data": {"text": "Advanced", "col": 12}},
        {"type": "card", "data": {"card_name": "Support tools", "col": 4}},
        {"type": "paragraph", "data": {"text": "My café note", "col": 12}},
    ]
    first, first_changed = simplify_content(json.dumps(old_layout))
    second, second_changed = simplify_content(first)
    assert first_changed is True
    assert second_changed is False
    migrated = json.loads(second)
    assert [block["data"].get("text") for block in migrated] == ["Run your café", "My café note"]


def test_no_destination_shows_a_record_id() -> None:
    workspace = _workspace()
    labels = [row["label"] for row in workspace["links"]] + [row["label"] for row in workspace["shortcuts"]]
    for label in labels:
        assert label == label.strip()
        assert "_" not in label, f"{label!r} reads like a table name"
        assert not label.isupper(), f"{label!r} reads like a code"
