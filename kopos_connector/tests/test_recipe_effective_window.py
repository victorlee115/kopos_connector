"""One effective published recipe version per Item/company/business time."""

from __future__ import annotations

from datetime import datetime

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.api import catalog


# Business time is Asia/Kuala_Lumpur; frappe resolves it from the site
# timezone, so these boundaries are expressed in that local wall clock.
SALE_TIME = datetime(2026, 8, 15, 9, 0, 0)


def _row(effective_from=None, effective_to=None) -> dict[str, object]:
    return {"effective_from": effective_from, "effective_to": effective_to}


@pytest.mark.parametrize(
    "row, expected, reason",
    [
        (_row(), True, "an open-ended version is always effective"),
        (_row(effective_from="2026-08-15 08:59:00"), True, "started a minute ago"),
        (_row(effective_from="2026-08-15 09:00:00"), True, "starts exactly now"),
        (_row(effective_from="2026-08-15 09:00:01"), False, "starts a second later"),
        (_row(effective_to="2026-08-15 09:00:01"), True, "ends a second later"),
        (_row(effective_to="2026-08-15 09:00:00"), True, "ends exactly now"),
        (_row(effective_to="2026-08-15 08:59:59"), False, "ended a second ago"),
        (
            _row(effective_from="2026-08-15 08:00:00", effective_to="2026-08-15 10:00:00"),
            True,
            "inside a closed window",
        ),
        (
            _row(effective_from="2026-08-16 08:00:00", effective_to="2026-08-16 10:00:00"),
            False,
            "a future closed window is not yet effective",
        ),
        (
            _row(effective_from="2026-08-14 08:00:00", effective_to="2026-08-14 10:00:00"),
            False,
            "a past closed window is retired",
        ),
    ],
)
def test_effective_window_boundaries(row, expected, reason) -> None:
    assert catalog.is_effective_recipe_row(row, SALE_TIME) is expected, reason


def test_exactly_one_version_is_effective_across_an_adjacent_handover() -> None:
    """A cutover replaces a version without leaving a gap or an overlap."""

    previous = _row(effective_from="2026-08-01 00:00:00", effective_to="2026-08-15 08:59:59")
    current = _row(effective_from="2026-08-15 09:00:00")

    effective = [
        candidate
        for candidate in (previous, current)
        if catalog.is_effective_recipe_row(candidate, SALE_TIME)
    ]
    assert effective == [current]

    # A moment earlier, the previous version is the only authority.
    just_before = datetime(2026, 8, 15, 8, 59, 59)
    earlier = [
        candidate
        for candidate in (previous, current)
        if catalog.is_effective_recipe_row(candidate, just_before)
    ]
    assert earlier == [previous]
