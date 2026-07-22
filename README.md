# KoPOS Connector for ERPNext

ERPNext connector app for KoPOS mobile POS system with full modifier and availability management.

## Features

- **Modifier Groups**: Create reusable modifier groups (size, milk type, add-ons, etc.)
- **Modifier Options**: Define options with price adjustments and defaults
- **Item-Modifier Linking**: Link multiple modifier groups to items
- **Stock-Based Availability**: Automatic availability based on inventory levels
- **Manual Availability Override**: Force items available/unavailable regardless of stock
- **SST Tax Control**: Configure SST rate per POS profile
- **Real-time Catalog API**: RESTful API for KoPOS mobile app integration

## Requirements

- Frappe Framework >= 16.0.0, < 17.0.0
- ERPNext >= 16.0.0, < 17.0.0
- Python >= 3.10

## Installation

### 1. Get the App

```bash
cd /path/to/erpnext/frappe-bench
bench get-app kopos_connector /absolute/path/to/JiJiPOS-Everything/worktree-fnb-erpnext
```

Or from a Git repository:

```bash
bench get-app kopos_connector https://github.com/your-org/kopos-connector.git
```

### 2. Install on Site

```bash
bench --site your-site install-app kopos_connector
```

### 3. Run Migrations

```bash
bench --site your-site migrate
```

## Configuration

### 1. Create Modifier Groups

Navigate to: **KoPOS Connector > KoPOS Modifier Group > New**

Example modifier groups:
- **Size** (Single-select, Required): Small, Medium, Large
- **Milk Type** (Single-select, Required): Regular, Oat, Almond, Soy
- **Add-ons** (Multiple-select, Optional): Boba, Jelly, Pudding

### 2. Link Modifiers to Items

1. Go to **Stock > Item**
2. Open an item
3. Scroll to **KoPOS Modifiers** section
4. Add modifier groups in the child table

### 3. Configure Availability

In the Item form, under **KoPOS Availability** section:

- **Availability Mode**:
  - **Auto**: Advisory stock warnings when below minimum (item stays sellable with warning)
  - **Force Available**: Always show as available (ignores stock)
  - **Force Unavailable**: Hard-block sold out (prevents sale)
- **Track Stock**: Enable stock-based availability checking
- **Min Qty**: Minimum quantity threshold for advisory warning trigger

### 4. Configure SST (Optional)

In POS Profile form, under **KoPOS SST Configuration** section:

- **Enable SST**: Toggle SST for this POS profile
- **SST Rate (%)**: Set SST percentage (default: 8%)

## API Endpoints

### Submit Order

Create an idempotent `FB Order` and project it to a submitted ERPNext
`Sales Invoice`. New clients must use the `sen_v1` integer-money contract.

```http
POST /api/method/kopos_connector.api.submit_order
```

Request excerpt:
```json
{
  "money_contract_version": "sen_v1",
  "order_id": "ORDER-00042",
  "idempotency_key": "TAB-A-001:SHIFT-001:00042",
  "device_id": "TAB-A-001",
  "shift_id": "SHIFT-001",
  "staff_id": "cashier@example.test",
  "warehouse": "Main Booth - JC",
  "company": "JiJi Cafe",
  "currency": "MYR",
  "order": {
    "display_number": "A042",
    "order_type": "takeaway",
    "created_at": "2026-07-14T12:30:00+08:00",
    "subtotal_sen": 1200,
    "tax_amount_sen": 0,
    "rounding_adjustment_sen": 0,
    "total_sen": 1200,
    "items": [{
      "line_id": "LINE-1",
      "item_code": "ICED-MATCHA",
      "item_name": "Iced Matcha",
      "qty": 1,
      "unit_price_sen": 1200,
      "modifier_total_sen": 0,
      "discount_amount_sen": 0,
      "line_total_sen": 1200,
      "modifiers": []
    }],
    "payments": [{
      "payment_method": "Cash",
      "amount_sen": 1200,
      "tendered_amount_sen": 1500,
      "change_amount_sen": 300
    }]
  }
}
```

