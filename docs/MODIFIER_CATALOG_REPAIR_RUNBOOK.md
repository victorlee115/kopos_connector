# Modifier catalog repair runbook

Use this when a catalog refresh says that a recipe changes the selection rules
of a shared modifier group. This is a menu setup problem, not a tablet problem
and not a database migration to guess automatically.

## Important safety rules

- Do not edit recipe ingredients, quantities, stock settings, UOMs, warehouses,
  or costs. Inventory is outside this repair.
- Do not change a recipe definition that has already been published or used.
  This non-inventory campaign may only retire the unchanged invalid version.
- Do not weaken the catalog check. Every tablet must receive one clear rule for
  each modifier group.
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
5. Generate the complete catalog twice for every enabled tablet. This proves
   the invalid recipe no longer breaks menu refresh, but it does not prove a
   replacement recipe or inventory behavior.
6. Keep the sellable item unavailable until the inventory revamp owner approves
   creation of a new recipe version. Creating or copying component rows is not
   authorized by this campaign, even when their values would be identical.

## Apply to the target ERP

Only an authorized menu manager should repeat the rehearsed status-only
retirement. Take a fresh backup first. After the save, run the target read-only
preflight and the two-pass full catalog again. Stop if any enabled tablet fails;
do not hide the error or delete the old evidence. A replacement recipe remains
outside this campaign until the inventory revamp owner explicitly approves it.

Containment is complete only when every enabled tablet catalog succeeds with the
invalid recipe retired. Full menu repair remains blocked until an approved
replacement recipe exists. This runbook does not make an inventory-readiness
claim.
