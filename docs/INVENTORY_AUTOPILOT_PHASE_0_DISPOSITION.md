# Inventory Autopilot Phase-0 disposition

Evidence type: static source/worktree disposition.  This document is not a
restored-data, device, canary, or production-acceptance result.

## Binding

| Binding | Value |
| --- | --- |
| Campaign | `pos_erp_inventory_autopilot_2026_08` (schema v2) |
| Inventory treatment | `included` / `included_evaluated` / acceptance required |
| POS authoritative baseline | `6b2c45b17a521329a6ba06187411b2f91bcbd4ad` |
| ERP authoritative baseline | `22417d6f102407a16b161495f5666f827a1a7eda` |
| POS redesign checkpoint in history | `1e13c5919acefc3b2dfac4b9c9a1a8198a6562ff` |
| ERP redesign checkpoint in history | `eae824a602ac455e540cc1a35fc244f2d6e8a3e7` |
| POS current inspected tip | `e85fc074c436d61b7b4f38e91e27d9ff953ef618` |
| ERP current inspected tip | `e2418b0b9859e2a515dfaa3daf3244485110bbaf` |
| Source condition | Dirty worktrees; not a production artifact |

The baseline and checkpoint SHAs are commit identities only.  They do not
claim that their branches are clean, installed, or accepted.  The parked
inventory branches remain read-only reference and are not release sources.

## Disposition by vertical slice

“Keep” means retain the intent and current final owner.  “Revise” means the
surface may be retained only after the listed authority and behavioral gates
are satisfied.  “Remove” means do not include the duplicate, demo, or parked
surface in the release source.

| Slice | Current final owner | Disposition | Required boundary |
| --- | --- | --- | --- |
| Campaign and release governance | `JiJiPOS/config/release-campaigns/` and `JiJiPOS/scripts/` | Keep + revise | Derive inventory treatment from the campaign bound to each receipt; preserve schema-v1 historical bytes and keep checkout/payment/shift fences. |
| Canonical recipe and modifier authoring | `worktree-fnb-erpnext/kopos_connector/kopos/doctype/fb_recipe/`, `fb_recipe_component/`, `fb_recipe_modifier_effect/`, `kopos/services/inventory_autopilot/recipe_compiler.py` | Keep + revise | Commission real ingredient authorities; retain historical stub references without treating them as current stock recipes; use one Decimal compiler. |
| Ingredient projection and COGS | `worktree-fnb-erpnext/kopos_connector/kopos/services/inventory_autopilot/projection_worker.py`, `stock_issue_service.py`, `FB Projection Log`, `FB Resolved Sale` | Keep + revise | Separate worker/lease from commercial retries; prove one resolved sale, one Material Issue Stock Entry, Stock Ledger and GL on the exact candidate. |
| Availability overlay and holds | `worktree-fnb-erpnext/kopos_connector/kopos/services/inventory_autopilot/overlay.py`, `holds.py`, and the inventory API | Keep + revise | Preserve the base catalog when optional inventory work fails; prove precedence, stale behavior, acknowledgement, and matching automation restore. |
| Counts and opening cutover | `worktree-fnb-erpnext/kopos_connector/kopos/doctype/fb_inventory_count_*`, `FB Inventory Policy`, inventory API; POS guided count repository | Keep + revise | Opening stock must use standard Stock Reconciliation; blind observations remain immutable and idempotent; no historical ingredient backfill. |
| Forecast and replenishment | `worktree-fnb-erpnext/kopos_connector/kopos/services/inventory_autopilot/forecast.py`, `planning.py`, `replenishment.py`, `FB Inventory Plan` | Keep + revise | Use only post-cutover resolved consumption; remain `Not ready`/`Please check` until the 14+14 evidence gate and shelf-life checks pass. |
| POS persistence and guided UI | `JiJiPOS/apps/mobile/src/db/inventory-stock-persistence.ts`, `sqlite-inventory-count-repository.ts`, `sqlite-inventory-guided-task-repository.ts`, `JiJiPOS/kopos/src/services/inventory-autopilot/` | Keep + revise | Keep checkout/session ownership intact; task drafts and outbox commands must survive restart/offline and expose only outlet-operational data. |
| ERP Menu & Recipes page | `worktree-fnb-erpnext/kopos_connector/page/jiji_menu_recipes/` plus standard Item/BOM/FB Recipe forms | Keep + revise | One director-facing commissioning surface; standard documents remain authorities; no parallel authoring draft engine. |
| ERP Stock Autopilot page | `worktree-fnb-erpnext/kopos_connector/page/jiji_stock_autopilot/` plus standard ERPNext documents | Keep + revise | Read model and navigation only; exceptions remain the durable state authority; Draft POs remain unsent and unsubmitted. |
| Café demo/seed implementation | Parked branch/demo-only paths | Remove from release | Retain only compact isolated fixtures with explicit prefixes; never use demo data as restored production evidence. |
| Duplicate ERP page paths and parked branch copies | Any non-authoritative duplicate path or parked branch | Remove from release | Do not merge or package them; delete only after reference checks and a separate cleanup change. |

