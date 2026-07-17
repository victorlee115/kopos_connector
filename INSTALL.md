# KoPOS Connector - Installation Guide

This guide provides step-by-step instructions for installing and configuring the KoPOS Connector app on ERPNext.

## Prerequisites

- **ERPNext**: Version 16.0.0 or higher
- **Frappe Framework**: Version 16.0.0 or higher
- **Python**: Version 3.10 or higher
- **Database**: MariaDB 10.6+ or PostgreSQL 12+
- **Bench**: Latest version

## Installation Steps

### 1. Verify ERPNext Installation

Ensure ERPNext is properly installed and running:

```bash
bench version
```

You should see output similar to:
```
erpnext 16.x.x
frappe 16.x.x
```

### 2. Get the KoPOS Connector App

#### Option A: From Local Directory

If you have the app code locally:

```bash
cd /home/frappe/frappe-bench
bench get-app kopos_connector /absolute/path/to/JiJiPOS-Everything/worktree-fnb-erpnext
```

#### Option B: From Git Repository

```bash
cd /home/frappe/frappe-bench
bench get-app kopos_connector https://github.com/your-org/kopos-connector.git
```

### 3. Install the App on Your Site

```bash
bench --site your-site.com install-app kopos_connector
```

This will:
- Install the app
- Create DocTypes (KoPOS Modifier Group, KoPOS Modifier Option, KoPOS Item Modifier Group)
- Add custom fields to Item and POS Profile DocTypes
- Run database migrations

### 4. Verify Installation

Check that the app is installed:

```bash
bench --site your-site.com list-apps
```

You should see `kopos_connector` in the list.

### 5. Create Sample Modifier Groups (Optional)

For testing and demonstration:

```bash
bench --site your-site.com console
```

```python
from kopos_connector.setup import create_sample_modifiers
create_sample_modifiers()
```

This creates 5 sample modifier groups:
- Size (Small/Medium/Large)
- Milk Type (Regular/Oat/Almond/Soy)
- Ice Level (No Ice/Less Ice/Normal/Extra Ice)
- Sugar Level (No Sugar/25%/50%/75%/100%)
- Add-ons (Boba, Jelly, Pudding)

## Configuration

### 1. Configure the Manager Approval Signing Secret

Privileged void and refund approvals fail closed until every site has a unique,
random signing secret of at least 32 characters:

```bash
bench --site your-site set-config kopos_manager_approval_secret "$(openssl rand -hex 32)"
bench --site your-site migrate
```

Keep this value out of source control and application logs. Do not rotate it
while an issued approval token may still be in flight (tokens expire within
five minutes).

### 2. Provision Maybank Provider Identity

The connector persists one Maybank provider device identity and version-neutral
metadata during `install-app`/`migrate`. The production endpoint defaults to the
official HTTPS origin and rejects HTTP, embedded credentials, query redirects,
unlisted origins, and `mock://`.

For the small Tab A11 deployment, set the exact model/OS labels before the first
migration if they differ from the validated defaults:

```bash
bench --site your-site.com set-config maybank_provider_device_name "Samsung Galaxy Tab A11 Small SM-X130"
bench --site your-site.com set-config maybank_provider_device_os "Android 16"
bench --site your-site.com migrate
```

Use the OS actually installed on the release tablet; do not copy the example
version blindly. The persisted provider identity and metadata are immutable
during normal settings edits. Changing them requires an approved provider
re-registration procedure. Additional HTTPS provider origins require an explicit
`maybank_allowed_origins` site configuration and security review. `mock://`
requires both `allow_maybank_mock=1` and a test/developer context and must never
be enabled on a production site.

Configure the **DuitNow QR** Mode of Payment as type **Bank** with exactly one
company account. That account must be an enabled, non-group **Bank** account or
an untyped Asset clearing account in the sale currency. Never map verified QR
payments to a physical Cash account. ERP rejects generation before contacting
Maybank when this accounting destination is unsafe or ambiguous.

