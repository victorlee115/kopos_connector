# Promotion Snapshot Fix Execution Checklist

This file is a strict implementation checklist for the permanent promotion snapshot fix.

Use this together with `erpnext/kopos_connector/PROMOTION_SNAPSHOT_FIX_PLAN.md`.

If an agent follows this file exactly, it should be able to implement the fix without guessing.

## Mission

Eliminate the reconciliation error:

`Referenced promotion snapshot was not found`

Do this by making promotion snapshot identity deterministic, persisted, reusable, and reconcilable.

## Scope

Allowed scope:

- `erpnext/kopos_connector/kopos_connector/api/promotions.py`
- `erpnext/kopos_connector/kopos_connector/api/orders.py`
- `erpnext/kopos_connector/kopos_connector/api/__init__.py` if needed
- `erpnext/kopos_connector/kopos_connector/kopos/doctype/kopos_promotion_snapshot/kopos_promotion_snapshot.py`
- `erpnext/kopos_connector/tests/test_promotion_workflow.py`
- `erpnext/kopos_connector/patches.txt`
- `erpnext/kopos_connector/patches/<new_patch_file>.py`

Do not touch:

- `kopos_connector-modifiers`

## Required Behavioral Rules

1. The API must never return a non-persisted snapshot version.
2. If effective promotions did not change, snapshot version must not change.
3. If effective promotions changed, snapshot version must change.
4. Reconciliation must use historical persisted evidence, not current live promotions.
5. Existing invoices with valid hash evidence should be recoverable.

## Step 0: Read These Existing Functions First

Read these before editing anything:

From `erpnext/kopos_connector/kopos_connector/api/promotions.py`:

- `get_promotion_snapshot_payload`
- `publish_promotion_snapshot`
- `reconcile_promotion_payload`
- `build_snapshot_payload`
- `get_latest_published_snapshot`
- `get_snapshot_by_version`
- `build_snapshot_version`

From `erpnext/kopos_connector/kopos_connector/api/orders.py`:

- `set_invoice_promotion_metadata`

From snapshot doctype:

- `on_trash`

## Step 1: Replace Timestamp-Based Identity With Content-Based Identity

### File

- `erpnext/kopos_connector/kopos_connector/api/promotions.py`

### Action

Refactor snapshot generation so identity is based only on effective promotion content.

### Add These Functions

Add these exact function signatures or their equivalent typed form:

```python
def build_effective_snapshot_body(
    pos_profile: str,
    at_time: Any | None = None,
) -> dict[str, Any]:
    ...


def compute_snapshot_content_hash(body: dict[str, Any]) -> str:
    ...


def build_snapshot_version_from_hash(snapshot_hash: str) -> str:
    ...


def build_snapshot_payload_for_persistence(
    pos_profile: str,
    at_time: Any | None = None,
) -> dict[str, Any]:
    ...
```

### Function Rules

#### `build_effective_snapshot_body`

Must include only material pricing content:

- `pos_profile`
- `promotions`

Each promotion payload must keep all fields that affect pricing or eligibility, including current ones already emitted by `serialize_promotion()`.

Must not include:

- `published_at`
- generated request timestamps
- volatile metadata used only for transport or logging

#### `compute_snapshot_content_hash`

Must serialize deterministically using compact sorted JSON and hash with SHA-256.

#### `build_snapshot_version_from_hash`

Must be deterministic.

Example acceptable format:

```python
return "KOPOS-PROMO-{0}".format(snapshot_hash[:16].upper())
```

Do not include timestamps.

#### `build_snapshot_payload_for_persistence`

Must:

1. call `build_effective_snapshot_body`
2. compute the stable content hash
3. build the stable version
4. return payload with:
   - `pos_profile`
   - `promotions`
   - `effective_from`
   - `published_at`
   - `snapshot_hash`
   - `snapshot_version`

Important:

- `effective_from` and `published_at` can remain metadata fields
- they must not influence identity

