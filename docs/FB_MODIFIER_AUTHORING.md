# FB Modifier Dependency Authoring

Use the FB doctypes as the only admin authoring path for dependent modifier visibility. Managers should not configure new dependencies through legacy KoPOS modifier forms.

## Desk authoring flow

1. Open **KoPOS > FB Modifier Group** and create the controlling group `Temperature`.
2. Create FB Modifiers inside `Temperature` named `Hot` and `Iced`.
3. Create another **FB Modifier Group** named `Ice Level`.
4. In `Ice Level`, set **Parent Modifier Option** to the `Iced` FB Modifier.
5. Save the group and link both groups to the item or recipe that should use them.

## Resulting behavior

- Selecting `Iced` in `Temperature` reveals the `Ice Level` group.
- Selecting `Hot` keeps `Ice Level` hidden.
- `Parent Modifier Option` must point to an `FB Modifier` that belongs to another `FB Modifier Group`.
- Circular chains are rejected. Example: `Temperature -> Ice Level -> Temperature` cannot be saved.

## Validation rules

- Leave **Parent Modifier Option** blank when the group should always be visible.
- Choose a modifier from the controlling group, not from the dependent group itself.
- If the chosen FB Modifier is missing its `modifier_group`, the save is rejected until the modifier is fixed.
