"""JiJi Staff owns the scoped business workflow; the raw record is support-only."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "api" / "staff_desk.py"
PAGE = ROOT / "kopos" / "page" / "jiji_staff" / "jiji_staff.js"
DOCTYPE = ROOT / "kopos" / "doctype" / "kopos_staff_access" / "kopos_staff_access.py"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_the_verifier_is_written_by_the_doctype_alone() -> None:
    source = _source()
    # Hashing, the revision bump, and invalidating every bound tablet all live
    # on the controller. Assigning pin_hash here would skip all three.
    assert "document.pin = entered" in source
    assert "hash_pin" not in source
    assert re.search(r"pin_hash\s*=", source) is None
    assert "invalidate_devices_for_staff_access" in DOCTYPE.read_text(encoding="utf-8")


def test_the_pin_is_never_returned_stored_or_logged() -> None:
    source = _source()
    tree = ast.parse(source)
    endpoints = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"set_staff_pin", "add_staff_access"}
    ]
    for endpoint in endpoints:
        returned = [node for node in ast.walk(endpoint) if isinstance(node, ast.Return)]
        for node in returned:
            names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
            assert names.isdisjoint({"entered", "pin", "confirm", "repeated"})
    assert "log_error" not in source
    assert "frappe.logger" not in source


def test_a_permlevel_write_that_did_not_happen_is_not_reported_as_success() -> None:
    source = _source()
    # Frappe drops a permlevel field the role cannot write and saves the rest.
    # Without the re-read, a director is told the PIN changed while the tablet
    # keeps asking for the old one.
    assert 'frappe.db.get_value("KoPOS Staff Access", name, "pin_hash")' in source
    assert "after == before" in source
    assert "frappe.PermissionError" in source


def test_the_scoped_page_is_the_only_business_write_path() -> None:
    source = _source()
    definition = (ROOT / "kopos" / "doctype" / "kopos_staff_access" / "kopos_staff_access.json").read_text(encoding="utf-8")
    assert '"role":"System Manager"' in definition
    assert '"role":"Company Director"' not in definition
    assert "can_manage_staff_access(name)" in source
    assert "resolve_director_context(company=company)" in source
    assert "save(ignore_permissions=True)" in source


def test_the_typo_is_caught_before_anything_is_written() -> None:
    source = _source()
    body = source.split("def _checked_pin")[1].split("def set_staff_pin")[0]
    typo_check = body.index("entered != repeated")
    assert typo_check >= 0
    set_pin = source.split("def set_staff_pin")[1]
    assert set_pin.index("_checked_pin(pin, confirm)") < set_pin.index("document.save")


def test_the_two_states_the_old_form_could_not_say() -> None:
    source = _source()
    assert '"No PIN yet"' in source
    assert '"Not working here now"' in source


def test_a_tablet_is_a_place_not_a_person() -> None:
    source = _source()
    page = PAGE.read_text(encoding="utf-8")
    # Who is signed in comes from the open FB Shift, which already knows; the
    # device record never learns to answer it a second way.
    assert '"FB Shift"' in source and '"status": "Open"' in source
    assert "Where each tablet is, and who is on it" in page


def test_a_login_is_never_shown_as_a_person() -> None:
    source = _source()
    # FB Shift records staff_id, which is an email. Reading the field is fine;
    # putting it on screen is not, so every use is wrapped by the shared name
    # reader before it reaches a sentence.
    assert "person_name(" in source
    for line in source.splitlines():
        if "staff_id" not in line or line.lstrip().startswith("#"):
            continue
        assert "person_name(" in line or "fields=" in line, f"a login could reach the page: {line.strip()}"


def test_the_digits_do_not_survive_in_the_page() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert 'pin.val("")' in page and 'confirm.val("")' in page
    assert 'type="password"' in page


def test_the_write_is_a_post() -> None:
    source = _source()
    page = PAGE.read_text(encoding="utf-8")
    assert 'methods=["POST"]' in source
    assert 'type: "POST"' in page


def test_the_page_is_registered_for_directors() -> None:
    install = (ROOT / "install" / "install.py").read_text(encoding="utf-8")
    assert '"name": "jiji_staff"' in install
    assert '"title": "JiJi Staff"' in install


def test_the_page_degrades_instead_of_blanking() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "could not read your people just now" in page
    assert "Nobody has been changed" in page


def test_an_empty_staff_list_says_what_to_do_next() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "No one can use a tablet yet. Add someone to get started." in page
    assert "No one else is ready to add. Ask Support if the person you need is missing." in page
    assert 'set_primary_action(__("Add someone")' in page
    assert 'add_inner_button(__("Refresh")' in page
    assert 'add_inner_button(__("Refresh"), () => this.load(), "refresh")' not in page
    assert 'primary_action_label: __("Give tablet access")' in page
