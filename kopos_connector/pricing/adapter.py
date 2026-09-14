"""Translate ERP wire data to the shared kernel; no discount algorithm here."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from importlib.resources import files
import json
from zoneinfo import ZoneInfo

from jsonschema import Draft7Validator

from .runtime import calculate, PromotionRuntimeError


def runtime_hash():
    return json.loads(files("kopos_connector.pricing").joinpath("runtime.json").read_text())["bundleSha256"]


@lru_cache(maxsize=1)
def body_validator():
    schema = json.loads(files("kopos_connector").joinpath("contracts/promotion-snapshot.v2.schema.json").read_text())
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def validate_body(body):
    error = next(body_validator().iter_errors(body), None)
    if error:
        path = "/".join(str(part) for part in error.absolute_path) or "offer snapshot"
        raise PromotionRuntimeError(f"Check {path}: {error.message}")
    if body["kernel_hash"] != runtime_hash():
        raise PromotionRuntimeError("This offer needs a different pricing version. No price has been accepted")
    identities = [row["promotion_id"] for row in body["promotions"]]
    items = [row["item_code"] for row in body["frozen_items"]]
    if len(set(identities)) != len(identities) or len(set(items)) != len(items):
        raise PromotionRuntimeError("The offer snapshot contains duplicate identities")
    if any(row["pos_profile"] != body["pos_profile"] for row in body["promotions"]):
        raise PromotionRuntimeError("The offer snapshot contains another outlet's offer")


def time_iso(value):
    """ERP Datetime values without an offset are explicitly Malaysia time."""
    if value is None or value == "":
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    return parsed.isoformat()


def rule_for_kernel(row):
    # The versioned schema validates each wire field before this translation.
    rule = {key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:]): value
            for key, value in row.items()}
    rule["validFrom"] = time_iso(row["valid_from"])
    rule["validUpto"] = time_iso(row["valid_upto"])
    return rule


def snapshot_body(profile, promotions, frozen_items):
    body = {
        "schema_version": 2, "kernel_version": 2, "kernel_hash": runtime_hash(),
        "pos_profile": profile,
        "promotions": sorted(promotions, key=lambda row: (row["priority"], row["promotion_id"])),
        "frozen_items": sorted(frozen_items, key=lambda row: row["item_code"]),
    }
    validate_body(body)
    return body


def _quote_input(body, cart_items, at_time, *, selected=()):
    validate_body(body)
    now = time_iso(at_time)
    # Freshness is checked on the tablet. ERP replays against the retained
    # snapshot and sale time, not against today's catalog or receipt time.
    snapshot = {
        "kernelVersion": 2, "kernelHash": body["kernel_hash"], "posProfile": body["pos_profile"],
        "source": "published", "snapshotVersion": "ERP-REPLAY", "snapshotHash": "ERP-REPLAY",
        "downloadedAt": now, "publishedAt": now, "effectiveFrom": now,
        "promotions": [rule_for_kernel(row) for row in body["promotions"]],
        "frozenItems": [{"itemCode": row["item_code"], "itemGroup": row["item_group"]} for row in body["frozen_items"]],
    }
    return {
        "kernelVersion": 2, "cartItems": cart_items,
        "menuItems": [{"id": row["item_code"], "categoryId": row["item_group"]} for row in body["frozen_items"]],
        "snapshot": snapshot, "nowIso": now, "posProfile": body["pos_profile"],
        "networkOnline": True, "selectedPromotionIds": list(selected),
    }


def quote_snapshot(body, cart_items, at_time, *, selected=()):
    result = calculate("quote", _quote_input(body, cart_items, at_time, selected=selected))
    if result.get("status") != "complete":
        raise PromotionRuntimeError("The best price could not be proved for this basket. No partial price has been accepted")
    return result


def cost_basket_totals(body, baskets, at_time):
    return calculate("basket_totals", {"input": _quote_input(body, [], at_time,
        selected=[row["promotion_id"] for row in body["promotions"]]), "baskets": baskets})


def promotion_cost_cases(promotion, profile, items):
    """Every launch-template basket, including same-item and mixed-price pairs.

    Costs are ERP-owned; the discount is always calculated by the shared kernel.
    Each case is independently checked for a loss, never averaged with another.
    """
    from itertools import combinations_with_replacement
    from kopos_connector.api.promotions import serialize_promotion

    if not 1 <= len(items) <= 50:
        raise PromotionRuntimeError("Choose between one and 50 products for this offer's cost review")
    rule = serialize_promotion(promotion, profile, kernel_version=2)
    # Cost review evaluates the full discount even when the offer is scheduled
    # for later. Removing only the time gate avoids treating an inactive price
    # as cost-safe. The original schedule remains bound in the source hash.
    rule.update({"valid_from": None, "valid_upto": None, "schedule": None})
    pair = rule["promotion_type"] in {"buy_x_get_y", "nth_item_discount"}
    if pair and (rule["buy_qty"] != 1 or rule["discount_qty"] != 1):
        raise PromotionRuntimeError("This advanced pair offer needs a separate cost review")
    cases = list(combinations_with_replacement(items, 2)) if pair else [(item,) for item in items]
    body = snapshot_body(profile, [rule], [{"item_code": row["item"], "item_group": row["item_group"]} for row in items])
    baskets = [[{"id": str(index), "menuItemId": row["item"], "quantity": 1,
                 "unitPriceSen": row["baseline_price_sen"], "discountAmountSen": 0,
                 "modifierTotalSen": 0, "modifiers": []}
                for index, row in enumerate(case)] for case in cases]
    discounts = cost_basket_totals(body, baskets, "2030-01-01T00:00:00Z")
    result = []
    for case, discount in zip(cases, discounts, strict=True):
        baseline = sum(row["baseline_price_sen"] for row in case)
        net = baseline - discount
        result.append({"item": " + ".join(row["item"] for row in case), "units": 1,
                       "baseline_price_sen": baseline, "baseline_net_revenue_sen": baseline,
                       "promoted_price_sen": net, "net_revenue_sen": net,
                       "cogs_sen": sum(row["cogs_sen"] for row in case),
                       "item_group": "", "components": [component for row in case for component in row["components"]]})
    return result
