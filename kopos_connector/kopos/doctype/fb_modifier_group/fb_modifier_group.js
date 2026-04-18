// Copyright (c) 2026, KoPOS and contributors
// For license information, please see license.txt

const FB_PARENT_MODIFIER_EMPTY_DESCRIPTION = __(
    "Optional. Select the FB Modifier option that reveals this group. Example: set Ice Level -> Parent Modifier Option = Iced from Temperature."
);

async function koposRenderParentModifierContext(frm) {
    const parentModifierName = String(frm.doc.parent_modifier || "").trim();
    if (!parentModifierName) {
        frm.set_df_property(
            "parent_modifier",
            "description",
            FB_PARENT_MODIFIER_EMPTY_DESCRIPTION
        );
        return;
    }

    const response = await frappe.db.get_value(
        "FB Modifier",
        parentModifierName,
        ["modifier_name", "modifier_group"]
    );
    const modifier = response?.message || response || {};
    const modifierName = String(modifier.modifier_name || parentModifierName).trim();
    const parentGroupName = String(modifier.modifier_group || "").trim();

    if (frm.doc.name && parentGroupName && parentGroupName === frm.doc.name) {
        await frm.set_value("parent_modifier", "");
        frappe.throw(
            __(
                "Parent Modifier Option must come from another FB Modifier Group. Example: create Hot/Iced inside Temperature, then set Ice Level -> Parent Modifier Option = Iced."
            )
        );
    }

    const description = parentGroupName
        ? __(
              "This group is visible only when {0} from {1} is selected.",
              [modifierName, parentGroupName]
          )
        : __(
              "Selected modifier: {0}. Save will fail until it belongs to an FB Modifier Group.",
              [modifierName]
          );

    frm.set_df_property("parent_modifier", "description", description);
}

frappe.ui.form.on("FB Modifier Group", {
    setup(frm) {
        frm.set_query("parent_modifier", function() {
            const filters = {
                active: 1,
            };

            if (frm.doc.name) {
                filters.modifier_group = ["!=", frm.doc.name];
            }

            return { filters };
        });
    },

    async refresh(frm) {
        frm.set_intro(
            __(
                "Use Parent Modifier Option only for dependent groups. Example: Temperature contains Hot and Iced, then Ice Level links to Iced so it stays hidden until Iced is selected."
            ),
            "blue"
        );
        await koposRenderParentModifierContext(frm);
    },

    async parent_modifier(frm) {
        await koposRenderParentModifierContext(frm);
    },
});