#### Isolated test-site Maybank payment simulation

To test the real QR paid/finalization path without calling Maybank, use a
disposable non-production site only:

```bash
bench --site test-site.local set-config developer_mode 1
bench --site test-site.local set-config allow_maybank_mock 1
bench --site test-site.local set-config allow_maybank_desk_simulation 1
bench --site test-site.local set-config maybank_mock_payment_mode manual
bench --site test-site.local migrate
bench restart
```

As a System Manager, enable Maybank Settings, configure a valid test outlet and
manual QR suspense account, and set **API Base URL** to `mock://`. Generate an
Automatic QR through the normal POS flow, then open its **Maybank QR
Transaction** in ERP Desk and choose **Testing > Simulate Successful Payment
(Test Only)**. Type `SIMULATE MAYBANK PAYMENT` when prompted. The transaction
remains pending until this explicit action.

Never enable these flags or `developer_mode` on production. After testing,
disable both allow flags, restore the official HTTPS Maybank URL, and restart
all web, worker, and scheduler processes. Directly changing the transaction
status is neither supported nor equivalent to provider payment.

### 3. Create Modifier Groups

Navigate to ERPNext Desk:

1. Go to **KoPOS Connector > KoPOS Modifier Group > New**
2. Fill in the details:
   - **Group Name**: e.g., "Size"
   - **Selection Type**: Single or Multiple
   - **Required**: Check if cashier must select an option
   - **Display Order**: Order in modifier sheet (1, 2, 3...)
3. Add options in the child table:
   - **Option Name**: e.g., "Large"
   - **Price Adjustment**: Additional charge (for example RM3.00; the POS wire contract carries this as `300` sen)
   - **Default**: Check if this is the default selection
   - **Display Order**: Order within group (1, 2, 3...)
4. Save

### 4. Link Modifiers to Items

1. Go to **Stock > Item**
2. Open or create an item
3. Scroll to **KoPOS Modifiers** section (near bottom)
4. Add modifier groups:
   - Click **Add Row**
   - Select a **Modifier Group**
   - Set **Display Order**
   - Check **Always Prompt** if you want the sheet to always open
5. Save

### 5. Configure Availability

In the Item form, under **KoPOS Availability** section:

- **Availability Mode**:
  - **Auto**: Advisory stock warnings when below minimum (item stays sellable with `stock_warning: "erp_stock_short"`)
  - **Force Available**: Always show as available (ignores stock)
  - **Force Unavailable**: Hard-block sold out (prevents add-to-cart and checkout)

- **Track Stock**: Enable to use stock-based availability checking

- **Min Qty**: Minimum quantity threshold for advisory warning trigger (default: 1)

### 6. Configure SST (Optional)

1. Go to **POS > POS Profile**
2. Open or create a POS profile
3. Scroll to **KoPOS SST Configuration** section
4. Configure:
   - **Enable SST**: Check to enable SST
   - **SST Rate (%)**: Set rate (default: 8)

## Testing the Installation

### 1. Test API Endpoint

Use curl or Postman to test the catalog API:

```bash
curl -X GET \
  "https://your-site.com/api/method/kopos_connector.api.get_catalog" \
  -H "Authorization: token your-api-key:your-api-secret"
```

You should receive a JSON response with:
- `categories`: Item groups
- `items`: Items with `modifier_group_ids`
- `modifier_groups`: Modifier group definitions
- `modifier_options`: Individual modifier options

### 2. Test in KoPOS Mobile App

1. Configure ERPNext URL and API credentials in KoPOS app
2. Trigger catalog pull
3. Verify:
   - Items with modifiers show modifier sheet
   - Defaults are preselected
   - Price adjustments are applied
   - Availability reflects ERPNext state

### 2a. Test Order Submission and Refunds

1. Submit a sale through the public API:

