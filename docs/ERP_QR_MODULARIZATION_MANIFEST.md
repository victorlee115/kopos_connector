# ERP QR modularization reconstruction manifest

## Purpose and immutable source

This manifest accounts for every path in the 63-path ERP candidate delta that
was preserved before modularization. It is a reconstruction ledger, not release
or physical-acceptance evidence.

- Candidate base: `0cc5955`
- Archival safety branch: `safety/erp-pre-modularization-20260717`
- Archival snapshot commit: `79637b7`
- Reconstruction branch: `feat/erp-contract-invariants`
- Release rule: the archival snapshot is never merged or packaged.

No source record, evidence field, operator instruction, or test from the
snapshot is intentionally discarded. Differences between the archival snapshot
and the reconstructed candidate are limited to the integrity fixes described in
C8, mechanical module moves, explicit import ownership, and tests for those
fixes and moves.

## Target commit ledger

| ID | Target commit subject | Scope |
| --- | --- | --- |
| C1 | `chore(erp): add automatic QR schema and indexes` | Additive DocType fields, accounting settings, audit fields, indexes, and idempotent install/migration work. |
| C2 | `feat(erp): persist prepared automatic QR sales` | Immutable prepared-sale snapshots, exact identities, and preparation lifecycle. |
| C3 | `feat(erp): reconcile offline QR payments` | Receipt-backed offline completion and suspense reclassification/failure accounting. |
| C4 | `feat(erp): account for duplicate automatic QR payments` | Duplicate detection, liability recognition, refund posting, and terminal proof. |
| C5 | `feat(erp): finalize provider-paid automatic QR sales` | Provider-paid finalization and scheduler recovery. |
| C6 | `feat(erp): harden Maybank generation and polling` | Maybank contract, generation, status, persistence, rate limit, support resolution, and polling. |
| C7 | `feat(erp): isolate guarded Maybank payment simulation` | Test-only Desk simulation and its capability guards. |
| C8 | `fix(erp): enforce shift and safe-reset settlement fences` | Typed shift conflicts, operational locking, durable provider-rejection fences, and live duplicate-refund terminal evidence. |
| C9 | `test(erp): prove business state and document operations` | Smoke state proof, packaging inventory coverage, operator documentation, contracts, the fixed build-tool pin, and this manifest. |

## Complete 63-path snapshot mapping

When one snapshot path contains independent hunks, every owning commit is
listed. A facade entry means the public import or Frappe method remains at the
snapshot path while its implementation moves to the derived modules listed
below.

