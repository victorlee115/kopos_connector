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
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--input-digest")
    args = parser.parse_args()
    try:
        from kopos_connector.kopos.services.inventory_autopilot.legacy_migration import (
            discover_legacy_values,
            legacy_input_digest,
            migrate_legacy_values,
        )
        values = discover_legacy_values(company=args.company)
        digest = legacy_input_digest(values)
        if not args.execute:
            print(json.dumps({"status": "dry_run", "input_digest": digest, "values": values}, default=str, indent=2))
            return 0
        if args.input_digest != digest:
            raise SystemExit("input digest does not match the current dry-run rows; run dry-run again")
        report = migrate_legacy_values(company=args.company, warehouse=args.warehouse, dry_run=False)
        print(json.dumps({"status": "applied", **report}, default=str, indent=2))
        return 0
    except Exception as error:
        print(f"inventory migration blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