```bash
curl -X POST \
  "https://your-site.com/api/method/kopos_connector.api.submit_order" \
  -H "Authorization: token your-api-key:your-api-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "money_contract_version": "sen_v1",
    "order_id": "device-1-order-1",
    "idempotency_key": "device-1-order-1",
    "device_id": "device-1",
    "shift_id": "SHIFT-1",
    "staff_id": "cashier@example.test",
    "warehouse": "Main Booth - JC",
    "company": "JiJi Cafe",
    "currency": "MYR",
    "order": {
      "display_number": "001",
      "order_type": "dine_in",
      "subtotal_sen": 2400,
      "tax_amount_sen": 0,
      "rounding_adjustment_sen": 0,
      "total_sen": 2400,
      "created_at": "2026-07-14T15:35:00+08:00",
      "items": [
        {
          "line_id": "line-1",
          "item_code": "ICED-MATCHA",
          "item_name": "Iced Matcha Latte",
          "qty": 2,
          "unit_price_sen": 1200,
          "modifier_total_sen": 0,
          "discount_amount_sen": 0,
          "line_total_sen": 2400,
          "modifiers": []
        }
      ],
      "payments": [
        {
          "payment_method": "Cash",
          "amount_sen": 2400,
          "tendered_amount_sen": 2400,
          "change_amount_sen": 0
        }
      ]
    }
  }'
```

2. Request a scoped `refund_order` manager approval token, then refund the sale
   as a return `Sales Invoice`:

```bash
curl -X POST \
  "https://your-site.com/api/method/kopos_connector.api.process_refund" \
  -H "Authorization: token your-api-key:your-api-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "return_id": "device-1-order-1-refund-1",
    "device_id": "device-1",
    "fb_order": "FB-ORDER-2026-00001",
    "original_sales_invoice": "ACC-SINV-2026-00001",
    "reason_code": "wrong_order",
    "reason_text": "Customer received the wrong drink",
    "refund_method": "cash",
    "return_to_stock": false,
    "lines": [
      {
        "original_resolved_sale": "FB-RESOLVED-SALE-00001-1",
        "qty_returned": 1
      }
    ],
    "manager_approval_token": "<short-lived-scoped-token>"
  }'
```

3. Verify:
   - sale response returns `status: ok`, an `fb_order`, a `sales_invoice`, and `projection_status: posted`
   - refund response returns `status: ok`, a `return_sales_invoice`, and posted settlement proof
   - replaying the same refund request returns `status: duplicate`
   - ERPNext marks the return document with `is_return = 1` and `return_against = <original invoice>`
   - ERPNext stores the reason and refund audit data on the return event and return `Sales Invoice`

### 2b. Test Refund Reason Presets

```bash
curl -X GET \
  "https://your-site.com/api/method/kopos_connector.api.get_refund_reasons" \
  -H "Authorization: token your-api-key:your-api-secret"
```

Verify the response includes the preset codes:
- `customer_changed_mind`
- `wrong_order`
- `quality_issue`
- `item_damaged`
- `service_issue`
- `pricing_error`
- `other`

### 3. Test Advisory Stock Warnings (Auto Mode)

1. Create a stock entry for an item:
   ```bash
   bench --site your-site.com console
   ```
   
   ```python
   import frappe
   from frappe.stock.doctype.stock_entry.stock_entry import make_stock_entry
   
   # Create stock entry
   se = make_stock_entry(
       item_code="ICED-MATCHA",
       qty=10,
       to_warehouse="Stores - YC",
       company="Your Company",
       expense_account="Stock Adjustment - YC",
       cost_center="Main - YC"
   )
   se.submit()
   ```

2. Enable stock tracking on the item:
   - Go to Item
   - Under **KoPOS Availability**:
     - Set **Availability Mode** to "Auto"
     - Check **Track Stock**
     - Set **Min Qty** to 1

3. Pull catalog in KoPOS app
4. Verify item shows as available (`is_available: true`, no `stock_warning`)