Response:
```json
{
  "status": "ok",
  "fb_order": "FB-ORDER-2026-00042",
  "sales_invoice": "ACC-SINV-2026-00042",
  "idempotency_key": "TAB-A-001:SHIFT-001:00042",
  "projection_status": "posted",
  "partial_failure": false
}
```

### Open Shift and Recover Conflicts

Open-shift conflicts are enforced transactionally by ERP under device, staff,
and FB Shift locks. An exact duplicate retry remains idempotent. A different
open shift on the device or staff account returns request-bound proof with
`status: "conflict"` without creating the requested shift. The optional
`staff_id` on `get_device_open_shift` provides the same staff-conflict preflight
after the staff assignment and active status are validated. See the
[FB Shift open-conflict contract](docs/FB_SHIFT_OPEN_CONFLICT_CONTRACT.md).

```http
POST /api/method/kopos_connector.api.open_shift
GET /api/method/kopos_connector.api.get_device_open_shift?device_id=TAB-A-001&staff_id=cashier@example.test
```

### Generate Automatic QR

ERPNext owns Maybank QR generation and settlement. Before contacting Maybank,
the tablet must send the complete immutable sale to
`prepare_automatic_qr_sale`. ERP persists a Draft FB Order, its exact payment
row, and frozen recipe/modifier/component resolutions, then returns the
`fb_order`, `fb_order_payment`, and `accepted_sale_fingerprint` identities that
must accompany QR generation. Amount-only QR generation is rejected.

A tablet may release a local
provider intent only when this endpoint returns the exact, request-bound HTTP
`409` preflight rejection registered by ERP. All ambiguous or post-provider-call
failures remain fail-closed. See
[Maybank QR preflight rejection contract](docs/MAYBANK_QR_PREFLIGHT_REJECTION_CONTRACT.md).

```http
POST /api/method/kopos_connector.api.prepare_automatic_qr_sale
POST /api/method/kopos_connector.api.confirm_prepared_automatic_qr_static_payment
GET /api/method/kopos_connector.api.get_maybank_qr_readiness?device_id=...
POST /api/method/kopos_connector.api.generate_maybank_qr
GET /api/method/kopos_connector.api.check_maybank_payment?transaction_refno=...
```

The tablet may confirm the prepared sale against the always-available static QR
at any time. The confirmation route requires the exact prepared order, payment,
device, integer-sen amount, accepted-sale fingerprint, local static session, and
versioned manual evidence. Under the same order and attempt locks, ERP submits
that FB Order once, creates the one Sales Invoice and stock issue, and records
the exact Manual QR Reconciliation as the `static_qr` winner. The response and
an exact `submit_order` retry both return that same sale and invoice. The payment
remains visibly `pending_reconciliation` for back-office work, but this state
does not block fulfillment, printing, or the cashier's next sale.

If Maybank finishes the prepared sale before an offline static confirmation
reaches ERP, ERP keeps the already-posted Maybank payment, invoice, stock issue,
and ledger unchanged. It accepts the exact static evidence once as a
`secondary_possible_duplicate` claim and returns the same completed sale to the
tablet. Exact retries return that same claim. The claim stays visibly pending
for bank review and cannot use the ordinary suspense reclassification actions,
so it cannot create a second invoice or silently change the winning payment.

Static confirmation never deletes or cancels a Maybank attempt. Every attempt
stays linked to the prepared payment as evidence. Each issued nonterminal,
expired, ambiguous, provider-failed, or never-displayed reference remains in the
fair grouped polling schedule unless ERP has exact durable proof of a
pre-provider release or audited provider cancellation. A reservation with no
provider reference is retained but cannot be polled until a reference is known.
A provider result that returns after static confirmation cannot change the
winning channel or create another invoice. If any retained attempt is later
reported paid, ERP first proves the exact static reconciliation and winning
invoice, then records the provider payment as duplicate customer liability for
the existing refund workflow. Missing accounting setup may delay that secondary
incident record, but it cannot roll back or interrupt the completed sale.

