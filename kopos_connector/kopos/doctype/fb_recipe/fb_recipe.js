const RECIPE_DEFINITION_FIELDS = [
  "recipe_code",
  "recipe_name",
  "sellable_item",
  "recipe_type",
  "version_no",
  "yield_qty",
  "yield_uom",
  "default_serving_qty",
  "default_serving_uom",
  "station",
  "company",
  "effective_from",
  "effective_to",
  "components",
  "allowed_modifier_groups",
  "notes",
];

function applyPublishedRecipeLock(frm) {
  const isPublished = ["Active", "Retired"].includes(frm.doc.status);

  for (const fieldname of RECIPE_DEFINITION_FIELDS) {
    frm.set_df_property(fieldname, "read_only", isPublished ? 1 : 0);
  }

  frm.set_df_property("status", "read_only", frm.doc.status === "Retired" ? 1 : 0);

  if (frm.doc.status === "Active") {
    frm.set_intro(
      __("This recipe is already available to outlets, so its ingredients and customer choices cannot be changed. Use Copy or change a recipe on JiJi Menu, Recipes & Offers. You can also stop using this version by changing Status to Retired."),
      "blue",
    );
  } else if (frm.doc.status === "Retired") {
    frm.set_intro(
      __("This recipe is no longer in use and cannot be changed. Use Copy or change a recipe on JiJi Menu, Recipes & Offers to create a new version."),
      "grey",
    );
  } else {
    frm.set_intro("");
  }
}

frappe.ui.form.on("FB Recipe", {
  refresh(frm) {
    applyPublishedRecipeLock(frm);
  },

  status(frm) {
    applyPublishedRecipeLock(frm);
  },
});
