# Promotion Snapshot Fix Plan

This file is the exact execution plan for permanently fixing the KoPOS promotion reconciliation error:

`Referenced promotion snapshot was not found`

The plan is written so a low-context agent can execute it safely and consistently.

## Problem Statement

The backend currently allows promotion snapshot identity to drift even when the effective promotion rules did not materially change.

This breaks reconciliation because invoices store historical snapshot evidence, but reconciliation later tries to find an exact persisted snapshot row.

The old failure mode was worse: the API could return a snapshot identity that was never persisted at all.

The permanent fix is to make snapshot identity deterministic, persisted, immutable, and reusable.

## Required End State

After this work is complete, all of the following must be true:

1. `get_promotion_snapshot` never returns a fabricated or non-persisted snapshot identity.
2. Snapshot identity changes only when the effective promotion content changes.
3. Repeated fetches with unchanged effective promotions return the same snapshot version.
4. Reconciliation can match invoices against persisted snapshots by exact version and safe fallback rules.
5. Historical snapshot rows remain available for audit and reconciliation.
6. Existing broken invoices can be repaired in bulk where valid evidence still exists.
7. Docker validation proves the whole flow works end to end.

## Files To Change

Primary backend file:

- `erpnext/kopos_connector/kopos_connector/api/promotions.py`

Possible supporting files:

- `erpnext/kopos_connector/kopos_connector/api/__init__.py`
- `erpnext/kopos_connector/kopos_connector/api/orders.py`
- `erpnext/kopos_connector/kopos_connector/kopos/doctype/kopos_promotion_snapshot/kopos_promotion_snapshot.py`
- `erpnext/kopos_connector/tests/test_promotion_workflow.py`
- `erpnext/kopos_connector/patches.txt`
- `erpnext/kopos_connector/patches/<new_patch_file>.py`

Optional if schema/index support is needed:

- doctype metadata or migration patch for unique enforcement on snapshot identity

## Non-Negotiable Rules

1. Never return a snapshot version unless that exact snapshot row exists in the database.
2. Never base snapshot identity on publish time.
3. Keep snapshot rows immutable once created.
4. Never auto-delete a snapshot that may be referenced by invoices.
5. Reconciliation must validate historical evidence, not current live promotion state.

## Implementation Strategy

### Step 1: Refactor snapshot construction into deterministic pieces

In `erpnext/kopos_connector/kopos_connector/api/promotions.py`, split the current snapshot logic into separate functions.

Add or refactor helpers with these responsibilities:

1. `build_effective_snapshot_body(pos_profile: str, at_time: datetime | None = None) -> dict[str, Any]`
   - Returns only the effective promotion content.
   - Includes:
     - `pos_profile`
     - normalized active promotions
   - Excludes:
     - `published_at`
     - generated timestamps used only for metadata
     - any volatile values that change on every request

2. `compute_snapshot_content_hash(body: dict[str, Any]) -> str`
   - Uses deterministic JSON serialization.
   - Hashes only the effective content body.

3. `build_snapshot_version_from_hash(snapshot_hash: str) -> str`
   - Builds a stable version from content, not from current time.
   - Example format is acceptable if stable and deterministic.
   - Do not include a timestamp.

4. `build_persisted_snapshot_payload(pos_profile: str, at_time: datetime | None = None) -> dict[str, Any]`
   - Uses the deterministic body and hash.
   - Adds metadata fields for persistence and API response.

Goal of this step:

- If the effective promotions are identical, the hash and version must also be identical.

### Step 2: Add a single source of truth for snapshot persistence

In `erpnext/kopos_connector/kopos_connector/api/promotions.py`, implement:

- `ensure_persisted_snapshot(pos_profile: str, at_time: datetime | None = None) -> Any`

This function must:

1. Build the deterministic snapshot payload.
2. Look for an existing snapshot row for the same `pos_profile` and `snapshot_hash`.
3. If found, return that persisted row.
4. If not found, create a new `KoPOS Promotion Snapshot` row and insert it.
5. Handle duplicate creation races safely.

This function becomes the only valid path for producing a snapshot identity.

