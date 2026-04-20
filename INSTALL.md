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
bench get-app kopos_connector /path/to/JiJiPOS/erpnext/kopos_connector
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

### 1. Create Modifier Groups

Navigate to ERPNext Desk:

1. Go to **KoPOS Connector > KoPOS Modifier Group > New**
2. Fill in the details:
   - **Group Name**: e.g., "Size"
   - **Selection Type**: Single or Multiple
   - **Required**: Check if cashier must select an option
   - **Display Order**: Order in modifier sheet (1, 2, 3...)
3. Add options in the child table:
   - **Option Name**: e.g., "Large"
   - **Price Adjustment**: Additional charge (e.g., 3.00)
   - **Default**: Check if this is the default selection
   - **Display Order**: Order within group (1, 2, 3...)
4. Save

### 2. Link Modifiers to Items

1. Go to **Stock > Item**
2. Open or create an item
3. Scroll to **KoPOS Modifiers** section (near bottom)
4. Add modifier groups:
   - Click **Add Row**
   - Select a **Modifier Group**
   - Set **Display Order**
   - Check **Always Prompt** if you want the sheet to always open
5. Save

### 3. Configure Availability

In the Item form, under **KoPOS Availability** section:

- **Availability Mode**:
  - **Auto**: Advisory stock warnings when below minimum (item stays sellable with `stock_warning: "erp_stock_short"`)
  - **Force Available**: Always show as available (ignores stock)
  - **Force Unavailable**: Hard-block sold out (prevents add-to-cart and checkout)

- **Track Stock**: Enable to use stock-based availability checking

- **Min Qty**: Minimum quantity threshold for advisory warning trigger (default: 1)

### 4. Configure SST (Optional)

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
    "idempotency_key": "device-1-order-1",
    "device_id": "device-1",
    "pos_profile": "KoPOS Main",
    "order": {
      "display_number": "001",
      "order_type": "dine_in",
      "subtotal": 24,
      "tax_amount": 0,
      "tax_rate": 0,
      "discount_amount": 0,
      "rounding_adj": 0,
      "total": 24,
      "created_at": "2026-03-09 15:35:00",
      "items": [
        {
          "item_code": "ICED-MATCHA",
          "item_name": "Iced Matcha Latte",
          "qty": 2,
          "rate": 12,
          "amount": 24,
          "modifiers": []
        }
      ],
      "payments": [
        {
          "method": "cash",
          "amount": 24
        }
      ]
    }
  }'
```

2. Refund the sale as a return POS Invoice:

```bash
curl -X POST \
  "https://your-site.com/api/method/kopos_connector.api.process_refund" \
  -H "Authorization: token your-api-key:your-api-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "idempotency_key": "device-1-order-1-refund-1",
    "device_id": "device-1",
    "original_invoice": "ACC-PSINV-2026-00001",
    "refund_type": "partial",
    "refund_reason_code": "wrong_order",
    "refund_reason": "Wrong order",
    "refund_reason_notes": "Customer received the wrong drink",
    "return_to_stock": false,
    "payment_mode": "cash",
    "items": [
      {
        "item_code": "ICED-MATCHA",
        "qty": 1,
        "rate": 12
      }
    ]
  }'
```

3. Verify:
   - sale response returns `status: ok` and a `pos_invoice`
   - refund response returns `status: ok` and a `credit_note`
   - replaying the same refund request returns `status: duplicate`
   - ERPNext marks the return document with `is_return = 1` and `return_against = <original invoice>`
   - ERPNext stores `custom_kopos_refund_reason_code`, `custom_kopos_refund_reason`, and refund remarks on the return `POS Invoice`

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

### Backup

```bash
bench --site your-site.com backup
```

### Restore

```bash
bench --site your-site.com restore /path/to/backup.sql
bench --site your-site.com migrate
```

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

- **Documentation**: `/erpnext/kopos_connector/README.md`
- **Issues**: https://github.com/your-org/kopos-connector/issues
- **Email**: support@kopos.my

## Next Steps

After successful installation:

1. Create modifier groups for your menu
2. Link modifiers to items
3. Configure availability modes
4. Test with KoPOS mobile app
5. Train staff on modifier configuration
6. Go live!

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
