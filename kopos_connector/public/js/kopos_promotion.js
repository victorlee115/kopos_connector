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
							if (result.status !== "ok") {
								frappe.msgprint({
									title: __("Promotion blocked"),
									indicator: "red",
									message: escapeHtml(result.reason || __("Complete recipe, price, and valuation data first.")),
								});
								return;
							}
							frappe.msgprint({
								title: __("Promotion economics"),
								indicator: Number(result.economics.margin_percent) < 0 ? "red" : "green",
								message: renderEconomics(result.economics),
							});
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
	return `RM ${(Number(sen || 0) / 100).toFixed(2)}`;
}

function renderEconomics(economics) {
	const scenarioRows = (economics.scenarios || []).map((row) => `
		<tr><td>${escapeHtml(row.label)}</td><td>${row.units}</td><td>${money(row.revenue_sen)}</td><td>${money(row.cogs_sen)}</td><td>${money(row.gross_profit_sen)}</td></tr>`).join("");
	const ingredientRows = Object.entries(economics.ingredient_demand || {}).map(([item, qty]) => `<li>${escapeHtml(item)}: ${escapeHtml(qty)}</li>`).join("") || `<li>${__("No recipe components")}</li>`;
	const breakEven = economics.break_even_additional_units === null
		? __("Not available")
		: `${economics.break_even_additional_units} ${__("additional units")}`;
	return `<div>
		<p><strong>${__("Base case")}</strong></p>
		<table class="table table-bordered"><tbody>
		<tr><td>${__("Revenue")}</td><td>${money(economics.revenue_sen)}</td></tr>
		<tr><td>${__("COGS")}</td><td>${money(economics.cogs_sen)}</td></tr>
		<tr><td>${__("Gross profit")}</td><td>${money(economics.gross_profit_sen)}</td></tr>
		<tr><td>${__("Margin")}</td><td>${escapeHtml(economics.margin_percent)}%</td></tr>
		<tr><td>${__("Margin change")}</td><td>${escapeHtml(economics.margin_change_percent)}%</td></tr>
		<tr><td>${__("Break-even")}</td><td>${escapeHtml(breakEven)}</td></tr>
		</tbody></table>
		<p><strong>${__("Volume scenarios")}</strong></p>
		<table class="table table-bordered"><thead><tr><th>${__("Scenario")}</th><th>${__("Units")}</th><th>${__("Revenue")}</th><th>${__("COGS")}</th><th>${__("Gross profit")}</th></tr></thead><tbody>${scenarioRows}</tbody></table>
		<p><strong>${__("Ingredient demand")}</strong></p><ul>${ingredientRows}</ul>
		<p class="text-muted">${__("Planning remains Review First. This calculation does not forecast promotion lift or create purchasing automatically.")}</p>
	</div>`;
}
