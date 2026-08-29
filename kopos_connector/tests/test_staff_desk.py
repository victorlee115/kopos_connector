"""The desk may set a PIN; the doctype is still the one that stores it.

JiJi Staff exists because ``KoPOS Staff Access`` carries its PIN at permlevel 1
and the doctype had no permlevel-1 permission row, so no role could set one
through the form. The page on top of that fix has one write, and these tests
hold the properties that keep it safe: it hands four digits to the controller
that hashes them, it never touches the verifier itself, and it refuses to
report success for a write that Frappe silently dropped.
"""

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
    returned = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and "entered" in ast.dump(node)
    ]
    assert not returned, "the digits must not travel back to the browser"
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


def test_the_save_is_permission_checked() -> None:
    # A director's permlevel-1 write is exactly what is being exercised here,
    # so no call may wave the check through.
    for node in ast.walk(ast.parse(_source())):
        if not isinstance(node, ast.Call):
            continue
        assert not any(
            keyword.arg == "ignore_permissions" for keyword in node.keywords
        ), "the staff desk must save through the normal permission check"


def test_the_typo_is_caught_before_anything_is_written() -> None:
    source = _source()
    body = source.split("def set_staff_pin")[1]
    typo_check = body.index("entered != repeated")
    save = body.index("document.save()")
    assert typo_check < save, "a mistyped PIN would lock someone out of their own shift"


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
