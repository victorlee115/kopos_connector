#!/usr/bin/env python3
"""One-off central POS staff-access migration.

Run inside a Frappe bench.  The default is a read-only report; ``--execute``
requires the exact digest returned by that report and never deletes legacy
device-user rows.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate KoPOS Device Users to central KoPOS Staff Access")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--input-digest")
    args = parser.parse_args()
    try:
        from kopos_connector.kopos.services.inventory_autopilot.staff_access import migrate_legacy_staff_access

        report = migrate_legacy_staff_access(
            dry_run=not args.execute,
            expected_digest=args.input_digest if args.execute else None,
        )
        print(json.dumps(report, default=str, indent=2, sort_keys=True))
        return 0 if report.get("status") in {"dry_run", "applied"} else 1
    except Exception as error:
        print(f"staff access migration blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
