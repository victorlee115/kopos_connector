# KoPOS ERP Connector Agent Rules

These rules apply to the entire ERP connector repository. They are release
requirements, not suggestions.

## Non-negotiable methodology

- Production readiness belongs to an exact connector commit, wheel SHA-256,
  target configuration, migrated database, and running process set. A branch,
  merge, version bump, tag, or mocked test run is not production acceptance.
- Label evidence accurately as static inspection, mocked unit test, real
  Frappe integration, restored-production-data rehearsal, canary, or live
  verification. Never use a lower-fidelity result to claim a higher one.
- Changes that depend on DocType defaults, MariaDB values, transactions,
  locks, Redis, scheduler behavior, or ERPNext accounting must have a test
  against the real service involved. Mocks may supplement but never replace it.
- Before release, install the exact candidate wheel on a restored recent
  production backup, run migration twice, and generate the complete catalog for
  every enabled POS device/profile. The second migration must be idempotent.
- Build once, then test and deploy the same wheel bytes. Do not copy individual
  Python files into a live Bench.
- After deployment, restart every web, scheduler, short-worker, and long-worker
  process. Each process must prove the same connector artifact identity before
  traffic is accepted.
- A required gate that is unavailable, stale, tied to another artifact, or not
  run is a blocker. Do not describe the candidate as production-ready.
- Repeating scheduler, catalog, accounting, deadlock, or reconciliation errors
  stop rollout and canary expansion.
- Real Frappe integration is mandatory for persistence-dependent release proof.

## ERP invariants

- Store and compare money as exact integer sen at API boundaries and explicit
  `Decimal` values inside ERP accounting. Never introduce float authority.
- Preserve idempotency: one sale identity creates exactly one FB Order and one
  Sales Invoice. Retried requests must return the existing result.
- A verified QR payment must resolve to an enabled, non-group Bank or approved
  untyped Asset clearing account for the same company and currency, never Cash.
- The Sales Invoice payment row must retain the exact FB Order source payment
  identity. A fully paid sale must have zero outstanding balance.
- Catalog validation and FB Order validation must use one shared interpretation
  of modifier bounds. Frappe `Int` fields persist an unset value as zero; tests
  must use production-shaped values.
- Telemetry such as a device heartbeat must not turn a read-only catalog or
  shift request into a shared-row write bottleneck.
- Provider polling locks must be proven with the installed Frappe Redis wrapper,
  including acquisition, renewal, ownership-safe release, and concurrency.

## Current inventory exclusion

Inventory is being redesigned outside this work. Unless a user explicitly
changes the scope, do not modify or claim acceptance for ingredient Items,
recipe components used for stock, quantities, UOM conversions, Stock Entries,
stock projections, warehouses, COGS configuration, or inventory balances.
Regression fixtures for non-inventory work must use non-stock items and must not
post stock movements.

## Minimum verification for affected code

1. Run focused mocked tests while iterating.
2. Run the complete connector contract suite.
3. Run affected tests inside a real Frappe/MariaDB/Redis Bench.
4. For catalog or migration changes, run the populated-backup full-catalog
   rehearsal.
5. For payment or accounting changes, inspect the submitted invoice, payment
   identity, account type, GL entries, and outstanding balance.
6. Record failures and blockers explicitly; never convert an unrun gate to a
   pass.
