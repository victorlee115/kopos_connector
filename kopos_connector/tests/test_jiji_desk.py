"""The desk has one routing rule and one look.

Each director page used to carry its own copy of "how the desk routes a click"
and "what a tile looks like". Copies of a rule are answers to the same question
that only have to be edited once, separately, to start disagreeing. These tests
hold the single owner in place and keep the pages wired to it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "public" / "js" / "jiji_desk.js"
PAGES = ROOT / "kopos" / "page"
HOOKS = ROOT / "hooks.py"

# The pages that speak the desk vocabulary. jiji_menu_recipes and
# jiji_stock_autopilot predate it and are being moved onto it; they are wired
# so they can use it, and listed here so a new page cannot be forgotten.
DESK_PAGES = (
    "jiji_home",
    "jiji_money",
    "jiji_supplies",
    "jiji_staff",
    "jiji_menu_recipes",
    "jiji_stock_autopilot",
)


def _page_script(name: str) -> str:
    return (PAGES / name / f"{name}.js").read_text(encoding="utf-8")


def _existing_page_scripts() -> dict[str, str]:
    return {name: _page_script(name) for name in DESK_PAGES if (PAGES / name / f"{name}.js").exists()}


def test_routing_has_exactly_one_implementation() -> None:
    shared = SHARED.read_text(encoding="utf-8")
    assert "frappe.set_route.apply" in shared
    for name, script in _existing_page_scripts().items():
        assert "frappe.set_route.apply" not in script, f"{name} re-implements desk routing"
        assert "function routeLink" not in script, f"{name} carries its own routeLink"


def test_routes_are_segments_never_hard_coded_desk_urls() -> None:
    shared = SHARED.read_text(encoding="utf-8")
    # The desk is served at /app on some deployments and /desk on others, and
    # its router intercepts clicks rather than following an href. The href is
    # written for middle-click only, and a filter object never enters a path.
    assert "/desk/" not in shared
    assert 'typeof part === "string"' in shared


def test_every_desk_page_is_wired_to_the_shared_file() -> None:
    hooks = HOOKS.read_text(encoding="utf-8")
    block = re.search(r"page_js\s*=\s*\{(.*?)\}", hooks, re.S)
    assert block, "page_js hook is missing"
    wired = set(re.findall(r'"([^"]+)":\s*"public/js/jiji_desk\.js"', block.group(1)))
    assert wired == set(DESK_PAGES), f"page_js and DESK_PAGES disagree: {wired ^ set(DESK_PAGES)}"


def test_pages_only_reference_the_helper_after_load() -> None:
    # page_js appends the shared file after the page script, so `jiji.desk` does
    # not exist while the page script is being evaluated. Referencing it at the
    # top level would throw before on_page_load ever ran.
    for name, script in _existing_page_scripts().items():
        for line in script.splitlines():
            if "jiji.desk" not in line:
                continue
            assert line.startswith((" ", "\t")), f"{name} touches jiji.desk at the top level: {line.strip()}"


def test_the_shared_style_is_injected_once() -> None:
    shared = SHARED.read_text(encoding="utf-8")
    assert 'document.getElementById(STYLE_ID)' in shared
    assert "if (desk.routeLink) return;" in shared


def test_the_status_pill_carries_the_four_levels_the_tablet_uses() -> None:
    shared = SHARED.read_text(encoding="utf-8")
    for level in ("jiji-pill--attention", "jiji-pill--ok", "jiji-pill--stop"):
        assert level in shared, f"{level} is missing from the desk vocabulary"
