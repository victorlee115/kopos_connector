# Modifier catalog repair runbook

Use this when a catalog refresh says that a recipe changes the selection rules
of a shared modifier group. This is a menu setup problem, not a tablet problem
and not a database migration to guess automatically.

## Important safety rules

- Do not edit recipe ingredients, quantities, stock settings, UOMs, warehouses,
  or costs. Inventory is outside this repair.
- Do not change a recipe that has already been published or used. Create a new
  recipe version.
- Do not weaken the catalog check. Every tablet must receive one clear rule for
  each modifier group.
- Make the repair on a restored copy first. Keep the before and after export,
  the full-catalog result, the approving manager, and the exact ERP artifact
  identity.

## Choose the correct business meaning

Open the recipe and the named **FB Modifier Group** in ERP Desk. Ask the menu
owner one simple question: should this recipe follow the same minimum, maximum,
and required rule as the shared group?

- If yes, the new recipe version must leave its recipe-level Required, Override
  Min Selection, and Override Max Selection values blank/zero so the group is
  the only rule.
- If no, clone the modifier group under a new clear name, put the intended rule
  on that new group, and link the new recipe version to it. Do not use recipe
  overrides to make one shared group mean two different things.

For the reported case, review `AMERICANO_COFFEE_RECIPE` and
`ADDITIONAL_ESPRESSO_SHOT` using that decision. The software must not decide the
menu rule on the manager's behalf.

## Rehearse on a restored copy

1. Export the affected recipe version, its allowed modifier rows, the modifier
   group, and its modifiers. Do not export secrets or customer data.
2. Record the shared group's Selection Type, Required, Min Selection, and Max
   Selection values.
3. Create the separate modifier group when the intended rules differ. Copy only
   the modifier choices and dependency relationships that the menu owner
   approves.
4. Create a new recipe version. Copy its existing definition without changing
   any recipe component or stock field. Link the correct modifier group and
   remove recipe-level selection overrides.
5. Retire the old recipe version and activate the new version with a reviewed,
   non-overlapping effective time.
6. Save and reload both documents. A bad rule must now be rejected at save time
   instead of breaking a later tablet refresh.
7. Generate the complete catalog twice for every enabled tablet. Both passes
   must succeed and produce the same catalog identity.
8. Test the item on a tablet using the saved menu, including the minimum,
   maximum, required, default, and dependency behavior.

## Apply to the target ERP

Only an authorized menu manager should repeat the already-rehearsed document
changes. Take a fresh backup first. After the save, run the target read-only
preflight and the two-pass full catalog again. Stop if any enabled tablet fails;
do not hide the error or delete the old evidence.

The repair is complete only when the manager-approved menu behavior, every
enabled tablet catalog, and the exact target artifact all agree. This runbook
does not make an inventory-readiness claim.
