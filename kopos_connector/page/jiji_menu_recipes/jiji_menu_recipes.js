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
		this.bindEvents();
		this.setupControls();
		this.refresh();
	}

	render() {
		const cards = [
			["Menu Items", "Start here: classify a sellable Item and complete its standard Item form.", "item", "Classify an Item"],
			["Recipes", "Enter the measured ingredients, UOMs, yield and modifier choices for one menu Item.", "recipe", "Enter a recipe"],
			["Prepared Components", "Set up a standard BOM for cold foam, juice or another batch-made component.", "bom", "Set up a batch"],
			["Modifiers", "Create modifier groups and options, then select them in the recipe editor.", "modifier", "Set up modifiers"],
			["Promotions", "Review revenue, COGS, margin and simple volume scenarios before activating a promotion.", "promotion", "Create a promotion"],
			["Publish & validation", "Fix the remaining checklist items, review Draft recipes and publish them together.", "validate", "Review checklist"],
		];
		$(this.page.body).html(`
			<div class="jiji-menu-page">
				<div class="alert alert-info"><strong>${__("Director workflow")}</strong><br>${__("Complete one menu Item from left to right. This page guides you to the right standard ERPNext form; it does not create a second menu system or autosave unfinished work.")}</div>
				<div class="card mb-4" data-completion-card><div class="card-body">
					<div class="d-flex flex-wrap justify-content-between align-items-start"><div><h4 class="mb-1">${__("Commissioning checklist")}</h4><p class="text-muted mb-0">${__("Pick a company to see what is ready, what is missing and your next action.")}</p></div><div class="small text-muted" data-progress-summary>${__("Choose a company")}</div></div>
					<div class="progress mt-3" style="height: 8px" aria-label="${__("Commissioning progress")}"><div class="progress-bar" role="progressbar" data-progress-bar style="width: 0%"></div></div>
					<div class="d-flex flex-wrap justify-content-between align-items-center mt-3"><span class="small" data-next-action>${__("Next: choose a company")}</span><button class="btn btn-sm btn-primary" type="button" data-next-action-button disabled>${__("Choose a company")}</button></div>
				</div></div>
				<div class="card mb-4"><div class="card-body">
					<div class="d-flex flex-wrap justify-content-between align-items-start mb-3"><div><h4 class="mb-1">${__("Commission one menu item")}</h4><p class="text-muted mb-0">${__("The guided editor uses standard Item, FB Recipe and BOM documents as the authorities. It does not autosave or create a separate authoring record.")}</p></div><div class="small text-muted" data-company-status>${__("Choose a company, then a menu item")}</div></div>
					<div class="row mb-3"><div class="col-md-4"><div data-company-link></div></div><div class="col-md-4"><div data-menu-item-link></div></div><div class="col-md-4"><div data-catalog-profile-link></div></div></div>
					<div class="row"><div class="col-lg-8"><ol class="mb-0 pl-3"><li>${__("Classify the saleable Item or record an explicit exclusion.")}</li><li>${__("Enter measured recipe rows, modifier groups and prepared-component guidance, then preview the exact conversion and cost.")}</li><li>${__("Save a Draft, review the checklist, and publish selected Draft recipes atomically.")}</li></ol></div><div class="col-lg-4 mt-3 mt-lg-0 d-flex flex-wrap align-content-start gap-2"><button class="btn btn-sm btn-default" type="button" data-menu-commission="item" disabled>${__("1. Classify item")}</button><button class="btn btn-sm btn-primary" type="button" data-menu-commission="recipe" disabled>${__("2. Guided recipe")}</button><button class="btn btn-sm btn-default" type="button" data-menu-commission="recipes">${__("Copy / revise")}</button><button class="btn btn-sm btn-default" type="button" data-menu-catalog-preview disabled>${__("Check outlet catalog")}</button></div></div>
					<pre class="small bg-light p-2 mt-3 mb-0 d-none" data-menu-catalog-results></pre><div class="small mt-2" data-guided-editor-status></div>
				</div></div>
				<div class="card mb-4"><div class="card-body"><div class="d-flex flex-wrap justify-content-between align-items-center mb-2"><div><h4 class="mb-1">${__("Publish selected recipes")}</h4><p class="text-muted mb-0">${__("Every selected Draft is validated first. If one fails, none are published.")}</p></div><button class="btn btn-sm btn-primary" type="button" data-publish-selected disabled>${__("Publish selected")}</button></div><div data-publish-queue class="small text-muted">${__("Choose a company to load Draft recipes.")}</div></div></div>
				<div class="card mb-4"><div class="card-body">
					<div class="d-flex flex-wrap justify-content-between align-items-start mb-2"><div><h4 class="mb-1">${__("Quick recipe preflight")}</h4><p class="text-muted mb-0">${__("Paste the fixed spreadsheet template to catch row errors. This remains a dry run; use the guided editor or standard documents to save.")}</p></div><div class="small text-muted" data-csv-status>${__("No file checked")}</div></div>
					<textarea class="form-control mb-2" rows="4" data-csv-input placeholder="${__("recipe_code,recipe_name,sellable_item,company,recipe_type,...")}"></textarea><div class="d-flex flex-wrap gap-2"><button class="btn btn-sm btn-default" type="button" data-csv-template>${__("Download template")}</button><button class="btn btn-sm btn-primary" type="button" data-csv-validate>${__("Check rows")}</button></div><pre class="small bg-light p-2 mt-3 mb-0 d-none" data-csv-results></pre>
				</div></div>
				<div class="card mb-4"><div class="card-body">
					<div class="d-flex flex-wrap justify-content-between align-items-start mb-2"><div><h4 class="mb-1">${__("Promotion economics")}</h4><p class="text-muted mb-0">${__("Check server-calculated revenue, COGS, margin, stock risk and simple volume scenarios before publishing. Scenarios are planning-only and never place an order.")}</p></div><div class="small text-muted" data-promotion-status>${__("No promotion checked")}</div></div>
					<div class="row mb-3"><div class="col-md-6"><div data-promotion-link></div></div><div class="col-md-6"><div data-promotion-profile></div></div></div><div class="row mb-3"><div class="col-sm-4"><label class="text-muted small" for="jiji-promotion-low">${__("Low units")}</label><input id="jiji-promotion-low" class="form-control" type="number" min="1" step="1" value="10" data-promotion-low></div><div class="col-sm-4"><label class="text-muted small" for="jiji-promotion-base">${__("Base units")}</label><input id="jiji-promotion-base" class="form-control" type="number" min="1" step="1" value="25" data-promotion-base></div><div class="col-sm-4"><label class="text-muted small" for="jiji-promotion-high">${__("High units")}</label><input id="jiji-promotion-high" class="form-control" type="number" min="1" step="1" value="50" data-promotion-high></div></div><button class="btn btn-sm btn-primary" type="button" data-promotion-check disabled>${__("Check COGS & margin")}</button><div class="small mt-3 mb-0 d-none" role="status" aria-live="polite" data-promotion-results></div>
				</div></div>
				<div class="row">${cards.map(([title, copy, action, primary]) => `
					<div class="col-sm-6 col-lg-4 mb-3"><div class="card h-100" data-menu-card="${action}"><div class="card-body d-flex flex-column"><div class="d-flex justify-content-between align-items-start"><h4>${__(title)}</h4><span class="badge badge-light" data-card-state="${action}">${__("Not checked")}</span></div><p class="text-muted">${__(copy)}</p><div class="small text-muted mb-3" data-check="${action}">${__("Checking…")}</div><div class="mt-auto d-flex flex-wrap gap-2"><button class="btn btn-sm btn-primary" type="button" data-menu-create="${action}">${__(primary)}</button><button class="btn btn-sm btn-default" type="button" data-menu-action="${action}">${action === "validate" ? __("Open checklist") : __("Open records")}</button></div></div></div></div>`).join("")}</div>
				<div class="alert alert-warning mt-2">${__("Costs and margins are visible only to Company Directors. Outlet staff and managers use POS guided tasks and do not receive financial inventory values.")}</div>
			</div>`);
	}

	bindEvents() {
		this.page.body.find("[data-menu-action]").on("click", (event) => this.openAction($(event.currentTarget).attr("data-menu-action")));
		this.page.body.find("[data-menu-create]").on("click", (event) => this.createAction($(event.currentTarget).attr("data-menu-create")));
		this.page.body.find("[data-menu-commission]").on("click", (event) => this.runCommissioningAction($(event.currentTarget).attr("data-menu-commission")));
		this.page.body.find("[data-menu-catalog-preview]").on("click", () => this.previewCatalog());
		this.page.body.find("[data-publish-selected]").on("click", () => this.publishSelected());
		this.page.body.find("[data-next-action-button]").on("click", () => this.runNextAction());
		this.page.body.find("[data-csv-template]").on("click", () => this.downloadTemplate());
		this.page.body.find("[data-csv-validate]").on("click", () => this.validateCsv());
		this.page.body.find("[data-promotion-check]").on("click", () => this.checkPromotionEconomics());
	}

	setupControls() {
		this.companyControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-company-link]")[0], df: { fieldtype: "Link", fieldname: "company", options: "Company", label: __("Company"), onchange: () => this.onSelectionChanged(true) } });
		this.companyControl.refresh();
		this.menuItemControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-menu-item-link]")[0], df: { fieldtype: "Link", fieldname: "menu_item", options: "Item", label: __("Menu item"), get_query: () => ({ filters: { is_sales_item: 1, disabled: 0 } }), onchange: () => this.onSelectionChanged(false) } });
		this.menuItemControl.refresh();
		this.catalogProfileControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-catalog-profile-link]")[0], df: { fieldtype: "Link", fieldname: "catalog_profile", options: "POS Profile", label: __("Outlet / POS Profile"), get_query: () => ({ filters: { disabled: 0 } }), onchange: () => this.onSelectionChanged(false) } });
		this.catalogProfileControl.refresh();
		this.promotionControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-promotion-link]")[0], df: { fieldtype: "Link", fieldname: "promotion", options: "KoPOS Promotion", label: __("Promotion"), onchange: () => this.updatePromotionActionState() } });
		this.promotionControl.refresh();
		this.profileControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-promotion-profile]")[0], df: { fieldtype: "Link", fieldname: "pos_profile", options: "POS Profile", label: __("POS Profile (optional)") } });
		this.profileControl.refresh();
		this.updateActionState();
		this.updatePromotionActionState();
	}

	onSelectionChanged(refreshSummary) {
		this.updateActionState();
		if (refreshSummary) this.refresh(); else this.refreshPublishQueue();
	}

	updateActionState() {
		const item = Boolean(this.menuItemControl?.get_value?.());
		const companyAndItem = item && Boolean(this.companyControl?.get_value?.());
		const profile = Boolean(this.catalogProfileControl?.get_value?.());
		this.page.body.find('[data-menu-commission="item"]').prop("disabled", !Boolean(this.companyControl?.get_value?.()));
		this.page.body.find('[data-menu-commission="recipe"]').prop("disabled", !companyAndItem);
		this.page.body.find("[data-menu-catalog-preview]").prop("disabled", !profile);
	}

	updatePromotionActionState() {
		this.page.body.find("[data-promotion-check]").prop("disabled", !this.promotionControl?.get_value?.());
	}

	refresh() {
		frappe.call({ method: "kopos_connector.api.inventory.get_menu_authoring_summary", type: "GET", args: { company: this.companyControl?.get_value?.() || "" }, callback: (response) => {
			const summary = response.message || {};
			if (!this.companyControl?.get_value?.() && summary.selected_company) {
				this.companyControl.set_value(summary.selected_company);
			}
			this.page.body.find('[data-check="item"]').text(`${summary.items_ready || 0}/${summary.saleable_items || 0} ${__("ready")} · ${summary.items_missing_recipe || 0} ${__("missing recipe")} · ${summary.approved_exclusions || 0} ${__("excluded")}`);
			this.page.body.find('[data-check="recipe"]').text(`${summary.published_recipes || 0} ${__("published")} · ${summary.draft_recipes || 0} ${__("draft")}`);
			this.page.body.find('[data-check="bom"]').text(`${summary.boms || 0} ${__("standard BOMs")}`);
			this.page.body.find('[data-check="modifier"]').text(`${summary.modifier_groups || 0} ${__("modifier groups")}`);
			this.page.body.find('[data-check="promotion"]').text(`${summary.active_promotions || 0} ${__("active promotions")}`);
			this.page.body.find('[data-check="validate"]').text(summary.ready ? __("Ready to publish reviewed records") : __("Resolve the checklist before publishing"));
			this.renderCommissioningStatus(summary);
			this.page.set_indicator(summary.ready ? __("Ready") : __("Needs review"), summary.ready ? "green" : "orange");
			this.refreshPublishQueue();
		}, error: () => { this.page.body.find("[data-company-status]").text(__("The checklist could not be loaded")); this.page.set_indicator(__("Needs attention"), "red"); } });
	}

	refreshPublishQueue() {
		const company = this.companyControl?.get_value?.() || "";
		const output = this.page.body.find("[data-publish-queue]");
		if (!company) { output.text(__("Choose a company to load Draft recipes.")); this.page.body.find("[data-publish-selected]").prop("disabled", true); return; }
		frappe.call({ method: "kopos_connector.api.menu_authoring.get_menu_recipe_publish_queue", type: "GET", args: { company }, callback: (response) => {
			const recipes = response.message?.recipes || [];
			if (!recipes.length) { output.text(__("No Draft recipes yet.")); this.page.body.find("[data-publish-selected]").prop("disabled", true); return; }
			output.html(recipes.map((row) => `<label class="d-flex align-items-start mb-2"><input class="mr-2 mt-1" type="checkbox" data-publish-recipe="${this.escape(row.name)}" ${row.valid ? "" : "disabled"}><span><strong>${this.escape(row.recipe_name || row.recipe_code || row.name)}</strong> <span class="text-muted">v${row.version_no || "?"} · ${row.component_count || 0} ${__("components")}</span><br><span class="${row.valid ? "text-success" : "text-danger"}">${row.valid ? __("Ready for review") : this.escape((row.errors || []).join("; "))}</span></span></label>`).join(""));
			this.page.body.find("[data-publish-selected]").prop("disabled", !output.find("[data-publish-recipe]").length);
		}, error: () => { output.text(__("Draft recipes could not be loaded.")); this.page.body.find("[data-publish-selected]").prop("disabled", true); } });
	}

	renderCommissioningStatus(summary) {
		const messages = [];
		if (summary.company_selection_required) messages.push(__("Choose a company to review its recipe coverage."));
		if (!summary.item_fields_ready || !summary.recipe_schema_ready) messages.push(__("Run the inventory migration before commissioning menu data."));
		if (summary.unclassified_items) messages.push(`${summary.unclassified_items} ${__("saleable item(s) need the Sellable Drink role.")}`);
		if (summary.items_missing_recipe) messages.push(`${summary.items_missing_recipe} ${__("saleable item(s) need a published recipe or explicit exclusion.")}`);
		if (summary.invalid_exclusions) messages.push(`${summary.invalid_exclusions} ${__("exclusion(s) need a reason.")}`);
		const companyReady = Boolean(summary.selected_company) && !summary.company_selection_required;
		const migrationReady = Boolean(summary.item_fields_ready) && Boolean(summary.recipe_schema_ready);
		const classificationReady = migrationReady && !Number(summary.unclassified_items || 0);
		const recipesReady = classificationReady && !Number(summary.items_missing_recipe || 0) && !Number(summary.invalid_exclusions || 0);
		const complete = Boolean(summary.ready);
		const completedSteps = [companyReady, migrationReady, classificationReady, recipesReady].filter(Boolean).length;
		const percent = complete ? 100 : Math.round((completedSteps / 4) * 100);
		this.page.body.find("[data-company-status]").text(messages.length ? messages.join(" ") : __("Commissioning checklist is complete."));
		this.page.body.find("[data-progress-bar]").removeClass("bg-success bg-warning").addClass(complete ? "bg-success" : "bg-warning").css("width", `${percent}%`).attr("aria-valuenow", percent);
		this.page.body.find("[data-progress-summary]").text(`${completedSteps}/4 ${__("required steps complete")}`);
		this.setCardState("item", classificationReady ? __("Ready") : companyReady ? __("Needs review") : __("Start here"), classificationReady ? "success" : "warning");
		this.setCardState("recipe", recipesReady ? __("Ready") : __("Needs review"), recipesReady ? "success" : "warning");
		this.setCardState("bom", Number(summary.boms || 0) ? __("Configured") : __("Optional"), Number(summary.boms || 0) ? "success" : "secondary");
		this.setCardState("modifier", Number(summary.modifier_groups || 0) ? __("Configured") : __("Optional"), Number(summary.modifier_groups || 0) ? "success" : "secondary");
		this.setCardState("promotion", Number(summary.active_promotions || 0) ? __("Configured") : __("Optional"), Number(summary.active_promotions || 0) ? "success" : "secondary");
		this.setCardState("validate", complete ? __("Ready") : __("Review"), complete ? "success" : "warning");
		this.setNextAction({ companyReady, migrationReady, classificationReady, recipesReady, complete });
	}

	setCardState(action, label, tone) {
		this.page.body.find(`[data-card-state="${action}"]`).removeClass("badge-light badge-success badge-warning badge-secondary").addClass(`badge-${tone}`).text(label);
	}

	setNextAction(state) {
		let message = __("Next: choose a company");
		let label = __("Choose a company");
		let action = "company";
		let disabled = false;
		if (state.companyReady && !state.migrationReady) {
			message = __("Next: ask an administrator to run the inventory migration.");
			label = __("Migration required");
			action = "none";
			disabled = true;
		} else if (state.migrationReady && !state.classificationReady) {
			message = __("Next: classify each saleable Item as Sellable Drink or record an approved exclusion.");
			label = __("Classify an Item");
			action = "item";
		} else if (state.classificationReady && !state.recipesReady) {
			message = __("Next: enter and publish the measured recipe for each menu Item.");
			label = __("Enter a recipe");
			action = "recipe";
		} else if (state.recipesReady && !state.complete) {
			message = __("Next: review the Draft recipe queue and publish the selected recipes.");
			label = __("Review Drafts");
			action = "validate";
		} else if (state.complete) {
			message = __("All required menu records are ready. Optional batch, modifier and promotion setup is below.");
			label = __("Review Drafts");
			action = "validate";
		}
		this.nextAction = action;
		this.page.body.find("[data-next-action]").text(message);
		this.page.body.find("[data-next-action-button]").text(label).prop("disabled", disabled);
	}

	runNextAction() {
		if (this.nextAction === "company") {
			this.companyControl?.$input?.focus();
			return;
		}
		if (this.nextAction === "validate") {
			this.page.body.find("[data-publish-queue]")[0]?.scrollIntoView({ behavior: "smooth", block: "center" });
			return;
		}
		if (this.nextAction === "item" || this.nextAction === "recipe") {
			this.runCommissioningAction(this.nextAction);
		}
	}

	openAction(action) {
		if (action === "validate") {
			this.page.body.find("[data-publish-queue]")[0]?.scrollIntoView({ behavior: "smooth", block: "center" });
			return;
		}
		const routes = { item: ["List", "Item"], recipe: ["List", "FB Recipe"], bom: ["List", "BOM"], modifier: ["List", "FB Modifier Group"], promotion: ["List", "KoPOS Promotion"] };
		if (routes[action]) frappe.set_route(...routes[action]);
	}

	createAction(action) {
		if (action === "item") return this.openGuidedItemDialog();
		if (action === "recipe") return this.openGuidedRecipeDialog();
		if (action === "bom") return this.openPreparedBatchDialog();
		if (action === "modifier") return this.openModifierDialog();
		if (action === "promotion") return this.openPromotionDialog();
		if (action === "validate") return this.openAction("validate");
	}

	runCommissioningAction(action) {
		const item = this.menuItemControl?.get_value?.() || "";
		const company = this.companyControl?.get_value?.() || "";
		if (action === "item") { if (!item) return this.openGuidedItemDialog(); frappe.set_route("Form", "Item", item); return; }
		if (action === "recipe") { if (!item || !company) return this.showMessage(__("Choose both a company and menu item first.")); return this.openGuidedRecipeDialog({ item, company }); }
		if (action === "recipes") return this.openCopyDialog({ item, company });
	}

	openGuidedRecipeDialog({ item, company, recipe } = {}) {
		item = item || this.menuItemControl?.get_value?.() || "";
		company = company || this.companyControl?.get_value?.() || "";
		if (!item || !company) return this.showMessage(__("Choose both a company and menu item first."));
		frappe.call({ method: "kopos_connector.api.menu_authoring.get_menu_recipe_editor", type: "GET", args: { company, sellable_item: item, recipe: recipe || "", warehouse: "" }, callback: (response) => {
			const editor = response.message || {};
			if (editor.status !== "ok") return this.showMessage(__("The guided editor could not be loaded."));
			this.showGuidedEditor(editor);
		}, error: () => this.showMessage(__("The guided editor could not be loaded.")) });
	}

	openGuidedItemDialog() {
		const company = this.companyControl?.get_value?.() || "";
		if (!company) return this.showMessage(__("Choose a company before adding a menu Item."));
		const dialog = new frappe.ui.Dialog({
			title: __("Add menu Item"),
			fields: [
				{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("This creates a disabled standard Item. Complete the standard Item form and recipe before enabling it for sale.")}</p>` },
				{ fieldtype: "Data", fieldname: "item_name", label: __("Item name"), reqd: 1 },
				{ fieldtype: "Data", fieldname: "item_code", label: __("Item code (optional)") },
				{ fieldtype: "Link", fieldname: "item_group", options: "Item Group", label: __("Item group"), reqd: 1 },
				{ fieldtype: "Link", fieldname: "stock_uom", options: "UOM", label: __("Stock UOM"), reqd: 1 },
				{ fieldtype: "Select", fieldname: "item_role", label: __("Classification"), options: ["Sellable Drink", "Ingredient", "Prep Item", "Packaging"].join("\n"), reqd: 1, default: "Sellable Drink" },
			],
			primary_action_label: __("Create disabled Item"),
			primary_action: (values) => frappe.call({
				method: "kopos_connector.api.menu_authoring.create_menu_item_draft",
				type: "POST",
				args: { payload: JSON.stringify({ ...values, company }) },
				callback: (response) => {
					const result = response.message || {};
					if (result.status !== "ok" || !result.item) return this.showMessage(__("The Item could not be created."));
					dialog.hide();
					frappe.show_alert({ message: __("Disabled Item created; complete its standard form next."), indicator: "green" });
					frappe.set_route("Form", "Item", result.item);
					this.refresh();
				},
				error: () => this.showMessage(__("The Item could not be created. Check the required fields and migration status.")),
			}),
		});
		dialog.show();
	}

	openPreparedBatchDialog() {
		const dialog = new frappe.ui.Dialog({
			title: __("Set up a prepared batch"),
			fields: [
				{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("Choose the stocked component that staff will make in batches. Save this short setup, then complete the standard BOM form with its raw ingredients. The BOM is the authority; this page does not create a second recipe.")}</p>` },
				{ fieldtype: "Link", fieldname: "item", options: "Item", label: __("Prepared Item"), reqd: 1, get_query: () => ({ filters: { is_stock_item: 1, disabled: 0 } }) },
				{ fieldtype: "Float", fieldname: "quantity", label: __("BOM batch quantity"), reqd: 1, default: 1 },
				{ fieldtype: "Float", fieldname: "batch_qty", label: __("Usable batch output"), description: __("How much usable stock one batch creates."), reqd: 1, default: 1 },
				{ fieldtype: "Float", fieldname: "min_ready_qty", label: __("Alert when ready stock falls below"), reqd: 1, default: 1 },
				{ fieldtype: "Int", fieldname: "lead_minutes", label: __("Preparation lead time (minutes)"), reqd: 1, default: 30 },
				{ fieldtype: "Small Text", fieldname: "instructions", label: __("Short preparation instructions") },
			],
			primary_action_label: __("Open standard BOM"),
			primary_action: (values) => {
				if (!values.item) return;
				dialog.hide();
				frappe.new_doc("BOM", {
					item: values.item,
					quantity: values.quantity || 1,
					custom_kopos_autoprep_enabled: 1,
					custom_kopos_batch_qty: values.batch_qty || values.quantity || 1,
					custom_kopos_min_ready_qty: values.min_ready_qty || values.batch_qty || 1,
					custom_kopos_preparation_lead_minutes: values.lead_minutes || 30,
					custom_kopos_preparation_instructions: values.instructions || "",
				});
			},
		});
		dialog.show();
	}

	openModifierDialog() {
		const dialog = new frappe.ui.Dialog({
			title: __("Set up a modifier group"),
			fields: [
				{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("Create the group first, then add its options in the standard FB Modifier form. Return to the recipe editor to select the group and freeze its measured effects when you publish.")}</p>` },
				{ fieldtype: "Data", fieldname: "group_code", label: __("Group code"), reqd: 1 },
				{ fieldtype: "Data", fieldname: "group_name", label: __("What staff will see"), reqd: 1 },
				{ fieldtype: "Select", fieldname: "selection_type", label: __("Selection"), options: "Single\nMultiple", default: "Single", reqd: 1 },
				{ fieldtype: "Check", fieldname: "is_required", label: __("Customer must choose") },
			],
			primary_action_label: __("Open modifier group"),
			primary_action: (values) => {
				if (!values.group_code || !values.group_name) return;
				dialog.hide();
				frappe.new_doc("FB Modifier Group", {
					group_code: values.group_code,
					group_name: values.group_name,
					selection_type: values.selection_type || "Single",
					is_required: values.is_required ? 1 : 0,
					active: 1,
				});
			},
		});
		dialog.show();
	}

	openPromotionDialog() {
		const item = this.menuItemControl?.get_value?.() || "";
		const dialog = new frappe.ui.Dialog({
			title: __("Create a promotion draft"),
			fields: [
				{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("This opens the standard promotion form with the basic rule filled in. Save it there, then return here to calculate COGS, margin and low/base/high scenarios. An economics check is required before activation.")}</p>` },
				{ fieldtype: "Data", fieldname: "promotion_name", label: __("Promotion name"), reqd: 1 },
				{ fieldtype: "Select", fieldname: "promotion_type", label: __("Promotion type"), options: "item_discount\norder_discount\nhappy_hour\nbuy_x_get_y\nnth_item_discount", default: "item_discount", reqd: 1 },
				{ fieldtype: "Data", fieldname: "display_label", label: __("Label shown to customers"), reqd: 1, default: item ? __("Special offer") : "" },
				{ fieldtype: "Select", fieldname: "discount_type", label: __("Discount type"), options: "percentage\nfixed_amount\nfixed_price\nfree_item", default: "percentage", reqd: 1 },
				{ fieldtype: "Float", fieldname: "discount_value", label: __("Discount value"), reqd: 1, default: 10 },
				{ fieldtype: "HTML", fieldname: "cost_note", options: `<div class="alert alert-warning small mb-0">${__("Do not activate until the COGS and margin check below shows complete costs.")}</div>` },
			],
			primary_action_label: __("Open promotion form"),
			primary_action: (values) => {
				if (!values.promotion_name) return;
				dialog.hide();
				frappe.new_doc("KoPOS Promotion", {
					promotion_name: values.promotion_name,
					promotion_type: values.promotion_type || "item_discount",
					display_label: values.display_label || values.promotion_name,
					discount_type: values.discount_type || "percentage",
					discount_value: values.discount_value || 0,
					eligible_scope_mode: item ? "same_item" : "eligible_pool",
					eligible_items: item ? [{ item_code: item }] : [],
					is_active: 0,
				});
			},
		});
		dialog.show();
	}

	showGuidedEditor(editor) {
		const recipe = editor.recipe || {};
		const itemOptions = (editor.items || []).map((row) => `<option value="${this.escape(row.name)}">${this.escape(row.item_name || row.name)}</option>`).join("");
		const uomOptions = (editor.uoms || []).map((uom) => `<option value="${this.escape(uom)}">`).join("");
		const dialog = new frappe.ui.Dialog({ title: recipe.name ? __("Revise Draft recipe") : __("Guided recipe entry"), fields: [
			{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("Enter measured quantities. Preview uses ERP UOM conversions and warehouse valuation. Prepared Items require an active submitted BOM. Modifier effects are read-only snapshots generated when you publish.")}</p>` },
			{ fieldtype: "Data", fieldname: "recipe_name", label: __("Recipe name"), reqd: 1, default: recipe.recipe_name || "" },
			{ fieldtype: "Data", fieldname: "recipe_code", label: __("Recipe code"), default: recipe.recipe_code || "" },
			{ fieldtype: "Select", fieldname: "recipe_type", label: __("Recipe type"), options: ["Finished Drink", "Add-On", "Prep Batch", "Packaging Assembly"].join("\n"), default: recipe.recipe_type || "Finished Drink" },
			{ fieldtype: "Float", fieldname: "yield_qty", label: __("Yield quantity"), reqd: 1, default: recipe.yield_qty || 1 },
			{ fieldtype: "Link", fieldname: "yield_uom", options: "UOM", label: __("Yield UOM"), reqd: 1, default: recipe.yield_uom || "" },
			{ fieldtype: "Float", fieldname: "default_serving_qty", label: __("One serving quantity"), reqd: 1, default: recipe.default_serving_qty || 1 },
			{ fieldtype: "Link", fieldname: "default_serving_uom", options: "UOM", label: __("Serving UOM"), reqd: 1, default: recipe.default_serving_uom || "" },
			{ fieldtype: "Datetime", fieldname: "effective_from", label: __("Effective from"), reqd: 1, default: recipe.effective_from || "" },
			{ fieldtype: "Datetime", fieldname: "effective_to", label: __("Effective to (optional)"), default: recipe.effective_to || "" },
			{ fieldtype: "Link", fieldname: "warehouse", options: "Warehouse", label: __("Valuation warehouse (optional)"), default: editor.warehouse || "" },
			{ fieldtype: "HTML", fieldname: "components", options: `<hr><h5>${__("Measured components")}</h5><datalist id="jiji-menu-items">${itemOptions}</datalist><datalist id="jiji-menu-uoms">${uomOptions}</datalist><div class="table-responsive"><table class="table table-bordered table-sm mb-2"><thead><tr><th>${__("Item")}</th><th>${__("Type")}</th><th>${__("Qty")}</th><th>${__("UOM")}</th><th>${__("Loss %")}</th><th>${__("Stock")}</th><th>${__("COGS")}</th><th></th></tr></thead><tbody data-guided-rows></tbody></table></div><button class="btn btn-sm btn-default" type="button" data-guided-add-row>${__("Add component")}</button>` },
			{ fieldtype: "HTML", fieldname: "modifiers", options: `<hr><h5>${__("Modifier groups")}</h5><div data-guided-modifiers></div><div class="small text-muted mt-2">${__("Effects are read from active FB Modifier records and frozen on publish.")}</div>` },
			{ fieldtype: "HTML", fieldname: "preview", options: `<hr><h5>${__("ERP preview")}</h5><div data-guided-preview class="small text-muted">${__("Add rows, then preview.")}</div>` },
		], primary_action_label: __("Save Draft"), primary_action: (values) => this.saveGuidedDraft(dialog, editor, values), secondary_action_label: __("Preview"), secondary_action: () => this.previewGuidedDraft(dialog, editor) });
		dialog.show();
		this.renderGuidedRows(dialog, recipe.components || []);
		this.renderGuidedModifiers(dialog, editor, recipe.allowed_modifier_groups || []);
		dialog.$wrapper.on("click", "[data-guided-add-row]", () => this.addGuidedRow(dialog));
		dialog.$wrapper.on("click", "[data-guided-remove-row]", (event) => { $(event.currentTarget).closest("tr").remove(); });
		dialog.$wrapper.on("change", '[data-guided-field="component_type"]', (event) => { const row = $(event.currentTarget).closest("tr"); const isTool = $(event.currentTarget).val() === "Tool Usage"; row.find('[data-guided-field="affects_stock"], [data-guided-field="affects_cogs"]').prop("checked", !isTool); });
		if (!(recipe.components || []).length) this.addGuidedRow(dialog);
	}

	renderGuidedRows(dialog, rows) {
		const body = dialog.$wrapper.find("[data-guided-rows]");
		body.empty();
		(rows || []).forEach((row) => this.addGuidedRow(dialog, row));
	}

	addGuidedRow(dialog, row = {}) {
		const body = dialog.$wrapper.find("[data-guided-rows]");
		if (body.find("tr").length >= 100) return;
		const defaultStock = row.affects_stock == null ? row.component_type !== "Tool Usage" : Boolean(Number(row.affects_stock));
		const defaultCogs = row.affects_cogs == null ? row.component_type !== "Tool Usage" : Boolean(Number(row.affects_cogs));
		body.append(`<tr><td><input class="form-control form-control-sm" list="jiji-menu-items" data-guided-field="item" value="${this.escape(row.item || "")}" placeholder="${__("Item code")}"></td><td><select class="form-control form-control-sm" data-guided-field="component_type"><option>Ingredient</option><option>Prep Item</option><option>Packaging</option><option>Tool Usage</option></select></td><td><input class="form-control form-control-sm" type="number" min="0" step="any" data-guided-field="qty" value="${this.escape(row.qty || "")}"></td><td><input class="form-control form-control-sm" list="jiji-menu-uoms" data-guided-field="uom" value="${this.escape(row.uom || "")}" placeholder="${__("UOM")}"></td><td><input class="form-control form-control-sm" type="number" min="0" step="any" data-guided-field="loss_factor_pct" value="${this.escape(row.loss_factor_pct || 0)}"></td><td class="text-center"><input type="checkbox" data-guided-field="affects_stock" ${defaultStock ? "checked" : ""}></td><td class="text-center"><input type="checkbox" data-guided-field="affects_cogs" ${defaultCogs ? "checked" : ""}></td><td><button class="btn btn-sm btn-link text-danger" type="button" data-guided-remove-row aria-label="${__("Remove")}">×</button></td></tr>`);
		const select = body.find("tr:last [data-guided-field=component_type]");
		select.val(row.component_type || "Ingredient");
	}

	renderGuidedModifiers(dialog, editor, selected) {
		const selectedNames = new Set((selected || []).map((row) => row.modifier_group));
		dialog.$wrapper.find("[data-guided-modifiers]").html((editor.modifier_groups || []).map((group) => `<label class="d-block mb-1"><input type="checkbox" class="mr-2" data-guided-modifier="${this.escape(group.name)}" ${selectedNames.has(group.name) ? "checked" : ""}>${this.escape(group.group_name || group.name)} <span class="text-muted">${this.escape(group.selection_type || "")}</span></label>`).join("") || `<span class="text-muted">${__("No active modifier groups found.")}</span>`);
	}

	readGuidedPayload(dialog, editor) {
		const values = dialog.get_values() || {};
		const components = dialog.$wrapper.find("[data-guided-rows] tr").map(function () { const row = $(this); return { item: row.find('[data-guided-field="item"]').val(), component_type: row.find('[data-guided-field="component_type"]').val(), qty: row.find('[data-guided-field="qty"]').val(), uom: row.find('[data-guided-field="uom"]').val(), loss_factor_pct: row.find('[data-guided-field="loss_factor_pct"]').val(), affects_stock: row.find('[data-guided-field="affects_stock"]').is(":checked") ? 1 : 0, affects_cogs: row.find('[data-guided-field="affects_cogs"]').is(":checked") ? 1 : 0 }; }).get();
		const modifier_groups = dialog.$wrapper.find("[data-guided-modifier]:checked").map(function () { return { modifier_group: $(this).attr("data-guided-modifier") }; }).get();
		return { recipe: editor.recipe?.name || "", company: editor.company, sellable_item: editor.sellable_item, warehouse: values.warehouse || editor.warehouse || "", ...values, components, modifier_groups };
	}

	previewGuidedDraft(dialog, editor) {
		const payload = this.readGuidedPayload(dialog, editor);
		frappe.call({ method: "kopos_connector.api.menu_authoring.preview_menu_recipe", type: "POST", args: { payload: JSON.stringify(payload) }, callback: (response) => this.renderGuidedPreview(dialog, response.message || {}) });
	}

	renderGuidedPreview(dialog, response) {
		const preview = response.preview || {};
		const errors = (preview.errors || []).map((row) => `<div class="text-danger">${this.escape(row.message)}</div>`).join("");
		const warnings = (preview.warnings || []).map((row) => `<div class="text-warning">${this.escape(row.message)}</div>`).join("");
		const rows = (preview.components || []).map((row) => `${this.escape(row.item)}: ${this.escape(row.stock_qty_per_batch)} ${this.escape(row.stock_uom)} / ${this.escape(row.stock_qty_per_serving)} per serving`).join("<br>");
		const prep = (preview.components || []).filter((row) => row.component_type === "Prep Item").map((row) => row.prepared_bom ? `${this.escape(row.item)} → ${this.escape(row.prepared_bom.name || __("BOM"))}, ${this.escape(row.prepared_bom.batch_qty || "?")} ${__("per batch")}` : `${this.escape(row.item)} → ${__("BOM required")}`).join("<br>");
		const effects = (preview.modifier_effects || []).map((row) => `${this.escape(row.modifier_name || row.name)} (${this.escape(row.kind || "effect")})`).join(", ");
		const cost = preview.cost_per_batch_sen == null ? __("Cost unavailable") : `${__("Cost per batch")}: ${this.formatSen(preview.cost_per_batch_sen)} · ${__("per serving")}: ${this.formatSen(preview.cost_per_serving_sen)}`;
		dialog.$wrapper.find("[data-guided-preview]").html(`<div class="mb-2"><strong class="${response.status === "ok" ? "text-success" : "text-danger"}">${response.status === "ok" ? __("Preview valid") : __("Fix these rows before saving")}</strong></div>${errors}${warnings}<div class="mt-2">${rows || __("No components")}</div>${prep ? `<div class="mt-2"><strong>${__("Prepared BOMs")}</strong><br>${prep}</div>` : ""}<div class="mt-2"><strong>${this.escape(cost)}</strong></div><div class="mt-2 text-muted">${effects ? `${__("Modifier effects")}: ${effects}` : __("No modifier effects selected")}</div>`);
	}

	saveGuidedDraft(dialog, editor, values) {
		const payload = this.readGuidedPayload(dialog, editor);
		frappe.call({ method: "kopos_connector.api.menu_authoring.save_menu_recipe_draft", type: "POST", args: { payload: JSON.stringify(payload) }, callback: (response) => { const result = response.message || {}; if (result.status !== "ok") return this.showMessage(__("The Draft could not be saved.")); dialog.hide(); frappe.show_alert({ message: __("Draft saved"), indicator: "green" }); this.refresh(); } });
	}

	openCopyDialog({ item, company }) {
		if (!company) return this.showMessage(__("Choose a company before copying a recipe."));
		const dialog = new frappe.ui.Dialog({ title: __("Copy / revise recipe"), fields: [{ fieldtype: "Link", fieldname: "recipe", label: __("Source recipe"), options: "FB Recipe", reqd: 1, get_query: () => ({ filters: { company, ...(item ? { sellable_item: item } : {}) } }) }, { fieldtype: "Data", fieldname: "recipe_name", label: __("New Draft name") }, { fieldtype: "Data", fieldname: "recipe_code", label: __("New Draft code") }], primary_action_label: __("Create Draft revision"), primary_action: (values) => frappe.call({ method: "kopos_connector.api.menu_authoring.copy_menu_recipe_revision", type: "POST", args: { payload: JSON.stringify(values) }, callback: (response) => { const result = response.message || {}; if (!result.recipe) return; dialog.hide(); frappe.show_alert({ message: __("Draft revision created"), indicator: "green" }); this.openGuidedRecipeDialog({ company, item, recipe: result.recipe }); this.refresh(); } }) });
		dialog.show();
	}

	publishSelected() {
		const selected = this.page.body.find("[data-publish-recipe]:checked").map(function () { return $(this).attr("data-publish-recipe"); }).get();
		if (!selected.length) return this.showMessage(__("Select at least one valid Draft recipe."));
		frappe.call({ method: "kopos_connector.api.menu_authoring.publish_menu_recipe_selection", type: "POST", args: { payload: JSON.stringify({ recipes: selected }) }, callback: (response) => { const result = response.message || {}; if (result.status === "ok") { frappe.show_alert({ message: __(`${result.count || 0} recipe(s) published`), indicator: "green" }); this.refresh(); } }, error: () => this.showMessage(__("Nothing was published. Fix every selected recipe first.")) });
	}

	previewCatalog() {
		const posProfile = this.catalogProfileControl?.get_value?.() || "";
		if (!posProfile) return this.showMessage(__("Choose an outlet / POS Profile first."));
		const output = this.page.body.find("[data-menu-catalog-results]").removeClass("d-none").text(__("Checking the outlet catalog…"));
		frappe.call({ method: "kopos_connector.api.inventory.get_menu_catalog_preview", type: "GET", args: { pos_profile: posProfile }, callback: (response) => { const preview = response.message || {}; if (preview.status !== "ok") return output.text(preview.reason || __("The outlet catalog could not be previewed.")); const missing = (preview.missing_recipe_items || []).length ? `\n${__("Missing recipe / exclusion")}: ${(preview.missing_recipe_items || []).join(", ")}` : ""; output.text(`${preview.saleable_items || 0} ${__("saleable items")} · ${preview.recipe_ready_items || 0} ${__("with a current recipe")} · ${preview.explicit_exclusions || 0} ${__("explicit exclusions")} · ${preview.modifier_groups || 0} ${__("modifier groups")}${missing}`); }, error: () => output.text(__("The outlet catalog preview failed.")) });
	}

	checkPromotionEconomics() {
		const promotion = this.promotionControl?.get_value?.() || "";
		if (!promotion) return this.showMessage(__("Choose a promotion first."));
		const scenarios = {};
		for (const [label, selector] of [["low", "[data-promotion-low]"], ["base", "[data-promotion-base]"], ["high", "[data-promotion-high]"]]) { const value = Number.parseInt(this.page.body.find(selector).val(), 10); if (!Number.isInteger(value) || value <= 0) return this.showMessage(`${__("Enter a positive unit count for")} ${label}.`); scenarios[label] = value; }
		this.page.body.find("[data-promotion-status]").text(__("Checking on the ERP server…"));
		frappe.call({ method: "kopos_connector.api.inventory.get_promotion_economics", type: "POST", args: { payload: JSON.stringify({ promotion, pos_profile: this.profileControl?.get_value?.() || "", scenarios }) }, callback: (response) => this.renderPromotionEconomics(response.message || {}), error: () => this.renderPromotionEconomics({ status: "blocked", reason: __("The ERP could not calculate promotion economics") }) });
	}

	renderPromotionEconomics(response) {
		const output = this.page.body.find("[data-promotion-results]").removeClass("d-none small alert alert-success alert-danger").css("white-space", "normal");
		if (response.status !== "ok" || !response.economics) {
			this.page.body.find("[data-promotion-status]").text(__("Publication blocked"));
			output.addClass("alert alert-danger").text(response.reason || __("Missing cost, recipe or valuation evidence."));
			return;
		}

		const economics = response.economics;
		const publicationBlocked = response.publication_status === "Blocked" || Number(economics.gross_profit_sen) < 0;
		const statusLabel = publicationBlocked ? __("Below cost — director review required") : __("Economics checked — Review First");
		const statusTone = publicationBlocked ? "danger" : "success";
		this.page.body.find("[data-promotion-status]").text(statusLabel);
		output.addClass(`alert alert-${statusTone}`).html(`
			<div class="d-flex flex-wrap justify-content-between align-items-start mb-3">
				<strong>${this.escape(__("Server-calculated promotion review"))}</strong>
				<span class="badge badge-${statusTone}">${this.escape(statusLabel)}</span>
			</div>
			${publicationBlocked ? `<div class="alert alert-danger small mb-3"><strong>${this.escape(__("Activation is blocked."))}</strong> ${this.escape(__("A second Company Director must approve a one-time exception with a reason. This check does not activate the promotion."))}</div>` : `<div class="alert alert-info small mb-3">${this.escape(__("Review First: this report explains the economics. It does not forecast promotion lift or create purchasing automatically."))}</div>`}
			${this.renderPromotionSummary(economics)}
			${this.renderPromotionScenarios(economics)}
			${this.renderPromotionDemand(economics)}
			${this.renderPromotionBatchImpact(economics)}
			${this.renderPromotionRisk(economics)}
			${this.renderPromotionActualResults(economics)}
		`);
	}

	renderPromotionSummary(economics) {
		const metrics = [
			[__("Net revenue (excluding tax)"), this.formatSen(economics.net_revenue_sen ?? economics.revenue_sen)],
			[__("Tax"), this.formatSen(economics.tax_sen)],
			[__("COGS"), this.formatSen(economics.cogs_sen)],
			[__("Gross profit"), this.formatSen(economics.gross_profit_sen)],
			[__("Margin"), this.formatPercent(economics.margin_percent)],
			[__("Margin change"), this.formatPercent(economics.margin_change_percent)],
			[__("Break-even additional units"), economics.break_even_additional_units == null ? __("Not available") : `${this.escape(economics.break_even_additional_units)} ${__("units")}`],
		];
		const worstGroup = economics.worst_affected_item_group;
		return `<section aria-labelledby="jiji-promotion-summary-heading" class="mb-3"><h5 id="jiji-promotion-summary-heading" class="mb-2">${this.escape(__("Promotion summary"))}</h5><div class="row">${metrics.map(([label, value]) => `<div class="col-sm-6 col-lg-4 mb-2"><div class="border rounded p-2 h-100"><div class="small text-muted">${this.escape(label)}</div><strong>${this.escape(value)}</strong></div></div>`).join("")}</div><div class="small mt-1">${worstGroup ? `${this.escape(__("Worst affected Item group (largest COGS exposure)"))}: <strong>${this.escape(worstGroup.item_group || __("Unassigned"))}</strong> · ${this.escape(this.formatSen(worstGroup.cogs_sen))}` : this.escape(__("Worst affected Item group: Not available"))}</div></section>`;
	}

	renderPromotionScenarios(economics) {
		const rows = Array.isArray(economics.scenarios) ? economics.scenarios : [];
		if (!rows.length) return `<details class="mb-3"><summary><strong>${this.escape(__("Volume scenarios"))}</strong> <span class="text-muted">${this.escape(__("Not entered"))}</span></summary><p class="small text-muted mt-2 mb-0">${this.escape(__("Enter low, base and high unit counts above, then run the check."))}</p></details>`;
		const scenarioRows = rows.map((row) => {
			const revenue = row.net_revenue_sen ?? row.revenue_sen;
			const margin = Number(revenue) > 0 && Number.isFinite(Number(revenue)) ? `${((Number(row.gross_profit_sen || 0) / Number(revenue)) * 100).toFixed(2)}%` : __("Not available");
			return `<tr><td>${this.escape(row.label)}</td><td>${this.escape(row.units)}</td><td>${this.escape(this.formatSen(revenue))}</td><td>${this.escape(this.formatSen(row.cogs_sen))}</td><td>${this.escape(this.formatSen(row.gross_profit_sen))}</td><td>${this.escape(margin)}</td></tr>`;
		}).join("");
		return `<details open class="mb-3"><summary><strong>${this.escape(__("Volume scenarios"))}</strong> <span class="text-muted">${this.escape(__("planning only"))}</span></summary><div class="table-responsive mt-2"><table class="table table-bordered table-sm mb-0"><thead><tr><th>${this.escape(__("Scenario"))}</th><th>${this.escape(__("Units"))}</th><th>${this.escape(__("Net revenue"))}</th><th>${this.escape(__("COGS"))}</th><th>${this.escape(__("Gross profit"))}</th><th>${this.escape(__("Margin"))}</th></tr></thead><tbody>${scenarioRows}</tbody></table></div></details>`;
	}

	renderPromotionDemand(economics) {
		const rows = Object.entries(economics.ingredient_demand || {});
		const body = rows.length ? rows.map(([item, quantity]) => `<tr><td>${this.escape(item)}</td><td>${this.escape(quantity)}</td></tr>`).join("") : `<tr><td colspan="2" class="text-muted">${this.escape(__("No recipe components"))}</td></tr>`;
		return `<details open class="mb-3"><summary><strong>${this.escape(__("Ingredient demand"))}</strong> <span class="text-muted">${this.escape(__("for the checked promotion volume"))}</span></summary><div class="table-responsive mt-2"><table class="table table-bordered table-sm mb-0"><thead><tr><th>${this.escape(__("Item"))}</th><th>${this.escape(__("Required quantity"))}</th></tr></thead><tbody>${body}</tbody></table></div></details>`;
	}

	renderPromotionBatchImpact(economics) {
		const impact = economics.batch_preparation_impact || {};
		if (impact.status === "not_applicable") return `<details class="mb-3"><summary><strong>${this.escape(__("Prepared-batch impact"))}</strong> <span class="text-muted">${this.escape(__("Not applicable"))}</span></summary><p class="small text-muted mt-2 mb-0">${this.escape(__("This promotion does not use a prepared component."))}</p></details>`;
		const components = Array.isArray(impact.components) ? impact.components : [];
		if (!components.length) return `<details class="mb-3"><summary><strong>${this.escape(__("Prepared-batch impact"))}</strong> <span class="text-warning">${this.escape(__("Not available"))}</span></summary><p class="small text-warning mt-2 mb-0">${this.escape(__("Prepared-component or BOM evidence is missing."))}</p></details>`;
		const scenarioLabels = (Array.isArray(economics.scenarios) ? economics.scenarios : []).map((scenario) => String(scenario.label));
		const rows = components.map((row) => {
			if (row.status !== "available") return `<tr><td>${this.escape(row.item)}</td><td colspan="5" class="text-warning">${this.escape(row.reason || __("Batch evidence unavailable"))}</td></tr>`;
			const scenarioBatches = scenarioLabels.map((label) => `${this.escape(label)}: ${this.escape(row.scenario_batches?.[label] ?? __("Not available"))}`).join(" · ");
			return `<tr><td>${this.escape(row.item)}</td><td>${this.escape(row.base_demand)}</td><td>${this.escape(row.batch_qty)}</td><td>${this.escape(row.batches_required)}</td><td>${this.escape(scenarioBatches || __("Not entered"))}</td><td>${this.escape(row.lead_minutes ?? __("Not set"))}</td></tr>`;
		}).join("");
		const hasUnavailable = components.some((row) => row.status !== "available");
		return `<details open class="mb-3"><summary><strong>${this.escape(__("Prepared-batch impact"))}</strong> <span class="${hasUnavailable ? "text-warning" : "text-success"}">${this.escape(hasUnavailable ? __("Check evidence") : __("Calculated"))}</span></summary><div class="table-responsive mt-2"><table class="table table-bordered table-sm mb-0"><thead><tr><th>${this.escape(__("Prepared Item"))}</th><th>${this.escape(__("Base demand"))}</th><th>${this.escape(__("Batch output"))}</th><th>${this.escape(__("Batches"))}</th><th>${this.escape(__("Scenario batches"))}</th><th>${this.escape(__("Lead minutes"))}</th></tr></thead><tbody>${rows}</tbody></table></div><p class="small text-muted mt-2 mb-0">${this.escape(__("Use the batch alert and standard BOM flow to prepare stock; this report does not create a Work Order."))}</p></details>`;
	}

	renderPromotionRisk(economics) {
		const risk = economics.runout_waste_risk || {};
		if (risk.status !== "available") return `<details open class="mb-3"><summary><strong>${this.escape(__("Runout and waste risk"))}</strong> <span class="text-warning">${this.escape(__("Not available"))}</span></summary><p class="small text-warning mt-2 mb-0">${this.escape(risk.reason || __("Current stock and expiry evidence is incomplete."))}</p></details>`;
		const riskRows = Array.isArray(risk.items) ? risk.items : [];
		const rows = riskRows.map((row) => `<tr><td>${this.escape(row.item)}</td><td><span class="${row.runout === "risk" ? "text-danger" : "text-success"}">${this.escape(this.promotionRiskLabel(row.runout))}</span></td><td><span class="${row.waste === "risk" ? "text-warning" : row.waste === "no_waste_evidence" ? "text-success" : "text-muted"}">${this.escape(this.promotionRiskLabel(row.waste))}</span></td><td class="text-muted">${this.escape(row.reason || __("Based on current stock and expiry evidence"))}</td></tr>`).join("");
		return `<details open class="mb-3"><summary><strong>${this.escape(__("Runout and waste risk"))}</strong> <span class="text-muted">${this.escape(__("current evidence"))}</span></summary><div class="table-responsive mt-2"><table class="table table-bordered table-sm mb-0"><thead><tr><th>${this.escape(__("Component"))}</th><th>${this.escape(__("Runout"))}</th><th>${this.escape(__("Waste"))}</th><th>${this.escape(__("Evidence"))}</th></tr></thead><tbody>${rows || `<tr><td colspan="4" class="text-muted">${this.escape(__("No component risk rows returned."))}</td></tr>`}</tbody></table></div></details>`;
	}

	renderPromotionActualResults(economics) {
		const actual = economics.actual_results || {};
		const available = actual.status === "available";
		const notes = [actual.note, actual.reason, actual.tax_reason].filter(Boolean).join(" ");
		if (!available) return `<details class="mb-0"><summary><strong>${this.escape(__("Actual post-promotion results"))}</strong> <span class="text-warning">${this.escape(__("Not available yet"))}</span></summary><p class="small text-warning mt-2 mb-0">${this.escape(notes || __("No post-cutover attributed orders with complete ERP valuation evidence."))}</p></details>`;
		return `<details open class="mb-0"><summary><strong>${this.escape(__("Actual post-promotion results"))}</strong> <span class="text-success">${this.escape(__("Available"))}</span></summary><div class="row mt-2"><div class="col-sm-6 col-lg-3 mb-2"><div class="small text-muted">${this.escape(__("Orders"))}</div><strong>${this.escape(actual.order_count ?? __("Not available"))}</strong></div><div class="col-sm-6 col-lg-3 mb-2"><div class="small text-muted">${this.escape(__("Promoted units"))}</div><strong>${this.escape(actual.promoted_units ?? __("Not available"))}</strong></div><div class="col-sm-6 col-lg-3 mb-2"><div class="small text-muted">${this.escape(__("Actual net revenue"))}</div><strong>${this.escape(this.formatSen(actual.net_revenue_sen))}</strong></div><div class="col-sm-6 col-lg-3 mb-2"><div class="small text-muted">${this.escape(__("Actual COGS"))}</div><strong>${this.escape(this.formatSen(actual.cogs_sen))}</strong></div></div><div class="small">${this.escape(__("Gross profit"))}: <strong>${this.escape(this.formatSen(actual.gross_profit_sen))}</strong> · ${this.escape(__("Margin"))}: <strong>${this.escape(this.formatPercent(actual.margin_percent))}</strong></div><div class="small text-muted mt-2">${this.escape(actual.cogs_source || __("Valuation source not available"))}${notes ? ` · ${this.escape(notes)}` : ""}</div></details>`;
	}

	promotionRiskLabel(value) {
		const labels = { risk: __("Risk"), no_immediate_runout: __("No immediate runout"), no_waste_evidence: __("No waste evidence"), not_available: __("Not available") };
		return labels[value] || __("Not available");
	}

	downloadTemplate() {
		frappe.call({ method: "kopos_connector.api.inventory.get_menu_recipe_csv_template", type: "GET", callback: (response) => { const template = response.message || {}; const blob = new Blob([template.content || ""], { type: "text/csv;charset=utf-8" }); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = template.filename || "jiji-recipe-components-template.csv"; link.click(); URL.revokeObjectURL(link.href); this.page.body.find("[data-csv-status]").text(__("Template downloaded")); } });
	}

	validateCsv() {
		const csvText = this.page.body.find("[data-csv-input]").val() || "";
		if (!String(csvText).trim()) return this.showMessage(__("Paste CSV rows first."));
		frappe.call({ method: "kopos_connector.api.inventory.validate_menu_recipe_csv", type: "POST", args: { csv_text: csvText }, callback: (response) => { const result = response.message || {}; const errors = result.errors || []; const output = errors.length ? errors.map((error) => `${__("Row")} ${error.row}: ${error.message}`).join("\n") : __(`${result.recipe_count || 0} recipe(s) passed the dry run. Use the guided editor to save a Draft.`); this.page.body.find("[data-csv-results]").removeClass("d-none").text(output); this.page.body.find("[data-csv-status]").text(result.valid ? __("Rows look complete") : `${errors.length} ${__("row issue(s)")}`); } });
	}

	formatSen(value) { const number = Number(value); return value == null || value === "" || !Number.isFinite(number) ? __("Unavailable") : `RM ${(number / 100).toFixed(2)}`; }
	formatPercent(value) { return value == null || value === "" || !Number.isFinite(Number(value)) ? __("Not available") : `${Number(value).toFixed(2)}%`; }
	showMessage(message) { this.page.body.find("[data-guided-editor-status]").text(message); }
	escape(value) { return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[character]); }
}