5. Reduce stock below minimum:
   ```python
   se = make_stock_entry(
       item_code="ICED-MATCHA",
       qty=-10,  # Remove stock
       from_warehouse="Stores - YC",
       company="Your Company",
       expense_account="Stock Adjustment - YC",
       cost_center="Main - YC"
   )
   se.submit()
   ```

6. Pull catalog again
7. Verify item shows advisory warning (`is_available: true`, `stock_warning: "erp_stock_short"`)
8. Submit an order with the item
9. Verify order succeeds and shortfall is logged to `FB Stock Override Log`
10. For ERPNext v16 POS setups, also verify availability follows `actual_qty - POS reserved qty`, not only raw `Bin.actual_qty`

### 4. Test Hard-Block Sold Out (Force Unavailable)

1. Set **Availability Mode** to "Force Unavailable" on an item
2. Pull catalog
3. Verify item shows as unavailable (`is_available: false`)
4. Attempt to add to cart in POS
5. Verify hard-block error prevents add-to-cart

### 5. Test Manual Override Modes

1. Set **Availability Mode** to "Force Unavailable"
2. Pull catalog
3. Verify item shows as hard-block sold out (`is_available: false`)

4. Set **Availability Mode** to "Force Available"
5. Pull catalog
6. Verify item shows as available (regardless of stock state)

## Troubleshooting

### Custom Fields Not Created

If custom fields are missing, run manually:

```bash
bench --site your-site.com console
```

```python
from kopos_connector.install.install import create_kopos_custom_fields
create_kopos_custom_fields()
```

### DocTypes Not Created

Check migrations:

```bash
bench --site your-site.com migrate
```

### API Returns Empty Catalog

1. Check permissions:
   - Ensure user has read access to Item, Item Group, KoPOS Modifier Group
   - Verify API key/secret is correct

2. Check data:
   - Ensure items exist and are active
   - Ensure modifier groups are linked to items
   - Verify item groups are not disabled

### Modifier Sheet Not Opening

1. Check item configuration:
   - Verify `modifier_group_ids` array is not empty in API response
   - Ensure modifier groups are active

2. Check KoPOS app logs:
   - Look for errors in catalog sync
   - Verify catalog store loaded modifiers correctly

### Price Adjustments Not Applied

1. Check modifier option configuration:
   - Verify `price_adjustment` is set on options
   - Ensure options are active

2. Check KoPOS app:
   - Verify modifier flow is using `priceAdjustmentSen` field
   - Check cart total calculations

## Uninstallation

To remove the app:

```bash
bench --site your-site.com uninstall-app kopos_connector
bench remove-app kopos_connector
```

This will:
- Remove custom fields
- Remove DocTypes
- Remove all KoPOS Connector data

## Backup and Restore

Database-only backup is insufficient. A production recovery set must contain the
database, public files, private QR evidence files, the site encryption key, the
exact connector artifact/commit, and the external credential inventory.

Before release, the operator must approve and record these values; this guide
does not invent business-specific defaults:

| Required release input | Approved value |
|---|---|
| Recovery point objective (RPO) | `<required>` |
| Recovery time objective (RTO) | `<required>` |
| Online backup retention | `<required>` |
| Offline/immutable backup retention | `<required>` |
| Restore-drill frequency and owner | `<required>` |

### Create a Complete Recovery Set

```bash
bench --site your-site.com backup --with-files
bench version
```

Store together, under access control and encryption:

- the generated database backup and both public/private file archives;
- `sites/your-site.com/site_config.json`, including its encryption key, without
  printing it to logs or committing it to source control;
- the exact signed `kopos_connector` wheel/source artifact, namespaced release
  tag, commit SHA, manifest, and SHA-256 digest;
- an inventory of required external credentials and owners (device API keys,
  Maybank merchant/outlet credentials, manager approval signing secret, SMTP,
  object storage, and monitoring). Store actual secret values only in the
  approved secrets manager;