## Step 2: Add Persist-Or-Reuse Snapshot Creation

### File

- `erpnext/kopos_connector/kopos_connector/api/promotions.py`

### Add These Functions

```python
def get_snapshot_by_hash(pos_profile: str, snapshot_hash: str):
    ...


def ensure_persisted_snapshot(
    pos_profile: str,
    at_time: Any | None = None,
) -> tuple[Any, bool]:
    ...
```

### Function Rules

#### `get_snapshot_by_hash`

Must look up a snapshot row by:

- `pos_profile`
- `snapshot_hash`

#### `ensure_persisted_snapshot`

Must return:

- `(snapshot_doc, created_new)`

Behavior:

1. build deterministic payload
2. look for existing row by `pos_profile + snapshot_hash`
3. if found, return existing row and `False`
4. if not found, create new row and return it with `True`
5. handle duplicate insert races safely by re-reading on duplicate failure

### Snapshot Row Population

When creating a new row, set:

- `snapshot_version`
- `status`
- `pos_profile`
- `published_at`
- `effective_from`
- `snapshot_hash`
- `promotion_count`
- `snapshot_payload`

The stored `snapshot_payload` must be deterministic compact JSON.

## Step 3: Make Fetch Use Persisted Snapshots Only

### File

- `erpnext/kopos_connector/kopos_connector/api/promotions.py`

### Update Function

```python
def get_promotion_snapshot_payload(
    pos_profile: str | None = None,
    current_version: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any] | None:
    ...
```

### New Required Behavior

1. resolve POS profile
2. determine whether any effective promotions exist at the evaluation time
3. if none exist, return `None`
4. if effective promotions exist, call `ensure_persisted_snapshot()`
5. return payload from the persisted snapshot row only

### Important Constraint

Do not construct the returned API payload directly from a transient in-memory snapshot.

Always read from the persisted row that will later be used by reconciliation.

## Step 4: Make Publish Use The Same Code Path

### File

- `erpnext/kopos_connector/kopos_connector/api/promotions.py`

### Update Function

```python
def publish_promotion_snapshot(
    pos_profile: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    ...
```

### New Required Behavior

1. resolve POS profile
2. if there are no effective promotions, return a clear unavailable-style or no-op response
3. call `ensure_persisted_snapshot()`
4. if `created_new` is `False`, return `status: "unchanged"`
5. if `created_new` is `True`, return `status: "published"`

### Important Constraint

Do not supersede an earlier snapshot just because publish was clicked again.

Only create a new snapshot when effective content changed.

## Step 5: Add Reconciliation Fallbacks

### File

- `erpnext/kopos_connector/kopos_connector/api/promotions.py`

### Add These Functions

```python
def get_snapshot_by_version_any_profile(snapshot_version: str):
    ...


def get_snapshot_by_hash_any_profile(snapshot_hash: str):
    ...
```

The second helper is optional but recommended.

### Update Function

```python
def reconcile_promotion_payload(
    pos_profile: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ...
```

### New Required Lookup Order

Use this exact order:

1. lookup by `pos_profile + snapshot_version`
2. if not found and `snapshot_hash` exists, lookup by `pos_profile + snapshot_hash`
3. if still not found and `snapshot_version` exists, lookup version under any profile
4. if found under another profile, return message:
   - `Promotion snapshot profile mismatch`
5. if still not found, return message:
   - `Referenced promotion snapshot was not found`

### Important Constraint

Do not fallback to current live promotion rules.

Only fallback to persisted snapshot evidence.

## Step 6: Update Severity Mapping

### File

- `erpnext/kopos_connector/kopos_connector/api/promotions.py`

### Action

If a new message is introduced:

- `Promotion snapshot profile mismatch`

then add it to the major reconciliation issue set so it routes correctly.

## Step 7: Verify Snapshot Deletion Guard

### File

