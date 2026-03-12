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
bench get-app kopos_connector /path/to/JiJiPOS/erpnext/kopos_connector
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
  - **Auto**: Use stock level (if tracking enabled)
  - **Force Available**: Always show as available
  - **Force Unavailable**: Always show as sold out
- **Track Stock**: Enable stock-based availability
- **Min Qty**: Minimum quantity required for availability

### 4. Configure SST (Optional)

In POS Profile form, under **KoPOS SST Configuration** section:

- **Enable SST**: Toggle SST for this POS profile
- **SST Rate (%)**: Set SST percentage (default: 8%)

## API Endpoints

### Submit Order

Create and submit a POS Invoice using KoPOS' idempotent order contract.

```http
POST /api/method/kopos_connector.api.submit_order
```

Response:
```json
{
  "status": "ok",
  "pos_invoice": "ACC-PSINV-2026-00001",
  "idempotency_key": "TAB-A-001:SHIFT-001:042"
}
```

### Process Refund

Process a KoPOS refund as a return `POS Invoice` against the original POS sale.

```http
POST /api/method/kopos_connector.api.process_refund
```

Request:
```json
{
  "idempotency_key": "TAB-A-001:SHIFT-001:042:refund-1",
  "device_id": "TAB-A-001",
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
      "rate": 12.00
    }
  ]
}
```

Response:
```json
{
  "status": "ok",
  "credit_note": "ACC-PSINV-2026-00002",
  "idempotency_key": "TAB-A-001:SHIFT-001:042:refund-1",
  "refund_amount": 12.0
}
```

Duplicate-safe replay response:
```json
{
  "status": "duplicate",
  "credit_note": "ACC-PSINV-2026-00002",
  "idempotency_key": "TAB-A-001:SHIFT-001:042:refund-1",
  "message": "Refund already processed"
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

Returns full catalog with categories, items, and modifiers.

```http
GET /api/method/kopos_connector.api.get_catalog
```

Response:
```json
{
  "categories": [...],
  "items": [
    {
      "id": "ICED-MATCHA",
      "name": "Iced Matcha",
      "category_id": "Beverages",
      "price": 15.00,
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
      "price_adjustment": 3.00,
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

- POS-originated refunds are created as return `POS Invoice` documents in ERPNext v16.
- `refund_reason_code` accepts: `customer_changed_mind`, `wrong_order`, `quality_issue`, `item_damaged`, `service_issue`, `pricing_error`, `other`.
- `refund_reason` is required and is stored on the return `POS Invoice`; when `refund_reason_code` is `other`, `refund_reason_notes` becomes the stored reason text.
- `return_to_stock` controls whether the return updates inventory.

### Stock Availability Notes

- When `Track Stock` is enabled, catalog availability follows ERPNext v16 POS behavior: `actual_qty - get_pos_reserved_qty(item_code, warehouse) >= custom_kopos_min_qty`.
- This keeps KoPOS availability aligned with submitted POS sales that reserve stock before it is reflected in `Bin.actual_qty`.

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

### POS Invoice DocType

- `custom_kopos_idempotency_key`: Unique key used to deduplicate retries
- `custom_kopos_device_id`: Device identifier captured from KoPOS submissions

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
