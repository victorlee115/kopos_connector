"""D37: ERP can send the tablet a tax rate that ERP then refuses on every sale.

`api/catalog.get_tax_rate_value` turns the POS Profile's SST percentage into a
rate with float division - `flt(custom_kopos_sst_rate) / 100`. Float division
can manufacture digits: `1.1 / 100` is `0.011000000000000001`.

The tablet accepts that rate, computes tax correctly with it, and sends it back
on every sale (`erpnext-sync-client.ts:705`). ERP's order validator
(`fb_orders._normalize_optional_tax_rate`) then refuses any rate with more than
six decimal places - so every sale on that POS Profile is refused, and D22
records that a refused sale is failed, never requeued.

LATENT, not live: the rates in use (5%, 6%, 8%, 10%) and every other round rate
checked are emitted cleanly. Of the 10,001 two-decimal rates from 0.00% to
100.00%, 2,760 are affected; 1.1% is the most plausible one a person would type.

Recorded, not fixed, because it changes how a tax rate is derived and that is
an owner decision. The obvious fix is to derive the rate in Decimal from the
field's text (or quantize it to six places) before it leaves ERP.

The test is a strict expected failure: it passes today by failing, and the day
the fix lands it XPASSes, which fails the suite and forces this marker to be
removed. It cannot be forgotten, and it never turns the suite red meanwhile.
"""

from __future__ import annotations

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.api import fb_orders  # noqa: E402


def _rate_the_catalog_emits(percent: float) -> float:
    # catalog.get_tax_rate_value: flt(profile.custom_kopos_sst_rate) / 100
    return float(percent) / 100


@pytest.mark.parametrize("percent", [5, 6, 8, 10, 6.5, 12.5, 24])
def test_rates_in_use_round_trip(percent: float) -> None:
    """The rates a shop actually configures today survive the round trip."""
    rate = _rate_the_catalog_emits(percent)
    assert fb_orders._normalize_optional_tax_rate(rate, present=True) is not None


@pytest.mark.xfail(
    strict=True,
    reason="D37: a 1.1% SST rate is emitted as 0.011000000000000001, which ERP refuses",
)
def test_a_one_point_one_percent_rate_round_trips() -> None:
    rate = _rate_the_catalog_emits(1.1)
    assert rate == 0.011000000000000001  # the float artefact is real
    fb_orders._normalize_optional_tax_rate(rate, present=True)