The readiness route is a read-only capability prewarm. It validates the
authenticated device, local provider settings, and QR accounting destinations;
it does not prepare a sale, authenticate with Maybank, create a provider
transaction, or expose the outlet ID. The response contains only `ready` or
`unavailable`, an outlet-ID SHA-256 when ready, the check time, and contract
version.

Every successful generation and payment-status response returns the exact
nonempty persisted provider `transaction_refno` and integer
`sale_amount_sen`. The legacy decimal `sale_amount` is derived from that
integer-sen authority. Status checks use the request reference only to locate
the device-scoped ERP record; response identity and amount come from the
persisted Maybank QR Transaction, never from client-supplied values. Malformed
or mismatched provider/persisted identity fails closed.

Status reads retain the requested-reference fields and add the aggregate state
of every provider-issued attempt linked to the prepared payment. ERP dispatches
all due linked references independently, including expired, provider-failed, and
provider-timeout attempts that can still become paid late. Display expiry uses
60-second checks for the first hour, 5-minute checks through 24 hours, and
15-minute long-tail checks thereafter. An exact audited provider cancellation
is the only timeout release that stops polling. See the
[Maybank QR polling contract](docs/MAYBANK_QR_POLLING_CONTRACT.md).

If a provider-issued QR cannot be rendered, or its display expiry has passed,
the tablet may invisibly request a new display using the same generation route
and the exact old provider reference. ERP permits at most three issued QRs for
one prepared payment and never releases an old reference; every attempt remains
pollable and the earliest paid attempt wins. The successful response stays
backward compatible, while a no-provider replacement rejection is a durable
`409` scoped only to the fresh replacement intent. See the
[Maybank QR display replacement contract](docs/MAYBANK_QR_DISPLAY_REPLACEMENT_CONTRACT.md).

The **DuitNow QR** Mode of Payment must be type **Bank** and resolve to exactly
one enabled, non-group Bank or untyped Asset clearing account for the company
and currency. A physical Cash ledger is rejected during provider preflight, so
a customer cannot pay a QR that ERP would later post into till cash.

On an isolated developer/test site, a non-device System Manager may use the
Maybank QR Transaction Desk action **Simulate Successful Payment (Test Only)**.
Manual mock mode keeps the transaction pending until that explicit action; ERP
then applies a server-derived status through the same locked provider identity
validator and sale finalization path. The action is POST-only, audited,
idempotent, and unavailable unless every mock/developer/simulation guard is
enabled. Maybank QR Transaction itself is read-only and must never be edited to
forge `paid`. See the
[Maybank QR test simulation contract](docs/MAYBANK_QR_TEST_SIMULATION_CONTRACT.md).

Before a QR is issued, the cashier may switch to another payment method through
`cancel_prepared_automatic_qr_sale`. ERP returns local cancellation authority
only after locking the prepared sale and proving that every linked attempt is a
durable preflight rejection where Maybank was never contacted. Issued, pending,
ambiguous, scanned, or paid attempts remain fail-closed.

```http
POST /api/method/kopos_connector.api.cancel_prepared_automatic_qr_sale
```

If an issued Automatic QR cannot be confirmed online, the cashier may verify the
customer's exact-amount bank receipt and complete the sale locally. The later
`submit_order` call submits that same prepared FB Order and creates its one Sales Invoice,
but the Maybank payment is linked to its exact issued transaction and posts to
`Maybank Settings.manual_qr_suspense_account` with
`settlement_status: pending_reconciliation`. Fulfillment and printing do not
wait for this back-office settlement state. A delayed receipt upload and later
provider/bank review update the same Maybank QR Transaction and FB Order Payment;
they must never create a second sale or invoice.

The order/invoice/fulfillment state and settlement state are deliberately
separate. `pending_reconciliation` is a back-office accounting state only: it
does not reopen the sale, suppress sticker printing, block the next checkout, or
prevent an otherwise healthy submitted shift from operating. Provider-paid
evidence later reclassifies the exact suspense receipt to the configured bank or
clearing account through one idempotent submitted Journal Entry.

