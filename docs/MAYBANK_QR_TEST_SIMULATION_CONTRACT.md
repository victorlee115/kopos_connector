# Maybank QR Test Simulation Contract

The ERP Desk may simulate a successful Maybank QR payment only on an isolated
developer/test site. This is a release-verification tool, not a reconciliation,
support, or production payment workflow.

## Required server truth

Every capability check and every simulation request fails closed unless all of
the following are true at the time of the request:

- the session is a System Manager session without the `KoPOS Device API` role;
- `allow_maybank_mock=1`;
- `developer_mode=1` or the Frappe test runner is active;
- `allow_maybank_desk_simulation=1`;
- `maybank_mock_payment_mode=manual`;
- Maybank Settings is enabled and its validated API Base URL is exactly
  `mock://`.
- the DuitNow QR Mode of Payment resolves to one enabled Bank or untyped Asset
  clearing account for the company and currency, never a physical Cash ledger.

The mutation endpoint is authenticated, CSRF-protected, and POST-only. It is not
in the device API allowlist. Production sites must keep the two allow flags and
developer mode disabled and must use only the official allowlisted HTTPS
Maybank origin.

## Eligible transaction

The System Manager must type the exact confirmation `SIMULATE MAYBANK PAYMENT`.
The browser supplies only that confirmation and the ERP transaction name. ERP
locks the FB Order first, then every linked provider attempt in deterministic
order, and accepts only a `pending` or `scanned` record with:

- a generated `MOCK-TXN-` provider reference of the exact expected format;
- provider `maybank_qr` and currency `MYR`;
- matching positive integer-sen and decimal amounts;
- nonempty device, outlet, QR, idempotency, request-fingerprint, FB Order, and
  FB Order Payment identities;
- an existing draft prepared FB Order whose accepted-sale fingerprint,
  provider request fingerprint, device, currency, amount, Maybank payment row,
  and awaiting-provider state all match exactly.

Creating, unknown, failed, timeout, malformed, unprepared, and live provider
transactions are rejected. A real provider-paid transaction can never be
relabeled as simulated.

## Paid transition and audit

Manual mock mode keeps normal provider polling at `pending` until the Desk
action is confirmed. The action builds a server-derived mock provider response
and passes it through the same exact-identity validator, row lock, monotonic
paid transition, realtime notification, and automatic sale finalization used
by a real paid provider response. It never writes `status = paid` directly.

The same database transaction records:

- an immutable simulation key and canonical identity SHA-256;
- the actor and timestamp;
- one sanitized Info Comment containing the transition and identity digest.

QR data, credentials, tokens, and raw provider secrets are excluded from the
Comment. Failure to write the audit rolls back the request. A committed replay
with the same durable identity returns `already_simulated` without another
transition, audit Comment, poll increment, or finalization enqueue.

## Desk safety

Maybank QR Transaction is a read-only operational ledger. System Managers may
inspect it but cannot create, edit, or delete it through Desk or generic REST
document APIs. Named server services remain the only mutation boundary.

The form displays the destructive-looking test button only when the
server-provided capability is true. A simulated record's orange warning derives
from its durable test marker, so it remains visible after test configuration is
disabled. Simulated records must never be represented as real bank settlement
evidence. If the mutation response is ambiguous, the form instructs the manager
to reload and retry safely; an already committed retry is idempotent.
