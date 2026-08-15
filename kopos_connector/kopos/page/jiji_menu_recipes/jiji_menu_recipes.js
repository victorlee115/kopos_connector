frappe.pages["jiji_menu_recipes"].on_page_load = function (wrapper) {
	new JiJiMenuRecipesPage(wrapper);
};

class JiJiMenuRecipesPage {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("JiJi Menu & Recipes"),
			single_column: true,
		});
		this.render();
		this.page.set_primary_action(__("Refresh checklist"), () => this.refresh());
		this.page.body.find("[data-menu-action]").on("click", (event) => {
			this.openAction($(event.currentTarget).attr("data-menu-action"));
		});
		this.page.body.find("[data-menu-create]").on("click", (event) => {
			this.createAction($(event.currentTarget).attr("data-menu-create"));
		});
		this.page.body.find("[data-csv-template]").on("click", () => this.downloadTemplate());
		this.page.body.find("[data-csv-validate]").on("click", () => this.validateCsv());
		this.refresh();
	}

	render() {
		const cards = [
			["Menu Items", "Create or review the commercial Item first.", "item"],
			["Recipes", "Enter measured ingredients, UOMs, yields and modifier effects.", "recipe"],
			["Prepared Components", "Use standard BOMs for cold foam, juice and other batches.", "bom"],
			["Modifiers", "Review modifier groups and their recipe effects.", "modifier"],
			["Promotions", "Check revenue, COGS, margin and simple volume scenarios before publishing.", "promotion"],
			["Publish & validation", "Fix missing UOM, supplier, recipe and shelf-life evidence before publish.", "validate"],
		];
		$(this.page.body).html(`
			<div class="jiji-menu-page">
				<div class="alert alert-info"><strong>${__("Director workflow")}</strong><br>${__("Complete the required fields, save drafts, review the checklist, then publish selected records. Published recipes are immutable; corrections create a new version.")}</div>
				<div class="card mb-4"><div class="card-body">
					<div class="d-flex flex-wrap justify-content-between align-items-start mb-2"><div><h4 class="mb-1">${__("Quick recipe preflight")}</h4><p class="text-muted mb-0">${__("Paste the fixed spreadsheet template here to catch row errors before entering standard FB Recipe forms. This is a dry run; it never creates or changes a document.")}</p></div><div class="small text-muted" data-csv-status>${__("No file checked")}</div></div>
					<textarea class="form-control mb-2" rows="4" data-csv-input placeholder="${__("recipe_code,recipe_name,sellable_item,company,recipe_type,...")}"></textarea>
					<div class="d-flex flex-wrap gap-2"><button class="btn btn-sm btn-default" type="button" data-csv-template>${__("Download template")}</button><button class="btn btn-sm btn-primary" type="button" data-csv-validate>${__("Check rows")}</button></div>
					<pre class="small bg-light p-2 mt-3 mb-0 d-none" data-csv-results></pre>
				</div></div>
				<div class="row">${cards.map(([title, copy, action]) => `
					<div class="col-sm-6 col-lg-4 mb-3"><div class="card h-100"><div class="card-body d-flex flex-column">
						<h4>${__(title)}</h4><p class="text-muted">${__(copy)}</p>
						<div class="small text-muted mb-3" data-check="${action}">${__("Checking…")}</div>
						<div class="mt-auto d-flex flex-wrap gap-2">
							${action === "validate" ? "" : `<button class="btn btn-sm btn-primary" type="button" data-menu-create="${action}">${__("Add new")}</button>`}
							<button class="btn btn-sm btn-default" type="button" data-menu-action="${action}">${action === "validate" ? __("Open validation") : __("Review list")}</button>
						</div>
					</div></div></div>`).join("")}</div>
				<div class="alert alert-warning mt-2">${__("Costs and margins are visible only to Company Directors. Outlet staff and managers use POS guided tasks and do not receive financial inventory values.")}</div>
			</div>`);
	}

	refresh() {
		frappe.call({
			method: "kopos_connector.api.inventory.get_menu_authoring_summary",
			type: "GET",
			callback: (response) => {
				const summary = response.message || {};
				this.page.body.find('[data-check="item"]').text(`${summary.items_ready || 0} ${__("Items ready")} · ${summary.items_missing_recipe || 0} ${__("missing recipe")} · ${summary.unclassified_items || 0} ${__("unclassified")}`);
				this.page.body.find('[data-check="recipe"]').text(`${summary.published_recipes || 0} ${__("published")} · ${summary.draft_recipes || 0} ${__("draft")}`);
				this.page.body.find('[data-check="bom"]').text(`${summary.boms || 0} ${__("standard BOMs")}`);
				this.page.body.find('[data-check="modifier"]').text(`${summary.modifier_groups || 0} ${__("modifier groups")}`);
				this.page.body.find('[data-check="promotion"]').text(`${summary.active_promotions || 0} ${__("active promotions")}`);
				this.page.body.find('[data-check="validate"]').text(summary.ready ? __("Ready to publish selected records") : __("Resolve missing required evidence first"));
				this.page.set_indicator(summary.ready ? __("Ready") : __("Needs review"), summary.ready ? "green" : "orange");
			},
			error: () => this.page.set_indicator(__("Needs attention"), "red"),
		});
	}

	openAction(action) {
		const routes = {
			item: ["List", "Item"],
			recipe: ["List", "FB Recipe"],
			bom: ["List", "BOM"],
			modifier: ["List", "FB Modifier Group"],
			promotion: ["List", "KoPOS Promotion"],
			validate: ["List", "FB Inventory Exception"],
		};
		if (routes[action]) frappe.set_route(...routes[action]);
	}

	createAction(action) {
		const doctypes = {
			item: "Item",
			recipe: "FB Recipe",
			bom: "BOM",
			modifier: "FB Modifier Group",
			promotion: "KoPOS Promotion",
		};
		if (doctypes[action]) frappe.new_doc(doctypes[action]);
	}

	downloadTemplate() {
		frappe.call({
			method: "kopos_connector.api.inventory.get_menu_recipe_csv_template",
			type: "GET",
			callback: (response) => {
				const template = response.message || {};
				const blob = new Blob([template.content || ""], { type: "text/csv;charset=utf-8" });
				const link = document.createElement("a");
				link.href = URL.createObjectURL(blob);
				link.download = template.filename || "jiji-recipe-components-template.csv";
				link.click();
				URL.revokeObjectURL(link.href);
				this.page.body.find("[data-csv-status]").text(__("Template downloaded"));
			},
		});
	}

	validateCsv() {
		const csvText = this.page.body.find("[data-csv-input]").val() || "";
		if (!String(csvText).trim()) {
			this.page.body.find("[data-csv-status]").text(__("Paste CSV rows first"));
			return;
		}
		frappe.call({
			method: "kopos_connector.api.inventory.validate_menu_recipe_csv",
			type: "POST",
			args: { csv_text: csvText },
			callback: (response) => {
				const result = response.message || {};
				const errors = result.errors || [];
				const output = errors.length
					? errors.map((error) => `${__("Row")} ${error.row}: ${error.message}`).join("\n")
					: __(`${result.recipe_count || 0} recipe(s) passed the dry run. Open standard FB Recipe forms to review and save drafts.`);
				this.page.body.find("[data-csv-results]").removeClass("d-none").text(output);
				this.page.body.find("[data-csv-status]").text(result.valid ? __("Rows look complete") : `${errors.length} ${__("row issue(s)")}`);
			},
		});
	}
}
