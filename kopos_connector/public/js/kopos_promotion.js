frappe.ui.form.on("KoPOS Promotion", {
	refresh(frm) {
		configureOfferLabels(frm);
		frm.set_intro("");
		if (frm.is_new()) {
			frm.set_intro(__("Use Offers for a guided setup with current menu prices, schedules and a sample receipt."), "blue");
			frm.add_custom_button(__("Create an offer"), () => {
				frappe.route_options = { create: true }; frappe.set_route("jiji_promotions");
			});
			return;
		}
		frm.add_custom_button(__("Open in Offers"), () => {
			frappe.route_options = { offer: frm.doc.name }; frappe.set_route("jiji_promotions");
		});
		const guided = Number(frm.doc.pricing_version) === 2;
		const inclusion = Number(frm.doc.is_active) === 1 ? __("Included in the next publication — not necessarily live yet.") : __("Not included in the next publication.");
		frm.set_intro(guided ? __("This is a guided draft. Use Open in Offers to edit, review or activate it. Saving this record does not change tablet prices.") : `<strong>${inclusion}</strong><br>${__("This is an older offer. Use Offers to see its published status or stop it. Advanced publication is available to support only.")}`, "blue");
		if (guided) frm.disable_save();
		if (!frm.doc.name || !(frappe.user.has_role("Company Director") || frappe.user.has_role("System Manager"))) {
			return;
		}
		if (!guided && frappe.user.has_role("System Manager")) frm.add_custom_button(__("Review and publish outlet offers"), () => reviewOutletOffers(frm));
		frm.add_custom_button(__("Check costs and profit"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint({ title: __("Save your changes first"), message: __("Save this offer, then check costs and profit for those saved details.") });
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Check costs and profit"),
				fields: [
					{ fieldtype: "HTML", options: `<p>${__("Try three possible sales quantities to see ingredient costs and money left over. These are examples, not a sales forecast.")}</p>` },
					{ fieldname: "low", fieldtype: "Int", label: __("If we sell fewer items"), default: 10, reqd: 1 },
					{ fieldname: "base", fieldtype: "Int", label: __("If sales are as expected"), default: 25, reqd: 1 },
					{ fieldname: "high", fieldtype: "Int", label: __("If we sell more items"), default: 50, reqd: 1 },
					{ fieldname: "pos_profile", fieldtype: "Link", options: "POS Profile", label: __("Outlet"), reqd: 1, default: frm.doc.eligible_pos_profiles?.length === 1 ? frm.doc.eligible_pos_profiles[0].pos_profile : "" },
				],
				primary_action_label: __("Calculate"),
				primary_action(values) {
					const promotionName = frm.doc.name;
					frappe.call({
						method: "kopos_connector.api.inventory.get_promotion_economics",
						type: "POST",
						args: {
							payload: JSON.stringify({
								promotion: promotionName,
								pos_profile: values.pos_profile || "",
								scenarios: { low: values.low, base: values.base, high: values.high },
							}),
						},
						async callback(response) {
							// The cost service persists its result and advances modified.
							// Refresh the saved form so the next save is not a stale write.
							if (frm.doc.name === promotionName) {
								if (frm.is_dirty()) {
									frappe.msgprint(__("The cost check updated the saved offer. Your unsaved edits were kept on screen; copy them before reloading this offer."));
								} else {
									try { await frm.reload_doc(); }
									catch (error) { frappe.msgprint(__("The cost result was returned, but the saved offer could not be reloaded. Refresh this offer before making further changes.")); }
								}
							}
							dialog.hide();
							const result = response.message || {};
							if (frm.doc.name === promotionName) frm._kopos_economics_hash = result.economics_hash || result.economics?.economics_hash || "";
							if (result.status !== "ok") {
								frappe.msgprint({
									title: __("Promotion blocked"),
									indicator: "red",
									message: escapeHtml(result.reason || __("Complete recipe, price, and valuation data first.")),
								});
								return;
							}
							frappe.msgprint({
								title: result.publication_status === "Blocked" ? __("Promotion publication blocked") : __("Costs and profit"),
								indicator: result.publication_status === "Blocked" ? "red" : "green",
								message: renderEconomics(result.economics) + (result.publication_status === "Blocked" ? `<p class="text-danger"><strong>${__("A second Company Director must approve a one-time exception before publication.")}</strong></p>` : ""),
							});
						},
					});
				},
			});
			dialog.show();
		}, __("Costs and profit"));
		frm.add_custom_button(__("Approve one-time exception"), () => {
			const economicsHash = frm._kopos_economics_hash || frm.doc.economics_source_hash || "";
			if (!economicsHash) {
				frappe.msgprint({ title: __("Approval blocked"), indicator: "red", message: __("Run Check costs and profit first so the approval uses the current offer and ingredient costs.") });
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Second-director approval"),
				fields: [
					{
						fieldname: "reason",
						fieldtype: "Small Text",
						label: __("Why should this offer proceed despite a loss or missing ingredient costs?"),
						reqd: 1,
						description: __("This approval is bound to the exact server economics evidence and cannot be reused after the Promotion changes."),
					},
				],
				primary_action_label: __("Approve once"),
				primary_action(values) {
					frappe.call({
						method: "kopos_connector.api.inventory.approve_promotion_economics_override",
						type: "POST",
						args: {
							payload: JSON.stringify({ promotion: frm.doc.name, economics_hash: economicsHash, reason: values.reason }),
						},
						callback(response) {
							const result = response.message || {};
							if (result.status !== "approved") {
								frappe.msgprint({ title: __("Approval blocked"), indicator: "red", message: escapeHtml(result.message || __("Run the server-side economics check first.")) });
								return;
							}
							dialog.hide();
							frm.reload_doc();
							frappe.show_alert({ message: __("One-time promotion exception approved"), indicator: "green" });
						},
					});
				},
			});
			dialog.show();
		}, __("Costs and profit"));
	},
});