- ERPNext/Frappe app versions from `bench version` and the site configuration
  needed to recreate workers, scheduler, TLS, queues, and storage mounts.

Verify the backup command produced non-empty database, public-file, and
private-file artifacts. Copy the recovery set off the ERP host according to the
approved RPO, retention, geographic, and immutability policy.

### Restore to an Isolated Drill Site

Never test a restore against the production site. Build an isolated host with
the exact recorded Frappe/ERPNext and connector artifacts, restore
`site_config.json` securely, and keep outbound Maybank, email, webhooks, printers,
and production device credentials disabled until validation is complete.

```bash
bench --site restore-drill.local restore /secure/recovery/database.sql.gz \
  --with-public-files /secure/recovery/public-files.tar \
  --with-private-files /secure/recovery/private-files.tar
bench --site restore-drill.local migrate
bench --site restore-drill.local list-apps
```

Credential restoration must preserve the original site encryption key so
encrypted passwords remain readable. Then restore or rotate secrets through the
approved secrets manager, verify worker/scheduler health, and keep the restored
site isolated while proving:

- FB Shift, FB Order, Sales Invoice, return, settlement, GL, stock ledger,
  projection and idempotency records are readable and reconcile;
- private manual-QR evidence opens only for authorized support roles;
- device configuration and catalog endpoints remain device-scoped;
- the Maybank provider device identity matches the pre-backup identity;
- a synthetic sale/refund/shift lifecycle passes without contacting production
  providers; and
- measured recovery point and elapsed recovery time meet the approved RPO/RTO.

A witnessed restore drill with retained evidence is mandatory before production
launch and at the approved recurring frequency. A successful backup without a
successful restore drill is not release evidence.

## Production Deployment

### 1. Enable Production Mode

```bash
sudo bench setup production frappe
bench setup nginx
sudo service nginx reload
```

### 2. Configure SSL

```bash
bench setup lets-encrypt your-site.com
```

### 3. Setup Background Workers

```bash
bench setup supervisor
sudo supervisorctl restart all
```

### 4. Monitor Logs

```bash
# Watch ERPNext logs
tail -f /home/frappe/frappe-bench/logs/bench-start.log

# Watch KoPOS Connector logs
tail -f /home/frappe/frappe-bench/logs/web.error.log | grep kopos
```

## Support

- **Documentation**: the authoritative `worktree-fnb-erpnext/README.md`
- **Issues**: https://github.com/your-org/kopos-connector/issues
- **Email**: support@kopos.my

## Next Steps

After successful installation:

1. Create modifier groups for your menu
2. Link modifiers to items
3. Configure availability modes
4. Test with KoPOS mobile app
5. Train staff on modifier configuration
6. Complete the signed release, live ERP smoke, backup/restore drill, exact small
   Tab A11 and printer acceptance, canary, monitoring, and rollback gates before
   authorizing production traffic

## Checklist

- [ ] ERPNext installed and running
- [ ] KoPOS Connector app installed
- [ ] Custom fields created (Item, POS Profile)
- [ ] DocTypes created (Modifier Group, Modifier Option, Item Modifier Group)
- [ ] Sample modifiers created
- [ ] Modifier groups configured
- [ ] Items linked to modifier groups
- [ ] Availability modes configured
- [ ] SST configured (if applicable)
- [ ] API endpoint tested
- [ ] KoPOS mobile app connected
- [ ] Modifier sheet verified
- [ ] Stock-based availability tested
- [ ] Production deployment completed
- [ ] Exact connector release artifact, tag, commit, manifest and digest recorded
- [ ] RPO, RTO, retention and restore-drill ownership approved
- [ ] Database, public files, private files and site encryption key backed up
- [ ] Isolated restore drill reconciled and witnessed
- [ ] Maybank HTTPS origin, immutable provider identity and real credentials validated
- [ ] Live Sales Invoice/GL/stock/refund/shift smoke evidence retained
