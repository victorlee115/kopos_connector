from __future__ import annotations

import hashlib
import json
from typing import Any


def build_recipe_snapshot(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
