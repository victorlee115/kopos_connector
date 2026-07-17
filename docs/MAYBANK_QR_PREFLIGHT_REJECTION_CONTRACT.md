# Maybank QR Preflight Rejection Contract

ERPNext owns Maybank provider sessions and settlement state. The tablet keeps a
durable local provider intent only until ERP can prove one of these outcomes:

1. ERP registered a provider-generation reservation, so the tablet must remain
   fail-closed until generation or support reconciliation resolves it.
2. ERP registered a terminal rejection fence before any Maybank request, so the
   tablet may release only the exactly matching local provider intent.

## Releasable preflight rejection

`kopos_connector.api.generate_maybank_qr` returns HTTP `409` with this top-level
JSON shape only after committing the terminal rejection fence:

```json
{
  "status": "rejected",
  "error_code": "MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT",
  "message": "Automatic QR is unavailable because its ERP provider configuration is not ready",
  "preflight_reason_code": "provider_configuration_rejected",
  "provider_request_attempted": false,
  "rejection_fence_registered": true,
  "local_release_authorized": true,
  "recovery_action": "release_local_provider_intent",
  "device_id": "DEVICE-001",
  "idempotency_key": "device:shift:order:attempt-1",
  "amount_sen": 1250,
  "currency": "MYR",
  "checked_at": "2026-07-17T11:15:30+08:00"
}
```

`preflight_reason_code` is exactly one of:

- `provider_configuration_rejected`
- `rate_limit_exceeded`
- `rate_limiter_unavailable`

The mobile client must require HTTP `409`, every fixed discriminator above,
an explicit-timezone `checked_at`, and exact equality of `device_id`,
`idempotency_key`, `amount_sen`, and `currency` with its retained intent. It must
persist the terminal local update before clearing the durability guard.

## Durable replay and concurrency rule

The rejection fence uses the same unique request fingerprint and placeholder
reference as a generation reservation. It is committed before the response is
sent. Therefore:

- a lost response can be retried and returns the same bound rejection;
- a delayed replay of that idempotency key cannot later call Maybank;
- if a concurrent generation reservation wins first, ERP returns its
  `creating`/ambiguous recovery contract and never authorizes local release.

The fence is an ERP audit record with status `failed`, no Maybank status, no QR
payload, and a placeholder `REQUEST-...` reference. It is not a provider
transaction.

## Automatic QR accounting preflight

Before ERP creates a provider reservation or contacts Maybank for a new
Automatic QR attempt, it resolves
`Maybank Settings.manual_qr_suspense_account` against the locked, prepared FB
Order. The configured Account must exist and must be:

- owned by the prepared FB Order company;
- an enabled, non-group ledger;
- an Asset account; and
- denominated in exactly the prepared FB Order currency.

If that proof fails, ERP uses the same durable
`provider_configuration_rejected` fence described above. No Maybank client is
created and no provider request is attempted. The check is scoped to Automatic
QR generation; it is not a global readiness gate for cash or static-QR sales.

## Fail-closed boundary

No other failure authorizes local release. In particular, timeout, disconnect,
5xx, malformed provider data, provider rejection after the network call,
reservation finalization failure, unrecognized/tampered fence evidence, and
device-authority errors remain fail-closed. Those paths continue through the
existing generation recovery or non-device System Manager reconciliation flow.
