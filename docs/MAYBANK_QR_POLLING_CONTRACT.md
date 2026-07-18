# Maybank QR Polling Contract

## ERP ownership

ERP owns every Maybank provider request. A tablet may read ERP status, but it
must never call Maybank directly or require a cashier to press a status button.
Each issued provider reference remains linked to the immutable
`fb_order` + `fb_order_payment` identity of the prepared sale.

When one linked attempt becomes due, the scheduler expands that logical payment
to every due provider-issued sibling. Each reference is dispatched as a separate
deduplicated RQ job with its own Redis lease. This allows references for the same
sale to be checked concurrently without checking one reference twice. Current
QRs use the short queue; expired/long-tail attempts use the long queue so they
cannot delay a live checkout.

Provider `failed` and `timeout` responses are not proof that settlement can no
longer occur. ERP continues checking them and permits only a monotonic late
transition to authenticated `paid`. An exact audited
`provider_transaction_cancelled` release is no longer polled. A QR display
expiry alone never releases or closes provider settlement.

An automatically replaced display follows
`MAYBANK_QR_DISPLAY_REPLACEMENT_CONTRACT.md`. Replacement never changes an old
attempt's provider status or pollability; it only adds another independently
polled reference for the same prepared payment.

## Cadence

- Before display expiry: scanned attempts are eligible after 1 second; other
  issued attempts back off from 2 to 15 seconds while a live status read is
  active. The scheduler itself runs on Frappe's `all` event.
- First hour after display expiry: at most once per 60 seconds.
- From one hour through 24 hours after display expiry: at most once per 5
  minutes.
- More than 24 hours after display expiry: at most once per 15 minutes until an
  exact paid result or audited cancellation is durable.

## Additive status response

The compatible route remains:

```text
GET /api/method/kopos_connector.api.check_maybank_payment
```

`status`, `transaction_refno`, `sale_amount`, `sale_amount_sen`, and `paid_at`
continue to describe the requested reference. ERP additionally returns:

- `sale_payment_status`: best durable state across all issued attempts;
- `sale_attempt_count` and `sale_attempts`: identity and status of every issued
  attempt, without QR payloads or credentials;
- `paid_transaction_name` and `paid_transaction_refno`: the earliest paid
  attempt using the same ordering as finalization.

Older tablets may ignore the additive fields. Updated tablets should prefer the
aggregate state and paid reference, while retaining fallback to the original
single-reference fields for older ERP versions.

## Cashier and accounting separation

Manual confirmation completes the local sale, fulfillment, and print queues
immediately. It remains `pending_reconciliation` in ERP and never blocks the
next order. Background polling continues across every issued attempt. The first
authenticated paid attempt wins the one prepared sale; every later paid attempt
uses the idempotent duplicate-liability/refund workflow and never creates a
second Sales Invoice.
