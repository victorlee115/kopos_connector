frappe.pages["jiji_stock_autopilot"].on_page_load = function (wrapper) {
	new KoPOSInventoryAutopilotPage(wrapper);
};

class KoPOSInventoryAutopilotPage {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("JiJi Stock Autopilot"),
			single_column: true,
		});
		this.render();
		this.warehouseControl = frappe.ui.form.make_control({
				parent: this.page.body.find("[data-warehouse]")[0],
				df: {
					fieldtype: "Link",
					options: "Warehouse",
					label: __("Warehouse"),
					description: __("Use an outlet warehouse; this page never changes stock."),
					reqd: 1,
				},
			});
		this.warehouseControl.refresh();
		this.warehouseControl.$input.on("change awesomplete-selectcomplete", () => this.updateWarehouseHelp());
		this.updateWarehouseHelp();
		this.page.set_primary_action(__("Refresh health"), () => this.refresh());
		this.page.body.find("[data-open-card]").on("click", (event) => {
			this.openCard($(event.currentTarget).attr("data-open-card"));
		});
	}

	render() {
		$(this.page.body).html(`
			<div class="kopos-autopilot-page">
				<div class="alert alert-info">${__("Inventory automation is a read model. Use standard ERPNext forms for counts, receiving, transfers, manufacture, and Purchase Order approval.")}</div>
				<div class="row align-items-end mb-4">
					<div class="col-sm-5"><div data-warehouse></div></div>
					<div class="col-sm-7"><p data-warehouse-help class="text-muted mb-0">${__("Select a warehouse to load its health and next safe action. Refresh health only reads state; it never changes stock.")}</p></div>
				</div>
				<div class="row kopos-autopilot-cards">
					${["Needs you", "Today", "Stock", "Counts", "Plans & buying", "Settings"].map((title) => `<div class="col-sm-6 col-lg-4"><div class="card h-100"><div class="card-body d-flex flex-column"><h4>${__(title)}</h4><p class="text-muted">${__("Open the linked standard ERPNext records and resolve the next safe action.")}</p><div data-card="${title}" class="mb-3">${__("Choose a warehouse first")}</div><button class="btn btn-sm btn-default mt-auto align-self-start" type="button" data-open-card="${title}">${__("Open records")}</button></div></div></div>`).join("")}
				</div>
			</div>`);
	}

	updateWarehouseHelp() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		const message = warehouse
			? __("Showing health and the next safe action for {0}. Refresh health only reads state; it never changes stock.", [warehouse])
			: __("Select a warehouse to load its health and next safe action. Refresh health only reads state; it never changes stock.");
		this.page.body.find("[data-warehouse-help]").text(message);
	}

	refresh() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		this.updateWarehouseHelp();
		if (!warehouse) {
			frappe.msgprint({ title: __("Choose a warehouse"), message: __("Select a warehouse before refreshing health.") });
			return;
		}
		this.page.set_indicator(__("Loading"), "orange");
		frappe.call({
			method: "kopos_connector.api.inventory.get_autopilot_health",
			type: "GET",
			args: { warehouse },
			callback: (response) => {
				const health = response.message || {};
				$('[data-card="Needs you"]').text(`${health.exceptions?.open_critical || 0} ${__("critical exceptions")}`);
				$('[data-card="Today"]').text(`${health.scheduler?.last_success || __("No successful scheduler run")}`);
				$('[data-card="Stock"]').text(`${health.devices?.current || 0} ${__("current devices")}, ${health.devices?.stale || 0} ${__("stale")}, ${health.devices?.dirty || 0} ${__("dirty")}`);
				$('[data-card="Counts"]').text(__("Use assigned count tasks and Draft Stock Reconciliations"));
				$('[data-card="Plans & buying"]').text(`${health.draft_purchase_order_safety || __("not enabled")}`);
				$('[data-card="Settings"]').text(`${health.automation_state || __("Review First")}`);
				this.page.set_indicator(__("Updated"), "green");
			},
			error: () => {
				this.page.set_indicator(__("Needs attention"), "red");
				frappe.msgprint({ title: __("Health check failed"), message: __("No inventory state was changed. Check the warehouse and try again.") });
			},
		});
	}

	openCard(title) {
		const routes = {
			"Needs you": ["List", "FB Inventory Exception"],
			Today: ["List", "FB Projection Log"],
			Stock: ["List", "Bin"],
			Counts: ["List", "FB Inventory Count Task"],
			"Plans & buying": ["List", "Material Request"],
			Settings: ["List", "FB Inventory Policy"],
		};
		const route = routes[title];
		if (route) frappe.set_route(...route);
	}
}
