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