A manager may mark a settlement `reconciliation_failed` only after ERP proves
the submitted Sales Invoice suspense debit and posts one exact compensating
Journal Entry: debit the company's configured KoPOS QR failure variance Expense
account and credit the snapshotted QR suspense account. The Journal Entry, its
server-derived accounting key, reason, source, order, payment row, invoice,
amount, company, currency, historical target account, and default Cost Center
are bound and verified before the terminal state is written. If configuration or
accounting evidence is missing, the settlement remains `pending_reconciliation`;
the already-submitted
sale, fulfillment, printing, and next checkout remain unaffected. See the
[QR reconciliation accounting contract](docs/QR_RECONCILIATION_ACCOUNTING_CONTRACT.md).
If provider-paid truth arrives after a terminal failure, ERP proves the failure
Journal Entry and posts variance-to-bank recovery; it never credits suspense a
second time and never creates or reopens a sale.

If Maybank later reports a second paid attempt for the same prepared payment,
the first exact attempt remains the winning settlement and ERP never creates a
second Sales Invoice. The later attempt progresses independently through
`accounting_pending -> refund_required -> refunded`. ERP must first prove one
bank/clearing-to-customer-liability Journal Entry; a non-device System Manager
may then record only an exact provider refund and ERP proves the inverse Journal
Entry before marking it refunded. Missing configuration or accounting evidence
cannot block the cashier or roll back the winning sale. Offset/store-credit
resolution is intentionally unsupported. See the
[duplicate Automatic QR refund contract](docs/DUPLICATE_AUTOMATIC_QR_REFUND_CONTRACT.md).

A reusable static DuitNow QR is emitted in device setup only after ERP validates
its PayNet v1.5 TLV structure, static mode, Malaysia AID, Acquirer/QR identity,
MYR/country fields, CRC, payload SHA-256, commissioning timestamp, and POS
Profile company binding. Invalid or legacy uncommissioned payloads are omitted
without disabling Cash or Automatic QR. See the
[static QR commissioning contract](docs/STATIC_QR_COMMISSIONING_CONTRACT.md).

### Process Refund

Create a return `Sales Invoice` and its posted accounting settlement against an
ERP-verified original sale. Obtain a scoped `refund_order` manager approval token
immediately before this call; tokens are short-lived and single-use.

```http
POST /api/method/kopos_connector.api.process_refund
```

Request:
```json
{
  "return_id": "TAB-A-001:SHIFT-001:00042:refund-1",
  "device_id": "TAB-A-001",
  "fb_order": "FB-ORDER-2026-00042",
  "original_sales_invoice": "ACC-SINV-2026-00042",
  "reason_code": "wrong_order",
  "reason_text": "Customer received the wrong drink",
  "refund_method": "cash",
  "return_to_stock": false,
  "lines": [{
    "original_resolved_sale": "FB-RESOLVED-SALE-00042-1",
    "qty_returned": 1
  }],
  "manager_approval_token": "<short-lived-scoped-token>"
}
```

Response:
```json
{
  "status": "ok",
  "return_event": "FB-RETURN-2026-00001",
  "return_sales_invoice": "ACC-SINV-RET-2026-00001",
  "settlement_doctype": "Payment Entry",
  "settlement_document": "ACC-PAY-2026-00001",
  "settlement_status": "Posted"
}
```

Duplicate-safe replay response:
```json
{
  "status": "duplicate",
  "return_event": "FB-RETURN-2026-00001",
  "return_sales_invoice": "ACC-SINV-RET-2026-00001",
  "settlement_status": "Posted"
}
```

### Get Refund Reasons

Return the preset refund reason choices supported by KoPOS clients.

```http
GET /api/method/kopos_connector.api.get_refund_reasons
```

