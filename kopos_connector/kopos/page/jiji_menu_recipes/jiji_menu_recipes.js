frappe.pages["jiji_menu_recipes"].on_page_load = function (wrapper) {
	new JiJiMenuRecipesPage(wrapper);
};

class JiJiMenuRecipesPage {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Menu"),
			single_column: true,
		});
		jiji.desk.injectStyle();
		$(wrapper).addClass("jiji-desk");
		this.menuGroupChoices = [];
		this.render();
		this.bindEvents();
		this.setupControls();
		this.refreshChoices();
	}

	render() {
		$(this.page.body).html(`
			<div class="jiji-menu-page">
				<div class="card mb-4"><div class="card-body">
					<div class="d-flex flex-wrap justify-content-between align-items-start mb-3"><div><h3 class="mb-1">${__("Products")}</h3><p class="text-muted mb-0">${__("Add a product once. JiJi uses what goes into it to manage stock automatically.")}</p></div><button class="btn btn-primary" type="button" data-add-product>${__("Add product")}</button></div>
					<div class="row mb-3">
						<div class="col-md-4"><label class="control-label" for="jiji-menu-cafe">${__("Your café")}</label><select class="form-control" id="jiji-menu-cafe" data-company-select><option value="">${__("Choose your café")}</option></select></div>
						<div class="col-md-4"><label class="control-label" for="jiji-menu-product">${__("Product")}</label><select class="form-control" id="jiji-menu-product" data-menu-item-select><option value="">${__("Choose a product")}</option></select></div>
						<div class="col-md-4"><label class="control-label" for="jiji-menu-outlet">${__("Outlet")}</label><select class="form-control" id="jiji-menu-outlet" data-catalog-profile-select><option value="">${__("Choose an outlet")}</option></select></div>
					</div>
					<div class="d-flex flex-wrap align-items-center gap-2"><button class="btn btn-sm btn-primary" type="button" data-menu-commission="recipe" disabled>${__("Edit ingredients and choices")}</button><button class="btn btn-sm btn-default" type="button" data-menu-catalog-preview disabled>${__("Check the counter")}</button></div>
					<p class="text-muted small mt-2 mb-0" data-sequence-waiting></p>
					<div data-drink class="mt-4"></div>
					<div class="jiji-card mt-3 d-none" data-menu-catalog-results></div><div class="small mt-2" data-guided-editor-status></div>
				</div></div>
				<div class="card mb-4"><div class="card-body"><div class="d-flex flex-wrap justify-content-between align-items-center mb-2"><div><h4 class="mb-1">${__("Ready to publish")}</h4><p class="text-muted mb-0">${__("Publishing makes the selected products available to staff.")}</p></div><button class="btn btn-sm btn-primary" type="button" data-publish-selected disabled>${__("Publish selected")}</button></div><div data-publish-queue class="small text-muted">${__("Choose a café to see saved products.")}</div></div></div>
			</div>`);
	}

	bindEvents() {
		this.page.body.find("[data-add-product]").on("click", () => this.openGuidedItemDialog());
		this.page.body.find("[data-menu-commission]").on("click", (event) => this.runCommissioningAction($(event.currentTarget).attr("data-menu-commission")));
		this.page.body.find("[data-menu-catalog-preview]").on("click", () => this.previewCatalog());
		this.page.body.find("[data-publish-selected]").on("click", () => this.publishSelected());
		this.page.body.find("[data-company-select]").on("change", () => this.refreshChoices());
		this.page.body.find("[data-menu-item-select], [data-catalog-profile-select]").on("change", () => this.onSelectionChanged());
	}

	setupControls() {
		this.companyControl = this.selectControl("[data-company-select]");
		this.menuItemControl = this.selectControl("[data-menu-item-select]");
		this.catalogProfileControl = this.selectControl("[data-catalog-profile-select]");
		this.updateActionState();
	}

	selectControl(selector) {
		const input = this.page.body.find(selector);
		return {
			$input: input,
			get_value: () => String(input.val() || ""),
			set_value: (value) => {
				input.val(value || "").trigger("change");
				return Promise.resolve();
			},
		};
	}

	setSelectOptions(selector, rows, placeholder, selected) {
		const input = this.page.body.find(selector);
		const options = (rows || []).map((row) => `<option value="${this.escape(row.value)}">${this.escape(row.label)}</option>`).join("");
		input.html(`<option value="">${this.escape(placeholder)}</option>${options}`);
		if ((rows || []).some((row) => row.value === selected)) input.val(selected);
	}

	refreshChoices() {
		if (this.loadingChoices) return;
		this.loadingChoices = true;
		const requestedCompany = this.companyControl?.get_value?.() || "";
		const selectedProduct = this.menuItemControl?.get_value?.() || "";
		const selectedOutlet = this.catalogProfileControl?.get_value?.() || "";
		frappe.call({
			method: "kopos_connector.api.menu_authoring.get_menu_page_choices",
			type: "GET",
			args: { company: requestedCompany },
			callback: (response) => {
				const choices = response.message || {};
				const company = requestedCompany || choices.selected_company || "";
				this.setSelectOptions("[data-company-select]", choices.cafes, __("Choose your café"), company);
				this.setSelectOptions("[data-menu-item-select]", choices.products, __("Choose a product"), selectedProduct);
				this.setSelectOptions("[data-catalog-profile-select]", choices.outlets, __("Choose an outlet"), selectedOutlet);
				this.menuGroupChoices = choices.menu_groups || [];
				this.loadingChoices = false;
				this.updateActionState();
				this.loadDrink();
				this.refresh();
			},
			error: () => {
				this.loadingChoices = false;
				this.showMessage(__("JiJi could not load your products. Try again."));
				this.page.set_indicator(__("Needs attention"), "red");
			},
		});
	}

	onSelectionChanged() {
		this.updateActionState();
		this.loadDrink();
		this.refreshPublishQueue();
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
		const name = (data.item || {}).item_name || __("Product");
		const lines = (preview.components || []).filter((row) => row.item);

		const card = $('<div class="jiji-card"></div>').appendTo(target);
		const head = $('<div class="jiji-head"></div>').appendTo(card);
		head.append(
			$("<span>")
				.addClass("jiji-drink__title")
				.text(lines.length ? __("What goes into a {0}?", [name]) : __("{0} has no recipe yet", [name])),
		);
		if (recipe.status) {
			head.append(jiji.desk.pill(
				recipe.status === "Active" ? __("Available to staff") : __("Not published yet"),
				recipe.status === "Active" ? "ok" : "",
			));
		}
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
				row.append($("<span>").addClass("jiji-row__main").text(line.item_name || __("Ingredient or supply")));
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
		this.page.body.find('[data-menu-commission="recipe"]').prop("disabled", !companyAndItem);
		this.page.body.find("[data-menu-catalog-preview]").prop("disabled", !profile);

		// A greyed step that never says why is the same as a broken one. Name
		// what the sequence is waiting for, in the order it needs it.
		let waiting = "";
		if (!company) waiting = __("Choose your café first.");
		else if (!item) waiting = __("Choose a product, or add a new one.");
		this.page.body.find("[data-sequence-waiting]").text(waiting);
	}

	refresh() {
		frappe.call({ method: "kopos_connector.api.inventory.get_menu_authoring_summary", type: "GET", args: { company: this.companyControl?.get_value?.() || "" }, callback: (response) => {
			const summary = response.message || {};
			const hasCompany = Boolean(this.companyControl?.get_value?.());
			this.page.set_indicator(
				hasCompany && summary.ready ? __("Ready") : __("Needs you"),
				hasCompany && summary.ready ? "green" : "orange",
			);
			this.refreshPublishQueue();
		}, error: () => { this.showMessage(__("JiJi could not check the menu. Try again.")); this.page.set_indicator(__("Needs attention"), "red"); } });
	}

	refreshPublishQueue() {
		const company = this.companyControl?.get_value?.() || "";
		const output = this.page.body.find("[data-publish-queue]");
		if (!company) { output.text(__("Choose a café to see saved products.")); this.page.body.find("[data-publish-selected]").prop("disabled", true); return; }
		frappe.call({ method: "kopos_connector.api.menu_authoring.get_menu_recipe_publish_queue", type: "GET", args: { company }, callback: (response) => {
			const recipes = response.message?.recipes || [];
			if (!recipes.length) { output.text(__("Nothing is waiting to be published.")); this.page.body.find("[data-publish-selected]").prop("disabled", true); return; }
			output.html(recipes.map((row) => `<label class="d-flex align-items-start mb-2"><input class="mr-2 mt-1" type="checkbox" data-publish-recipe="${this.escape(row.name)}" ${row.valid ? "" : "disabled"}><span><strong>${this.escape(row.product_name || __("Product"))}</strong> <span class="text-muted">· ${row.component_count || 0} ${__("ingredients or supplies")}</span><br><span class="${row.valid ? "text-success" : "text-danger"}">${row.valid ? __("Ready to publish") : this.escape((row.errors || []).join("; "))}</span></span></label>`).join(""));
			this.page.body.find("[data-publish-selected]").prop("disabled", !output.find("[data-publish-recipe]").length);
		}, error: () => { output.text(__("JiJi could not load your saved products. Try again.")); this.page.body.find("[data-publish-selected]").prop("disabled", true); } });
	}

	runCommissioningAction(action) {
		const item = this.menuItemControl?.get_value?.() || "";
		const company = this.companyControl?.get_value?.() || "";
		if (action === "recipe") { if (!item || !company) return this.showMessage(__("Choose a café and something on the menu first.")); return this.openGuidedRecipeDialog({ item, company }); }
	}

	openGuidedRecipeDialog({ item, company, recipe } = {}) {
		item = item || this.menuItemControl?.get_value?.() || "";
		company = company || this.companyControl?.get_value?.() || "";
		if (!item || !company) return this.showMessage(__("Choose a café and something on the menu first."));
		frappe.call({ method: "kopos_connector.api.menu_authoring.get_menu_recipe_editor", type: "GET", args: { company, sellable_item: item, recipe: recipe || "", warehouse: "" }, callback: (response) => {
			const editor = response.message || {};
			if (editor.status !== "ok") return this.showMessage(__("JiJi could not open this product. Try again."));
			if (editor.recipe?.name && editor.recipe?.status !== "Draft") {
				return frappe.call({
					method: "kopos_connector.api.menu_authoring.copy_menu_recipe_revision",
					type: "POST",
					args: { payload: JSON.stringify({ recipe: editor.recipe.name }) },
					callback: (copyResponse) => {
						const draft = copyResponse.message?.recipe;
						if (!draft) return this.showMessage(__("JiJi could not start the change."));
						this.openGuidedRecipeDialog({ company, item, recipe: draft });
					},
					error: () => this.showMessage(__("JiJi could not start the change.")),
				});
			}
			this.showGuidedEditor(editor);
		}, error: () => this.showMessage(__("JiJi could not open this product. Try again.")) });
	}

	openGuidedItemDialog() {
		const company = this.companyControl?.get_value?.() || "";
		if (!company) return this.showMessage(__("Choose a café before adding something to the menu."));
		if (!this.menuGroupChoices.length) return this.showMessage(__("Ask Support to add a menu group first."));
		const menuGroupOptions = this.menuGroupChoices.map((group) => `<option value="${this.escape(group.value)}">${this.escape(group.label)}</option>`).join("");
		const saleModes = {
			"Made when ordered": "Sellable Drink",
			"Bought ready to sell": "Packaged Finished Good",
			"Does not use stock": "Non-Stock Service",
		};
		const dialog = new frappe.ui.Dialog({
			title: __("Add product"),
			fields: [
				{
					fieldtype: "Data",
					fieldname: "item_name",
					label: __("Product name"),
					description: __("Use the name staff say at the counter, for example Matcha Latte."),
					reqd: 1,
				},
				{ fieldtype: "HTML", fieldname: "menu_group_picker", options: `<label class="control-label">${__("Where should staff find it?")}</label><select class="form-control" data-new-menu-group><option value="">${__("Choose one")}</option>${menuGroupOptions}</select>` },
				{ fieldtype: "Data", fieldname: "price", label: __("Price (RM)"), description: __("For example: 12.90"), reqd: 1 },
				{ fieldtype: "Select", fieldname: "sale_mode", label: __("How is it sold?"), options: Object.keys(saleModes).join("\n"), reqd: 1, default: "Made when ordered" },
				{ fieldtype: "Attach Image", fieldname: "image", label: __("Photo (optional)") },
			],
			primary_action_label: __("Continue"),
			primary_action: (values) => {
				const menuGroup = dialog.$wrapper.find("[data-new-menu-group]").val() || "";
				if (!menuGroup) return this.showMessage(__("Choose where staff should find this product."));
				const priceSen = this.moneyToSen(values.price);
				if (priceSen == null) return this.showMessage(__("Enter the price like 12.90."));
				frappe.call({
				method: "kopos_connector.api.menu_authoring.create_menu_item_draft",
				type: "POST",
				args: { payload: JSON.stringify({ item_name: values.item_name, item_group: menuGroup, image: values.image || "", item_role: saleModes[values.sale_mode] || "Sellable Drink", price_sen: priceSen, company }) },
				callback: (response) => {
					const result = response.message || {};
					if (result.status !== "ok" || !result.item) return this.showMessage(__("The product could not be saved."));
					dialog.hide();
					this.menuItemControl.set_value(result.item);
					if (result.item_role === "Sellable Drink") {
						frappe.show_alert({ message: __("Product saved. Now add what goes into one."), indicator: "green" });
						return this.openGuidedRecipeDialog({ company, item: result.item });
					}
					this.publishSimpleProduct(company, result.item);
				},
				error: () => this.showMessage(__("The product could not be saved. Check the details and try again.")),
				});
			},
		});
		dialog.show();
	}

	publishSimpleProduct(company, item) {
		frappe.call({
			method: "kopos_connector.api.menu_authoring.publish_product_without_recipe",
			type: "POST",
			args: { company, item },
			callback: (response) => {
				if (response.message?.status !== "ok") return this.showMessage(__("The product was saved but is not available to staff yet."));
				frappe.show_alert({ message: __("Product published"), indicator: "green" });
				this.refresh();
			},
			error: () => this.showMessage(__("The product was saved but is not available to staff yet. Try publishing it again.")),
		});
	}

	showGuidedEditor(editor) {
		const recipe = editor.recipe || {};
		this.guidedEditorItems = editor.items || [];
		this.guidedEditorUnits = editor.units || [];
		const productName = editor.item?.item_name || __("product");
		const dialog = new frappe.ui.Dialog({ title: __("What goes into one {0}?", [productName]), size: "extra-large", fields: [
			{ fieldtype: "HTML", fieldname: "help", options: `<p class="text-muted">${__("Add each ingredient or supply and the amount used for one order. JiJi works out stock and cost automatically.")}</p>` },
			{ fieldtype: "HTML", fieldname: "components", options: `<div class="table-responsive"><table class="table table-bordered table-sm mb-2"><thead><tr><th style="min-width:220px">${__("Ingredient or supply")}</th><th style="width:120px">${__("Amount")}</th><th style="min-width:150px">${__("Measured in")}</th><th style="width:48px"></th></tr></thead><tbody data-guided-rows></tbody></table></div><div class="d-flex flex-wrap gap-2"><button class="btn btn-sm btn-default" type="button" data-guided-add-row>${__("Add another row")}</button><button class="btn btn-sm btn-link" type="button" data-guided-add-stock>${__("Add a new ingredient or supply")}</button></div>` },
			{ fieldtype: "HTML", fieldname: "modifiers", options: `<hr><h5>${__("Customer choices")}</h5><p class="small text-muted">${__("Only add choices customers can ask for, such as oat milk or extra matcha.")}</p><div data-guided-modifiers></div><button class="btn btn-sm btn-default mt-2" type="button" data-guided-add-choice>${__("Add customer choice")}</button>` },
			{ fieldtype: "HTML", fieldname: "preview", options: `<hr><h5>${__("Check before saving")}</h5><div data-guided-preview class="small text-muted">${__("Check the amounts to see what JiJi will track.")}</div>` },
		], primary_action_label: __("Save product"), primary_action: () => this.saveGuidedDraft(dialog, editor), secondary_action_label: __("Check amounts"), secondary_action: () => this.previewGuidedDraft(dialog, editor) });
		dialog.show();
		this.renderGuidedRows(dialog, recipe.components || []);
		this.renderGuidedModifiers(dialog, editor, recipe.allowed_modifier_groups || []);
		dialog.$wrapper.on("click", "[data-guided-add-row]", () => this.addGuidedRow(dialog));
		dialog.$wrapper.on("click", "[data-guided-add-stock]", () => this.openGuidedStockItemDialog(dialog, editor));
		dialog.$wrapper.on("click", "[data-guided-add-choice]", () => {
			this.saveGuidedDraft(dialog, editor, (result) => this.openGuidedChoiceDialog({
				company: editor.company,
				item: editor.sellable_item,
				recipe: result.recipe,
			}));
		});
		dialog.$wrapper.on("click", "[data-guided-remove-row]", (event) => { $(event.currentTarget).closest("tr").remove(); });
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
		const itemOptions = this.guidedEditorItems.map((item) => `<option value="${this.escape(item.name)}" ${item.name === row.item ? "selected" : ""}>${this.escape(item.item_name || __("Ingredient or supply"))}</option>`).join("");
		const unitOptions = this.guidedEditorUnits.map((unit) => `<option value="${this.escape(unit.value)}" ${unit.value === row.uom ? "selected" : ""}>${this.escape(unit.label)}</option>`).join("");
		body.append(`<tr><td><select class="form-control form-control-sm" data-guided-field="item"><option value="">${__("Choose one")}</option>${itemOptions}</select></td><td><input class="form-control form-control-sm" type="number" min="0" step="any" inputmode="decimal" data-guided-field="qty" value="${this.escape(row.qty || "")}"></td><td><select class="form-control form-control-sm" data-guided-field="uom"><option value="">${__("Choose one")}</option>${unitOptions}</select></td><td><button class="btn btn-sm btn-link text-danger" type="button" data-guided-remove-row aria-label="${__("Remove")}">×</button></td></tr>`);
	}

	renderGuidedModifiers(dialog, editor, selected) {
		const selectedNames = new Set((selected || []).map((row) => row.modifier_group));
		dialog.$wrapper.find("[data-guided-modifiers]").html((editor.modifier_groups || []).map((group) => `<label class="d-block mb-1"><input type="checkbox" class="mr-2" data-guided-modifier="${this.escape(group.name)}" ${selectedNames.has(group.name) ? "checked" : ""}>${this.escape(group.group_name || __("Customer choice"))} <span class="text-muted">${this.escape(group.selection_type === "Multiple" ? __("Customers may choose more than one") : __("Customers choose one"))}</span></label>`).join("") || `<span class="text-muted">${__("No customer choices yet.")}</span>`);
	}

	openGuidedStockItemDialog(parentDialog, editor) {
		const unitOptions = (editor.units || []).map((unit) => `<option value="${this.escape(unit.value)}">${this.escape(unit.label)}</option>`).join("");
		const dialog = new frappe.ui.Dialog({
			title: __("Add ingredient or supply"),
			fields: [
				{ fieldtype: "Data", fieldname: "name", label: __("Name"), reqd: 1, description: __("For example: matcha powder, oat milk, or 16 oz cup.") },
				{ fieldtype: "Select", fieldname: "kind", label: __("What is it?"), options: `${__("Ingredient")}\n${__("Supply")}`, default: __("Ingredient"), reqd: 1 },
				{ fieldtype: "HTML", fieldname: "unit_picker", options: `<label class="control-label">${__("How do staff count it?")}</label><select class="form-control" data-new-stock-unit><option value="">${__("Choose one")}</option>${unitOptions}</select><p class="small text-muted mt-1">${__("Choose the amount staff see when counting, such as grams, ml, bottles, or cartons.")}</p>` },
			],
			primary_action_label: __("Add"),
			primary_action: (values) => {
				const unit = dialog.$wrapper.find("[data-new-stock-unit]").val() || "";
				if (!unit) return this.showMessage(__("Choose how staff count it."));
				frappe.call({
					method: "kopos_connector.api.menu_authoring.create_guided_stock_item",
					type: "POST",
					args: { payload: JSON.stringify({
						company: editor.company,
						name: values.name,
						kind: values.kind === __("Supply") ? "Packaging" : "Ingredient",
						unit,
					}) },
					callback: (response) => {
						const item = response.message?.item;
						if (!item?.name) return this.showMessage(__("The ingredient or supply could not be added."));
						this.guidedEditorItems = [...this.guidedEditorItems.filter((row) => row.name !== item.name), item];
						this.addGuidedRow(parentDialog, { item: item.name, uom: item.stock_uom });
						dialog.hide();
						frappe.show_alert({ message: __("Added"), indicator: "green" });
					},
					error: () => this.showMessage(__("The ingredient or supply could not be added. Check the details and try again.")),
				});
			},
		});
		dialog.show();
	}

	openGuidedChoiceDialog({ company, item, recipe }) {
		const dialog = new frappe.ui.Dialog({
			title: __("Add customer choice"),
			fields: [
				{ fieldtype: "Data", fieldname: "name", label: __("Question shown to staff"), description: __("For example: Milk choice or Add extra?"), reqd: 1 },
				{ fieldtype: "Select", fieldname: "selection_mode", label: __("How many can the customer choose?"), options: `${__("Choose one")}\n${__("Choose any")}`, default: __("Choose one"), reqd: 1 },
				{ fieldtype: "Check", fieldname: "required", label: __("Staff must choose before adding the product"), default: 0 },
				{ fieldtype: "HTML", fieldname: "options", options: `<hr><h5>${__("Choices")}</h5><div data-choice-options></div><button class="btn btn-sm btn-default mt-2" type="button" data-add-choice-option>${__("Add another choice")}</button>` },
			],
			primary_action_label: __("Add customer choice"),
			primary_action: (values) => {
				const read = this.readGuidedChoiceOptions(dialog);
				if (read.error) return this.showMessage(read.error);
				frappe.call({
					method: "kopos_connector.api.menu_authoring.create_guided_customer_choice",
					type: "POST",
					args: { payload: JSON.stringify({
						company,
						recipe,
						name: values.name,
						selection_mode: values.selection_mode === __("Choose any") ? "many" : "one",
						required: values.required ? 1 : 0,
						options: read.options,
					}) },
					callback: (response) => {
						if (response.message?.status !== "ok") return this.showMessage(__("The customer choice could not be added."));
						dialog.hide();
						frappe.show_alert({ message: __("Customer choice added"), indicator: "green" });
						this.openGuidedRecipeDialog({ company, item, recipe });
					},
					error: () => this.showMessage(__("The customer choice could not be added. Check each choice and try again.")),
				});
			},
		});
		dialog.show();
		dialog.$wrapper.on("click", "[data-add-choice-option]", () => this.addGuidedChoiceOption(dialog));
		dialog.$wrapper.on("click", "[data-remove-choice-option]", (event) => $(event.currentTarget).closest("[data-choice-option]").remove());
		dialog.$wrapper.on("change", "[data-choice-change]", (event) => this.renderGuidedChoiceDetails($(event.currentTarget).closest("[data-choice-option]")));
		this.addGuidedChoiceOption(dialog);
	}

	addGuidedChoiceOption(dialog) {
		const itemOptions = this.guidedEditorItems.map((entry) => `<option value="${this.escape(entry.name)}">${this.escape(entry.item_name || __("Ingredient or supply"))}</option>`).join("");
		const unitOptions = this.guidedEditorUnits.map((unit) => `<option value="${this.escape(unit.value)}">${this.escape(unit.label)}</option>`).join("");
		const row = $(`<div class="border rounded p-3 mb-2" data-choice-option data-item-options="${this.escape(itemOptions)}" data-unit-options="${this.escape(unitOptions)}">
			<div class="row align-items-end">
				<div class="col-md-4"><label>${__("Choice name")}</label><input class="form-control" data-choice-name placeholder="${__("For example: Oat milk")}"></div>
				<div class="col-md-3"><label>${__("Extra price (RM)")}</label><input class="form-control" data-choice-price inputmode="decimal" value="0.00"></div>
				<div class="col-md-4"><label>${__("What changes?")}</label><select class="form-control" data-choice-change><option value="instructions_only">${__("Instructions only")}</option><option value="add">${__("Add an ingredient")}</option><option value="replace">${__("Swap an ingredient")}</option><option value="remove">${__("Leave out an ingredient")}</option><option value="change_amount">${__("Use a different amount")}</option></select></div>
				<div class="col-md-1 text-right"><button class="btn btn-link text-danger" type="button" data-remove-choice-option aria-label="${__("Remove")}">×</button></div>
			</div>
			<label class="mt-2 mb-0"><input type="radio" name="guided-usual-choice" class="mr-2" data-choice-default>${__("Usual choice")}</label>
			<div class="mt-2" data-choice-details></div>
		</div>`);
		dialog.$wrapper.find("[data-choice-options]").append(row);
		this.renderGuidedChoiceDetails(row);
	}

	renderGuidedChoiceDetails(row) {
		const change = row.find("[data-choice-change]").val();
		const itemOptions = row.attr("data-item-options") || "";
		const unitOptions = row.attr("data-unit-options") || "";
		const itemSelect = (attribute, label) => `<div class="col-md-4"><label>${label}</label><select class="form-control" ${attribute}><option value="">${__("Choose one")}</option>${itemOptions}</select></div>`;
		const amount = `<div class="col-md-2"><label>${__("Amount")}</label><input class="form-control" inputmode="decimal" data-choice-amount></div><div class="col-md-3"><label>${__("Measured in")}</label><select class="form-control" data-choice-unit><option value="">${__("Choose one")}</option>${unitOptions}</select></div>`;
		let html = `<span class="small text-muted">${__("This choice changes the instructions but not stock.")}</span>`;
		if (change === "add") html = `<div class="row">${itemSelect("data-choice-ingredient", __("Ingredient to add"))}${amount}</div>`;
		if (change === "replace") html = `<div class="row">${itemSelect("data-choice-ingredient", __("Instead of"))}${itemSelect("data-choice-replacement", __("Use"))}</div>`;
		if (change === "remove") html = `<div class="row">${itemSelect("data-choice-ingredient", __("Leave out"))}</div>`;
		if (change === "change_amount") html = `<div class="row">${itemSelect("data-choice-ingredient", __("Ingredient"))}${amount}</div>`;
		row.find("[data-choice-details]").html(html);
	}

	readGuidedChoiceOptions(dialog) {
		const options = [];
		let error = "";
		dialog.$wrapper.find("[data-choice-option]").each((index, element) => {
			if (error) return;
			const row = $(element);
			const name = String(row.find("[data-choice-name]").val() || "").trim();
			if (!name) { error = __("Give every choice a name."); return; }
			const priceSen = this.moneyToSen(row.find("[data-choice-price]").val() || "0");
			if (priceSen == null) { error = __("Enter each extra price like 0.00 or 2.50."); return; }
			options.push({
				name,
				price_sen: priceSen,
				is_default: row.find("[data-choice-default]").is(":checked") ? 1 : 0,
				change_type: row.find("[data-choice-change]").val() || "instructions_only",
				ingredient: row.find("[data-choice-ingredient]").val() || "",
				replacement: row.find("[data-choice-replacement]").val() || "",
				amount: row.find("[data-choice-amount]").val() || "",
				unit: row.find("[data-choice-unit]").val() || "",
			});
		});
		return { options, error };
	}

	readGuidedPayload(dialog, editor) {
		const components = dialog.$wrapper.find("[data-guided-rows] tr").map(function () { const row = $(this); return { item: row.find('[data-guided-field="item"]').val(), qty: row.find('[data-guided-field="qty"]').val(), uom: row.find('[data-guided-field="uom"]').val() }; }).get();
		const modifier_groups = dialog.$wrapper.find("[data-guided-modifier]:checked").map(function () { return { modifier_group: $(this).attr("data-guided-modifier") }; }).get();
		return { human_guided: 1, recipe: editor.recipe?.name || "", company: editor.company, sellable_item: editor.sellable_item, components, modifier_groups };
	}

	previewGuidedDraft(dialog, editor) {
		const payload = this.readGuidedPayload(dialog, editor);
		frappe.call({ method: "kopos_connector.api.menu_authoring.preview_menu_recipe", type: "POST", args: { payload: JSON.stringify(payload) }, callback: (response) => this.renderGuidedPreview(dialog, response.message || {}) });
	}

	renderGuidedPreview(dialog, response) {
		const preview = response.preview || {};
		const errors = (preview.errors || []).map((row) => `<div class="text-danger">${this.escape(row.message)}</div>`).join("");
		const warnings = (preview.warnings || []).map((row) => `<div class="text-warning">${this.escape(row.message)}</div>`).join("");
		const rows = (preview.tracked_items || []).map((row) => `<div><strong>${this.escape(row.name || __("Ingredient or supply"))}</strong> · ${this.escape(row.amount)}</div>`).join("");
		const effects = (preview.modifier_effects || []).map((row) => this.escape(row.modifier_name || __("Customer choice"))).join(", ");
		const cost = this.moneyLineHtml(preview, response.status === "ok");
		dialog.$wrapper.find("[data-guided-preview]").html(`<div class="mb-2"><strong class="${response.status === "ok" ? "text-success" : "text-danger"}">${response.status === "ok" ? __("Ready to save") : __("Fix these details first")}</strong></div>${errors}${warnings}<div class="mt-2"><strong>${__("JiJi will track")}</strong>${rows || `<div>${__("No ingredients or supplies added")}</div>`}</div><div class="mt-3">${cost}</div><div class="mt-2 text-muted">${effects ? `${__("Customer choices")}: ${effects}` : __("No customer choices")}</div>`);
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
			return `<div class="text-muted">${__("Add the cost of at least one ingredient before JiJi can show what you keep.")}</div>`;
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
				<div class="text-muted small">${__("Add a selling price to see what you keep.")}</div></div>`;
		}
		// Selling below cost is not a rounding detail; it is the answer.
		const tone = keeps > 0 ? "text-success" : "text-danger";
		const verdict = keeps > 0
			? __("Keeps {0}", [this.formatSen(keeps)])
			: __("Loses {0} on every one sold", [this.formatSen(Math.abs(keeps))]);
		return `<div class="d-flex flex-wrap align-items-baseline jiji-money-line">
			<span>${__("Sells for")} <strong>${this.escape(this.formatSen(price))}</strong></span>
			<span>${__("Ingredients cost")} <strong>${this.escape(this.formatSen(cost))}</strong></span>
			<span class="${tone}"><strong>${this.escape(verdict)}</strong></span>
		</div>`;
	}

	saveGuidedDraft(dialog, editor, onSaved = null) {
		const payload = this.readGuidedPayload(dialog, editor);
		frappe.call({ method: "kopos_connector.api.menu_authoring.save_menu_recipe_draft", type: "POST", args: { payload: JSON.stringify(payload) }, callback: (response) => {
			const result = response.message || {};
			if (result.status !== "ok") return this.showMessage(__("The product could not be saved. Nothing was changed. Try again."));
			dialog.hide();
			frappe.show_alert({ message: __("Product saved"), indicator: "green" });
			if (onSaved) onSaved(result);
			else this.refresh();
		}, error: () => this.showMessage(__("The product could not be saved. Check the details and try again.")) });
	}

	publishSelected() {
		const selected = this.page.body.find("[data-publish-recipe]:checked").map(function () { return $(this).attr("data-publish-recipe"); }).get();
		if (!selected.length) return this.showMessage(__("Choose at least one product marked ready."));
		frappe.call({ method: "kopos_connector.api.menu_authoring.publish_menu_recipe_selection", type: "POST", args: { payload: JSON.stringify({ recipes: selected }) }, callback: (response) => { const result = response.message || {}; if (result.status === "ok") { const count = result.count || 0; frappe.show_alert({ message: count === 1 ? __("Product is now available to staff") : __("{0} products are now available to staff", [count]), indicator: "green" }); this.refreshChoices(); } }, error: () => this.showMessage(__("Nothing was published. Fix the products marked in red and try again.")) });
	}

	previewCatalog() {
		const posProfile = this.catalogProfileControl?.get_value?.() || "";
		if (!posProfile) return this.showMessage(__("Choose an outlet first."));
		const output = this.page.body.find("[data-menu-catalog-results]").removeClass("d-none").html(`<span class="text-muted">${__("Checking what this outlet will see…")}</span>`);
		frappe.call({ method: "kopos_connector.api.inventory.get_menu_catalog_preview", type: "GET", args: { pos_profile: posProfile }, callback: (response) => {
			const preview = response.message || {};
			if (preview.status !== "ok") return output.html(`<strong class="text-danger">${this.escape(preview.reason || __("JiJi could not check what this outlet will see."))}</strong>`);
			const missing = preview.missing_recipe_items || [];
			if (missing.length) {
				const names = missing.map((name) => `<li>${this.escape(name)}</li>`).join("");
				const heading = missing.length === 1
					? __("1 product needs ingredients")
					: __("{0} products need ingredients", [missing.length]);
				return output.html(`<strong class="text-danger">${heading}</strong><p class="mb-2 mt-1">${__("Open each product and add what goes into one. If it never uses stock, choose that when adding it.")}</p><ul class="mb-0 pl-3">${names}</ul>`);
			}
			const total = preview.saleable_items || 0;
			const tracked = preview.recipe_ready_items || 0;
			const notTracked = preview.explicit_exclusions || 0;
			const note = notTracked ? `<div class="text-muted small mt-1">${__("{0} do not use stock", [notTracked])}</div>` : "";
			return output.html(`<strong class="text-success">${total === 1 ? __("This product is ready at the counter") : __("All {0} products are ready at the counter", [total])}</strong><div class="mt-1">${tracked === 1 ? __("JiJi updates stock from the ingredients in 1 product.") : __("JiJi updates stock from the ingredients in {0} products.", [tracked])}</div>${note}`);
		}, error: () => output.html(`<strong class="text-danger">${__("JiJi could not check what this outlet will see.")}</strong>`) });
	}

	formatSen(value) { const number = Number(value); return value == null || value === "" || !Number.isFinite(number) ? __("Unavailable") : `RM ${(number / 100).toFixed(2)}`; }
	moneyToSen(value) {
		const match = String(value ?? "").trim().match(/^(\d+)(?:\.(\d{1,2}))?$/);
		if (!match) return null;
		const sen = (Number.parseInt(match[1], 10) * 100) + Number.parseInt((match[2] || "").padEnd(2, "0") || "0", 10);
		return Number.isSafeInteger(sen) && sen <= 999999999 ? sen : null;
	}
	showMessage(message) { this.page.body.find("[data-guided-editor-status]").text(message); }
	escape(value) { return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[character]); }
}