- `erpnext/kopos_connector/kopos_connector/kopos/doctype/kopos_promotion_snapshot/kopos_promotion_snapshot.py`

### Action

Check that `on_trash()` blocks deletion when invoices reference the snapshot.

If needed, strengthen it so the guard checks both:

- explicit snapshot version references
- any invoice metadata path that may indirectly reference the snapshot

Do not allow deletion of historical evidence.

## Step 8: Add Uniqueness Or Duplicate Protection

### Preferred Rule

The system should not allow duplicate rows for the same:

- `pos_profile`
- `snapshot_hash`

### Implementation Options

Pick one of these:

1. add a database unique index by patch
2. use doc-level uniqueness if available and appropriate
3. keep application-level duplicate handling plus post-failure re-read

### If Using A Patch

Create:

- `erpnext/kopos_connector/patches/v1_0/<descriptive_patch_name>.py`

Register it in:

- `erpnext/kopos_connector/patches.txt`

### Patch Skeleton

```python
from __future__ import annotations

import frappe


def execute() -> None:
    if not frappe.db.table_exists("KoPOS Promotion Snapshot"):
        return

    duplicates = frappe.db.sql(
        """
        select pos_profile, snapshot_hash, count(*) as row_count
        from `tabKoPOS Promotion Snapshot`
        where ifnull(snapshot_hash, '') != ''
        group by pos_profile, snapshot_hash
        having count(*) > 1
        """,
        as_dict=True,
    )
    if duplicates:
        frappe.throw("Duplicate promotion snapshot hashes must be resolved before applying unique constraint")

    frappe.db.sql(
        """
        alter table `tabKoPOS Promotion Snapshot`
        add unique index if not exists kopos_snapshot_profile_hash_uniq (pos_profile, snapshot_hash)
        """
    )
```
```

If MariaDB syntax support makes `if not exists` unsafe, write explicit index existence checks before running alter statements.

## Step 9: Add Historical Repair Utility Or Patch

### Goal

Repair old invoices where valid persisted evidence exists by hash or corrected lookup.

### Recommended Path

Create a backend patch or utility with this function:

```python
def repair_promotion_reconciliation_invoices(limit: int | None = None) -> dict[str, Any]:
    ...