| # | Snapshot path | Target | Hunk or compatibility disposition |
| ---: | --- | --- | --- |
| 1 | `INSTALL.md` | C9 | Bank-versus-Cash setup, isolated simulator enablement, teardown, and production warning. |
| 2 | `README.md` | C9 | Shift conflict, prepared sale, Maybank, offline reconciliation, duplicate-refund, and operator-facing API guidance. |
| 3 | `docs/DUPLICATE_AUTOMATIC_QR_REFUND_CONTRACT.md` | C9 | Duplicate liability/refund contract, strengthened to describe C8 live GL and retained-file proof. |
| 4 | `docs/FB_SHIFT_OPEN_CONFLICT_CONTRACT.md` | C9 | Exact identity-bound typed-conflict and local-release contract. |
| 5 | `docs/MAYBANK_QR_PREFLIGHT_REJECTION_CONTRACT.md` | C9 | Durable preflight-rejection and ambiguous-generation contract. |
| 6 | `docs/MAYBANK_QR_TEST_SIMULATION_CONTRACT.md` | C9 | Non-production simulator authorization, audit, replay, visibility, and cleanup contract. |
| 7 | `docs/QR_RECONCILIATION_ACCOUNTING_CONTRACT.md` | C9 | Suspense-to-bank, suspense-to-variance, and late provider recovery accounting contract. |
| 8 | `kopos_connector/api/__init__.py` | C2, C4, C6, C7, C8 | Preserve public prepare/cancel, duplicate refund, Maybank, support-resolution and simulation exports; C8 carries the `get_device_open_shift` staff-conflict wrapper shape. |
| 9 | `kopos_connector/api/automatic_qr.py` | C6 | Prepared-sale cancellation and provider-attempt proof; private Maybank imports move to explicit owner modules. |
| 10 | `kopos_connector/api/device_safe_reset.py` | C8 | Durable no-provider release fence and shared duplicate-refund terminal-evidence gate. |
| 11 | `kopos_connector/api/devices.py` | C8 | Device operational mutation lock and Administrator/device-role separation. |
| 12 | `kopos_connector/api/duplicate_qr_payment.py` | C4 | Thin public duplicate-refund API facade. |
| 13 | `kopos_connector/api/manual_qr_receipt.py` | C3 | Immutable receipt evidence upload and reconciliation request handling. |
| 14 | `kopos_connector/api/maybank_qr.py` | C6, C7 | Thin compatibility facade; generation/status/support move in C6 and simulation export moves in C7. |
| 15 | `kopos_connector/api/shifts.py` | C8 | Typed, exact identity-bound open-shift conflict responses. |
| 16 | `kopos_connector/auth.py` | C2, C6 | Operational mutation lock coverage for prepared-sale and Maybank mutations. |
| 17 | `kopos_connector/extensions/journal_entry.py` | C4 | Duplicate-payment Journal Entry audit validation. |
| 18 | `kopos_connector/extensions/sales_invoice.py` | C4 | Winning-invoice/void binding used by duplicate terminal proof. |
| 19 | `kopos_connector/hooks.py` | C4, C5 | DocType extension wiring in C4 and paid-finalization scheduler path in C5. |
| 20 | `kopos_connector/install/install.py` | C1, C8 | Idempotent field/index installation in C1; C8 adds the device/duplicate-status/status lookup index used by bounded safe-reset proof. |
| 21 | `kopos_connector/kopos/api/fb_orders.py` | C2 | Immutable preparation, exact order/payment identities, and prepared-sale submission. |
| 22 | `kopos_connector/kopos/doctype/fb_order/fb_order.json` | C1 | Additive prepared-sale, QR, projection, and accounting schema. |
| 23 | `kopos_connector/kopos/doctype/fb_order/fb_order.py` | C2 | Prepared-sale validation and state invariants. |
| 24 | `kopos_connector/kopos/doctype/fb_order_payment/fb_order_payment.json` | C1 | Exact payment identity and settlement fields. |
| 25 | `kopos_connector/kopos/doctype/fb_resolved_sale/fb_resolved_sale.json` | C1 | Frozen sale/recipe resolution fields. |
| 26 | `kopos_connector/kopos/doctype/fb_shift/fb_shift.py` | C8 | Shift conflict locking, identity proof, and close fences. |
| 27 | `kopos_connector/kopos/doctype/manual_qr_reconciliation/manual_qr_reconciliation.json` | C1 | Additive receipt and reconciliation evidence fields. |
| 28 | `kopos_connector/kopos/doctype/maybank_qr_transaction/maybank_qr_transaction.js` | C7 | Guarded test-only Desk action and persistent TEST warning. |
| 29 | `kopos_connector/kopos/doctype/maybank_qr_transaction/maybank_qr_transaction.json` | C1 | Provider, preparation, reconciliation, duplicate, and evidence fields/states. |
| 30 | `kopos_connector/kopos/doctype/maybank_qr_transaction/maybank_qr_transaction.py` | C7 | Read-only controller and guarded simulation capability response. |
| 31 | `kopos_connector/kopos/install/fb_custom_fields.py` | C1 | Company accounting settings and Journal Entry audit fields. |
| 32 | `kopos_connector/kopos/services/accounting/automatic_qr_finalization_service.py` | C5 | Compatibility facade; finalization core and recovery scan move to derived modules. |
| 33 | `kopos_connector/kopos/services/accounting/duplicate_qr_payment_service.py` | C4, C8 | C4 compatibility facade and derived contract/incident/journal/refund/terminal proof; C8 hardens the derived refund resolver's operational device lock and revalidation. |
| 34 | `kopos_connector/kopos/services/accounting/maybank_payment_service.py` | C2, C3 | Canonical token normalization in C2; receipt/manual-payment authenticity and settlement behavior in C3. |
| 35 | `kopos_connector/kopos/services/accounting/qr_reconciliation_service.py` | C3 | Compatibility facade; context, success, and failure implementations move to derived modules. |
| 36 | `kopos_connector/kopos/services/accounting/sales_invoice_service.py` | C3 | Exact suspense/bank/variance invoice and GL invariants. |
| 37 | `kopos_connector/kopos/tests/test_maybank_qr_sales_invoice_flow.py` | C3, C6 | Offline/invoice behavior in C3; explicit Maybank owner-module patch target in C6. |
| 38 | `kopos_connector/kopos/tests/test_sales_invoice_service.py` | C3 | Sales Invoice, payment, and posting-account proof. |
| 39 | `kopos_connector/services/maybank/client.py` | C7 | Explicit manual mock-payment mode needed by the guarded simulator. |
| 40 | `kopos_connector/smoke.py` | C9 | Exact business dump, Bank posting, stock, invoice, projection, idempotency, and duplicate-liability proof. |
| 41 | `kopos_connector/tasks/poll_maybank.py` | C6 | Fair polling, status-owner import, retry, and late-result handling. |
| 42 | `kopos_connector/tests/test_fb_order_stock_policy.py` | C2 | Frozen recipe/component and prepared-order stock invariants. |
| 43 | `kopos_connector/tests/test_fb_schema_contract.py` | C1 | Additive field/state/index contract coverage. |
| 44 | `kopos_connector/tests/test_manual_qr_receipt_upload.py` | C3 | Private exact receipt evidence and replay coverage. |
| 45 | `kopos_connector/tests/test_manual_qr_reconciliation.py` | C3 | Success, failure, late recovery, and non-blocking settlement coverage. |
| 46 | `kopos_connector/tests/test_maybank_qr_preflight_rejection.py` | C6 | Durable rejection, ambiguity, preflight fencing, and owner-module import coverage. |
| 47 | `kopos_connector/tests/test_pos_profile_config_version.py` | C4 | Hook registry coverage includes the duplicate-payment Journal Entry and Sales Invoice integrity extensions. |
| 48 | `kopos_connector/tests/test_smoke_reliability_seed_contract.py` | C9 | Deterministic seed/reset, Bank-versus-Cash, business-state dump, and smoke assertion contract. |
| 49 | `tests/test_automatic_qr_finalization.py` | C5 | Core finalization, restart recovery, scheduler, lock, and idempotency coverage. |
| 50 | `tests/test_automatic_qr_prepared_sale.py` | C2 | Immutable snapshot, exact identity, money, company, currency, shift, recipe, customer, and stock coverage. |
| 51 | `tests/test_device_operational_mutation_lock.py` | C2, C3, C6, C8 | Deterministic operational lock coverage for each new mutation family and safe reset. |
| 52 | `tests/test_device_safe_reset.py` | C8 | Durable provider fence, exact terminal refund proof, tamper, workload cap, replay, and lock-order coverage. |
| 53 | `tests/test_duplicate_automatic_qr_refund.py` | C4, C8 | Duplicate lifecycle/accounting in C4; exact GL/file/void/company terminal-proof hardening in C8. |
| 54 | `tests/test_maybank_payment_authenticity.py` | C3 | Authentic provider/manual evidence and immutable settlement identity. |
| 55 | `tests/test_maybank_poll_priority.py` | C6 | Fair eligible polling and recovery/backoff coverage. |
| 56 | `tests/test_maybank_qr.py` | C6 | Generation, status, support resolution, persistence, rate limits, late results, and facade compatibility. |
| 57 | `tests/test_maybank_qr_desk_simulation.py` | C7 | POST/role/flag/phrase/audit/replay/warning/production-hiding coverage. |
| 58 | `tests/test_pos_provisioning.py` | C8 | Administrator/device-role separation and provisioning/safe-reset interaction. |
| 59 | `tests/test_prepared_automatic_qr_cancellation.py` | C6 | Cancellation only after exact durable no-provider proof. |
| 60 | `tests/test_qr_reconciliation_accounting.py` | C3 | Exact suspense-to-bank, suspense-to-variance, and late-recovery GL proof. |
| 61 | `tests/test_shift_open_conflict_contract.py` | C8 | Exact typed device/staff conflict response and POS-parser identity shape. |
| 62 | `tests/test_task15_smoke_business_state.py` | C9 | Smoke rejection of missing/ambiguous business records and unresolved/unproved duplicate liability. |
| 63 | `tests/test_task6_shift_lifecycle.py` | C8 | Transactional shift opening, conflicts, projection/close fences, and idempotency. |

