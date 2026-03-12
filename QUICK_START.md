# KoPOS Connector - Quick Start Guide

Get up and running with KoPOS modifiers in 5 minutes.

## Prerequisites

- ERPNext v16+ installed
- Bench CLI available
- Site administrator access

## Installation (3 steps)

### 1. Get the App

```bash
cd /home/frappe/frappe-bench
bench get-app kopos_connector /path/to/JiJiPOS/erpnext/kopos_connector
```

### 2. Install on Site

```bash
bench --site your-site.com install-app kopos_connector
```

### 3. Create Sample Modifiers

```bash
bench --site your-site.com console
```

```python
from kopos_connector.setup import create_sample_modifiers
create_sample_modifiers()
```

## Configuration (2 steps)

### 1. Link Modifiers to an Item

1. Go to **Stock > Item**
2. Open an item (e.g., "Iced Matcha")
3. Scroll to **KoPOS Modifiers** section
4. Add modifier groups: Size, Milk Type, Add-ons
5. Save

### 2. Test the API

```bash
curl -X GET \
  "https://your-site.com/api/method/kopos.api.get_catalog" \
  -H "Authorization: token your-api-key:your-api-secret"
```

You should see the item with `modifier_group_ids` in the response.

## Connect KoPOS Mobile App

1. Open KoPOS app settings
2. Enter ERP URL: `https://your-site.com`
3. Enter API Key and Secret
4. Tap "Pull Catalog"
5. Tap an item with modifiers → modifier sheet opens!

## What's Next?

- **Full Documentation**: See `README.md` and `INSTALL.md`
- **API Reference**: See `docs/ERPNEXT_MODIFIER_IMPLEMENTATION.md`
- **Troubleshooting**: See `INSTALL.md` → Troubleshooting section

## Common Tasks

### Create a Custom Modifier Group

1. Go to **KoPOS Connector > KoPOS Modifier Group > New**
2. Fill in:
   - Group Name: "Toppings"
   - Selection Type: Multiple
   - Required: No
   - Max Selections: 3
3. Add options:
   - Whipped Cream (+1.00)
   - Chocolate Drizzle (+0.50)
   - Sprinkles (+0.50)
4. Save

### Set Stock-Based Availability

1. Open an Item
2. Under **KoPOS Availability**:
   - Mode: Auto
   - Track Stock: ✓
   - Min Qty: 2
3. Create stock entry
4. Pull catalog in POS app
5. Item availability updates automatically!

### Disable SST for a POS Profile

1. Go to **POS > POS Profile**
2. Open your profile
3. Under **KoPOS SST Configuration**:
   - Enable SST: ☐
4. Save
5. Pull catalog in POS app
6. Orders no longer include SST

## Need Help?

- **Email**: support@kopos.my
- **Docs**: See README.md and INSTALL.md
- **Issues**: https://github.com/your-org/kopos-connector/issues

---

**That's it!** You're ready to use modifiers in KoPOS.
