# Static DuitNow QR Commissioning Contract

ERP is the authority for a reusable static DuitNow QR. The tablet must never
display an uncommissioned raw payload.

## PayNet validation

Saving a static QR on a KoPOS Device validates the supported ASCII TLV payload:

- PayNet payload version `02` first, and static initiation method `11`;
- merchant-account tag `26`, Malaysia AID `A0000006150001`, Acquirer ID, and QR
  ID;
- MYR currency `458`, country `MY`, merchant category, merchant name, and city;
- CRC-16/CCITT-FALSE with tag `63` as the final field; and
- no embedded transaction amount or convenience fee, because this reusable QR
  is paired with the current sale amount shown by the POS.

Both ERP and POS reject empty field values, incomplete/non-numeric headers,
length overflows and duplicate tags at the root and inside merchant-account and
additional-data (`62`) templates. Additional-data fields may appear in any order;
valid unknown nonempty fields remain compatible. The payload limit remains 512
printable ASCII characters. ERP profile-config serialization applies the same
inspection before returning a payable QR, including for retained imports.

ERP delegates the checksum calculation to Python's standard
[`binascii.crc_hqx`](https://docs.python.org/3/library/binascii.html#binascii.crc_hqx)
with initial value `0xFFFF`. There is no custom Python CRC loop or new dependency.
The checksum includes the exact bytes through `6304`, and both hexadecimal cases
remain accepted. Original payload bytes, SHA-256 and commissioned merchant/company
bindings are not rewritten. The tablet retains its existing portable CRC routine;
this change adds no Node-only API or mobile package.

CRC detects corruption; it does **not** authenticate a merchant or prove payment.
Commissioning still requires independent verification of the intended merchant.
This supported static-QR contract is not full PayNet/EMV extension validation:
alternate-language and data-integrity/signature extensions are not interpreted as
payment authority. Actual bank scanning, merchant verification, production-data
compatibility and physical-device acceptance remain required.

ERP derives and stores the lowercase payload SHA-256, QR/merchant ID, Acquirer
ID, merchant name, version, commissioning timestamp, and POS Profile company.
The company binding and every derived field must still match whenever device
configuration is serialized.

## Additive device configuration

The existing `static_qr_payload` field remains compatible. A commissioned
configuration additionally provides:

```text
static_qr_payload_sha256
static_qr_merchant_id
static_qr_acquirer_id
static_qr_merchant_name
static_qr_version
static_qr_commissioned_at
static_qr_company
static_qr_available
static_qr_configuration_status
```

If the payload, hash, merchant identity, commissioning timestamp, or company is
missing or mismatched, ERP emits no payable payload and reports the static QR as
invalid. Cash and Automatic QR configuration remain available. The existing
direct KoPOS Device replacement workflow requires a System Manager to clear the
old payload, save, verify the merchant details, and save the exact replacement
payload under the correct POS Profile. The profile setup API has the reviewed
replacement path described below; it does not change the direct-device workflow.

The POS must recompute the payload SHA-256 and parse the same PayNet identity
before display. It should show only the current sale amount in large plain text;
commissioning metadata is diagnostic and not cashier-facing copy.

## Reviewed POS Profile setup

The QR setup API requires System Manager or Accounts Manager role **and** normal
Frappe document permission: read for an existing preview, write for a proposed
preview or save. A manager role does not bypass profile-specific User Permissions.
Only the existing QR configuration fields are accepted; company is not editable
through this API. It does not change company-wide Mode of Payment accounts.

For a new or incompletely commissioned payload, preview derives the merchant
identity and SHA-256 through the existing static-QR inspector without saving.
The manager must check the displayed merchant against the intended bank QR.
Apply requires `confirmed_static_qr_sha256` matching that exact payload, then
stores server-derived metadata and commissioning time. Confirmation records a
human decision, not bank authentication. Reapplying an unchanged commissioned QR
does not change its commissioning timestamp or unnecessarily bump tablet config
versions. Disabling an unchanged invalid retained QR remains possible.

Apply requires `expected_modified` from the reviewed profile. Ordinary Frappe
`Document.save()` enforces permissions and optimistic locking. An intervening
edit must be reloaded and reviewed; it cannot be silently overwritten. The
existing shared savepoint owner covers save, linked-tablet version updates and
response construction. The request, not this API, commits the transaction. A
server-side preview failure rolls back this setup operation; a lost network
response still requires checking the saved state before retrying.

Both GET and Frappe's default form POST may request a preview. Mode of Payment
mapping reads ERPNext's native `default_account` child field; lookup failures
are not represented as a successful empty mapping. Automatic readiness validates
the proposed profile suspense account with the existing account validator,
instead of reloading stale saved account settings.

Development evidence: 8 real Frappe/MariaDB/Redis QR setup tests and 52 native
regressions pass on the Phase 70 wheel recorded in the POS audit ledger. The
Client Script test proves native save/read and stops the installer's intentional
final commit; it is **not** migration or installation acceptance. No real merchant
has been commissioned by these tests.

### Guided image import

The existing POS Profile Client Script is installed from the single packaged
`public/js/pos_profile_setup.js` asset through standard Python package resources.
The installer keeps its existing record name and intentional installation commit.
Frappe owns the dialog, account Link controls, section layout and requests. Its
already-installed html5-qrcode 2.3.8 dependency owns QR image decoding; there is no
new QR decoder, package, upload service or camera permission.

The manager chooses a PNG/JPEG/WebP image up to 5 MB, selects an existing eligible
account, reviews the server-inspected merchant and explicitly confirms it before
saving. The image stays in the browser; the decoded code is sent to ERP for
validation and persisted only on Save. Account suggestions are limited to the
profile's company/currency and appropriate enabled non-group Asset accounts;
server validation remains authoritative. Optional automatic-bank settings are
collapsed and retain their saved values. No account is created or guessed.

Setup refuses a dirty profile and never automatically saves/discards form edits.
Each opening captures the saved revision. Edits invalidate review; late image or
preview results cannot reinstate it. Invalid images do not replace the saved QR
or silently reuse it for an enabled static lane. Duplicate Save clicks cannot
send another in-flight request. Closing releases the one-use dialog/file input;
unsaved form changes made during a request are retained without reload. A lost
save response directs the manager to inspect saved settings before retrying.

Phase 71's latest source passes 33 Node UI tests, 2,328 connector contracts plus
378 subtests against its built wheel, and desktop/narrow Chromium interaction
using the installed Frappe controls and real image decoder. Browser backend and
parent-form host are simulated; this is not a full ERP HTTP or novice-user pass.
An earlier Phase 71 image passed the 60 native tests and unchanged-state checks,
but it predates the final hidden-dialog cleanup. The final wheel's fresh native
image/gates remain pending local Colima disk capacity. These evidence levels must
not be combined into a current native acceptance claim.

See [the setup guide and screenshot review](QR_SETUP_GUIDE.md). Uncoached novice
acceptance, real ERP browser installation/migration, bank verification, direct
profile company changes and original-company binding for retained imports remain
unfinished. The setup API's whitelist alone does not certify other entry points.
