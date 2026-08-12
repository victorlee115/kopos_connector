# Modifier catalog repair runbook

Use this when the menu owner is ready to restore optional recipe-driven
modifiers after the inventory redesign. A broken recipe is a menu setup issue,
not a tablet problem and not a database migration to guess automatically.

The cashier catalog and commercial sale path do not load this optional recipe
configuration. The base item remains saleable without recipe modifiers, and
this repair is not a prerequisite for taking orders, QR payments, refunds,
printing, synchronization, or shift close.

## Important safety rules

- Do not edit recipe ingredients, quantities, stock settings, UOMs, warehouses,
  or costs. Inventory is outside this repair.
- Do not change a recipe definition that has already been published or used.
  This non-inventory campaign may only retire the unchanged invalid version.
- Do not re-enable optional modifiers until every tablet can receive one clear
  rule for each modifier group.
- Make the repair on a restored copy first. Keep the before and after export,
  the full-catalog result, the approving manager, and the exact ERP artifact
  identity.

## Choose the correct business meaning

Open the recipe and the named **FB Modifier Group** in ERP Desk. Ask the menu
owner one simple question: should this recipe follow the same minimum, maximum,
and required rule as the shared group?

- If yes, a future owner-approved replacement recipe must leave its recipe-level
  Required, Override Min Selection, and Override Max Selection values
  blank/zero so the group is the only rule.
- If no, the menu owner may create a separate modifier group under a new clear
  name and put the intended rule there. Linking it requires a future new recipe
  version; do not use recipe overrides to make one shared group mean two
  different things.

For the reported case, review `AMERICANO_COFFEE_RECIPE` and
`ADDITIONAL_ESPRESSO_SHOT` using that decision. The software must not decide the
menu rule on the manager's behalf.

## Rehearse on a restored copy

1. Export the affected recipe version, its allowed modifier rows, the modifier
   group, and its modifiers. Do not export secrets or customer data.
2. Record the shared group's Selection Type, Required, Min Selection, and Max
   Selection values.
3. Retire the invalid recipe without changing any other authored field. The
   save is allowed only for an exact Active-to-Retired, status-only change.
4. Save and reload it. If another field changed, stop; the retirement must fail.
5. Generate the commercial catalog twice for every enabled tablet. The base
   item must remain present and usable with no recipe or modifier dependency.
6. Do not expose the optional modifier group until the inventory revamp owner
   approves and tests a replacement recipe version. Do not disable the base
   sellable item merely because optional recipe enrichment is unavailable.
   Creating or copying component rows is not authorized by this campaign, even
   when their values would be identical.

## Apply to the target ERP

Only an authorized menu manager should repeat the rehearsed status-only
retirement. Take a fresh backup first. After the save, run the target read-only
preflight and the two-pass commercial catalog again. Stop if any enabled tablet
loses the base item; do not hide the error or delete the old evidence. A
replacement recipe remains outside this campaign until the inventory revamp
owner explicitly approves it.

Commercial containment is already complete when every enabled tablet can sell
the base item without consulting recipe or inventory code. Restoring recipe
modifiers remains separate, owner-approved follow-up work. This runbook does
not make an inventory-readiness claim.
