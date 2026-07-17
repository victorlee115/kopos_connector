# QR Reconciliation Accounting Contract

## Operational invariant

A manually confirmed QR sale is complete when its FB Order and Sales Invoice
are submitted. Fulfillment, sticker printing, the next checkout, and normal
shift operation do not wait for bank reconciliation. Reconciliation changes the
payment settlement ledger only; it never reopens, cancels, or duplicates the
sale or invoice.

The Sales Invoice initially debits the configured QR suspense Asset account.
`pending_reconciliation` accurately reports that ERP has registered the sale but
has not yet proven the final bank disposition.

Shift close does not wait for those payment rows. Every successful close response
reports the current submitted-sale exposure as
`pending_reconciliation: {count, amount_sen, blocks_close: false}`. The count and
integer-sen amount are operational settlement exposure only: they do not become
cash, do not change cash variance, and do not reopen the closed shift. An exact
close retry returns the then-current reconciliation exposure, which may be lower
after back-office reconciliation completes.

## Automatic QR provider truth

Manual confirmation remains `pending_reconciliation` unless the exact issued and
sale-bound Maybank transaction already contains complete authenticated
provider-paid evidence. ERP requires both monotonic paid status fields plus the
exact transaction reference, provider identity, issued QR data, outlet, device,
MYR currency, integer-sen amount, issued expiry, and provider paid timestamp. A
partial paid signal, missing provider evidence, or mismatched sale fact can never
promote a manual confirmation to `verified`.

When that complete provider evidence is already durable, it is stronger than the
cashier's manual observation and the payment may be recorded as `verified`
immediately. The manual evidence and its reconciliation idempotency identity are
still retained for audit. Retries must reproduce those exact immutable settlement
facts; sharing only the same provider reference is insufficient.

## Successful settlement

After exact provider or bank evidence is verified, ERP posts one submitted,
idempotent Journal Entry:

- debit the configured bank or clearing Asset account;
- credit the original QR suspense Asset account; and
- bind the source reconciliation, FB Order, Sales Invoice, amount, company, and
  currency.

Only then may the source and FB Order Payment become `reconciled`.

## Failed settlement

`reconciliation_failed` is an accounting disposition, not the absence of one.
Before writing that terminal state ERP must:

1. lock and reload the Manual QR Reconciliation or Maybank QR Transaction;
2. prove the exact submitted FB Order, Sales Invoice, payment row, and original
   suspense GL debit;
3. reject provider-paid Maybank truth;
4. resolve `Company.custom_kopos_qr_failure_variance_account` and prove that it
   is an enabled Expense ledger for the same company and currency;
5. resolve and validate the Company's default Cost Center required for the
   Expense posting;
6. snapshot the failure account, Cost Center, and reason on the reconciliation
   source;
7. derive a server-owned `kopos:qr-failure:v1:` key from the source, order,
   payment row, invoice, amount, company, currency, suspense account, failure
   account, Cost Center, and reason;
8. create or recover one submitted Journal Entry that debits the failure
   variance Expense at that Cost Center and credits QR suspense for the exact
   amount;
9. verify the Journal Entry metadata and submitted GL rows; and
10. link that Journal Entry to the source before setting the source and FB Order
   Payment to `reconciliation_failed`.

An exact retry re-proves and returns the same Journal Entry. A changed reason,
identity, amount, account, metadata field, or GL row is rejected. The dedicated,
globally unique Journal Entry failure-key field is separate from successful
reconciliation keys, preventing replay and cross-disposition/source namespace
collisions.

If the Company account, source context, submitted invoice, suspense debit, or
Journal Entry proof is absent or invalid, ERP rolls back the request and leaves
the settlement `pending_reconciliation`. This fail-closed back-office behavior
does not alter the submitted sale and does not block cashier work.

## Late provider-paid truth after failure

Provider-paid truth remains monotonic and supersedes an earlier manual failure
decision. ERP must not post a second credit to suspense after the failure Journal
Entry has already cleared it. Instead, ERP re-proves the exact historical
suspense-to-variance Journal Entry and posts one idempotent recovery Journal
Entry:

- debit the configured bank or clearing Asset account; and
- credit the snapshotted QR failure variance Expense at its snapshotted Cost
  Center.

The recovery Journal Entry binds the historical failure Journal Entry and source
account. Across the original Sales Invoice, failure disposition, and late-paid
recovery, the net effect is one debit to bank and one credit to suspense. The FB
Order and Sales Invoice remain the same submitted documents throughout.
