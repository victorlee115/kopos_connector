# Restored Inventory Autopilot preflight

Source backup: `/private/tmp/20260811_142652-jijierp_cathouse_io-database.sql.gz`

Verified before any restore attempt on 2026-08-15 (Asia/Kuala_Lumpur):

- SHA-256: `969df338233890bae22e3bc29a975dfe7c064475286ec505fc5b099cd72ed697`
- `gzip -t`: passed
- `FB Recipe`: 25 rows, all pre-cutover identity-stub recipes
- `FB Resolved Sale`: 2,546 rows and `FB Resolved Component`: 2,546 rows
- `FB Order`: 2,190 rows
- `Item`: 34 rows; no active ingredient master was inferred
- `Supplier`, `Bin`, `Stock Ledger Entry`, `BOM`, `Batch`, `Stock Entry`, and `Purchase Receipt`: 0 rows

The backup therefore proves commercial resolution plumbing only. It does not
provide production inventory authority. No historical recipe, resolved sale,
Stock Entry, or accounting record is rewritten by the redesign.

## Required commissioning before pilot cutover

1. A Company Director classifies every Item as sellable, purchased stock,
   prepared component, packaged good, service, or approved exclusion.
2. Real ingredient Items, stock/purchase UOM conversions, supplier packs,
   shelf life, batch/expiry rules, warehouses, and accounts are created.
3. Directors physically measure and approve new FB Recipe versions and
   standard BOMs for prepared components such as cold foam and orange juice.
4. Suppliers, quotations, lead times, transfer warehouses, dimensions, staff
   access, devices, monitor destination, and pilot outlet are configured from
   the generated outlet matrix; none are guessed from this backup.
   Staff access preflight verifies that every active central record points to
   an existing enabled User and an active Employee whose `user_id` matches
   exactly.  It also discovers the real POS users' `Has Role`/`DocPerm`
   assignments and blocks legacy manager or sensitive stock/purchasing ERP
   access; System Manager is reported as technical administration and Company
   Director as the authorised business role.
5. A standard opening Stock Reconciliation is submitted before an outlet
   policy can activate its immutable cutover token.

The restored-data rehearsal must run with email, notifications, webhooks,
Server Scripts, supplier/provider calls, printers, and other outbound effects
fenced. Docker/Frappe v16 was not available on the development host during
this preflight, so no restore or live ERP acceptance is claimed here.