## Derived module inventory

The following runtime files are intentional mechanical extractions from the
snapshot paths above. They are release inputs and must appear in the built wheel
and artifact inventory even though they did not exist as separate paths in the
archival snapshot.

| Original snapshot path | Derived modules |
| --- | --- |
| `kopos_connector/kopos/services/accounting/qr_reconciliation_service.py` | `_qr_reconciliation_context.py`, `_qr_reconciliation_success.py`, `_qr_reconciliation_failure.py` |
| `kopos_connector/kopos/services/accounting/duplicate_qr_payment_service.py` | `_duplicate_qr_contract.py`, `_duplicate_qr_incident.py`, `_duplicate_qr_journal.py`, `_duplicate_qr_refund.py`, `_duplicate_qr_terminal_evidence.py` |
| `kopos_connector/kopos/services/accounting/automatic_qr_finalization_service.py` | `automatic_qr_finalization_core.py`, `automatic_qr_finalization_recovery.py` |
| `kopos_connector/api/maybank_qr.py` | `_maybank_qr_contract.py`, `_maybank_qr_persistence.py`, `_maybank_qr_rate_limit.py`, `_maybank_qr_prepared_sale.py`, `_maybank_qr_generation.py`, `_maybank_qr_replacement.py`, `_maybank_qr_status.py`, `_maybank_qr_resolution.py`, `maybank_qr_simulation.py` |