function configureOfferLabels(frm) {
	const choices = {
		promotion_type: { item_discount: "Discount on menu items", order_discount: "Whole-order discount (advanced)", happy_hour: "Discount during a date and time window", buy_x_get_y: "Buy items and get a reward", nth_item_discount: "Discount on an additional item" },
		activation_mode: { automatic: "Apply automatically", manual_selectable: "Cashier chooses the offer" },
		stacking_policy: { exclusive: "Do not add this to another offer", stackable: "Combine offers (advanced)", best_price: "Compare offers (advanced)" },
		outlet_scope_mode: { all_pos_profiles: "All outlets", selected_pos_profiles: "Only the outlets selected below" },
		eligible_scope_mode: { same_item: "Each matching item separately", eligible_pool: "Mix and match the selected items" },
		repeat_mode: { once: "Once for each qualifying group", repeat: "Repeat for every qualifying set" },
		discount_type: { percentage: "Percent off", fixed_amount: "RM off each item", fixed_price: "Final price of each item (RM)", free_item: "Free reward item" },
		modifier_policy: { excluded_by_default: "Paid extras stay at full price", include_paid_modifiers: "Discount paid extras (advanced)" },
		comparison_basis: { base_item_only: "Menu item price, without extras" },
		discount_basis: { base_item_only: "Menu item price, without extras" },
		discount_target: { cheaper_eligible: "The cheaper qualifying item" },
	};
	for (const [field, options] of Object.entries(choices)) {
		frm.set_df_property(field, "options", Object.entries(options).map(([value, label]) => ({ value, label: __(label) })));
		frm.set_df_property(field, "description", "");
	}
	frm.set_df_property("repeat_mode", "description", __("For mix-and-match offers, Once means once in the order. For matching-item offers, it means once for each different item. This setting applies to buy-and-get offers."));
}


