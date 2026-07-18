# Static DuitNow QR Commissioning Contract

ERP is the authority for a reusable static DuitNow QR. The tablet must never
display an uncommissioned raw payload.

## PayNet validation

Saving a static QR on a KoPOS Device parses the payload as strict TLV and proves:

- PayNet payload version `02` and static initiation method `11`;
- merchant-account tag `26`, Malaysia AID `A0000006150001`, Acquirer ID, and QR
  ID;
- MYR currency `458`, country `MY`, merchant category, merchant name, and city;
- CRC-16/CCITT-FALSE with tag `63` as the final field; and
- no embedded transaction amount or convenience fee, because this reusable QR
  is paired with the current sale amount shown by the POS.

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
invalid. Cash and Automatic QR configuration remain available. Recommissioning
requires a System Manager to clear the old payload, save, verify the merchant
details, and save the exact replacement payload under the correct POS Profile.

The POS must recompute the payload SHA-256 and parse the same PayNet identity
before display. It should show only the current sale amount in large plain text;
commissioning metadata is diagnostic and not cashier-facing copy.