The compatibility facades preserve the scheduler callable, DocType-controller
imports, public Frappe method names, request/response fields, GET payment-status
compatibility, idempotency keys, database fields, and state literals. Domain
modules import their owners directly; tests patch the owner module rather than a
facade alias.

## Final intentional differences from the snapshot

The final source is allowed to differ from `79637b7` only as follows:

1. The four large new QR implementations are mechanically extracted into the
   derived modules above. The original paths become explicit compatibility
   facades and keep the same public callables.
2. Runtime private imports now point to the owning module. This includes
   `automatic_qr.py`, `poll_maybank.py`, and `smoke.py`; no production module
   reaches through the Maybank facade for a private implementation symbol.
3. Modularization-only test differences patch the owning module, not a facade
   alias. They are confined to
   `kopos_connector/kopos/tests/test_maybank_qr_sales_invoice_flow.py`,
   `kopos_connector/tests/test_maybank_qr_preflight_rejection.py`,
   `tests/test_automatic_qr_finalization.py`,
   `tests/test_duplicate_automatic_qr_refund.py`, `tests/test_maybank_qr.py`,
   `tests/test_maybank_qr_desk_simulation.py`, and
   `tests/test_qr_reconciliation_accounting.py`, plus tests added for C8.
4. A cached `provider_rejected` value is not release authority. Every affected
   draft is enumerated deterministically and revalidated through
   `has_durable_no_provider_release_fence`; the query uses a 65-row sentinel and
   fails closed above 64 drafts per safe-reset attempt.
5. A refunded duplicate payment is not release authority based on copied SQL
   metadata. The shared terminal-evidence assertion re-proves the winning
   invoice or void, exact liability and refund GL rows, private attachment,
   actual retained bytes, declared length, and SHA-256 under the exact lock
   order **Device -> Safe Reset -> FB Order -> source Maybank QR Transaction ->
   liability/refund Journal Entries -> evidence File**. The check fails closed
   above 64 refunded transactions or 64 MiB of declared or observed evidence
   per safe-reset attempt.
6. Typed shift conflicts retain their exact request identity and are accepted by
   the existing POS parser only when `local_release_authorized` and every bound
   field match.
7. C8 adds only tests for those two safe-reset integrity fixes and their
   deterministic lock/workload behavior. C9 adds the final smoke assertions,
   operator contracts, and this traceability manifest.
8. C9 updates the exact build-only `setuptools` pin from `80.9.0` to `83.0.0`
   in `pyproject.toml` and `.github/workflows/connector-ci.yml`. This closes the
   audited build-tool vulnerability without changing the connector runtime
   dependency set or any public ERP behavior.

No transaction boundary, accounting state name, request field, response field,
or provider/settlement state is intentionally renamed.

## Candidate parity and artifact evidence

Before C9 is committed, compare the reconstructed candidate against
`79637b7`. Every difference must be one of the derived module moves, import
ownership updates, the three C8 integrity changes, or their tests and this
manifest. Any other difference requires an explicit entry here before review.

The final release record must bind the clean `1.0.11` wheel, its complete runtime
file inventory, SHA-256, SBOM, ERP candidate commit, production APK hash,
campaign nonce, and physical acceptance evidence. Simulator output is test
evidence only and cannot substitute for Maybank UAT, the specified Samsung
tablet, printers, soak, canary, pilot, or required sign-off.

The smoke evidence is accepted only when it proves business state, including:

- exactly one Sales Invoice per idempotency key;
- DuitNow QR posted to the configured Bank/clearing account, never till Cash;
- item and payment rows, stock issue, FB Shift projection and totals;
- no failed projections and no active legacy POS document path;
- no unresolved or unproved duplicate-payment customer liability;
- closed shifts reject new sales; and
- the business-state dump contains the exact documents and accounting evidence.
