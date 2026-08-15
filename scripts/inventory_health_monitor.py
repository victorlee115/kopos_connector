#!/usr/bin/env python3
"""Small external watchdog for the manager-only Inventory Autopilot health API.

The process is intentionally stateless: it polls, prints one JSON result, and
returns a useful exit code.  A supervisor (cron, systemd, or an existing alert
runner) owns scheduling and notifications; no alert credentials are stored by
this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from kopos_connector.kopos.services.inventory_autopilot.health_monitor import classify_health


def fetch_health(url: str, warehouse: str, timeout_seconds: float, auth: str | None) -> dict[str, Any]:
    query = urllib.parse.urlencode({"warehouse": warehouse})
    endpoint = f"{url.rstrip('/')}/api/method/kopos_connector.api.inventory.get_autopilot_health?{query}"
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    if auth:
        request.add_header("Authorization", auth)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8"))
    message = parsed.get("message") if isinstance(parsed, dict) else None
    if not isinstance(message, dict):
        raise ValueError("health response did not contain a message object")
    return message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll JiJi Inventory Autopilot health")
    parser.add_argument("--url", default=os.environ.get("KOPOS_HEALTH_URL", ""), help="ERP base URL")
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--auth-env", default="KOPOS_HEALTH_AUTH", help="Environment variable containing an Authorization header value")
    args = parser.parse_args(argv)
    if not args.url.strip():
        print(json.dumps({"status": "critical", "critical_reasons": ["monitor_url_missing"], "warning_reasons": []}, sort_keys=True))
        return 2
    try:
        payload = fetch_health(args.url, args.warehouse, args.timeout_seconds, os.environ.get(args.auth_env))
        result = {"warehouse": args.warehouse, **classify_health(payload), "health": payload}
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        result = {"warehouse": args.warehouse, "status": "critical", "critical_reasons": ["monitor_endpoint_unreachable"], "warning_reasons": [], "error": str(error)}
    print(json.dumps(result, default=str, sort_keys=True))
    return 2 if result["status"] == "critical" else 1 if result["status"] == "warning" else 0


if __name__ == "__main__":
    raise SystemExit(main())
