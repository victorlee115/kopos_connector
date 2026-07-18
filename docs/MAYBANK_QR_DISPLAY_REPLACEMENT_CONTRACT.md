# Maybank QR Display Replacement Contract

## Scope

This contract applies only to a dynamic Maybank QR returned by Maybank's
generation API. ERP treats that QR payload as provider-owned opaque display
data. It validates the provider success envelope, exact provider reference,
nonempty QR payload, amount binding, and display expiry; it does not reject the
payload by independently reinterpreting its DuitNow/EMV fields.

A display replacement changes only which QR the tablet shows. It never
cancels, releases, voids, or financially supersedes the old provider
reference. Every generated reference remains linked to the same immutable
prepared `FB Order` payment and remains pollable for late settlement.

## Additive request

The existing authenticated endpoint remains:

```text
POST /api/method/kopos_connector.api.generate_maybank_qr
```

The first generation request is unchanged. A replacement supplies the normal
prepared-sale request plus both additive fields:

```json
{
  "amount_sen": 1250,
  "device_id": "DEVICE-001",
  "idempotency_key": "device:shift:order:attempt-2",
  "fb_order": "FB-ORDER-001",
  "fb_order_payment": "FBPAY-001",
  "accepted_sale_fingerprint": "<64-lowercase-hex>",
  "replacement_reason": "unrenderable_display",
  "replaces_transaction_refno": "<exact-current-provider-reference>"
}
```

`replacement_reason` is exactly one of:

- `expired_display`: ERP's clock must be at or after the target row's exact
  persisted `expires_at`;
- `unrenderable_display`: the authenticated tablet may request an immediate
  replacement after it cannot render the nonempty provider payload.

The two replacement fields must be present together. Each replacement uses a
new idempotency key. The replacement reason and exact old provider reference
participate in the request fingerprint, so an exact retry replays one durable
result while a changed retry is rejected.

The successful response is unchanged:

```json
{
  "status": "ok",
  "qr_data": "<provider-owned-opaque-payload>",
  "transaction_refno": "<new-provider-reference>",
  "sale_amount": "12.50",
  "sale_amount_sen": 1250,
  "expires_at": "2026-07-19T14:01:00+08:00",
  "fb_order": "FB-ORDER-001",
  "fb_order_payment": "FBPAY-001"
}
```

## Eligibility and locking

ERP locks the `FB Order` first and then all linked Maybank transaction rows.
Before constructing a provider client or performing provider I/O, it proves:

- the sale is still a draft and is not cancelled;
- the target is the exact latest provider-issued reference;
- provider, device, company, currency, integer-sen amount, `FB Order`, payment
  row, and accepted-sale fingerprint all match;
- no linked attempt is `creating`, `scanned`, `paid`, or `unknown`;
- the target is `pending`, `failed`, or `timeout` and has a trusted expiry;
- the expiry condition for `expired_display` is satisfied; and
- fewer than three provider-issued attempts already exist for the payment.

The maximum is three provider-issued QRs total: the original plus no more than
two replacements. Preflight fences do not count as issued QRs because they
prove no provider request occurred. Rate limits still apply to every provider
generation.

The provider call remains outside the broad order lock after ERP durably
inserts and commits the new `creating` reservation. Provider finalization
reacquires the order lock before the transaction lock, preserving the canonical
lock order. A concurrent exact retry observes the same reservation and cannot
make a second provider call.

## Durable replacement rejection

A valid, identity-bound replacement rejected before provider I/O returns the
existing durable HTTP `409` no-provider fence. The fixed fields remain:

```json
{
  "status": "rejected",
  "error_code": "MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT",
  "provider_request_attempted": false,
  "rejection_fence_registered": true,
  "local_release_authorized": true,
  "recovery_action": "release_local_provider_intent",
  "device_id": "DEVICE-001",
  "idempotency_key": "device:shift:order:attempt-2",
  "amount_sen": 1250,
  "currency": "MYR",
  "checked_at": "2026-07-19T14:00:00+08:00"
}
```

It additionally includes:

```json
{
  "replacement_intent_rejected": true,
  "replacement_rejection_code": "replacement_not_yet_expired",
  "replacement_reason": "expired_display",
  "replaces_transaction_refno": "<old-provider-reference>",
  "prior_provider_reference_retained": true,
  "release_scope": "replacement_intent_only"
}
```

For deterministic replacement rules, `preflight_reason_code` is
`replacement_request_rejected`, and `replacement_rejection_code` is exactly one
of:

- `replacement_not_yet_expired`;
- `replacement_attempt_limit_reached`;
- `replacement_state_not_eligible`;
- `replacement_target_mismatch`; or
- `replacement_sale_terminal`.

If provider configuration or rate limiting rejects an otherwise valid
replacement, both reason fields retain the corresponding existing code:
`provider_configuration_rejected`, `rate_limit_exceeded`, or
`rate_limiter_unavailable`.

The tablet may clear only its new, empty replacement intent after validating
this complete fence. It must retain the earlier issued attempt and must keep
manual confirmation available. A malformed request, idempotency conflict,
timeout, disconnect, 5xx, ambiguous generation, or response that fails exact
fence validation remains fail-closed.

## Settlement

All old and new provider references remain active settlement evidence and use
the cadence in `MAYBANK_QR_POLLING_CONTRACT.md`. The earliest authenticated
paid attempt is the winning payment for the one prepared sale. Any later paid
attempt enters the existing duplicate-liability and verified-refund workflow;
it never creates another Sales Invoice.
