"""Replay runner for recorded transaction cases.

Phase 29's purpose: pin what the money code does *today*, so a refactor has to
declare its behaviour changes instead of discovering them in production.

These are **characterization** fixtures. Their expected values were captured from
the code as it stood on 2026-09-22 — they are not hand-derived from the business
rules, and they are not evidence that the current behaviour is *correct*. They are
evidence that it has not changed. Correctness is argued separately, by
`test_refund_allocation_properties.py` (properties) and
`test_refund_reference_equivalence.py` (an independent oracle).

So when a case fails, there are exactly two honest responses:

  1. The change was unintended. Fix the code.
  2. The change was intended. Regenerate with `--regenerate`, and say in the commit
     message and the audit ledger what changed and why.

Never edit an expected value by hand to make a test pass.

All data here is synthetic. No production or customer data belongs in this
directory.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CASES_DIR = Path(__file__).resolve().parent / "cases"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kopos_connector.kopos.services.accounting.refund_allocation import (  # noqa: E402
    RefundableSale,
    RefundableSaleLine,
    RefundAllocationError,
    allocate_refund,
)


@dataclass(frozen=True)
class CaseResult:
    name: str
    passed: bool
    detail: str


def _sale_from(spec: dict[str, Any]) -> RefundableSale:
    return RefundableSale(
        lines=tuple(
            RefundableSaleLine(
                line_id=line["line_id"],
                quantity=line["quantity"],
                line_total_sen=line["line_total_sen"],
            )
            for line in spec["lines"]
        ),
        tax_sen=spec["tax_sen"],
        rounding_sen=spec["rounding_sen"],
        total_sen=spec["total_sen"],
        cash_unit_sen=spec["cash_unit_sen"],
    )


def observe(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the case's refund sequence and record what the code does now."""
    sale = _sale_from(case["sale"])
    refunded: list[dict[str, int]] = []
    observed: list[dict[str, Any]] = []

    for step in case["sequence"]:
        request = [
            {"line_id": entry["line_id"], "quantity": entry["quantity"]}
            for entry in step
        ]
        try:
            allocation = allocate_refund(sale, refunded, request)
        except RefundAllocationError as error:
            observed.append({"refused": error.code})
            continue

        observed.append(
            {
                "net_sen": allocation.net_sen,
                "tax_sen": allocation.tax_sen,
                "rounding_sen": allocation.rounding_sen,
                "amount_sen": allocation.amount_sen,
                "is_final": allocation.is_final,
                "lines": {row.line_id: row.net_sen for row in allocation.lines},
            }
        )
        # Only an accepted refund moves the position forward.
        totals = {entry["line_id"]: entry["quantity"] for entry in refunded}
        for entry in request:
            totals[entry["line_id"]] = totals.get(entry["line_id"], 0) + entry["quantity"]
        refunded = [{"line_id": k, "quantity": v} for k, v in totals.items()]

    return observed


def load_cases() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text())
        for path in sorted(CASES_DIR.glob("*.json"))
    ]


def check(case: dict[str, Any]) -> CaseResult:
    observed = observe(case)
    expected = case["expected"]
    if observed == expected:
        return CaseResult(case["name"], True, "")
    lines = [f"case {case['name']!r} no longer replays:"]
    for index, (want, got) in enumerate(zip(expected, observed, strict=False)):
        if want != got:
            lines.append(f"  step {index}: expected {want}")
            lines.append(f"            observed {got}")
    if len(observed) != len(expected):
        lines.append(f"  step count: expected {len(expected)}, observed {len(observed)}")
    return CaseResult(case["name"], False, "\n".join(lines))


def regenerate() -> int:
    """Rewrite every case's expected block from current behaviour."""
    count = 0
    for path in sorted(CASES_DIR.glob("*.json")):
        case = json.loads(path.read_text())
        case["expected"] = observe(case)
        path.write_text(json.dumps(case, indent=2) + "\n")
        count += 1
    return count


def main(argv: list[str]) -> int:
    if "--regenerate" in argv:
        count = regenerate()
        print(f"regenerated {count} case(s) from current behaviour")
        print("Review the diff. An unexplained change here is a behaviour change.")
        return 0

    results = [check(case) for case in load_cases()]
    for result in results:
        print(f"{'PASS' if result.passed else 'FAIL'}  {result.name}")
        if not result.passed:
            print(result.detail)
    failed = [r for r in results if not r.passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} cases replay unchanged")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