function formatOfferTime(value) {
	if (!value) return "";
	const parts = String(value).match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
	if (!parts) return String(value);
	// Format Malaysia wall-clock components without converting to the account
	// or browser timezone. This is display-only; saved and hashed values stay intact.
	const date = new Date(`${parts[1]}T${parts[2]}:00Z`);
	if (!Number.isFinite(date.getTime())) return String(value);
	return new Intl.DateTimeFormat("en-MY", { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit", hour12: true, timeZone: "UTC" }).format(date);
}


function reviewOutletOffers(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save your changes first. Then review exactly what will be sent to the tablets."));
		return;
	}
	const profiles = frm.doc.eligible_pos_profiles || [];
	const dialog = new frappe.ui.Dialog({
		title: __("Review offers for one outlet"),
		fields: [
			{ fieldtype: "HTML", options: `<p>${__("To include this offer, switch on Include when publishing offers, save it, and run Check costs and profit first. To remove it, switch it off and save. This review covers all offers for the outlet, not just this one.")}</p>` },
			{ fieldname: "pos_profile", fieldtype: "Link", options: "POS Profile", label: __("Outlet"), reqd: 1, default: profiles.length === 1 ? profiles[0].pos_profile : "", get_query: () => ({ filters: { disabled: 0 } }) },
		],
		primary_action_label: __("Check the offers"),
		async primary_action(values) {
			dialog.get_primary_btn().prop("disabled", true);
			try {
				const { message: review } = await frappe.call({ method: "kopos_connector.api.promotion_setup.review_outlet_offers", type: "POST", args: values });
				if (!review?.expected_hash || !Array.isArray(review.offers)) throw new Error("Missing outlet review");
				dialog.hide();
				const confirmation = new frappe.ui.Dialog({
					title: __("Confirm the outlet's offers"), size: "large",
					fields: [{ fieldtype: "HTML", options: `<h4>${escapeHtml(review.pos_profile)}</h4>
						<p>${__("Publishing replaces the outlet's previous offer list with the list below. Dates and times are Malaysia time. Scheduled offers start automatically after a tablet has downloaded them.")}</p>
						${review.offers.length ? `<table class="table table-bordered"><thead><tr><th>${__("Offer")}</th><th>${__("Starts")}</th><th>${__("Ends")}</th></tr></thead><tbody>${review.offers.map(offer => `<tr><td>${escapeHtml(offer.name)}</td><td>${escapeHtml(formatOfferTime(offer.start) || __("No start restriction"))}</td><td>${escapeHtml(formatOfferTime(offer.end) || __("No end date"))}</td></tr>`).join("")}</tbody></table>` : `<p class="text-danger"><strong>${__("No offers are included. Publishing this empty list removes all automatic and selectable offers from this outlet after its tablets refresh.")}</strong></p>`}
						<p>${__("Offline tablets keep the previous list until they reconnect. Publishing does not prove that every tablet has received the change.")}</p>` }],
					primary_action_label: review.offers.length ? __("Publish these offers") : __("Publish empty list — remove outlet offers"),
					async primary_action() {
						confirmation.get_primary_btn().prop("disabled", true);
						try {
							const { message: result } = await frappe.call({ method: "kopos_connector.api.promotion_setup.publish_reviewed_offers", type: "POST", args: { pos_profile: review.pos_profile, expected_hash: review.expected_hash } });
							if (!["published", "unchanged"].includes(result?.status)) throw new Error("Publication was not confirmed");
							confirmation.hide();
							frappe.msgprint(__("Published in ERP. Connected tablets will receive this list on their next promotion refresh. Check the tablet before advertising the offer."));
						} catch (error) {
							frappe.msgprint(__("Publication was not confirmed. Review the outlet again before retrying. Do not assume the tablets have changed."));
						} finally { confirmation.get_primary_btn().prop("disabled", false); }
					},
				});
				confirmation.show();
			} catch (error) {
				frappe.msgprint(__("The outlet is not ready to publish. Check the permission or cost-review message, correct it, then review again. No offers were published by this review."));
			} finally { dialog.get_primary_btn().prop("disabled", false); }
		},
	});
	dialog.show();
}

function escapeHtml(value) {
	return String(value)
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;")
		.replaceAll("'", "&#039;");
}

function money(sen) {
	return sen === null || sen === undefined || sen === ""
		? `<span class="text-muted">${__("Not available")}</span>`
		: `RM ${(Number(sen) / 100).toFixed(2)}`;
}

function actualMoney(sen) {
	return sen === null || sen === undefined || sen === ""
		? `<span class="text-muted">${__("Not available")}</span>`
		: money(sen);
}

function renderEconomics(economics) {
	const scenarioRows = (economics.scenarios || []).map((row) => `
		<tr><td>${escapeHtml(row.label)}</td><td>${row.units}</td><td>${money(row.net_revenue_sen ?? row.revenue_sen)}</td><td>${money(row.tax_sen)}</td><td>${money(row.cogs_sen)}</td><td>${money(row.gross_profit_sen)}</td></tr>`).join("");
	const ingredientRows = Object.entries(economics.ingredient_demand || {}).map(([item, qty]) => `<li>${escapeHtml(item)}: ${escapeHtml(qty)}</li>`).join("") || `<li>${__("No recipe components")}</li>`;
	const batchImpact = economics.batch_preparation_impact || {};
	const batchRows = (batchImpact.components || []).map((row) => row.status === "available"
		? `<tr><td>${escapeHtml(row.item)}</td><td>${escapeHtml(row.bom || "")}</td><td>${escapeHtml(row.base_demand)}</td><td>${escapeHtml(row.batch_qty)}</td><td>${row.batches_required}</td><td>${escapeHtml(row.lead_minutes ?? "—")}</td></tr>`
		: `<tr><td>${escapeHtml(row.item)}</td><td colspan="5" class="text-warning">${escapeHtml(row.reason || __("Evidence unavailable"))}</td></tr>`).join("");
	const risk = economics.runout_waste_risk || {};
	const riskRows = (risk.items || []).map((row) => `<tr><td>${escapeHtml(row.item)}</td><td>${escapeHtml(row.runout || "—")}</td><td>${escapeHtml(row.waste || "—")}</td><td>${escapeHtml(row.reason || "")}</td></tr>`).join("");
	const worstGroup = economics.worst_affected_item_group
		? `${escapeHtml(economics.worst_affected_item_group.item_group)} (${money(economics.worst_affected_item_group.cogs_sen)} ${__("ingredient costs")})`
		: __("Not available");
	const actual = economics.actual_results || {};
	const actualNotes = [actual.note, actual.reason, actual.tax_reason].filter(Boolean).join(" ");
	const actualResult = actual.attribution_status === "available"
		? `<p><strong>${__("Orders")}</strong>: ${actual.order_count} &nbsp; <strong>${__("Promoted units")}</strong>: ${actual.promoted_units || __("Not available")} &nbsp; <strong>${__("Net revenue")}</strong>: ${actualMoney(actual.net_revenue_sen)} &nbsp; <strong>${__("Discount")}</strong>: ${actualMoney(actual.discount_sen)}</p>
			<table class="table table-bordered"><tbody>
			<tr><td>${__("Actual tax")}</td><td>${actualMoney(actual.tax_sen)}</td></tr>
			<tr><td>${__("Actual ingredient costs")}</td><td>${actualMoney(actual.cogs_sen)}</td></tr>
			<tr><td>${__("Actual gross profit")}</td><td>${actualMoney(actual.gross_profit_sen)}</td></tr>
			<tr><td>${__("Actual margin")}</td><td>${actual.margin_percent === null || actual.margin_percent === undefined ? `<span class="text-muted">${__("Not available")}</span>` : `${escapeHtml(actual.margin_percent)}%`}</td></tr>
			<tr><td>${__("Valuation source")}</td><td>${escapeHtml(actual.cogs_source || __("Not available"))}</td></tr>
			</tbody></table>
			<p class="${actual.status === "available" ? "text-muted" : "text-warning"}">${escapeHtml(actualNotes || __("Based on recorded promotion attribution."))}</p>`
		: `<p class="text-muted">${__("Not available yet")}: ${escapeHtml(actual.reason || __("No post-cutover attributed orders"))}</p>`;
	const breakEven = economics.break_even_additional_units === null
		? __("Not available")
		: `${economics.break_even_additional_units} ${__("additional units")}`;
	return `<div>
		<p><strong>${__("Base case")}</strong></p>
		<table class="table table-bordered"><tbody>
		<tr><td>${__("Net revenue (excluding tax)")}</td><td>${money(economics.net_revenue_sen ?? economics.revenue_sen)}</td></tr>
		<tr><td>${__("Tax")}</td><td>${money(economics.tax_sen)}</td></tr>
		<tr><td>${__("Ingredient costs")}</td><td>${money(economics.cogs_sen)}</td></tr>
		<tr><td>${__("Gross profit")}</td><td>${money(economics.gross_profit_sen)}</td></tr>
		<tr><td>${__("Margin")}</td><td>${escapeHtml(economics.margin_percent)}%</td></tr>
		<tr><td>${__("Margin change")}</td><td>${escapeHtml(economics.margin_change_percent)}%</td></tr>
		<tr><td>${__("Break-even")}</td><td>${escapeHtml(breakEven)}</td></tr>
		<tr><td>${__("Worst affected Item group")}</td><td>${worstGroup}</td></tr>
		</tbody></table>
		<p><strong>${__("Volume scenarios")}</strong></p>
		<table class="table table-bordered"><thead><tr><th>${__("Scenario")}</th><th>${__("Units")}</th><th>${__("Net revenue")}</th><th>${__("Tax")}</th><th>${__("Ingredient costs")}</th><th>${__("Gross profit")}</th></tr></thead><tbody>${scenarioRows}</tbody></table>
		<p><strong>${__("Ingredient demand")}</strong></p><ul>${ingredientRows}</ul>
		<p><strong>${__("Prepared-batch impact")}</strong></p>
		${batchImpact.status === "not_applicable" ? `<p class="text-muted">${__("No prepared BOM is used by this promotion.")}</p>` : `<table class="table table-bordered"><thead><tr><th>${__("Component")}</th><th>${__("BOM")}</th><th>${__("Base demand")}</th><th>${__("Batch size")}</th><th>${__("Batches")}</th><th>${__("Lead minutes")}</th></tr></thead><tbody>${batchRows}</tbody></table>`}
		<p><strong>${__("Runout and waste risk")}</strong></p>
		${risk.status === "not_available" ? `<p class="text-muted">${__("Not available")}: ${escapeHtml(risk.reason || __("Current stock and expiry evidence is incomplete."))}</p>` : `<table class="table table-bordered"><thead><tr><th>${__("Component")}</th><th>${__("Runout")}</th><th>${__("Waste")}</th><th>${__("Evidence")}</th></tr></thead><tbody>${riskRows}</tbody></table>`}
		<p><strong>${__("Actual post-promotion results")}</strong></p>${actualResult}
		<p class="text-muted">${__("Planning remains Review First. This calculation does not forecast promotion lift or create purchasing automatically.")}</p>
	</div>`;
}
