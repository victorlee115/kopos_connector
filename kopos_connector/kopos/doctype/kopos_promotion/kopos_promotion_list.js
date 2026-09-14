frappe.listview_settings["KoPOS Promotion"] = {
	onload(listview) {
		listview.page.set_primary_action(__("Open Offers"), () => frappe.set_route("jiji_promotions"));
	},
	hide_name_column: true,
	add_fields: ["is_active"],
	get_indicator(doc) {
		// This is inclusion in a future publication, not tablet delivery status.
		return Number(doc.is_active) === 1
			? [__("Next publish: yes"), "blue", "is_active,=,1"]
			: [__("Next publish: no"), "gray", "is_active,=,0"];
	},
	formatters: {
		promotion_type(value) {
			// Compact list labels only; stored values and pricing stay unchanged.
			const labels = {
				item_discount: __("Menu item discount"),
				order_discount: __("Whole-order discount"),
				happy_hour: __("Timed discount"),
				buy_x_get_y: __("Buy and get a reward"),
				nth_item_discount: __("Additional-item discount"),
			};
			return Object.hasOwn(labels, value) ? labels[value] : __("Unrecognised offer type");
		},
	},
};