### Step 3: Route both fetch and publish through the same persistence path

Update `get_promotion_snapshot_payload()` in `erpnext/kopos_connector/kopos_connector/api/promotions.py`.

New behavior:

1. Resolve the POS profile.
2. Determine the effective promotions.
3. If there are no effective promotions and the intended business behavior is still "unavailable", return `None`.
4. If there are effective promotions, call `ensure_persisted_snapshot()`.
5. Return data from the persisted snapshot row only.

Then update `publish_promotion_snapshot()` in the same file.

New behavior:

1. Call the same `ensure_persisted_snapshot()` function.
2. If a matching persisted snapshot already exists, return `status: "unchanged"`.
3. If a new row was created, return `status: "published"`.

Important:

- Do not keep a separate logic path for fetch and publish.
- Both operations must use the same deterministic snapshot generation and lookup code.

### Step 4: Make snapshot lookup more resilient for reconciliation

Update `reconcile_promotion_payload()` in `erpnext/kopos_connector/kopos_connector/api/promotions.py`.

Current behavior is too narrow because it only checks:

- `pos_profile + snapshot_version`

Implement this lookup order:

1. Find by exact `pos_profile + snapshot_version`
2. If not found and `snapshot_hash` exists, find by exact `pos_profile + snapshot_hash`
3. If still not found, try finding the same `snapshot_version` under any profile
4. If version exists under another profile, return a specific reconciliation message such as:
   - `Promotion snapshot profile mismatch`
5. If still not found, return:
   - `Referenced promotion snapshot was not found`

This keeps audit strictness while reducing false negatives.

### Step 5: Keep historical snapshots immutable and undeletable when referenced

Review `erpnext/kopos_connector/kopos_connector/kopos/doctype/kopos_promotion_snapshot/kopos_promotion_snapshot.py`.

Make sure:

1. Referenced snapshots cannot be deleted.
2. Superseded snapshots remain readable.
3. No cleanup logic removes rows that invoices may still need.

If current delete protection already exists, verify it covers all invoice reference paths.

### Step 6: Add uniqueness and race safety

Prevent duplicate snapshot rows for the same effective content.

Preferred uniqueness rule:

- unique on `pos_profile + snapshot_hash`

Implement this via one of these approaches:

1. database unique index through a patch
2. doctype-level constraint if appropriate
3. defensive duplicate handling on insert plus re-read after failure

If adding schema/index changes, create a patch file and register it in `erpnext/kopos_connector/patches.txt`.

### Step 7: Add a repair path for already broken invoices

Create a backend patch or utility script that scans existing `POS Invoice` rows with promotion reconciliation issues.

The repair logic should:

1. Read invoice promotion metadata.
2. Extract:
   - `pos_profile`
   - `pricing_context.snapshot_version`
   - `pricing_context.snapshot_hash`
   - `applied_promotions`
3. Re-run reconciliation using the new fallback logic.
4. If the invoice now matches by version or hash, update its reconciliation fields so it no longer remains stuck in review.
5. Leave truly unrecoverable invoices untouched for manual review.

Do not silently approve invoices that still lack valid evidence.

### Step 8: Add a support diagnostic helper

Add a small diagnostic utility in the backend that can answer, for any invoice:

1. invoice name
2. invoice `pos_profile`
3. invoice `snapshot_version`
4. invoice `snapshot_hash`
5. whether a snapshot exists by exact version
6. whether a snapshot exists by exact hash
7. whether the version exists under another profile
8. final reconciliation result

This can be a utility function, admin API, or patch helper, but it must make support work easy.

## Detailed Execution Checklist

Execute these items in order.

### Phase A: Refactor core logic

1. Open `erpnext/kopos_connector/kopos_connector/api/promotions.py`.
2. Identify the existing functions:
   - `get_promotion_snapshot_payload`
   - `publish_promotion_snapshot`
   - `build_snapshot_payload`
   - `build_snapshot_version`
   - `get_latest_published_snapshot`
   - `get_snapshot_by_version`
