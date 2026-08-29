frappe.pages["jiji_menu_recipes"].on_page_load = function (wrapper) {
	new JiJiMenuRecipesPage(wrapper);
};

const RECIPE_TYPE_LABELS = {
	"Finished Drink": "Drink or food made when ordered",
	"Add-On": "Add-on",
	"Prep Batch": "Item made in advance",
	"Packaging Assembly": "Packaging set",
};

const PROMOTION_TYPE_VALUES = {
	"Discount selected items": "item_discount",
	"Discount the full order": "order_discount",
	"Happy hour": "happy_hour",
	"Buy items and get one": "buy_x_get_y",
	"Discount every nth item": "nth_item_discount",
};

const DISCOUNT_TYPE_VALUES = {
	"Percentage off": "percentage",
	"Fixed amount off": "fixed_amount",
	"Set a fixed price": "fixed_price",
	"Give a free item": "free_item",
};

const ITEM_TYPE_VALUES = {
	"Made to order from a recipe": "Sellable Drink",
	"Packaged item bought ready to sell": "Packaged Finished Good",
	"Service that does not use stock": "Non-Stock Service",
	"Ingredient": "Ingredient",
	"Item made in advance": "Prep Item",
	"Packaging or supply": "Packaging",
};

class JiJiMenuRecipesPage {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("JiJi Menu, Recipes & Offers"),
			single_column: true,
		});
		jiji.desk.injectStyle();
		$(wrapper).addClass("jiji-desk");
		this.render();
		this.bindEvents();
		this.setupControls();
		this.refresh();
	}

	render() {
		const cards = [
			["Menu items", "Start here. Tell JiJi whether each item is made from a recipe, sold as a pack, or does not use stock.", "item", "Set up a menu item"],
			["Recipes", "List exactly what goes into one serving, including the amount and the unit used to count it.", "recipe", "Enter a recipe"],
			["Items made in advance", "Set up cold foam, juice, syrup, or anything else staff make before an order arrives.", "bom", "Set up a prepared item"],
			["Customer choices", "Set up choices such as milk type, size, or an extra shot, then add them to a recipe.", "modifier", "Set up customer choices"],
			["Offers", "See sales after the discount, ingredient cost, and money left before turning on an offer.", "promotion", "Create an offer"],
			["Final checks", "Fix anything missing, review saved recipes, and make the approved recipes on sale.", "validate", "Review final checks"],
		];
		$(this.page.body).html(`
			<div class="jiji-menu-page">
				<details class="card mb-4 jiji-help"><summary class="card-body py-3"><strong>${__("New to running a food or drink business?")}</strong></summary><div class="card-body pt-0 text-muted">${__("Start with one menu item and work from left to right. A recipe is the exact list of ingredients used. An item made in advance is something staff prepare before an order, such as cold foam. A draft is saved work that customers cannot order yet.")}</div></details>
				<div class="card mb-4" data-completion-card><div class="card-body">
					<div class="d-flex flex-wrap justify-content-between align-items-start"><div><h4 class="mb-1">${__("How far you have got")}</h4><p class="text-muted mb-0">${__("Choose a café to see what is ready, what is missing, and what to do next.")}</p></div><div class="small text-muted" data-progress-summary>${__("Choose your café")}</div></div>
					<div class="progress mt-3" style="height: 8px" aria-label="${__("How far you have got")}"><div class="progress-bar" role="progressbar" data-progress-bar style="width: 0%"></div></div>
					<div class="d-flex flex-wrap justify-content-between align-items-center mt-3"><span class="small" data-next-action>${__("Next: choose your café")}</span><button class="btn btn-sm btn-primary" type="button" data-next-action-button disabled>${__("Choose your café")}</button></div>
				</div></div>
				<div class="card mb-4"><div class="card-body">
					<div class="d-flex flex-wrap justify-content-between align-items-start mb-3"><div><h4 class="mb-1">${__("Look at one thing on your menu")}</h4><p class="text-muted mb-0">${__("Nothing is saved until you press Save. Saving does not change what staff can sell.")}</p></div><div class="small text-muted" data-company-status>${__("Choose a café, then something on the menu")}</div></div>
					<div class="row mb-3"><div class="col-md-4"><div data-company-link></div></div><div class="col-md-4"><div data-menu-item-link></div></div><div class="col-md-4"><div data-catalog-profile-link></div></div></div>
					<div class="d-flex flex-wrap align-items-center gap-2"><button class="btn btn-sm btn-default" type="button" data-menu-commission="item" disabled>${__("1. Choose item type")}</button><button class="btn btn-sm btn-primary" type="button" data-menu-commission="recipe" disabled>${__("2. Enter recipe")}</button><button class="btn btn-sm btn-default" type="button" data-menu-catalog-preview disabled>${__("3. Check what the tablet will show")}</button></div>
					<p class="text-muted small mt-2 mb-0" data-sequence-waiting></p>
					<div data-drink class="mt-4"></div>
					<p class="small mt-2 mb-0"><button class="btn btn-link btn-sm p-0" type="button" data-menu-commission="recipes">${__("Copy or change a recipe")}</button> <span class="text-muted">${__("— for an item that already has one.")}</span></p>
					<pre class="small bg-light p-2 mt-3 mb-0 d-none" data-menu-catalog-results></pre><div class="small mt-2" data-guided-editor-status></div>
				</div></div>
				<div class="card mb-4"><div class="card-body"><div class="d-flex flex-wrap justify-content-between align-items-center mb-2"><div><h4 class="mb-1">${__("Let staff start selling these")}</h4><p class="text-muted mb-0">${__("JiJi checks each one first. If any has a problem, nothing changes.")}</p></div><button class="btn btn-sm btn-primary" type="button" data-publish-selected disabled>${__("Let staff sell the ones you chose")}</button></div><div data-publish-queue class="small text-muted">${__("Choose a café to see the recipes you have saved.")}</div></div></div>
				<details class="card mb-4 jiji-help"><summary class="card-body py-3"><strong>${__("Check a recipe spreadsheet")}</strong> <span class="text-muted small">${__("if you keep your recipes in a sheet")}</span></summary><div class="card-body pt-0">
					<div class="d-flex flex-wrap justify-content-between align-items-start mb-2"><div><h4 class="mb-1 sr-only">${__("Check a recipe spreadsheet")}</h4><p class="text-muted mb-0">${__("Paste rows from the JiJi template and JiJi will tell you what is missing or wrong. Nothing is saved or changed.")}</p></div><div class="small text-muted" data-csv-status>${__("No rows checked")}</div></div>
					<textarea class="form-control mb-2" rows="4" data-csv-input placeholder="${__("recipe_code,recipe_name,sellable_item,company,recipe_type,...")}"></textarea><div class="d-flex flex-wrap gap-2"><button class="btn btn-sm btn-default" type="button" data-csv-template>${__("Download template")}</button><button class="btn btn-sm btn-primary" type="button" data-csv-validate>${__("Check rows")}</button></div><pre class="small bg-light p-2 mt-3 mb-0 d-none" data-csv-results></pre>
				</div></details>
				<details class="card mb-4 jiji-help"><summary class="card-body py-3"><strong>${__("Check promotion cost and profit")}</strong> <span class="text-muted small">${__("before you turn an offer on")}</span></summary><div class="card-body pt-0">
					<div class="d-flex flex-wrap justify-content-between align-items-start mb-2"><div><h4 class="mb-1 sr-only">${__("Check promotion cost and profit")}</h4><p class="text-muted mb-0">${__("See sales after the discount, ingredient cost, money left, and stock needed before turning on an offer. This check never orders stock or turns on the offer.")}</p></div><div class="small text-muted" data-promotion-status>${__("No offer checked")}</div></div>
					<div class="row mb-3"><div class="col-md-6"><div data-promotion-link></div></div><div class="col-md-6"><div data-promotion-profile></div></div></div><div class="row mb-3"><div class="col-sm-4"><label class="text-muted small" for="jiji-promotion-low">${__("If sales are low")}</label><input id="jiji-promotion-low" class="form-control" type="number" min="1" step="1" value="10" data-promotion-low></div><div class="col-sm-4"><label class="text-muted small" for="jiji-promotion-base">${__("Expected sales")}</label><input id="jiji-promotion-base" class="form-control" type="number" min="1" step="1" value="25" data-promotion-base></div><div class="col-sm-4"><label class="text-muted small" for="jiji-promotion-high">${__("If sales are high")}</label><input id="jiji-promotion-high" class="form-control" type="number" min="1" step="1" value="50" data-promotion-high></div></div><p class="text-muted small">${__("Enter how many discounted items you may sell in each case.")}</p><button class="btn btn-sm btn-primary" type="button" data-promotion-check disabled>${__("Check cost and money left")}</button><div class="small mt-3 mb-0 d-none" role="status" aria-live="polite" data-promotion-results></div>
				</div></details>
				<details class="card mb-4 jiji-help"><summary class="card-body py-3"><strong>${__("Everything else on your menu")}</strong> <span class="text-muted small">${__("items made in advance, customer choices, offers, and the final checks")}</span></summary><div class="card-body pt-0">
				<div class="row">${cards.map(([title, copy, action, primary]) => `
					<div class="col-sm-6 col-lg-4 mb-3"><div class="card h-100" data-menu-card="${action}"><div class="card-body d-flex flex-column"><div class="d-flex justify-content-between align-items-start"><h4>${__(title)}</h4><span class="badge badge-light" data-card-state="${action}">${__("Not checked")}</span></div><p class="text-muted">${__(copy)}</p><div class="small text-muted mb-3" data-check="${action}">${__("Checking…")}</div><div class="mt-auto d-flex flex-wrap gap-2"><button class="btn btn-sm btn-primary" type="button" data-menu-create="${action}">${__(primary)}</button><button class="btn btn-sm btn-default" type="button" data-menu-action="${action}">${action === "validate" ? __("Open checklist") : __("Open records")}</button></div></div></div></div>`).join("")}</div>
				</div></details>
				<p class="text-muted small mb-0">${__("Only you see ingredient cost and profit. Staff and managers see the stock amounts and tasks they need, and nothing else.")}</p>
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
		this.companyControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-company-link]")[0], df: { fieldtype: "Link", fieldname: "company", options: "Company", label: __("Your café"), onchange: () => this.onSelectionChanged(true) } });
		this.companyControl.refresh();
		this.menuItemControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-menu-item-link]")[0], df: { fieldtype: "Link", fieldname: "menu_item", options: "Item", label: __("Menu item"), get_query: () => ({ filters: { is_sales_item: 1, disabled: 0 } }), onchange: () => this.onSelectionChanged(false) } });
		this.menuItemControl.refresh();
		this.catalogProfileControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-catalog-profile-link]")[0], df: { fieldtype: "Link", fieldname: "catalog_profile", options: "POS Profile", label: __("Outlet"), description: __("Choose an outlet to see what its tablet will show."), get_query: () => ({ filters: { disabled: 0 } }), onchange: () => this.onSelectionChanged(false) } });
		this.catalogProfileControl.refresh();
		this.promotionControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-promotion-link]")[0], df: { fieldtype: "Link", fieldname: "promotion", options: "KoPOS Promotion", label: __("Promotion"), onchange: () => this.updatePromotionActionState() } });
		this.promotionControl.refresh();
		this.profileControl = frappe.ui.form.make_control({ parent: this.page.body.find("[data-promotion-profile]")[0], df: { fieldtype: "Link", fieldname: "pos_profile", options: "POS Profile", label: __("Outlet (optional)"), description: __("Choose one outlet to use its current prices and stock."), } });
		this.profileControl.refresh();
		this.updateActionState();
		this.updatePromotionActionState();
	}

	onSelectionChanged(refreshSummary) {
		this.updateActionState();
		this.loadDrink();
		if (refreshSummary) this.refresh(); else this.refreshPublishQueue();
	}

	/**
	 * The drink itself, on the page.
	 *
	 * Choosing a menu item used to change nothing visible until a dialog was
	 * opened. A director's question -- what goes into this, and does it still
	 * make money -- is answerable without opening anything, from figures the
	 * editor already computes.
	 */
	loadDrink() {
		const company = this.companyControl?.get_value?.();
		const item = this.menuItemControl?.get_value?.();
		const target = this.page.body.find("[data-drink]");
		if (!company || !item) return target.empty();
		frappe.call({
			method: "kopos_connector.api.menu_authoring.get_menu_recipe_editor",
			type: "GET",
			// No warehouse: the server resolves the director's own outlet.
			args: { company, sellable_item: item },
			callback: (response) => this.renderDrink(response.message || {}),
			error: () => target.empty(),
		});
	}

	renderDrink(data) {
		const target = this.page.body.find("[data-drink]").empty();
		if (data.status !== "ok") return;
		const preview = data.preview || {};
		const recipe = data.recipe || {};
		const name = (data.item || {}).item_name || data.sellable_item || "";
		const lines = (preview.components || []).filter((row) => row.item);

		const card = $('<div class="jiji-card"></div>').appendTo(target);
		const head = $('<div class="jiji-head"></div>').appendTo(card);
		head.append(
			$("<span>")
				.addClass("jiji-drink__title")
				.text(lines.length ? __("What goes into a {0}?", [name]) : __("{0} has no recipe yet", [name])),
		);
		if (recipe.status) head.append(jiji.desk.pill(recipe.status, recipe.status === "Active" ? "ok" : ""));
		card.append(
			$("<div>")
				.addClass("jiji-muted")
				.css("margin-top", "2px")
				.text(
					lines.length
						? __("Measured for one drink. JiJi works out the cost and what it can make from your stock.")
						: __("Add the ingredients and JiJi will cost it for you."),
				),
		);

		if (lines.length) {
			const rows = $('<div class="jiji-rows"></div>').css("margin-top", "14px").appendTo(card);
			lines.forEach((line) => {
				const row = $('<div class="jiji-row"></div>').appendTo(rows);
				row.append($("<span>").addClass("jiji-row__main").text(line.item_name || line.item));
				row.append(
					$("<span>")
						.addClass("jiji-drink__amount jiji-num")
						.text(line.component_type === "Packaging" ? __("packaging") : line.amount || ""),
				);
				row.append(
					$("<span>")
						.addClass("jiji-drink__cost jiji-num")
						.text(line.cost_per_serving_sen == null ? __("no cost yet") : this.formatSen(line.cost_per_serving_sen)),
				);
			});
			card.append($("<div>").addClass("mt-3").html(this.moneyLineHtml(preview, true)));
		}

		// What the saved recipe means, beyond whether it is valid.
		const facts = [];
		const canMake = data.can_make || {};
		if (canMake.value != null) {
			facts.push([__("Today's stock can make"), __("{0} more", [canMake.value])]);
		} else if (canMake.reason) {
			facts.push([__("Today's stock can make"), canMake.reason]);
		}
		if (facts.length) {
			const list = $("<div>").css("margin-top", "14px").appendTo(card);
			facts.forEach(([label, value]) => {
				const row = $('<div class="jiji-kv"></div>').appendTo(list);
				row.append($("<span>").addClass("jiji-kv__main").text(label));
				row.append($("<span>").addClass("jiji-num").css("font-weight", "600").text(value));
			});
		}
	}

	updateActionState() {
		const company = Boolean(this.companyControl?.get_value?.());
		const item = Boolean(this.menuItemControl?.get_value?.());
		const companyAndItem = item && company;
		const profile = Boolean(this.catalogProfileControl?.get_value?.());
		this.page.body.find('[data-menu-commission="item"]').prop("disabled", !company);
		this.page.body.find('[data-menu-commission="recipe"]').prop("disabled", !companyAndItem);
		this.page.body.find("[data-menu-catalog-preview]").prop("disabled", !profile);

		// A greyed step that never says why is the same as a broken one. Name
		// what the sequence is waiting for, in the order it needs it.
		let waiting = "";
		if (!company) waiting = __("Step 1 is waiting for a company.");
		else if (!item) waiting = __("Step 2 is waiting for a menu item.");
		else if (!profile) waiting = __("Step 3 is waiting for an outlet to check against.");
		this.page.body.find("[data-sequence-waiting]").text(waiting);
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
			if (summary.company_selection_required) {
				this.page.body.find("[data-check]").text(__("Choose a café to see this."));
			} else {
				this.page.body.find('[data-check="item"]').text(`${summary.items_ready || 0}/${summary.saleable_items || 0} ${__("ready")} · ${summary.items_missing_recipe || 0} ${__("missing recipe")} · ${summary.approved_exclusions || 0} ${__("excluded")}`);
				this.page.body.find('[data-check="recipe"]').text(`${summary.published_recipes || 0} ${__("on sale")} · ${summary.draft_recipes || 0} ${__("draft")}`);
				this.page.body.find('[data-check="bom"]').text(this.countLabel(summary.boms, __("prepared-item recipe"), __("prepared-item recipes")));
				this.page.body.find('[data-check="modifier"]').text(this.countLabel(summary.modifier_groups, __("customer choice group"), __("customer choice groups")));
				this.page.body.find('[data-check="promotion"]').text(this.countLabel(summary.active_promotions, __("active promotion"), __("active promotions")));
				this.page.body.find('[data-check="validate"]').text(summary.ready ? __("Ready for your last check") : __("Finish the missing pieces first"));
			}
			this.renderCommissioningStatus(summary);
			this.page.set_indicator(summary.ready ? __("Ready") : __("Needs you"), summary.ready ? "green" : "orange");
			this.refreshPublishQueue();
		}, error: () => { this.page.body.find("[data-company-status]").text(__("JiJi could not load this list")); this.page.set_indicator(__("Needs attention"), "red"); } });
	}

	refreshPublishQueue() {
		const company = this.companyControl?.get_value?.() || "";
		const output = this.page.body.find("[data-publish-queue]");
		if (!company) { output.text(__("Choose a café to see the recipes you have saved.")); this.page.body.find("[data-publish-selected]").prop("disabled", true); return; }
		frappe.call({ method: "kopos_connector.api.menu_authoring.get_menu_recipe_publish_queue", type: "GET", args: { company }, callback: (response) => {
			const recipes = response.message?.recipes || [];
			if (!recipes.length) { output.text(__("You have not saved any recipes yet.")); this.page.body.find("[data-publish-selected]").prop("disabled", true); return; }
			output.html(recipes.map((row) => `<label class="d-flex align-items-start mb-2"><input class="mr-2 mt-1" type="checkbox" data-publish-recipe="${this.escape(row.name)}" ${row.valid ? "" : "disabled"}><span><strong>${this.escape(row.recipe_name || row.recipe_code || row.name)}</strong> <span class="text-muted">${__("version")} ${row.version_no || "?"} · ${row.component_count || 0} ${__("ingredients or supplies")}</span><br><span class="${row.valid ? "text-success" : "text-danger"}">${row.valid ? __("Ready for your last check") : this.escape((row.errors || []).join("; "))}</span></span></label>`).join(""));
			this.page.body.find("[data-publish-selected]").prop("disabled", !output.find("[data-publish-recipe]").length);
		}, error: () => { output.text(__("JiJi could not load your saved recipes.")); this.page.body.find("[data-publish-selected]").prop("disabled", true); } });
	}

	renderCommissioningStatus(summary) {
		const messages = [];
		if (summary.company_selection_required) messages.push(__("Choose a café to see how its menu is doing."));
		if (!summary.item_fields_ready || !summary.recipe_schema_ready) messages.push(__("Ask whoever set up JiJi to finish the stock setup before you add recipes."));
		const hasSaleableItems = Number(summary.saleable_items || 0) > 0;
		const companyReady = Boolean(summary.selected_company) && !summary.company_selection_required;
		if (companyReady && !hasSaleableItems) messages.push(__("Add or import at least one menu item to begin."));
		if (companyReady && summary.unclassified_items) messages.push(`${this.countLabel(summary.unclassified_items, __("menu item still needs"), __("menu items still need"))} ${__("to be marked as made to order, packaged, or a service.")}`);
		if (companyReady && summary.items_missing_recipe) messages.push(`${this.countLabel(summary.items_missing_recipe, __("menu item needs"), __("menu items need"))} ${__("a recipe, or a note saying why it does not need one.")}`);
		if (companyReady && summary.invalid_exclusions) messages.push(`${this.countLabel(summary.invalid_exclusions, __("item without a recipe needs"), __("items without a recipe need"))} ${__("a reason.")}`);
		const migrationReady = Boolean(summary.item_fields_ready) && Boolean(summary.recipe_schema_ready);
		const classificationReady = migrationReady && hasSaleableItems && !Number(summary.unclassified_items || 0);
		const recipesReady = classificationReady && !Number(summary.items_missing_recipe || 0) && !Number(summary.invalid_exclusions || 0);
		const complete = Boolean(summary.ready);
		const completedSteps = companyReady ? [companyReady, migrationReady, classificationReady, recipesReady].filter(Boolean).length : 0;
		const percent = complete ? 100 : Math.round((completedSteps / 4) * 100);
		this.page.body.find("[data-company-status]").text(messages.length ? messages.join(" ") : __("You have everything you need to sell."));
		this.page.body.find("[data-progress-bar]").removeClass("bg-success bg-warning").addClass(complete ? "bg-success" : "bg-warning").css("width", `${percent}%`).attr("aria-valuenow", percent);
		this.page.body.find("[data-progress-summary]").text(__("{0} of the 4 things you need", [completedSteps]));
		// Setup progress is onboarding. Once a café is set up it is a bar at
		// 100% sitting above the work, every day, saying nothing.
		this.page.body.find("[data-completion-card]").toggleClass("d-none", complete);
		if (!companyReady) {
			["item", "recipe", "bom", "modifier", "promotion", "validate"].forEach((action) => this.setCardState(action, __("Not checked"), "secondary"));
		} else {
			this.setCardState("item", classificationReady ? __("Ready") : hasSaleableItems ? __("Needs you") : __("Start here"), classificationReady ? "success" : "warning");
			this.setCardState("recipe", recipesReady ? __("Ready") : __("Needs you"), recipesReady ? "success" : "warning");
			this.setCardState("bom", Number(summary.boms || 0) ? __("Configured") : __("Optional"), Number(summary.boms || 0) ? "success" : "secondary");
			this.setCardState("modifier", Number(summary.modifier_groups || 0) ? __("Configured") : __("Optional"), Number(summary.modifier_groups || 0) ? "success" : "secondary");
			this.setCardState("promotion", Number(summary.active_promotions || 0) ? __("Configured") : __("Optional"), Number(summary.active_promotions || 0) ? "success" : "secondary");
			this.setCardState("validate", complete ? __("Ready") : __("Review"), complete ? "success" : "warning");
		}
		this.setNextAction({ companyReady, migrationReady, hasSaleableItems, classificationReady, recipesReady, complete });
	}

	setCardState(action, label, tone) {
		this.page.body.find(`[data-card-state="${action}"]`).removeClass("badge-light badge-success badge-warning badge-secondary").addClass(`badge-${tone}`).text(label);
	}

	setNextAction(state) {
		let message = __("Next: choose your café");
		let label = __("Choose your café");
		let action = "company";
		let disabled = false;
		if (!state.companyReady) {
			this.nextAction = action;
			this.page.body.find("[data-next-action]").text(message);
			this.page.body.find("[data-next-action-button]").text(label).prop("disabled", disabled);
			return;
		}
		if (!state.migrationReady) {
			message = __("Next: ask whoever set up JiJi to finish the stock setup.");
			label = __("Stock setup unfinished");
			action = "none";
			disabled = true;
		} else if (!state.hasSaleableItems) {
			message = __("Next: add or import the first menu item, then choose how it uses stock.");
			label = __("Add a menu item");
			action = "item";
		} else if (!state.classificationReady) {
			message = __("Next: choose whether each menu item is made to order, packaged, a service, or does not need a recipe.");
			label = __("Choose an item type");
			action = "item";
		} else if (state.classificationReady && !state.recipesReady) {
			message = __("Next: enter and publish the measured recipe for each menu item.");
			label = __("Enter a recipe");
			action = "recipe";
		} else if (state.recipesReady && !state.complete) {
			message = __("Next: check the recipes you saved, then let staff start selling them.");
			label = __("Check your saved recipes");
			action = "validate";
		} else if (state.complete) {
			message = __("You have everything you need to sell. Below you can also set up things staff make in advance, customer choices, and offers.");
			label = __("Check your saved recipes");
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
		if (action === "recipe") { if (!item || !company) return this.showMessage(__("Choose a café and something on the menu first.")); return this.openGuidedRecipeDialog({ item, company }); }
		if (action === "recipes") return this.openCopyDialog({ item, company });
	}

	openGuidedRecipeDialog({ item, company, recipe } = {}) {
		item = item || this.menuItemControl?.get_value?.() || "";
		company = company || this.companyControl?.get_value?.() || "";
		if (!item || !company) return this.showMessage(__("Choose a café and something on the menu first."));
		frappe.call({ method: "kopos_connector.api.menu_authoring.get_menu_recipe_editor", type: "GET", args: { company, sellable_item: item, recipe: recipe || "", warehouse: "" }, callback: (response) => {
			const editor = response.message || {};
			if (editor.status !== "ok") return this.showMessage(__("JiJi could not open the recipe form."));
			this.showGuidedEditor(editor);
		}, error: () => this.showMessage(__("JiJi could not open the recipe form.")) });
	}

	openGuidedItemDialog() {
		const company = this.companyControl?.get_value?.() || "";
		if (!company) return this.showMessage(__("Choose a café before adding something to the menu."));
		const dialog = new frappe.ui.Dialog({
			title: __("Add a menu item"),
			fields: [
				{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("JiJi saves the new item as not for sale. Finish its details and recipe before making it available at an outlet.")}</p>` },
				{ fieldtype: "Data", fieldname: "item_name", label: __("Item name"), reqd: 1 },
				{ fieldtype: "Data", fieldname: "item_code", label: __("Item code (optional)") },
				{ fieldtype: "Link", fieldname: "item_group", options: "Item Group", label: __("Menu group"), description: __("For example: Coffee, Matcha, Pastries, or Merchandise."), reqd: 1 },
				{ fieldtype: "Link", fieldname: "stock_uom", options: "UOM", label: __("How this item is counted"), description: __("For example: Cup, Each, Gram, or Millilitre."), reqd: 1 },
				{ fieldtype: "Select", fieldname: "item_role", label: __("What kind of item is this?"), options: Object.keys(ITEM_TYPE_VALUES).join("\n"), description: __("Choose Made to order from a recipe for drinks or food prepared after a customer orders."), reqd: 1, default: "Made to order from a recipe" },
			],
			primary_action_label: __("Create item — not for sale yet"),
			primary_action: (values) => frappe.call({
				method: "kopos_connector.api.menu_authoring.create_menu_item_draft",
				type: "POST",
				args: { payload: JSON.stringify({ ...values, item_role: ITEM_TYPE_VALUES[values.item_role] || "Sellable Drink", company }) },
				callback: (response) => {
					const result = response.message || {};
					if (result.status !== "ok" || !result.item) return this.showMessage(__("The menu item could not be created."));
					dialog.hide();
					frappe.show_alert({ message: __("Menu item created. Finish its details before making it available for sale."), indicator: "green" });
					frappe.set_route("Form", "Item", result.item);
					this.refresh();
				},
				error: () => this.showMessage(__("The menu item could not be created. Check the required details and try again.")),
			}),
		});
		dialog.show();
	}

	openPreparedBatchDialog() {
		const dialog = new frappe.ui.Dialog({
			title: __("Set up an item made in advance"),
			fields: [
				{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("Choose something staff make before an order, such as cold foam or syrup. Next, JiJi opens the ingredient recipe used to make that stock.")}</p>` },
				{ fieldtype: "Link", fieldname: "item", options: "Item", label: __("Item staff will prepare"), reqd: 1, get_query: () => ({ filters: { is_stock_item: 1, disabled: 0 } }) },
				{ fieldtype: "Float", fieldname: "quantity", label: __("Recipe amount"), description: __("The amount shown on the standard making recipe."), reqd: 1, default: 1 },
				{ fieldtype: "Float", fieldname: "batch_qty", label: __("Usable amount made"), description: __("How much good, usable stock one preparation makes."), reqd: 1, default: 1 },
				{ fieldtype: "Float", fieldname: "min_ready_qty", label: __("Remind staff when less than"), reqd: 1, default: 1 },
				{ fieldtype: "Int", fieldname: "lead_minutes", label: __("Minutes needed to prepare"), reqd: 1, default: 30 },
				{ fieldtype: "Small Text", fieldname: "instructions", label: __("Simple preparation steps") },
			],
			primary_action_label: __("Continue to ingredient recipe"),
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
			title: __("Set up a customer choice group"),
			fields: [
				{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("A choice group is a question such as “Which milk?” or “Add an extra shot?”. Create the group, add its choices, then select the group in the recipe.")}</p>` },
				{ fieldtype: "Data", fieldname: "group_code", label: __("Group code"), reqd: 1 },
				{ fieldtype: "Data", fieldname: "group_name", label: __("What staff will see"), reqd: 1 },
				{ fieldtype: "Select", fieldname: "selection_type", label: __("Selection"), options: "Single\nMultiple", default: "Single", reqd: 1 },
				{ fieldtype: "Check", fieldname: "is_required", label: __("Customer must choose") },
			],
			primary_action_label: __("Continue to add choices"),
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
			title: __("Create an offer draft"),
			fields: [
				{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("Enter the basic discount here, then finish the dates and eligible items on the next form. Return to this page to check ingredient cost and money left before turning it on.")}</p>` },
				{ fieldtype: "Data", fieldname: "promotion_name", label: __("Offer name"), reqd: 1 },
				{ fieldtype: "Select", fieldname: "promotion_type", label: __("How does the offer work?"), options: Object.keys(PROMOTION_TYPE_VALUES).join("\n"), default: "Discount selected items", reqd: 1 },
				{ fieldtype: "Data", fieldname: "display_label", label: __("Label shown to customers"), reqd: 1, default: item ? __("Special offer") : "" },
				{ fieldtype: "Select", fieldname: "discount_type", label: __("What discount will customers get?"), options: Object.keys(DISCOUNT_TYPE_VALUES).join("\n"), default: "Percentage off", reqd: 1 },
				{ fieldtype: "Float", fieldname: "discount_value", label: __("Discount amount"), description: __("For example, enter 10 for 10% off when Percentage off is selected."), reqd: 1, default: 10 },
				{ fieldtype: "HTML", fieldname: "cost_note", options: `<div class="alert alert-warning small mb-0">${__("Do not turn on this offer until the cost and money-left check below has complete ingredient costs.")}</div>` },
			],
			primary_action_label: __("Continue setting up the offer"),
			primary_action: (values) => {
				if (!values.promotion_name) return;
				dialog.hide();
				frappe.new_doc("KoPOS Promotion", {
					promotion_name: values.promotion_name,
					promotion_type: PROMOTION_TYPE_VALUES[values.promotion_type] || "item_discount",
					display_label: values.display_label || values.promotion_name,
					discount_type: DISCOUNT_TYPE_VALUES[values.discount_type] || "percentage",
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
		const dialog = new frappe.ui.Dialog({ title: recipe.name ? __("Change a saved recipe") : __("Enter a recipe step by step"), fields: [
			{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("Measure every amount instead of guessing. JiJi converts counting units, such as grams and kilograms, and shows the ingredient cost before you save. An item made in advance must already have an approved recipe showing what goes into it.")}</p>` },
			{ fieldtype: "Data", fieldname: "recipe_name", label: __("Recipe name"), reqd: 1, default: recipe.recipe_name || "" },
			{ fieldtype: "Data", fieldname: "recipe_code", label: __("Recipe code (optional)"), default: recipe.recipe_code || "" },
			{ fieldtype: "Select", fieldname: "recipe_type", label: __("What does this recipe make?"), options: Object.values(RECIPE_TYPE_LABELS).join("\n"), default: RECIPE_TYPE_LABELS[recipe.recipe_type] || RECIPE_TYPE_LABELS["Finished Drink"] },
			{ fieldtype: "Float", fieldname: "yield_qty", label: __("How much the full recipe makes"), description: __("This is the total usable amount after preparation."), reqd: 1, default: recipe.yield_qty || 1 },
			{ fieldtype: "Link", fieldname: "yield_uom", options: "UOM", label: __("Unit used for the amount made"), description: __("For example: Cup, Gram, or Millilitre. 'Nos' means a count of whole items, such as one cup or one bottle."), reqd: 1, default: recipe.yield_uom || "" },
			{ fieldtype: "Float", fieldname: "default_serving_qty", label: __("Amount in one serving"), reqd: 1, default: recipe.default_serving_qty || 1 },
			{ fieldtype: "Link", fieldname: "default_serving_uom", options: "UOM", label: __("Unit used for one serving"), description: __("Use the unit staff understand when making one order. 'Nos' means one whole item."), reqd: 1, default: recipe.default_serving_uom || "" },
			{ fieldtype: "Datetime", fieldname: "effective_from", label: __("Start using this recipe on"), reqd: 1, default: recipe.effective_from || "" },
			{ fieldtype: "Datetime", fieldname: "effective_to", label: __("Stop using it on (optional)"), default: recipe.effective_to || "" },
			{ fieldtype: "Link", fieldname: "warehouse", options: "Warehouse", label: __("Stock location used to check cost (optional)"), default: editor.warehouse || "" },
			{ fieldtype: "HTML", fieldname: "components", options: `<hr><h5>${__("What goes into this recipe")}</h5><datalist id="jiji-menu-items">${itemOptions}</datalist><datalist id="jiji-menu-uoms">${uomOptions}</datalist><div class="table-responsive"><table class="table table-bordered table-sm mb-2"><thead><tr><th>${__("Ingredient or supply")}</th><th>${__("What is it?")}</th><th>${__("Amount")}</th><th>${__("Counting unit")}</th><th>${__("Normal waste %")}</th><th>${__("Reduce stock?")}</th><th>${__("Include in cost?")}</th><th></th></tr></thead><tbody data-guided-rows></tbody></table></div><button class="btn btn-sm btn-default" type="button" data-guided-add-row>${__("Add another ingredient or supply")}</button>` },
			{ fieldtype: "HTML", fieldname: "modifiers", options: `<hr><h5>${__("Customer choice groups")}</h5><div data-guided-modifiers></div><div class="small text-muted mt-2">${__("JiJi uses the latest approved amounts for each choice when this recipe is made available.")}</div>` },
			{ fieldtype: "HTML", fieldname: "preview", options: `<hr><h5>${__("Check amounts and cost")}</h5><div data-guided-preview class="small text-muted">${__("Add the ingredients and supplies, then run the check.")}</div>` },
		], primary_action_label: __("Save this recipe"), primary_action: (values) => this.saveGuidedDraft(dialog, editor, values), secondary_action_label: __("Check amounts and cost"), secondary_action: () => this.previewGuidedDraft(dialog, editor) });
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
		body.append(`<tr><td><input class="form-control form-control-sm" list="jiji-menu-items" data-guided-field="item" value="${this.escape(row.item || "")}" placeholder="${__("Search item")}"></td><td><select class="form-control form-control-sm" data-guided-field="component_type"><option value="Ingredient">${__("Ingredient")}</option><option value="Prep Item">${__("Item made in advance")}</option><option value="Packaging">${__("Packaging or supply")}</option><option value="Tool Usage">${__("Reusable tool")}</option></select></td><td><input class="form-control form-control-sm" type="number" min="0" step="any" data-guided-field="qty" value="${this.escape(row.qty || "")}"></td><td><input class="form-control form-control-sm" list="jiji-menu-uoms" data-guided-field="uom" value="${this.escape(row.uom || "")}" placeholder="${__("For example, Gram")}"></td><td><input class="form-control form-control-sm" type="number" min="0" step="any" data-guided-field="loss_factor_pct" value="${this.escape(row.loss_factor_pct || 0)}"></td><td class="text-center"><input type="checkbox" data-guided-field="affects_stock" ${defaultStock ? "checked" : ""}></td><td class="text-center"><input type="checkbox" data-guided-field="affects_cogs" ${defaultCogs ? "checked" : ""}></td><td><button class="btn btn-sm btn-link text-danger" type="button" data-guided-remove-row aria-label="${__("Remove")}">×</button></td></tr>`);
		const select = body.find("tr:last [data-guided-field=component_type]");
		select.val(row.component_type || "Ingredient");
	}

	renderGuidedModifiers(dialog, editor, selected) {
		const selectedNames = new Set((selected || []).map((row) => row.modifier_group));
		dialog.$wrapper.find("[data-guided-modifiers]").html((editor.modifier_groups || []).map((group) => `<label class="d-block mb-1"><input type="checkbox" class="mr-2" data-guided-modifier="${this.escape(group.name)}" ${selectedNames.has(group.name) ? "checked" : ""}>${this.escape(group.group_name || group.name)} <span class="text-muted">${this.escape(group.selection_type === "Multiple" ? __("Customers may choose more than one") : __("Customers choose one"))}</span></label>`).join("") || `<span class="text-muted">${__("No active customer choice groups found.")}</span>`);
	}

	readGuidedPayload(dialog, editor) {
		const values = dialog.get_values() || {};
		const components = dialog.$wrapper.find("[data-guided-rows] tr").map(function () { const row = $(this); return { item: row.find('[data-guided-field="item"]').val(), component_type: row.find('[data-guided-field="component_type"]').val(), qty: row.find('[data-guided-field="qty"]').val(), uom: row.find('[data-guided-field="uom"]').val(), loss_factor_pct: row.find('[data-guided-field="loss_factor_pct"]').val(), affects_stock: row.find('[data-guided-field="affects_stock"]').is(":checked") ? 1 : 0, affects_cogs: row.find('[data-guided-field="affects_cogs"]').is(":checked") ? 1 : 0 }; }).get();
		const modifier_groups = dialog.$wrapper.find("[data-guided-modifier]:checked").map(function () { return { modifier_group: $(this).attr("data-guided-modifier") }; }).get();
		const recipeType = Object.entries(RECIPE_TYPE_LABELS).find(([, label]) => label === values.recipe_type)?.[0] || "Finished Drink";
		return { recipe: editor.recipe?.name || "", company: editor.company, sellable_item: editor.sellable_item, warehouse: values.warehouse || editor.warehouse || "", ...values, recipe_type: recipeType, components, modifier_groups };
	}

	previewGuidedDraft(dialog, editor) {
		const payload = this.readGuidedPayload(dialog, editor);
		frappe.call({ method: "kopos_connector.api.menu_authoring.preview_menu_recipe", type: "POST", args: { payload: JSON.stringify(payload) }, callback: (response) => this.renderGuidedPreview(dialog, response.message || {}) });
	}

	renderGuidedPreview(dialog, response) {
		const preview = response.preview || {};
		const errors = (preview.errors || []).map((row) => `<div class="text-danger">${this.escape(row.message)}</div>`).join("");
		const warnings = (preview.warnings || []).map((row) => `<div class="text-warning">${this.escape(row.message)}</div>`).join("");
		const rows = (preview.components || []).map((row) => `${this.escape(row.item)}: ${this.escape(row.stock_qty_per_batch)} ${this.escape(row.stock_uom)} ${__("for the full recipe")} / ${this.escape(row.stock_qty_per_serving)} ${__("for one serving")}`).join("<br>");
		const prep = (preview.components || []).filter((row) => row.component_type === "Prep Item").map((row) => row.prepared_bom ? `${this.escape(row.item)} → ${this.escape(row.prepared_bom.name || __("prepared-item recipe"))}, ${this.escape(row.prepared_bom.batch_qty || "?")} ${__("made at a time")}` : `${this.escape(row.item)} → ${__("an approved prepared-item recipe is required")}`).join("<br>");
		const effects = (preview.modifier_effects || []).map((row) => `${this.escape(row.modifier_name || row.name)} (${this.escape(row.kind || __("ingredient change"))})`).join(", ");
		const cost = this.moneyLineHtml(preview, response.status === "ok");
		dialog.$wrapper.find("[data-guided-preview]").html(`<div class="mb-2"><strong class="${response.status === "ok" ? "text-success" : "text-danger"}">${response.status === "ok" ? __("Amounts and cost look ready") : __("Fix these details before saving")}</strong></div>${errors}${warnings}<div class="mt-2">${rows || __("No ingredients or supplies added")}</div>${prep ? `<div class="mt-2"><strong>${__("Items made in advance")}</strong><br>${prep}</div>` : ""}<div class="mt-3">${cost}</div><div class="mt-2 text-muted">${effects ? `${__("Changes from customer choices")}: ${effects}` : __("No customer choices selected")}</div>`);
	}

	/**
	 * Does this drink still make money?
	 *
	 * The preview used to answer "is the recipe valid", which nobody asks. The
	 * cost only means something beside the price the drink sells for, and both
	 * numbers are already computed -- the cost by the recipe preview, the price
	 * by the catalog the tablet charges from.
	 */
	moneyLineHtml(preview, valid) {
		const cost = preview.cost_per_serving_sen;
		if (cost == null) {
			return `<div class="text-muted">${__("JiJi cannot cost this yet — one ingredient has no current value.")}</div>`;
		}
		// A drink that costs nothing is an absence, not a fact. Nothing was
		// marked as counting towards cost, so there is no margin to report.
		if (!cost) {
			return `<div class="text-muted">${__("No ingredient on this recipe counts towards cost yet, so JiJi cannot say what it keeps.")}</div>`;
		}
		// An invalid recipe still has numbers; they just do not mean anything
		// yet, and a confident margin over a broken recipe is worse than none.
		if (!valid) {
			return `<div class="text-muted">${__("Fix the details above before these figures mean anything.")}
				<div>${__("Ingredients cost")} ${this.escape(this.formatSen(cost))}</div></div>`;
		}
		const price = preview.price_sen;
		const keeps = preview.keeps_sen;
		if (price == null) {
			return `<div><strong>${__("Ingredients cost")} ${this.escape(this.formatSen(cost))}</strong>
				<div class="text-muted small">${__("Set a selling price for this item to see what it keeps.")}</div></div>`;
		}
		// Selling below cost is not a rounding detail; it is the answer.
		const tone = keeps > 0 ? "text-success" : "text-danger";
		const verdict = keeps > 0
			? __("Keeps {0}", [this.formatSen(keeps)])
			: __("Loses {0} on every one sold", [this.formatSen(Math.abs(keeps))]);
		return `<div class="d-flex flex-wrap align-items-baseline gap-3">
			<span>${__("Sells for")} <strong>${this.escape(this.formatSen(price))}</strong></span>
			<span>${__("Ingredients cost")} <strong>${this.escape(this.formatSen(cost))}</strong></span>
			<span class="${tone}"><strong>${this.escape(verdict)}</strong></span>
		</div>`;
	}

	saveGuidedDraft(dialog, editor, values) {
		const payload = this.readGuidedPayload(dialog, editor);
		frappe.call({ method: "kopos_connector.api.menu_authoring.save_menu_recipe_draft", type: "POST", args: { payload: JSON.stringify(payload) }, callback: (response) => { const result = response.message || {}; if (result.status !== "ok") return this.showMessage(__("The recipe could not be saved. Nothing was changed. Try again.")); dialog.hide(); frappe.show_alert({ message: __("Recipe saved"), indicator: "green" }); this.refresh(); } });
	}

	openCopyDialog({ item, company }) {
		if (!company) return this.showMessage(__("Choose a company before copying a recipe."));
		const dialog = new frappe.ui.Dialog({ title: __("Copy or revise a recipe"), fields: [{ fieldtype: "Link", fieldname: "recipe", label: __("Recipe to copy"), options: "FB Recipe", reqd: 1, get_query: () => ({ filters: { company, ...(item ? { sellable_item: item } : {}) } }) }, { fieldtype: "Data", fieldname: "recipe_name", label: __("New recipe name") }, { fieldtype: "Data", fieldname: "recipe_code", label: __("New recipe code") }], primary_action_label: __("Save this recipe"), primary_action: (values) => frappe.call({ method: "kopos_connector.api.menu_authoring.copy_menu_recipe_revision", type: "POST", args: { payload: JSON.stringify(values) }, callback: (response) => { const result = response.message || {}; if (!result.recipe) return; dialog.hide(); frappe.show_alert({ message: __("Recipe saved"), indicator: "green" }); this.openGuidedRecipeDialog({ company, item, recipe: result.recipe }); this.refresh(); } }) });
		dialog.show();
	}

	publishSelected() {
		const selected = this.page.body.find("[data-publish-recipe]:checked").map(function () { return $(this).attr("data-publish-recipe"); }).get();
		if (!selected.length) return this.showMessage(__("Select at least one recipe marked ready for final review."));
		frappe.call({ method: "kopos_connector.api.menu_authoring.publish_menu_recipe_selection", type: "POST", args: { payload: JSON.stringify({ recipes: selected }) }, callback: (response) => { const result = response.message || {}; if (result.status === "ok") { frappe.show_alert({ message: __(`${result.count || 0} recipe(s) made on sale`), indicator: "green" }); this.refresh(); } }, error: () => this.showMessage(__("No recipes were made available. Fix every selected recipe first.")) });
	}

	previewCatalog() {
		const posProfile = this.catalogProfileControl?.get_value?.() || "";
		if (!posProfile) return this.showMessage(__("Choose an outlet first."));
		const output = this.page.body.find("[data-menu-catalog-results]").removeClass("d-none").text(__("Checking what this outlet will see…"));
		frappe.call({ method: "kopos_connector.api.inventory.get_menu_catalog_preview", type: "GET", args: { pos_profile: posProfile }, callback: (response) => { const preview = response.message || {}; if (preview.status !== "ok") return output.text(preview.reason || __("JiJi could not check what this outlet will see.")); const missing = (preview.missing_recipe_items || []).length ? `\n${__("Items that still need a recipe or a no-recipe reason")}: ${(preview.missing_recipe_items || []).join(", ")}` : ""; output.text(`${preview.saleable_items || 0} ${__("menu items")} · ${preview.recipe_ready_items || 0} ${__("with an approved recipe")} · ${preview.explicit_exclusions || 0} ${__("that do not need a recipe")} · ${preview.modifier_groups || 0} ${__("customer choice groups")}${missing}`); }, error: () => output.text(__("JiJi could not check what this outlet will see.")) });
	}

	checkPromotionEconomics() {
		const promotion = this.promotionControl?.get_value?.() || "";
		if (!promotion) return this.showMessage(__("Choose a promotion first."));
		const scenarios = {};
		for (const [label, selector] of [["low", "[data-promotion-low]"], ["base", "[data-promotion-base]"], ["high", "[data-promotion-high]"]]) { const value = Number.parseInt(this.page.body.find(selector).val(), 10); if (!Number.isInteger(value) || value <= 0) return this.showMessage(__("Enter a whole number greater than zero for all three sales cases.")); scenarios[label] = value; }
		this.page.body.find("[data-promotion-status]").text(__("Checking prices, recipes, and ingredient costs…"));
		frappe.call({ method: "kopos_connector.api.inventory.get_promotion_economics", type: "POST", args: { payload: JSON.stringify({ promotion, pos_profile: this.profileControl?.get_value?.() || "", scenarios }) }, callback: (response) => this.renderPromotionEconomics(response.message || {}), error: () => this.renderPromotionEconomics({ status: "blocked", reason: __("JiJi could not calculate the offer figures. Try again.") }) });
	}

	renderPromotionEconomics(response) {
		const output = this.page.body.find("[data-promotion-results]").removeClass("d-none small alert alert-success alert-danger").css("white-space", "normal");
		if (response.status !== "ok" || !response.economics) {
			this.page.body.find("[data-promotion-status]").text(__("Not ready to turn on"));
			output.addClass("alert alert-danger").text(response.reason || __("Some recipe details, ingredient costs, or recent stock costs are missing."));
			return;
		}

		const economics = response.economics;
		const publicationBlocked = response.publication_status === "Blocked" || Number(economics.gross_profit_sen) < 0;
		const statusLabel = publicationBlocked ? __("This offer loses money — another director must review it") : __("Figures are ready — a director must still approve the offer");
		const statusTone = publicationBlocked ? "danger" : "success";
		this.page.body.find("[data-promotion-status]").text(statusLabel);
		output.addClass(`alert alert-${statusTone}`).html(`
			<div class="d-flex flex-wrap justify-content-between align-items-start mb-3">
					<strong>${this.escape(__("Offer check"))}</strong>
				<span class="badge badge-${statusTone}">${this.escape(statusLabel)}</span>
			</div>
				${publicationBlocked ? `<div class="alert alert-danger small mb-3"><strong>${this.escape(__("JiJi will not turn on this offer yet."))}</strong> ${this.escape(__("A second Company Director must approve it once and record a reason. Running this check does not turn on the offer."))}</div>` : `<div class="alert alert-info small mb-3">${this.escape(__("A director must still approve the offer. These examples help with planning; they do not promise extra sales or order stock."))}</div>`}
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
			[__("Sales after discount, before tax"), this.formatSen(economics.net_revenue_sen ?? economics.revenue_sen)],
			[__("Tax collected"), this.formatSen(economics.tax_sen)],
			[__("Ingredient cost"), this.formatSen(economics.cogs_sen)],
			[__("Money left after ingredient cost"), this.formatSen(economics.gross_profit_sen)],
			[__("Percent left after ingredient cost"), this.formatPercent(economics.margin_percent)],
			[__("Change in that percent"), this.formatPercent(economics.margin_change_percent)],
			[__("Extra items to sell to recover the discount"), economics.break_even_additional_units == null ? __("Not available") : `${this.escape(economics.break_even_additional_units)} ${__("items")}`],
		];
		const worstGroup = economics.worst_affected_item_group;
		return `<section aria-labelledby="jiji-promotion-summary-heading" class="mb-3"><h5 id="jiji-promotion-summary-heading" class="mb-2">${this.escape(__("Offer summary"))}</h5><div class="row">${metrics.map(([label, value]) => `<div class="col-sm-6 col-lg-4 mb-2"><div class="border rounded p-2 h-100"><div class="small text-muted">${this.escape(label)}</div><strong>${this.escape(value)}</strong></div></div>`).join("")}</div><div class="small mt-1">${worstGroup ? `${this.escape(__("Menu group with the highest ingredient cost"))}: <strong>${this.escape(worstGroup.item_group || __("No group"))}</strong> · ${this.escape(this.formatSen(worstGroup.cogs_sen))}` : this.escape(__("Menu group with the highest ingredient cost: Not available"))}</div></section>`;
	}

	renderPromotionScenarios(economics) {
		const rows = Array.isArray(economics.scenarios) ? economics.scenarios : [];
		if (!rows.length) return `<details class="mb-3"><summary><strong>${this.escape(__("Sales examples"))}</strong> <span class="text-muted">${this.escape(__("Not entered"))}</span></summary><p class="small text-muted mt-2 mb-0">${this.escape(__("Enter low, expected, and high sales above, then run the check."))}</p></details>`;
		const scenarioRows = rows.map((row) => {
			const revenue = row.net_revenue_sen ?? row.revenue_sen;
			const margin = Number(revenue) > 0 && Number.isFinite(Number(revenue)) ? `${((Number(row.gross_profit_sen || 0) / Number(revenue)) * 100).toFixed(2)}%` : __("Not available");
			return `<tr><td>${this.escape(row.label)}</td><td>${this.escape(row.units)}</td><td>${this.escape(this.formatSen(revenue))}</td><td>${this.escape(this.formatSen(row.cogs_sen))}</td><td>${this.escape(this.formatSen(row.gross_profit_sen))}</td><td>${this.escape(margin)}</td></tr>`;
		}).join("");
		return `<details open class="mb-3"><summary><strong>${this.escape(__("Sales examples"))}</strong> <span class="text-muted">${this.escape(__("for planning only"))}</span></summary><div class="table-responsive mt-2"><table class="table table-bordered table-sm mb-0"><thead><tr><th>${this.escape(__("Sales case"))}</th><th>${this.escape(__("Items sold"))}</th><th>${this.escape(__("Sales before tax"))}</th><th>${this.escape(__("Ingredient cost"))}</th><th>${this.escape(__("Money left"))}</th><th>${this.escape(__("Percent left"))}</th></tr></thead><tbody>${scenarioRows}</tbody></table></div></details>`;
	}

	renderPromotionDemand(economics) {
		const rows = Object.entries(economics.ingredient_demand || {});
		const body = rows.length ? rows.map(([item, quantity]) => `<tr><td>${this.escape(item)}</td><td>${this.escape(quantity)}</td></tr>`).join("") : `<tr><td colspan="2" class="text-muted">${this.escape(__("No recipe ingredients"))}</td></tr>`;
		return `<details open class="mb-3"><summary><strong>${this.escape(__("Stock needed"))}</strong> <span class="text-muted">${this.escape(__("for the expected sales entered above"))}</span></summary><div class="table-responsive mt-2"><table class="table table-bordered table-sm mb-0"><thead><tr><th>${this.escape(__("Ingredient or supply"))}</th><th>${this.escape(__("Amount needed"))}</th></tr></thead><tbody>${body}</tbody></table></div></details>`;
	}

	renderPromotionBatchImpact(economics) {
		const impact = economics.batch_preparation_impact || {};
		if (impact.status === "not_applicable") return `<details class="mb-3"><summary><strong>${this.escape(__("Items to prepare in advance"))}</strong> <span class="text-muted">${this.escape(__("Not needed"))}</span></summary><p class="small text-muted mt-2 mb-0">${this.escape(__("This offer does not use anything made in advance."))}</p></details>`;
		const components = Array.isArray(impact.components) ? impact.components : [];
		if (!components.length) return `<details class="mb-3"><summary><strong>${this.escape(__("Items to prepare in advance"))}</strong> <span class="text-warning">${this.escape(__("Not available"))}</span></summary><p class="small text-warning mt-2 mb-0">${this.escape(__("The prepared item or its ingredient recipe is missing."))}</p></details>`;
		const scenarioLabels = (Array.isArray(economics.scenarios) ? economics.scenarios : []).map((scenario) => String(scenario.label));
		const rows = components.map((row) => {
			if (row.status !== "available") return `<tr><td>${this.escape(row.item)}</td><td colspan="5" class="text-warning">${this.escape(row.reason || __("Preparation details are unavailable"))}</td></tr>`;
			const scenarioBatches = scenarioLabels.map((label) => `${this.escape(label)}: ${this.escape(row.scenario_batches?.[label] ?? __("Not available"))}`).join(" · ");
			return `<tr><td>${this.escape(row.item)}</td><td>${this.escape(row.base_demand)}</td><td>${this.escape(row.batch_qty)}</td><td>${this.escape(row.batches_required)}</td><td>${this.escape(scenarioBatches || __("Not entered"))}</td><td>${this.escape(row.lead_minutes ?? __("Not set"))}</td></tr>`;
		}).join("");
		const hasUnavailable = components.some((row) => row.status !== "available");
		return `<details open class="mb-3"><summary><strong>${this.escape(__("Items to prepare in advance"))}</strong> <span class="${hasUnavailable ? "text-warning" : "text-success"}">${this.escape(hasUnavailable ? __("Check the missing details") : __("Calculated"))}</span></summary><div class="table-responsive mt-2"><table class="table table-bordered table-sm mb-0"><thead><tr><th>${this.escape(__("Prepared item"))}</th><th>${this.escape(__("Amount needed for expected sales"))}</th><th>${this.escape(__("Amount made each time"))}</th><th>${this.escape(__("Times to prepare"))}</th><th>${this.escape(__("Times to prepare for each sales case"))}</th><th>${this.escape(__("Minutes needed"))}</th></tr></thead><tbody>${rows}</tbody></table></div><p class="small text-muted mt-2 mb-0">${this.escape(__("Staff use the preparation task to make these items. This report does not start the work automatically."))}</p></details>`;
	}

	renderPromotionRisk(economics) {
		const risk = economics.runout_waste_risk || {};
		if (risk.status !== "available") return `<details open class="mb-3"><summary><strong>${this.escape(__("Risk of selling out or wasting stock"))}</strong> <span class="text-warning">${this.escape(__("Not available"))}</span></summary><p class="small text-warning mt-2 mb-0">${this.escape(risk.reason || __("Some current stock amounts or use-by dates are missing."))}</p></details>`;
		const riskRows = Array.isArray(risk.items) ? risk.items : [];
		const rows = riskRows.map((row) => `<tr><td>${this.escape(row.item)}</td><td><span class="${row.runout === "risk" ? "text-danger" : "text-success"}">${this.escape(this.promotionRiskLabel(row.runout))}</span></td><td><span class="${row.waste === "risk" ? "text-warning" : row.waste === "no_waste_evidence" ? "text-success" : "text-muted"}">${this.escape(this.promotionRiskLabel(row.waste))}</span></td><td class="text-muted">${this.escape(row.reason || __("Based on current stock amounts and use-by dates"))}</td></tr>`).join("");
		return `<details open class="mb-3"><summary><strong>${this.escape(__("Risk of selling out or wasting stock"))}</strong> <span class="text-muted">${this.escape(__("using current stock information"))}</span></summary><div class="table-responsive mt-2"><table class="table table-bordered table-sm mb-0"><thead><tr><th>${this.escape(__("Ingredient or prepared item"))}</th><th>${this.escape(__("Could sell out"))}</th><th>${this.escape(__("Could be wasted"))}</th><th>${this.escape(__("Why"))}</th></tr></thead><tbody>${rows || `<tr><td colspan="4" class="text-muted">${this.escape(__("No stock risks were returned."))}</td></tr>`}</tbody></table></div></details>`;
	}

	renderPromotionActualResults(economics) {
		const actual = economics.actual_results || {};
		const available = actual.status === "available";
		const notes = [actual.note, actual.reason, actual.tax_reason].filter(Boolean).join(" ");
		if (!available) return `<details class="mb-0"><summary><strong>${this.escape(__("Results after the offer has run"))}</strong> <span class="text-warning">${this.escape(__("Not available yet"))}</span></summary><p class="small text-warning mt-2 mb-0">${this.escape(notes || __("There are not yet enough completed offer sales with ingredient costs to show results."))}</p></details>`;
		return `<details open class="mb-0"><summary><strong>${this.escape(__("Results after the offer has run"))}</strong> <span class="text-success">${this.escape(__("Available"))}</span></summary><div class="row mt-2"><div class="col-sm-6 col-lg-3 mb-2"><div class="small text-muted">${this.escape(__("Orders"))}</div><strong>${this.escape(actual.order_count ?? __("Not available"))}</strong></div><div class="col-sm-6 col-lg-3 mb-2"><div class="small text-muted">${this.escape(__("Discounted items sold"))}</div><strong>${this.escape(actual.promoted_units ?? __("Not available"))}</strong></div><div class="col-sm-6 col-lg-3 mb-2"><div class="small text-muted">${this.escape(__("Sales after discount, before tax"))}</div><strong>${this.escape(this.formatSen(actual.net_revenue_sen))}</strong></div><div class="col-sm-6 col-lg-3 mb-2"><div class="small text-muted">${this.escape(__("Ingredient cost"))}</div><strong>${this.escape(this.formatSen(actual.cogs_sen))}</strong></div></div><div class="small">${this.escape(__("Money left after ingredient cost"))}: <strong>${this.escape(this.formatSen(actual.gross_profit_sen))}</strong> · ${this.escape(__("Percent left"))}: <strong>${this.escape(this.formatPercent(actual.margin_percent))}</strong></div><div class="small text-muted mt-2">${this.escape(actual.cogs_source || __("Recent ingredient cost details are not available"))}${notes ? ` · ${this.escape(notes)}` : ""}</div></details>`;
	}

	promotionRiskLabel(value) {
		const labels = { risk: __("Yes — check this"), no_immediate_runout: __("No immediate risk"), no_waste_evidence: __("No waste risk found"), not_available: __("Not available") };
		return labels[value] || __("Not available");
	}

	downloadTemplate() {
		frappe.call({ method: "kopos_connector.api.inventory.get_menu_recipe_csv_template", type: "GET", callback: (response) => { const template = response.message || {}; const blob = new Blob([template.content || ""], { type: "text/csv;charset=utf-8" }); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = template.filename || "jiji-recipe-components-template.csv"; link.click(); URL.revokeObjectURL(link.href); this.page.body.find("[data-csv-status]").text(__("Template downloaded")); } });
	}

	validateCsv() {
		const csvText = this.page.body.find("[data-csv-input]").val() || "";
		if (!String(csvText).trim()) return this.showMessage(__("Paste the spreadsheet rows first."));
		frappe.call({ method: "kopos_connector.api.inventory.validate_menu_recipe_csv", type: "POST", args: { csv_text: csvText }, callback: (response) => { const result = response.message || {}; const errors = result.errors || []; const output = errors.length ? errors.map((error) => `${__("Row")} ${error.row}: ${error.message}`).join("\n") : __(`${result.recipe_count || 0} recipe(s) passed the check. Use the step-by-step recipe form to save a draft.`); this.page.body.find("[data-csv-results]").removeClass("d-none").text(output); this.page.body.find("[data-csv-status]").text(result.valid ? __("Rows look complete") : `${errors.length} ${__("row issue(s)")}`); } });
	}

	countLabel(value, singular, plural) { const count = Number(value || 0); return `${count} ${count === 1 ? singular : plural}`; }
	formatSen(value) { const number = Number(value); return value == null || value === "" || !Number.isFinite(number) ? __("Unavailable") : `RM ${(number / 100).toFixed(2)}`; }
	formatPercent(value) { return value == null || value === "" || !Number.isFinite(Number(value)) ? __("Not available") : `${Number(value).toFixed(2)}%`; }
	showMessage(message) { this.page.body.find("[data-guided-editor-status]").text(message); }
	escape(value) { return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[character]); }
}