## Authority decisions

* Standard ERPNext Item, UOM, Warehouse, Bin, Stock Ledger, BOM, Batch,
  Work Order, Stock Reconciliation, Material Request, Purchase Receipt and
  Purchase Order remain document authorities.
* `FB Recipe` owns immutable made-to-order recipe versions; `FB Resolved Sale`
  owns frozen sale-time component vectors; `FB Projection Log` owns processing
  state; `FB Inventory Policy` owns cutover and automation state.
* POS owns offline drafts and durable commands only.  It never writes the ERP
  stock ledger or exposes COGS, valuation, margin, supplier price, or PO
  authority to outlet users.
* Existing commercial checkout, payment, invoice, printing, shift and
  acknowledgement paths remain independent of inventory success.

## Evidence still required

The following are explicitly unclaimed by this static disposition:

1. Build and install of one exact candidate ERP wheel and signed Android
   artifact.
2. Restored-backup migration twice, with the matrix generated from that exact
   installed candidate.
3. Positive inventory acceptance from the separate
   `kopos.restored-inventory-acceptance.v1` producer.
4. Real Frappe/MariaDB/Redis projection, accounting, locks, scheduler and
   restart proof.
5. Chrome stakeholder workflow screenshots, ADB tablet acceptance, novice
   acceptance, pilot days, forecast maturation, and outlet rollout gates.

## Read-only matrix invocation

No second smoke implementation was added in this slice.  The existing
`JiJiPOS/scripts/erp-test.sh` remains the smoke authority and is a shared hot
file.  Once the exact candidate is installed inside the contained restored
site, the operator can run the module through the existing Bench context:

```bash
bench --site <restored-site> execute \
  kopos_connector.acceptance.restored_outlet_matrix.run_v1 \
  --kwargs '{"restored_backup_sha256":"<backup-sha256>","erp_artifact_sha256":"<wheel-sha256>","expected_connector_version":"<version>","erp_commit":"<40-char-sha>","pos_commit":"<40-char-sha>"}'
```

The command performs reads only and emits a report with `readOnly: true`,
`providerNetworkCalls: 0`, explicit fixture exclusion, sanitized hashes/counts,
and `missingAuthorities`.  It must be run only after the candidate binding is
known; a successful command does not waive the positive inventory-acceptance
or time-bound rollout gates above.

## Matrix contract (v1)

The stable root fields are `schemaVersion`, `contractId`, `status`,
`evidenceLevel`, `producer`, `readOnly`, `providerNetworkCalls`,
`connectorVersion`, `erpArtifactSha256`, `restoredBackupSha256`, `scope`, the
optional source commit bindings, `fixtureExclusion`, the category objects,
`missingAuthorities`, `sourceCounts`, and `permissionAudit`.  The permission
audit is read-only and binds actual POS users to their existing `Has Role` and
`DocPerm` rows.  It reports hashed user/outlet identities, legacy manager
roles, sensitive stock/valuation/purchasing permissions, and explicit
System Manager (technical admin) and Company Director (business-authorized)
handling.  A POS user with a legacy manager role or effective sensitive ERP
permission, or a POS user whose role/permission evidence cannot be read,
marks the matrix `blocked`; preflight never creates or assigns a role bundle.
The technical `KoPOS Device API` direct-read permissions on the existing
promotion/snapshot, modifier-catalog, and Maybank QR records remain only as
audited compatibility paths for live scoped APIs.  Inventory count and hold
records have no generic device DocType permission.

Each category has:

```text
realCount: integer
fixtureCount: integer
configured: boolean
ready: boolean
missingReasons: string[]
```

The required categories are `companies`, `outlets`, `warehouses`, `pos`,
`people`, `accounts`, `items`, `recipes`, `modifiers`, `uoms`, `suppliers`,
`quotations`, `bomManufacturing`, `availabilityLegacy`, `operatingDays`,
`health`, `monitoring`, and `operationalOwners`.  Fixture classification is
limited to the explicit prefixes or marker fields named in the report; a
normal-looking authority is never silently reclassified.  A missing authority
keeps the report structurally valid and appears in both the category reason
and the root `missingAuthorities` list.