3. Replace timestamp-driven identity generation with deterministic content-driven identity.
4. Keep publish metadata separate from content identity.
5. Ensure all returned snapshot payloads come from persisted rows.

### Phase B: Add lookup helpers

Add helper functions as needed, such as:

1. `get_snapshot_by_hash(pos_profile: str, snapshot_hash: str)`
2. `get_snapshot_by_version_any_profile(snapshot_version: str)`
3. `get_snapshot_by_hash_any_profile(snapshot_hash: str)` if useful
4. `ensure_persisted_snapshot(...)`

Keep function responsibilities narrow and explicit.

### Phase C: Improve reconciliation

1. Update `reconcile_promotion_payload()` to use the new lookup order.
2. Preserve strict audit semantics.
3. Return better failure messages where possible.
4. Keep severity mapping correct.
5. Do not downgrade real major failures into matched results without evidence.

### Phase D: Add migration or repair support

1. Decide whether this belongs in a patch file or admin utility.
2. If patch-based, add a new file under `erpnext/kopos_connector/patches/`.
3. Register it in `erpnext/kopos_connector/patches.txt`.
4. Make the patch idempotent.
5. Log or count repaired invoices.

### Phase E: Tests

Update `erpnext/kopos_connector/tests/test_promotion_workflow.py`.

Add tests for all of these cases:

1. unchanged effective promotions produce the same content hash
2. unchanged effective promotions reuse the same snapshot version
3. fetching snapshot twice with unchanged rules returns the same persisted row
4. publishing unchanged rules returns `unchanged`
5. changing effective content produces a new version
6. reconciliation matches by exact version
7. reconciliation matches by hash fallback when version lookup fails
8. reconciliation reports profile mismatch if version exists under another profile
9. reconciliation still returns review-required when no evidence exists
10. referenced snapshots cannot be deleted
11. duplicate creation race resolves safely to one persisted snapshot identity

If test structure becomes too large, split tests into a second promotion-specific test module.

### Phase F: Docker verification

Run validation inside the ERPNext Docker environment.

Required scenario test:

1. create or confirm an active promotion
2. fetch a snapshot twice
3. verify the same version is returned both times
4. submit an order using that snapshot
5. verify reconciliation is `matched`
6. change effective promotion content
7. fetch again
8. verify a new version is returned
9. submit another order
10. verify reconciliation is still `matched`
11. run the repair path on seeded bad data if available

## Acceptance Criteria

Do not mark this work complete unless all of these are true:

1. Same effective promotions always produce the same snapshot version.
2. New snapshot versions appear only when the effective promotion content changes.
3. No backend endpoint returns a snapshot version that is not persisted.
4. Reconciliation can recover from legacy version mismatch if a valid snapshot hash exists.
5. Historical invoices reconcile correctly against stored snapshots.
6. Broken legacy invoices are reduced through automated repair where evidence exists.
7. Tests pass.
8. Docker validation passes.

## Suggested Command Sequence

Use these as the final verification steps after implementation.

From `erpnext/kopos_connector`:

```bash
python -m unittest tests.test_promotion_workflow
```

From `erpnext` using Docker:

```bash
docker compose -f docker-compose.v16-smoke.yml ps
docker compose -f docker-compose.v16-smoke.yml exec -T backend bench --site test.localhost migrate
docker compose -f docker-compose.v16-smoke.yml exec -T backend bench --site test.localhost execute kopos_connector.api.promotions.publish_promotion_snapshot --kwargs '{"pos_profile":"KoPOS Main"}'
```

Add any additional project-standard validation commands required by the repo.

## Notes For The Implementing Agent

1. Do not touch `kopos_connector-modifiers`.
2. Do not weaken reconciliation just to hide the bug.
3. Do not use current live promotions as proof for historical invoices.
4. Make the persisted snapshot row the source of truth.
5. Keep the fix forward-safe and audit-safe.

## Deliverables

The final implementation should include all of the following:

1. backend code changes in `promotions.py`
2. any needed helper updates in related backend files
3. tests covering the stable identity and reconciliation fallback behavior
4. repair patch or repair utility for historical invoices
5. proof of Docker verification
