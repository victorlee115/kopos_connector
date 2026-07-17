# FB Shift Open-Conflict Contract

ERP is authoritative for whether a device or staff account already owns an open
FB Shift. The tablet may use the lookup endpoint as a preflight, but the locked
`open_shift` transaction is the final enforcement point.

## Authenticated preflight

```http
GET /api/method/kopos_connector.api.get_device_open_shift?device_id=TAB-A-001&staff_id=cashier@example.test
```

ERP first authenticates the requesting device. When `staff_id` is supplied, ERP
also verifies that the staff user is assigned to that device, active on the
device, and an existing enabled ERP user. Unassigned, inactive, or disabled
users receive a validation error; their shift state is not disclosed.

An existing open shift on the requested device keeps the original response
shape:

```json
{
  "status": "ok",
  "shift": {
    "fb_shift": "FB-SHIFT-0001",
    "shift_id": "SHIFT-LOCAL-0001",
    "device_id": "TAB-A-001",
    "staff_id": "cashier@example.test",
    "opening_float_sen": 10000,
    "opened_at": "2026-07-17T08:00:00+08:00"
  }
}
```

Only when that device has no open shift, but the validated staff user has one on
another device, ERP adds a typed staff conflict:

```json
{
  "status": "ok",
  "shift": null,
  "staff_conflict": {
    "conflict_code": "staff_open_shift_conflict",
    "staff_id": "cashier@example.test",
    "conflicting_fb_shift": "FB-SHIFT-0002",
    "conflicting_device_id": "TAB-A-002",
    "message": "User cashier@example.test already has open FB Shift FB-SHIFT-0002 on device TAB-A-002"
  }
}
```

The preflight is advisory because state can change after the lookup. It does not
authorize the tablet to infer the outcome of a later `open_shift` request.

## Transactional open conflict

`open_shift` locks the device and staff user, then checks and locks any existing
requested or matching open FB Shift rows before it decides the result. It checks
an existing requested shift first, so an exact idempotency-key and fingerprint
retry still returns `status: "duplicate"`.

If no exact duplicate exists and a device or staff conflict is present, ERP does
not create or mutate the requested FB Shift. It returns exact request-bound
proof:

```json
{
  "status": "conflict",
  "conflict_code": "staff_open_shift_conflict",
  "idempotency_key": "TAB-A-001:SHIFT-LOCAL-0003:open",
  "shift_id": "SHIFT-LOCAL-0003",
  "device_id": "TAB-A-001",
  "staff_id": "cashier@example.test",
  "conflicting_fb_shift": "FB-SHIFT-0002",
  "conflicting_device_id": "TAB-A-002",
  "local_release_authorized": true,
  "message": "User cashier@example.test already has open FB Shift FB-SHIFT-0002 on device TAB-A-002"
}
```

`conflict_code` is one of:

- `device_open_shift_conflict`: the requested device already has an open shift.
- `staff_open_shift_conflict`: the requested staff user already has an open
  shift on another device.

`local_release_authorized: true` applies only when every request-binding field
(`idempotency_key`, `shift_id`, `device_id`, and `staff_id`) exactly matches the
tablet's pending open attempt. It proves that this attempt was rejected before
creation. A timeout, network error, HTTP 5xx, malformed response, validation
error, or mismatched field is not release authority and must remain pending for
safe retry or support recovery.

Neither response contains device credentials, API secrets, manager tokens, or
other authentication material.
