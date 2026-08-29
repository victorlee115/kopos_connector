"""A field behind a permlevel is unreachable without a matching permission row.

`KoPOS Staff Access` shipped a four-digit `pin` at permlevel 1 with no
permlevel-1 permission row, so no role could set a staff PIN through the
standard form. `KoPOS Device` already carried the row its permlevel-1 child
fields need; this keeps every doctype in the app on that side of the line.
"""

from __future__ import annotations

import json
from pathlib import Path

DOCTYPE_ROOT = Path(__file__).resolve().parents[1] / "kopos" / "doctype"


def _doctype_definitions() -> list[tuple[str, dict]]:
    definitions = []
    for path in sorted(DOCTYPE_ROOT.glob("*/*.json")):
        if path.stem != path.parent.name:
            continue
        definition = json.loads(path.read_text(encoding="utf-8"))
        if definition.get("doctype") != "DocType":
            continue
        definitions.append((definition.get("name", path.stem), definition))
    return definitions


def test_every_restricted_field_has_a_permission_row_at_its_level() -> None:
    missing: list[str] = []
    for name, definition in _doctype_definitions():
        permissions = definition.get("permissions") or []
        if not permissions:
            # Child tables inherit the parent doctype's permission rows.
            continue
        granted = {int(row.get("permlevel", 0)) for row in permissions}
        for field in definition.get("fields") or []:
            level = int(field.get("permlevel", 0))
            if level and level not in granted:
                missing.append(f"{name}.{field.get('fieldname')} needs a permlevel {level} row")
    assert not missing, "; ".join(missing)


def test_staff_access_pin_is_writable_by_the_role_that_owns_the_record() -> None:
    definition = dict(_doctype_definitions())["KoPOS Staff Access"]
    restricted = {
        field["fieldname"]: int(field.get("permlevel", 0))
        for field in definition["fields"]
        if int(field.get("permlevel", 0))
    }
    assert restricted == {"pin": 1, "pin_hash": 1}

    base_roles = {row["role"] for row in definition["permissions"] if not row.get("permlevel")}
    writers = {
        row["role"]
        for row in definition["permissions"]
        if int(row.get("permlevel", 0)) == 1 and row.get("write")
    }
    assert writers, "no role can rotate a staff PIN through the standard form"
    assert writers <= base_roles, "a permlevel-1 role must also hold record access"