Response:
```json
{
  "refund_reasons": [
    {
      "code": "customer_changed_mind",
      "label": "Customer changed mind"
    },
    {
      "code": "wrong_order",
      "label": "Wrong order"
    },
    {
      "code": "other",
      "label": "Other"
    }
  ]
}
```

### Get Catalog

Returns either a complete catalog snapshot or a small content-hash match response.
The endpoint does not expose partial/delta arrays: clients must not use a delta
contract until deletions have explicit tombstones.

```http
GET /api/method/kopos_connector.api.get_catalog?device_id=TABLET-1&known_version=sha256%3A...
```

Response:
```json
{
  "sync_mode": "full",
  "unchanged": 0,
  "catalog_version": "sha256:...",
  "categories": [...],
  "items": [
    {
      "id": "ICED-MATCHA",
      "name": "Iced Matcha",
      "category_id": "Beverages",
      "price_sen": 1500,
      "is_available": true,
      "is_active": true,
      "modifier_group_ids": ["size", "milk"]
    }
  ],
  "modifier_groups": [
    {
      "id": "size",
      "name": "Size",
      "selection_type": "single",
      "is_required": true,
      "min_selections": 1,
      "max_selections": 1,
      "display_order": 1
    }
  ],
  "modifier_options": [
    {
      "id": "size-large",
      "group_id": "size",
      "name": "Large",
      "price_adjustment_sen": 300,
      "is_default": false,
      "is_active": true,
      "display_order": 2
    }
  ],
  "timestamp": "2026-03-08T12:00:00+08:00",
  "metadata": {
    "company": "Your Company",
    "pos_profile": "Main POS",
    "warehouse": "Stores - YC",
    "currency": "MYR"
  }
}
```

If `known_version` matches the current content, the response omits all catalog
arrays so the tablet does not parse or persist an identical snapshot:

```json
{
  "sync_mode": "unchanged",
  "unchanged": 1,
  "catalog_version": "sha256:...",
  "timestamp": "2026-03-08T12:02:00+08:00",
  "metadata": {
    "company": "Your Company",
    "pos_profile": "Main POS",
    "warehouse": "Stores - YC",
    "currency": "MYR",
    "tax_rate": 0.08
  }
}
```

`price_sen` and `price_adjustment_sen` are authoritative. Compatibility decimal
fields may still appear at migration boundaries, but new clients must neither
send them nor use them for totals, tax, discounts, tender, change, or refunds.

### Get Item Modifiers

Get modifiers for a specific item.

```http
GET /api/method/kopos_connector.api.get_item_modifiers?item_code=ICED-MATCHA
```

### Get Tax Rate

Get SST rate for a POS profile.

```http
GET /api/method/kopos_connector.api.get_tax_rate?pos_profile=Main%20POS
```

Response:
```json
{
  "tax_rate": 0.08
}
```

### Refund Notes

- POS-originated refunds are created as return `Sales Invoice` documents in ERPNext v16.
- `reason_code` accepts: `customer_changed_mind`, `wrong_order`, `quality_issue`, `item_damaged`, `service_issue`, `pricing_error`, `other`.
- `reason_code` is required and is stored on the return `Sales Invoice`; use `reason_text` for approved free-text detail.
- `return_to_stock` controls whether the return updates inventory.

### Stock Availability Policy

**Advisory Stock Warnings (Auto Mode)**
- When `Track Stock` is enabled and stock falls below `custom_kopos_min_qty`, the catalog returns `stock_warning: "erp_stock_short"` with `is_available: true`
- POS shows a "LOW STOCK" warning but allows the sale to proceed
- At submit-time, ERP logs the shortfall to `FB Stock Override Log` instead of rejecting the order
- This keeps operations flowing while maintaining an audit trail

**Hard-Block Scenarios**
- `Force Unavailable` mode: item is hard-blocked (`is_available: false`)
- Disabled items: hard-blocked
- Local POS 86 (manual sold-out): hard-blocked
- These prevent add-to-cart and checkout with clear error messages

