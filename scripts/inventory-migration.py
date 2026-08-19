#!/usr/bin/env python3
"""One-off legacy availability migration; run through ``bench execute`` context."""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover or migrate legacy KoPOS availability fields")
    parser.add_argument("--company", required=True)
    parser.add_argument("--warehouse", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--input-digest")
    args = parser.parse_args()
    try:
        from kopos_connector.kopos.services.inventory_autopilot.legacy_migration import (
            execute_legacy_migration,
            migrate_legacy_values,
        )
        if args.dry_run:
            report = migrate_legacy_values(
                company=args.company,
                warehouse=args.warehouse,
                dry_run=True,
            )
            print(json.dumps(report, default=str, indent=2))
            return 2 if report.get("status") == "blocked" else 0
        if not args.input_digest:
            raise SystemExit("--input-digest is required with --execute")
        report = execute_legacy_migration(
            company=args.company,
            warehouse=args.warehouse,
            expected_digest=args.input_digest,
        )
        print(json.dumps(report, default=str, indent=2))
        return 2 if report.get("status") == "blocked" else 0
    except Exception as error:
        print(f"inventory migration blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
