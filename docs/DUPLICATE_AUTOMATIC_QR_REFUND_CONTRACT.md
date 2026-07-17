# Duplicate Automatic QR Refund Contract

## Customer and cashier invariant

The first exact provider-paid Automatic QR attempt wins the prepared sale. A
later paid attempt never creates a second Sales Invoice, reopens or mutates the
winning sale, suppresses printing, or prevents the tablet from accepting the
next order. A later manager-approved void of the original sale remains valid and
does not erase the separate duplicate-payment liability.

The later provider-paid attempt is a separate customer-liability incident with
this monotonic state machine:

```text
accounting_pending -> refund_required -> refunded
```

- `accounting_pending` means the paid provider evidence is durable, but the
  liability Journal Entry has not yet been submitted and re-proved from GL.
- `refund_required` means exactly one submitted Journal Entry debits the
  configured bank/clearing Asset account and credits the configured customer
  Liability account for the exact provider-paid amount.
- `refunded` means exact provider refund evidence is stored and exactly one
  submitted Journal Entry debits that Liability and credits the same
  bank/clearing account.

Manual reconciliation states are not used for this incident. In particular, a
duplicate provider payment is not `reconciliation_failed` and must not mutate
the winning FB Order Payment.

## Required company setup

Run `bench migrate`, then configure both Company fields:

- `custom_kopos_qr_duplicate_payment_clearing_account`: enabled, non-group MYR
  Asset ledger for the provider bank/clearing balance; a party Receivable is
  rejected.
- `custom_kopos_qr_customer_liability_account`: enabled, non-group MYR
  Liability ledger for customer refunds owed; a party Payable is rejected.

The accounts must belong to the sale company and must differ. Missing or invalid
configuration leaves the incident visibly `accounting_pending`; it never rolls
back or changes the winning sale lifecycle.

The accounting and refund paths accept the original Sales Invoice either while
submitted or after an exact KoPOS void. A cancelled invoice is accepted only
when it retains its sale idempotency key, void idempotency key, request
fingerprint, approving manager, approval token, consumed manager-approval
record, exact FB Order/company/currency links, and the FB Order's matching
`Cancelled`/`Reversed` lifecycle. A generic or manually cancelled invoice fails
closed at `accounting_pending`.

The Sales Invoice controller extension also blocks direct Desk/API cancellation
of an original KoPOS sale unless those exact void fields match the FB Order and
the linked manager-approval record is already consumed for `void_order`. The
approved void endpoint records that proof before invoking cancellation in the
same transaction.

## Refund-only support action

The active method is:

```text
/api/method/kopos_connector.api.resolve_duplicate_automatic_qr_refund
```

Only a non-device System Manager session may call it. The request must contain:

- `transaction`: duplicate Maybank QR Transaction name
- `provider_transaction_refno`: exact original provider payment reference
- `provider_refund_status`: exactly `refunded`
- `provider_refund_reference`: distinct provider-issued refund reference
- `provider_refund_amount_sen`: exact positive integer amount in sen
- `provider_refund_currency`: exact currency (`MYR`)
- `provider_refund_date`: exact provider refund date in `YYYY-MM-DD`; it cannot
  precede provider payment or be in the future
- `provider_evidence_reference`: durable provider/support evidence identifier
- `provider_evidence_file`: private ERP File attached to the duplicate Maybank QR
  Transaction
- `provider_evidence_sha256`: lowercase SHA-256 which must match the retained
  private File bytes exactly
- `note`: 20-1000 character operational audit note

Exact retries return the existing submitted accounting evidence. Any changed
reference, amount, currency, refund date, evidence reference, private File,
evidence hash, or note fails closed. The
incident is marked `refunded` only after exact submitted GL evidence is re-proved.

The liability-recognition Journal Entry uses the authenticated provider
`paid_at` date, never the sale business date. The refund Journal Entry uses the
exact validated `provider_refund_date`, so a later-day or later-period refund is
not backdated into the sale period.

Offsetting the duplicate against another sale, store credit, cash drawer
adjustments, or arbitrary bank-reference entry is intentionally unsupported.
Refund through the provider is the only resolution path in this contract.

## Accounting and idempotency

Both Journal Entries carry a server-derived unique accounting key and immutable
links to the provider transaction, winning transaction, FB Order, and winning
Sales Invoice. A database uniqueness race recovers the already-created Journal
Entry and re-validates its metadata and GL rows. Neither accounting stage writes
an invoice-consumption key for the duplicate attempt.

The generated duplicate-liability and refund Journal Entries are immutable:
their cancellation hook rejects cancellation. Corrections require a dedicated,
evidence-bound compensating posting; direct cancellation is never an allowed
support action. Safe reset and release smoke independently re-prove the winning
Sales Invoice lifecycle, exact void approval when cancelled, live submitted
Journal Entry metadata, the exact two active liability/refund GL rows, private
File attachment and visibility, retained File bytes, byte length, and SHA-256.
Neither gate trusts lifecycle status text, copied refund metadata, or a cached
File digest alone.

Refund support and safe reset preserve the deterministic lock order **Device ->
Safe Reset -> FB Order -> source Maybank QR Transaction -> liability/refund
Journal Entries -> evidence File**, revalidating the device/order binding after
the operational lock. Safe reset verifies at most 64 refunded duplicate
transactions and at most 64 MiB of declared and observed refund evidence per
attempt; exceeding either bound fails closed without deleting or weakening
evidence.

## Non-blocking shift-close visibility

Every successful shift close and exact close retry includes a
`duplicate_qr_liabilities` snapshot built from durable Maybank QR Transaction
incident rows linked to submitted FB Orders in that shift:

```json
{
  "accounting_pending": {"count": 1, "amount_sen": 1250},
  "refund_required": {"count": 1, "amount_sen": 325},
  "count": 2,
  "amount_sen": 1575,
  "blocks_close": false
}
```

`refunded` incidents are excluded. The winning FB Order Payment is not used to
infer this exposure because duplicate-payment accounting never mutates that
row. Counts and amounts are split by the two unresolved durable states and the
top-level values are their exact totals.

This report is operational visibility, not sale or shift-close authority. A
reporting/schema/storage fault is logged and returned explicitly with null
counts/amounts plus `report_status: unavailable`; it never lies with zeroes and
never prevents an otherwise healthy shift from closing. The existing
`pending_reconciliation` snapshot remains a separate response field and is
likewise non-blocking.
