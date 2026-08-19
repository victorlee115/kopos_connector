frappe.ui.form.on("KoPOS Promotion", {
	refresh(frm) {
		if (!frm.doc.name || !(frappe.user.has_role("Company Director") || frappe.user.has_role("System Manager"))) {
			return;
		}
		frm.add_custom_button(__("Check COGS & margin"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Promotion economics"),
				fields: [
					{ fieldname: "low", fieldtype: "Int", label: __("Low units"), default: 10, reqd: 1 },
					{ fieldname: "base", fieldtype: "Int", label: __("Base units"), default: 25, reqd: 1 },
					{ fieldname: "high", fieldtype: "Int", label: __("High units"), default: 50, reqd: 1 },
					{ fieldname: "pos_profile", fieldtype: "Link", options: "POS Profile", label: __("Price and stock profile") },
				],
				primary_action_label: __("Calculate"),
				primary_action(values) {
					frappe.call({
						method: "kopos_connector.api.inventory.get_promotion_economics",
						type: "POST",
						args: {
							payload: JSON.stringify({
								promotion: frm.doc.name,
								pos_profile: values.pos_profile || "",
								scenarios: { low: values.low, base: values.base, high: values.high },
							}),
						},
						callback(response) {
							dialog.hide();
							const result = response.message || {};
							frm._kopos_economics_hash = result.economics_hash || result.economics?.economics_hash || "";
							if (result.status !== "ok") {
								frappe.msgprint({
									title: __("Promotion blocked"),
									indicator: "red",
									message: escapeHtml(result.reason || __("Complete recipe, price, and valuation data first.")),
								});
								return;
							}
							frappe.msgprint({
								title: result.publication_status === "Blocked" ? __("Promotion publication blocked") : __("Promotion economics"),
								indicator: result.publication_status === "Blocked" ? "red" : "green",
								message: renderEconomics(result.economics) + (result.publication_status === "Blocked" ? `<p class="text-danger"><strong>${__("A second Company Director must approve a one-time exception before publication.")}</strong></p>` : ""),
							});
						},
					});
				},
			});
			dialog.show();
		}, __("Economics"));
		frm.add_custom_button(__("Approve one-time exception"), () => {
			const economicsHash = frm._kopos_economics_hash || frm.doc.economics_source_hash || "";
			if (!economicsHash) {
				frappe.msgprint({ title: __("Approval blocked"), indicator: "red", message: __("Run Check COGS & margin first so the approval is bound to current server evidence.") });
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Second-director approval"),
				fields: [
					{
						fieldname: "reason",
						fieldtype: "Small Text",
						label: __("Reason for approving this below-cost or incomplete COGS promotion"),
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
		}, __("Economics"));
	},
});

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
		? `${escapeHtml(economics.worst_affected_item_group.item_group)} (${money(economics.worst_affected_item_group.cogs_sen)} ${__("COGS exposure")})`
		: __("Not available");
	const actual = economics.actual_results || {};
	const actualNotes = [actual.note, actual.reason, actual.tax_reason].filter(Boolean).join(" ");
	const actualResult = actual.attribution_status === "available"
		? `<p><strong>${__("Orders")}</strong>: ${actual.order_count} &nbsp; <strong>${__("Promoted units")}</strong>: ${actual.promoted_units || __("Not available")} &nbsp; <strong>${__("Net revenue")}</strong>: ${actualMoney(actual.net_revenue_sen)} &nbsp; <strong>${__("Discount")}</strong>: ${actualMoney(actual.discount_sen)}</p>
			<table class="table table-bordered"><tbody>
			<tr><td>${__("Actual tax")}</td><td>${actualMoney(actual.tax_sen)}</td></tr>
			<tr><td>${__("Actual COGS")}</td><td>${actualMoney(actual.cogs_sen)}</td></tr>
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
		<tr><td>${__("COGS")}</td><td>${money(economics.cogs_sen)}</td></tr>
		<tr><td>${__("Gross profit")}</td><td>${money(economics.gross_profit_sen)}</td></tr>
		<tr><td>${__("Margin")}</td><td>${escapeHtml(economics.margin_percent)}%</td></tr>
		<tr><td>${__("Margin change")}</td><td>${escapeHtml(economics.margin_change_percent)}%</td></tr>
		<tr><td>${__("Break-even")}</td><td>${escapeHtml(breakEven)}</td></tr>
		<tr><td>${__("Worst affected Item group")}</td><td>${worstGroup}</td></tr>
		</tbody></table>
		<p><strong>${__("Volume scenarios")}</strong></p>
		<table class="table table-bordered"><thead><tr><th>${__("Scenario")}</th><th>${__("Units")}</th><th>${__("Net revenue")}</th><th>${__("Tax")}</th><th>${__("COGS")}</th><th>${__("Gross profit")}</th></tr></thead><tbody>${scenarioRows}</tbody></table>
		<p><strong>${__("Ingredient demand")}</strong></p><ul>${ingredientRows}</ul>
		<p><strong>${__("Prepared-batch impact")}</strong></p>
		${batchImpact.status === "not_applicable" ? `<p class="text-muted">${__("No prepared BOM is used by this promotion.")}</p>` : `<table class="table table-bordered"><thead><tr><th>${__("Component")}</th><th>${__("BOM")}</th><th>${__("Base demand")}</th><th>${__("Batch size")}</th><th>${__("Batches")}</th><th>${__("Lead minutes")}</th></tr></thead><tbody>${batchRows}</tbody></table>`}
		<p><strong>${__("Runout and waste risk")}</strong></p>
		${risk.status === "not_available" ? `<p class="text-muted">${__("Not available")}: ${escapeHtml(risk.reason || __("Current stock and expiry evidence is incomplete."))}</p>` : `<table class="table table-bordered"><thead><tr><th>${__("Component")}</th><th>${__("Runout")}</th><th>${__("Waste")}</th><th>${__("Evidence")}</th></tr></thead><tbody>${riskRows}</tbody></table>`}
		<p><strong>${__("Actual post-promotion results")}</strong></p>${actualResult}
		<p class="text-muted">${__("Planning remains Review First. This calculation does not forecast promotion lift or create purchasing automatically.")}</p>
	</div>`;
}