**Stock Calculation**
- Availability follows ERPNext v16 POS behavior: `actual_qty - get_pos_reserved_qty(item_code, warehouse) >= custom_kopos_min_qty`
- This keeps KoPOS availability aligned with submitted POS sales that reserve stock before it is reflected in `Bin.actual_qty`

## DocTypes

### KoPOS Modifier Group (Master)

Stores reusable modifier groups.

**Fields:**
- Group Name
- Selection Type (Single/Multiple)
- Required
- Min/Max Selections
- Display Order
- Options (Child Table)

### KoPOS Modifier Option (Child Table)

Individual options within a modifier group.

**Fields:**
- Option Name
- Price Adjustment (MYR)
- Default
- Active
- Display Order

### KoPOS Item Modifier Group (Child Table)

Links items to modifier groups.

**Fields:**
- Modifier Group (Link)
- Display Order
- Always Prompt

## Custom Fields

### Item DocType

- `custom_kopos_availability_mode`: Availability mode (Auto/Force Available/Force Unavailable)
- `custom_kopos_track_stock`: Enable stock tracking
- `custom_kopos_min_qty`: Minimum quantity for availability
- `modifier_groups`: Child table linking modifier groups

### POS Profile DocType

- `custom_kopos_enable_sst`: Enable SST
- `custom_kopos_sst_rate`: SST percentage rate

### Sales Invoice DocType

- `custom_fb_order`: Canonical source `FB Order`
- `custom_fb_shift`: Canonical source `FB Shift`
- `custom_fb_idempotency_key`: Unique sale key used to deduplicate retries
- `custom_fb_device_id`: Device identifier captured from KoPOS submissions

## Sample Data

To create sample modifier groups:

```bash
bench --site your-site console
```

```python
from kopos_connector.setup import create_sample_modifiers
create_sample_modifiers()
```

This will create:
- Size (Small/Medium/Large)
- Milk Type (Regular/Oat/Almond/Soy)
- Ice Level (No Ice/Less Ice/Normal/Extra Ice)
- Sugar Level (No Sugar/25%/50%/75%/100%)
- Add-ons (Boba, Jelly, Pudding)

## Testing

### Manual Testing Checklist

1. Create a modifier group with options
2. Link modifier group to an item
3. Verify API returns modifier data
4. Test availability modes (Auto/Force Available/Force Unavailable)
5. Test stock-based availability (create stock entries)
6. Test SST enable/disable per POS profile

### API Testing

```bash
# Get catalog
curl -X GET \
  "https://your-site.com/api/method/kopos_connector.api.get_catalog" \
  -H "Authorization: token your-api-key:your-api-secret"

# Get item modifiers
curl -X GET \
  "https://your-site.com/api/method/kopos_connector.api.get_item_modifiers?item_code=ICED-MATCHA" \
  -H "Authorization: token your-api-key:your-api-secret"

# Get tax rate
curl -X GET \
  "https://your-site.com/api/method/kopos_connector.api.get_tax_rate?pos_profile=Main%20POS" \
  -H "Authorization: token your-api-key:your-api-secret"
```

## Troubleshooting

### Custom Fields Not Created

Run the after_install hook manually:

```bash
bench --site your-site console
```

```python
from kopos_connector.install.install import create_kopos_custom_fields
create_kopos_custom_fields()
```

### Modifier Groups Not Appearing in Catalog

1. Ensure modifier groups are active
2. Check items are linked to modifier groups
3. Verify API permissions

### Stock-Based Availability Not Working

1. Enable "Track Stock" on item
2. Set "Availability Mode" to "Auto"
3. Create stock entries in warehouse
4. Ensure POS Profile has warehouse configured

## Uninstallation

```bash
bench --site your-site uninstall-app kopos_connector
```

Custom fields will be automatically removed during uninstallation.

## Support

- **Issues**: https://github.com/your-org/kopos-connector/issues
- **Email**: support@kopos.my
- **Documentation**: https://docs.kopos.my

## License

GNU General Public License v3.0

## Credits

- KoPOS Team
- Frappe Technologies
- ERPNext Community
