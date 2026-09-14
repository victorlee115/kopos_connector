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

## Provider transport boundary

The connector validates the configured HTTPS origin before use, and sets
the Requests session's `max_redirects` to zero. Redirect responses fail with
`TooManyRedirects` before any redirected request is sent, including same-origin
redirects, cross-origin HTTPS and HTTP downgrades. A legitimate endpoint move
requires explicitly configuring and validating its final allowed URL; credentials
and non-idempotent QR creation bodies must never follow a redirect automatically.

The ordinary status reader and acceptance-evidence reader share one status POST
owner. A 401 permits exactly one explicit credential refresh and status replay;
a second 401 fails. QR creation is never replayed after a 401, redirect or server
error. Existing request timeouts, TLS verification, branch snapshots and payment
identity/accounting ownership remain unchanged. Redirect rejection is not an
assertion that the original provider request was unprocessed or unpaid.

## Credential saves and token cache

Credential changes use the normal Frappe account `Document.save` lifecycle.
`before_save` records the rotation timestamp when a real, unmasked Password
value is supplied. Frappe then saves the password and account revision in the
same database transaction. Checking Password changes in `on_update` is too late:
Frappe has already masked them, and equal-length PINs can look identical.

Token keys use the saved account name and `modified` revision, username, user
type, outlet, API URL and site. The `v3` namespace admits validated tokens and
does not reuse the earlier unchecked `v2` or older keys.
Each normal account save intentionally starts a cold cache, including metadata
edits. Existing Redis expiry remains 3,540 seconds; old revisions expire without
scanning profiles or historical transactions. A late old client can write only
its old revision, not the latest account's token keys. Historical attempts keep
their outlet snapshot but load the current saved account credentials/revision.

Callers without a saved account revision, including the legacy settings factory,
operate uncached. Direct password-store writes, credential changes that bypass
normal account saving, and manually retained stale clients are outside this
lifecycle contract. Cache separation does not revoke a token at the provider or
cancel an already-running request. Release must still replace all old workers;
mixed runtime versions are not accepted.

## Authentication response boundary

The existing JSON Schema library validates successful login and outlet-token
response shapes before their consumed fields are accessed. Additional fields and
later outlet-array entries remain compatible; the first outlet entry is still
the consumed response. The encrypted outlet value must be a nonempty string.
Login tokens and decrypted outlet tokens must be strings matching the opaque
Bearer syntax in [RFC 6750 section 2.1](https://www.rfc-editor.org/rfc/rfc6750.html#section-2.1).
There is no numeric/object coercion, token trimming, JWT decoding or local
signature acceptance. Existing encrypted-envelope line wrapping is supported.

Invalid JSON, unsuccessful/malformed auth shapes, invalid encrypted envelopes
and invalid Bearer values raise fixed, operation-specific validation errors.
Raw provider text and JSON/decoder exception context are not included in those
errors. Only a validated token reaches Redis. A separate subsequent request may
recover; malformed responses do not trigger a hidden auth/QR-creation replay.
Existing TLS/redirect policy, single explicit 401 refresh and TTL remain unchanged.

This checks token representation, not provider authorization, expiry, revocation
or payment authenticity. Actual provider compatibility and UAT remain required.

## Bounded provider response bodies

The existing Requests TLS adapter accepts at most **1,048,576 bytes** in the
transfer-decoded HTTP body and at most the same number after content decompression.
It reads one byte beyond the limit to detect overflow; it does not truncate and
accept a partial JSON response. Missing Content-Length, chunked responses, gzip
and deflate remain supported. A final EOF read lets urllib3 detect truncated
framing before the body is accepted. Every response is released or closed,
including an oversized, truncated or undecodable response.

The declared **urllib3 >=2.7,<3** dependency provides maintained HTTP framing,
bounded decompression and encoding-chain protections. There is no custom gzip
decoder, second HTTP client or new service. The adapter caches only bounded
decoded bytes in Requests' existing response cache before Requests can perform
its default eager read or redirect handling. TLS evidence is captured first.
Transport-evidence response hashes retain their existing convention: exact
decoded response-content bytes, not compressed wire bytes or re-serialized JSON.

All five operation paths—login, outlet token, QR creation, status and status
evidence—share this body boundary. Their JSON object reader converts malformed,
non-object and recursion-limit failures to fixed operation-specific errors
without retaining the raw parser exception as context. JSON parsing remains
Requests' existing parser; this does not add a bespoke nesting limit or reinterpret
money. Additional valid provider fields remain compatible within the byte limit.

A rejected body is not a valid QR, token, payment result or transport-evidence
receipt. It does not cause an automatic replay. Even a 401 with an oversized body
is rejected before the ordinary one-time refresh path, leaving that request
unreplayed. A later independent read may recover. A failed QR-creation response
is **not** evidence that the provider never created a transaction: the existing
dispatched/unknown/reconciliation boundary still applies.

These are payload-size and parsing bounds, not a total wall-clock deadline,
universal process-memory ceiling, HTTP header quota, payment-authenticity proof
or general telemetry guarantee. Existing Requests connect/read timeouts and
worker/request deadlines are unchanged. Provider UAT and restored-data fault,
restart and concurrency verification remain required for acceptance.

## Legacy settings migration

The legacy settings are a Frappe Single DocType in `tabSingles`; existence is
checked through DocType metadata, not a nonexistent settings SQL table. Account
creation supplies the PIN to the normal Password field before insert. Native
Frappe encrypts it in `__Auth`, masks the document value and saves the audit
timestamp. There is no connector password setter or second save.

The existing provider URL normalizer runs before account lookup, so blank and
missing-trailing-slash legacy URLs reuse the canonical saved account. Legacy
settings represent production, never a same-named sandbox account. Repeated
migration must not overwrite an account's subsequently rotated PIN or device ID.
Missing stored credentials fail before account creation.

A dated identity for the same patch is registered using Frappe's native
space/comment suffix convention, allowing sites that recorded the old no-op to
run the repair. Existing profile assignments and populated transaction account
snapshots are retained. An unbound transaction is backfilled only when its profile
matches both the migrated account and its outlet. Ambiguous branches still need
guided assignment. Approved populated-backup migration twice, including real DDL
and patch commits, remains a release gate; rollback-only data-path tests do not
waive it.

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