```

### Required Behavior

1. fetch invoices where `custom_kopos_promotion_reconciliation_status = "review_required"`
2. parse invoice promotion metadata
3. re-run reconciliation using the new logic
4. if result is now `matched`, update fields on the invoice
5. if still unresolved, leave it in review_required
6. return counts such as:
   - scanned
   - repaired
   - still_pending

### Suggested Update Fields On Repair

If reconciliation becomes matched, update:

- `custom_kopos_promotion_reconciliation_status`
- `custom_kopos_promotion_review_status`
- `custom_kopos_promotion_payload`

Keep audit history in payload events if that pattern already exists.

### Important Constraint

Do not mark an invoice matched unless persisted snapshot evidence actually exists.

## Step 10: Exact Tests To Add

### File

- `erpnext/kopos_connector/tests/test_promotion_workflow.py`

If this file becomes too large, split out a second test module.

### Add These Test Names Or Equivalent

1. `test_compute_snapshot_content_hash_is_stable_for_unchanged_effective_promotions`
2. `test_build_snapshot_version_from_hash_is_deterministic`
3. `test_ensure_persisted_snapshot_reuses_existing_snapshot_for_same_effective_content`
4. `test_get_promotion_snapshot_payload_returns_persisted_snapshot_only`
5. `test_publish_promotion_snapshot_returns_unchanged_when_effective_content_matches`
6. `test_publish_promotion_snapshot_creates_new_snapshot_when_effective_content_changes`
7. `test_reconcile_promotion_payload_matches_by_exact_version`
8. `test_reconcile_promotion_payload_falls_back_to_hash_when_version_lookup_fails`
9. `test_reconcile_promotion_payload_reports_profile_mismatch_when_version_exists_under_other_profile`
10. `test_reconcile_promotion_payload_still_flags_review_when_no_persisted_evidence_exists`
11. `test_snapshot_deletion_blocked_when_referenced_by_invoice`
12. `test_repair_promotion_reconciliation_invoices_repairs_matchable_legacy_invoices`

### Minimal Mock Shape For Fallback Test

For the hash fallback test, structure it like this:

```python
payload = {
    "pricing_context": {
        "snapshot_version": "missing-version",
        "snapshot_hash": "hash-1",
    },
    "applied_promotions": [{"promotion_id": "PROMO-1", "amount": 4.5}],
    "order": {
        "items": [
            {
                "promotion_allocations": [
                    {"promotion_id": "PROMO-1", "amount": 4.5}
                ]
            }
        ]
    },
}
```

Patch:

- `get_snapshot_by_version` to return `None`
- `get_snapshot_by_hash` to return a valid snapshot doc

Expected:

- reconciliation status is `matched`

### Minimal Mock Shape For Profile Mismatch Test

Patch:

- `get_snapshot_by_version` to return `None`
- `get_snapshot_by_hash` to return `None`
- `get_snapshot_by_version_any_profile` to return a snapshot doc with different `pos_profile`

Expected:

- reconciliation status is `review_required`
- message is `Promotion snapshot profile mismatch`
- severity is `major`

## Step 11: Update Invoice Repair Logic Safely

### File

- `erpnext/kopos_connector/kopos_connector/api/orders.py`

### Action

If needed, add a small helper to reuse invoice metadata update logic during repair instead of duplicating field write logic.

Suggested helper shape:

```python
def apply_invoice_reconciliation_result(invoice: Any, metadata: dict[str, Any]) -> None:
    ...
```

Use this only if it simplifies repair implementation cleanly.

## Step 12: Docker Validation Script

Run this end-to-end sequence after coding and tests.

### Required Sequence

1. Ensure Docker stack is up.
2. Run migration if a patch or schema change was added.
3. Create or verify an active promotion in `KoPOS Main`.
4. Call snapshot fetch once and record version.
5. Call snapshot fetch again and verify same version.
6. Submit an order using that snapshot.
7. Verify invoice reconciliation is `matched`.
8. Change effective promotion content.
9. Fetch again and verify version changed.
10. Submit a new order.
11. Verify reconciliation is `matched` again.
12. Run repair utility against seeded legacy data if available.

### Required Verification Notes To Capture

Record these in the task output:

- first snapshot version
- second snapshot version
- whether they matched before content change
- third snapshot version after content change
- whether the third version differed
- invoice names tested
- reconciliation statuses observed

## Step 13: Final Command Checklist

Run all of these before claiming success.

From `erpnext/kopos_connector`:

```bash
python -m unittest tests.test_promotion_workflow
```

From `erpnext` if migration or repair patch was added:

```bash
docker compose -f docker-compose.v16-smoke.yml ps
docker compose -f docker-compose.v16-smoke.yml exec -T backend bench --site test.localhost migrate
```

Then run the relevant snapshot and invoice verification commands in Docker.

## Strict Done Definition

The work is not done unless every item below is true.

1. unchanged effective promotions reuse the same persisted snapshot version
2. changed effective promotions create a new persisted snapshot version
3. fetch never returns a non-persisted identity
4. reconciliation matches by version or hash fallback when valid persisted evidence exists
5. reconciliation still fails safely when no persisted evidence exists
6. delete protection still works
7. repair utility fixes recoverable legacy invoices
8. unit tests pass
9. Docker validation passes

## Common Failure Modes To Avoid

1. Do not hash `published_at`
2. Do not hash generated timestamps
3. Do not fallback to current live promotions during reconciliation
4. Do not create a new snapshot on every publish click
5. Do not return a transient in-memory snapshot version
6. Do not silently auto-approve broken invoices without persisted evidence
