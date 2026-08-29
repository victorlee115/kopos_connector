frappe.pages["jiji_stock_autopilot"].on_page_load = function (wrapper) {
	new JiJiStockAutopilotPage(wrapper);
};

class JiJiStockAutopilotPage {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("JiJi Stock Overview"),
			single_column: true,
		});
		this.loadedWarehouse = "";
		this.health = null;
		this.activeWorkflow = null;
		this.financePeriodDays = 30;
		this.refreshing = false;
		this.stockStatusKey = null;
		this.autoPollCount = 0;
		this.render();
		this.warehouseControl = frappe.ui.form.make_control({
			parent: this.page.body.find("[data-warehouse]")[0],
			df: {
				fieldtype: "Link",
				fieldname: "warehouse",
				options: "Warehouse",
					label: __("Outlet stock location"),
					description: __("Choose where this outlet keeps its ingredients and supplies."),
				reqd: 1,
			},
		});
		this.warehouseControl.refresh();
		this.warehouseControl.$input.on("change awesomplete-selectcomplete", () => this.onWarehouseChanged());
		this.chooseOutletForDirector();
		jiji.desk.injectStyle();
		$(wrapper).addClass("jiji-desk");
		this.page.set_primary_action(__("Check now"), () => this.refresh({ showMissingOutlet: true }));
		this.page.add_inner_button(__("Start using stock tracking"), () => this.activateCutover());
		this.page.body.on("click", "[data-primary-route]", (event) => {
			event.preventDefault();
			this.openRoute($(event.currentTarget).attr("data-primary-route"));
		});
		this.page.body.on("click", "[data-exception-route]", (event) => {
			event.preventDefault();
			this.openException($(event.currentTarget).attr("data-exception-route"));
		});
		this.page.body.on("click", "[data-count-review]", (event) => {
			event.preventDefault();
			this.reviewCount($(event.currentTarget).attr("data-count-review"));
		});
		this.page.body.on("click", "[data-advanced-route]", (event) => {
			event.preventDefault();
			this.openAdvancedRoute($(event.currentTarget).attr("data-advanced-route"));
		});
		this.page.body.on("click", "[data-open-document]", (event) => {
			event.preventDefault();
			const doctype = $(event.currentTarget).attr("data-doctype");
			const name = $(event.currentTarget).attr("data-open-document");
			if (doctype && name) frappe.set_route("Form", doctype, name);
		});
		this.page.body.on("click", "[data-close-workflow]", (event) => {
			event.preventDefault();
			this.activeWorkflow = null;
			this.page.body.find("[data-workflow]").empty();
		});
		this.page.body.on("click", "[data-finance-period]", (event) => {
			event.preventDefault();
			this.financePeriodDays = Number($(event.currentTarget).attr("data-finance-period")) || 30;
			this.openWorkflow("finance");
		});
		this.startAutomaticChecks();
	}

	render() {
		$(this.page.body).html(`
			<div class="jiji-stock-autopilot">
				<div class="jiji-outletbar mb-4">
					<div data-warehouse></div>
					<span data-warehouse-help class="jiji-muted"></span>
					<span class="jiji-outletbar__sp"></span>
					<span data-auto-status class="jiji-muted"></span>
				</div>
				<div data-overall class="mb-4"></div>
				<div data-next-step class="mb-4">${this.chooseOutletNextStepHtml()}</div>
				<div data-workflow class="mb-4"></div>
				<div data-counted class="mb-4"></div>
				<div data-running-low class="mb-4"></div>
				<div data-moved class="mb-4"></div>
				<div data-sections class="row"></div>
				<div data-count-reviews class="mt-2"></div>
				<div data-exceptions class="mt-4"></div>
				${this.stakeholderWorkflowHtml()}
			</div>`);
		this.updateWarehouseHelp();
	}

	/**
	 * Open on the outlet the director actually has.
	 *
	 * The filter is required, so until it was filled this page showed nothing
	 * but an instruction to fill it. A café with one outlet store, or a
	 * director with a default, has already answered the question. When the
	 * answer is genuinely ambiguous the filter stays empty and asks.
	 */
	chooseOutletForDirector() {
		if (this.warehouseControl.get_value()) return;
		frappe.call({
			method: "kopos_connector.api.director_scope.get_director_outlet",
			type: "GET",
			callback: (response) => {
				const warehouse = (response.message || {}).warehouse;
				if (!warehouse || this.warehouseControl.get_value()) return;
				// set_value fills the input but does not fire the change the page
				// listens for, and it resolves after its own link validation.
				// Calling onWarehouseChanged directly read the field back before
				// it held anything, so the outlet was filled in and the page went
				// on asking for an outlet. Trigger the control's own change so a
				// defaulted outlet and a chosen one take exactly the same path.
				Promise.resolve(this.warehouseControl.set_value(warehouse)).then(() =>
					this.warehouseControl.$input.trigger("change"),
				);
			},
			// One outlet still beats no page: leave the filter for the director.
			error: () => this.updateWarehouseHelp(),
		});
	}

	onWarehouseChanged() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		this.updateWarehouseHelp();
		if (!warehouse || warehouse === this.loadedWarehouse) return;
		this.loadedWarehouse = warehouse;
		this.stockStatusKey = null;
		this.autoPollCount = 0;
		this.refresh({ showMissingOutlet: false });
	}

	updateWarehouseHelp() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		const message = warehouse
				? __("Showing {0}.", [warehouse])
				: __("Choose an outlet.");
		this.page.body.find("[data-warehouse-help]").text(message);
		if (!warehouse) this.page.body.find("[data-auto-status]").text(__("Updates automatically after you choose an outlet."));
		else if (this.checkedAt) this.page.body.find("[data-auto-status]").text(__("Checked {0}", [this.checkedAt]));
	}

	/**
	 * Which things are running low, and what moved today.
	 *
	 * The area cards say "3 running low" and never say which three, and they
	 * show a waste figure with no cause. Both are separate reads so a slow or
	 * failing stock snapshot costs these two blocks and not the whole page.
	 */
	loadStockDesk(warehouse) {
		frappe.call({
			method: "kopos_connector.api.stock_desk.get_stock_desk",
			type: "GET",
			args: { warehouse },
			callback: (response) => this.renderStockDesk(response.message || {}),
			error: () => this.renderStockDesk({}),
		});
	}

	renderStockDesk(data) {
		const counted = this.page.body.find("[data-counted]");
		const low = this.page.body.find("[data-running-low]");
		const moved = this.page.body.find("[data-moved]");
		counted.empty();
		low.empty();
		moved.empty();
		if (data.status !== "ok") return;
		this.checkedAt = data.checked_at || "";
		this.updateWarehouseHelp();

		// Six things a director counts. The six area cards below answer how the
		// automatic work is behaving, which is support's question, so they are
		// demoted beneath these rather than opening the page.
		if ((data.tiles || []).length) {
			const grid = $('<div class="jiji-tiles"></div>').appendTo(counted);
			data.tiles.forEach((tile) => {
				// Built through the desk's own routing owner rather than a
				// second click handler on this page.
				const link = jiji.desk.routeLink(tile.route).addClass("jiji-tile");
				link.append(jiji.desk.label(tile.label || ""));
				link.append(
					$("<div>")
						.addClass("jiji-tile__value" + (tile.attention ? " jiji-tile__value--attention" : ""))
						.text(tile.value || ""),
				);
				link.append($("<div>").addClass("jiji-tile__detail").text(tile.detail || ""));
				grid.append(link);
			});
		}

		if (!(data.running_low || []).length && data.running_low_note) {
			// Blank would read as "nothing to see"; it may mean "JiJi cannot
			// say yet", which is a different thing a director should know.
			low.html(`<div class="jiji-head mb-2"><div class="jiji-label">${__("Running low")}</div></div>
				<div class="jiji-card jiji-muted">${this.escape(data.running_low_note)}</div>`);
		} else if ((data.running_low || []).length) {
			const rows = data.running_low.map((row) => `
				<div class="d-flex align-items-center gap-3 py-2 border-bottom">
					<span class="flex-grow-1">${this.escape(row.name)}</span>
					<span class="text-muted small" style="width: 120px; text-align: right">${this.escape(row.days)}</span>
					<span class="jiji-meter${row.on_order ? " jiji-meter--calm" : ""}"><span style="width: ${Number(row.fill) || 0}%"></span></span>
					<span class="jiji-pill${row.on_order ? "" : " jiji-pill--attention"}">${this.escape(row.state)}</span>
				</div>`).join("");
			low.html(`<div class="jiji-head mb-2"><div class="jiji-label">${__("Running low")}</div>
				<span class="jiji-muted">${__("Bars show how long each lasts at this week's sales.")}</span></div>
				<div class="card"><div class="card-body py-2">${rows}</div></div>`);
		}

		if (!(data.moved || []).length && data.moved_empty_note) {
			moved.html(`<div class="jiji-head mb-2"><div class="jiji-label">${__("What moved today")}</div></div>
				<div class="jiji-card jiji-muted">${this.escape(data.moved_empty_note)}</div>`);
		} else if ((data.moved || []).length) {
			const rows = data.moved.map((row) => `
				<div class="d-flex align-items-baseline gap-3 py-2 border-bottom">
					<span class="flex-grow-1">${this.escape(row.title)}</span>
					<span class="text-right">${this.escape(row.detail)}</span>
				</div>`).join("");
			moved.html(`<div class="jiji-head mb-2"><div class="jiji-label">${__("What moved today")}</div>
				<span class="jiji-muted">${this.escape(data.moved_note || "")}</span></div>
				<div class="card"><div class="card-body py-2">${rows}</div></div>`);
		}
	}

	refresh(options = {}) {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		this.updateWarehouseHelp();
		if (!warehouse) {
			if (options.showMissingOutlet) {
				frappe.msgprint({ title: __("Choose an outlet"), message: __("Choose where this outlet keeps stock first.") });
			}
			return;
		}
		if (this.refreshing) return;
		this.refreshing = true;
		this.page.set_indicator(__("Loading"), "orange");
		frappe.call({
			method: "kopos_connector.api.inventory.get_autopilot_health",
			type: "GET",
			args: { warehouse },
			callback: (response) => {
				this.refreshing = false;
				this.health = response.message || {};
				this.loadedWarehouse = warehouse;
				this.loadStockDesk(warehouse);
				this.renderHealth();
			},
			error: () => {
				this.refreshing = false;
				this.page.set_indicator(__("Could not refresh"), "red");
				this.page.body.find("[data-auto-status]").text(__("Couldn’t check for new stock changes."));
				if (!options.quiet) {
					frappe.msgprint({
						title: __("Couldn’t check stock"),
						message: __("Nothing changed. Check the connection and try again."),
					});
				}
			},
		});
	}

	startAutomaticChecks() {
		this.autoRefreshTimer = setInterval(() => this.pollStockUpdateStatus(), 10_000);
	}

	pollStockUpdateStatus() {
		if (document.visibilityState === "hidden") return;
		const route = (frappe.get_route && frappe.get_route()) || [];
		if (!route.some((part) => ["jiji-stock-autopilot", "jiji_stock_autopilot"].includes(String(part)))) return;
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		if (!warehouse) return;
		this.autoPollCount += 1;
		frappe.call({
			method: "kopos_connector.api.inventory.get_stock_update_status",
			type: "GET",
			args: { warehouse },
			callback: (response) => {
				const status = response.message || {};
				const nextKey = [status.state, status.latest_completed_at, status.availability_checked_at].join("|");
				const changed = this.stockStatusKey !== null && this.stockStatusKey !== nextKey;
				this.stockStatusKey = nextKey;
				const label = {
					needs_attention: __("Stock needs a manager."),
					taking_longer: __("Stock is still updating."),
					updating: __("Updating automatically."),
					current: __("Updates automatically."),
				}[status.state] || __("Updates automatically.");
				this.page.body.find("[data-auto-status]").text(label);
				if (changed || this.autoPollCount >= 6) {
					this.autoPollCount = 0;
					this.refresh({ quiet: true, showMissingOutlet: false });
				}
			},
			error: () => {
				this.page.body.find("[data-auto-status]").text(__("Couldn’t check for new stock changes."));
			},
		});
	}

	renderHealth() {
		const health = this.health || {};
		const exceptions = health.exceptions || {};
		const critical = Number(exceptions.open_critical || 0);
		const warningCount = (exceptions.warning_reasons || []).length;
		const devices = health.devices || {};
		const scheduler = health.scheduler || {};
		const runtime = health.runtime_artifact || {};
		const countReviews = health.count_director_reviews || {};
		const exceptionRouting = health.exception_routing || {};
		const stockReportAge = this.oldestDeviceReportAge(devices, health.as_of);
		const unacknowledgedDevices = Number(health.overlay?.unacknowledged_devices || 0);
		const schedulerState = this.schedulerStatus(health);
		const stockNeedsReview = Number(devices.stale || 0) > 0 || Number(devices.dirty || 0) > 0 || !health.overlay?.acknowledged;
		const overallCritical = critical > 0 || (exceptions.critical_reasons || []).length > 0 || health.draft_purchase_order_safety === "unsafe" || ["missing", "policy_missing"].includes(exceptionRouting.status);
		const overallWarning = warningCount > 0 || stockNeedsReview || Number(countReviews.open || 0) > 0 || Number(health.planning?.open || 0) > 0;
			let needs = {
				value: __("Nothing needs your attention"),
				detail: __("Stock work can continue. Check this page again later."),
				action: null,
				route: null,
			};
			if (["missing", "policy_missing"].includes(exceptionRouting.status)) {
				needs = exceptionRouting.status === "policy_missing"
					? { value: __("Stock settings are not finished"), detail: __("Finish the stock settings for this outlet before starting stock tracking."), action: __("Open stock settings"), route: "settings" }
					: { value: __("No director is assigned to stock problems"), detail: __("Ask an administrator to assign a Company Director to receive urgent stock problems."), action: __("Open stock settings"), route: "settings" };
			} else if (critical > 0) {
				needs = { value: critical === 1 ? __("1 urgent stock problem") : __("{0} urgent stock problems", [critical]), detail: this.firstExceptionDetail(exceptions), action: __("Open stock problems"), route: "needs" };
			} else if (Number(countReviews.open || 0) > 0) {
				const openReviews = Number(countReviews.open || 0);
				needs = { value: openReviews === 1 ? __("1 stock count needs review") : __("{0} stock counts need review", [openReviews]), detail: __("Check what staff counted, then create a saved stock correction for each count."), action: __("Review stock counts"), route: "counts" };
		} else if (schedulerState === "critical" || schedulerState === "warning") {
				needs = { value: __("A stock update needs attention"), detail: __("JiJi needs to finish one stock update before this outlet starts using stock tracking."), action: __("Check stock updates"), route: "today" };
		} else if (health.draft_purchase_order_safety === "unsafe") {
				needs = { value: __("Buying limits need review"), detail: __("New supplier order drafts stay paused until the limits are fixed. JiJi never sends or approves these orders."), action: __("Open stock settings"), route: "settings" };
			} else if ((exceptions.critical_reasons || []).length > 0 || warningCount > 0) {
				needs = { value: __("Stock checks need review"), detail: this.firstExceptionDetail(exceptions), action: __("Open stock problems"), route: "needs" };
		} else if (stockNeedsReview) {
			needs = { value: __("Tablet stock reports need review"), detail: stockReportAge, action: __("Open stock"), route: "stock" };
		} else if (Number(health.planning?.open || 0) > 0) {
			const openPlans = Number(health.planning.open || 0);
			needs = { value: openPlans === 1 ? __("1 restock plan needs review") : __("{0} restock plans need review", [openPlans]), detail: __("Check the suggested quantities and any supplier order draft before anything is sent."), action: __("Review restock plans"), route: "plans" };
		}
		this.page.set_indicator(
			overallCritical ? __("Action needed") : overallWarning ? __("Review needed") : __("All clear"),
			overallCritical ? "red" : overallWarning ? "orange" : "green",
		);
		// The consequence sits inside the one next step rather than above it.
		// Two full-width alarms stacked on one screen is the pattern this page
		// was redrawn to remove; the reader cannot tell which to act on.
		this.page.body.find("[data-overall]").empty();
		this.renderNextStep(
			needs,
			overallCritical ? "critical" : overallWarning ? "warning" : "ok",
			overallCritical
				? __("Automatic stock work is paused until the urgent problem is fixed.")
				: overallWarning
					? __("Stock tracking is running, but one or more items need review.")
					: __("No urgent stock problem was found."),
		);

		// The old first card repeated the next step -- same words, same button,
		// directly under it. Six areas remain, and the one that produced the
		// next step is marked as such rather than raising a second alarm.
		const sections = [
			{
				key: "today",
				title: __("Today"),
				icon: "◷",
				status: schedulerState,
					value: scheduler.last_success ? __("Stock last updated automatically {0} ago", [this.agePhrase(scheduler.last_success, health.as_of)]) : __("Stock has not updated automatically yet"),
					detail: scheduler.next_success_deadline ? __("Next check is expected by {0}", [this.shortDate(scheduler.next_success_deadline)]) : __("Run one successful automatic stock update before using it at an outlet"),
					action: __("Check stock updates"),
				route: "today",
			},
			{
				key: "stock",
				title: __("Stock"),
				icon: "▣",
				status: Number(devices.stale || 0) || Number(devices.dirty || 0) || !health.overlay?.acknowledged ? "warning" : "ok",
				value: __("{0} current · {1} need an update · {2} waiting to send", [Number(devices.current || 0), Number(devices.stale || 0), Number(devices.dirty || 0)]),
					detail: `${health.overlay?.acknowledged ? __("Every connected tablet has the latest menu and stock instructions") : unacknowledgedDevices === 1 ? __("1 tablet has not received the latest menu or stock instructions") : __("{0} tablets have not received the latest menu or stock instructions", [unacknowledgedDevices])} · ${stockReportAge}`,
				action: __("Open stock"),
				route: "stock",
			},
			{
				key: "counts",
					title: __("Stock counts"),
				icon: "▤",
				status: this.summaryStatus(health.counts),
					value: this.summaryValue(health.counts, __("stock count waiting"), __("stock counts waiting")),
					detail: this.summaryAge(health.counts, __("Oldest stock count")),
					action: __("Open stock counts"),
				route: "counts",
			},
			{
				key: "plans",
					title: __("Restocking & buying"),
				icon: "▥",
				status: health.draft_purchase_order_safety === "unsafe" ? "critical" : this.summaryStatus(health.planning),
					value: health.draft_purchase_order_safety === "unsafe" ? __("New supplier order drafts are paused") : this.summaryValue(health.planning, __("restock plan waiting"), __("restock plans waiting")),
					detail: health.draft_purchase_order_safety === "unsafe" ? __("Review the buying limits before creating another supplier order draft") : this.summaryAge(health.planning, __("Oldest restock plan")),
					action: __("Open restock plans"),
				route: "plans",
			},
			{
				key: "finance",
				title: __("Finance"),
				icon: "◫",
				status: "info",
				value: __("Stock value and changes"),
				detail: __("See what was received, used, transferred, or wasted over 7, 30, or 90 days."),
				action: __("Open finance view"),
				route: "ledger",
			},
			{
				key: "settings",
				title: __("Settings"),
				icon: "⚙",
				status: runtime.status === "verified" ? "info" : runtime.status === "present" ? "warning" : "critical",
				value: this.automationStateLabel(health.automation_state),
					detail: runtime.status === "verified"
						? `${__("JiJi setup checked · stock information may be up to {0} minutes old", [Number(health.max_source_age_minutes || 30)])} · ${this.checkedAge(health.as_of)}`
						: runtime.status === "present"
							? `${__("JiJi setup has not been checked yet")} · ${this.checkedAge(health.as_of)}`
							: runtime.status === "unmatched"
								? __("This JiJi setup is not approved. Ask Support before starting stock tracking.")
								: __("JiJi setup could not be checked. Ask Support before starting stock tracking."),
					action: __("Open stock settings"),
				route: "settings",
			},
		];
		this.page.body.find("[data-sections]").html(
			`<div class="col-12"><div class="jiji-head mb-2"><div class="jiji-label">${__("How the automatic work is doing")}</div>
				<span class="jiji-muted">${__("Six areas JiJi watches for you. Only one of them is ever your next step.")}</span></div></div>` +
			sections.map((section) => this.sectionHtml(section, section.route === needs.route)).join(""),
		);
		this.renderCountReviews(countReviews);
		this.renderExceptions(exceptions.top || [], overallCritical || overallWarning);
	}

	stakeholderWorkflowHtml() {
		const stakeholders = [
			{
				step: "1",
				title: __("Staff"),
				where: __("Tablet"),
				detail: __("Count what is there. Record preparation, deliveries, transfers, and waste. Enter what you actually see; do not guess."),
			},
			{
				step: "2",
				title: __("Outlet manager"),
				where: __("Tablet"),
				detail: __("Check unusual amounts and confirm stock changes. If something is unclear, leave it for review instead of changing the answer."),
			},
			{
				step: "3",
				title: __("Company director"),
				where: __("This page"),
				detail: __("Follow Your next step. Review large count differences, urgent problems, restock plans, and supplier order drafts."),
			},
			{
				step: "4",
				title: __("Buying"),
				where: __("This page"),
				detail: __("Check the restock plan, supplier quote, and supplier order draft. The order stays as a draft until a director checks and sends it."),
				action: __("Open restock plans"),
				route: "plans",
			},
			{
				step: "5",
				title: __("Supplier"),
				where: __("Outside JiJi"),
				detail: __("The supplier receives an order only after a director checks and sends it. JiJi never sends or approves a supplier order."),
			},
			{
				step: "6",
				title: __("Finance"),
				where: __("This page"),
				detail: __("Check ingredient cost, stock changes, and accounting after approved stock work is finished. Never change a finished accounting entry."),
				action: __("Open detailed stock changes"),
				route: "ledger",
			},
			{
				step: "7",
				title: __("Support"),
				where: __("This page and system checks"),
				detail: __("Fix the connection or stock update, then refresh. Keep all saved work; never delete it to hide a warning."),
				action: __("Check stock updates"),
				route: "today",
			},
			{
				step: "8",
				title: __("JiJi"),
				where: __("Automatic"),
				detail: __("Tries unfinished work again, saves stock work or drafts, and pauses risky work for a person to review."),
			},
		];
		const cards = stakeholders.map((stakeholder) => {
			const action = stakeholder.route && stakeholder.action
				? `<button type="button" class="btn btn-sm btn-default mt-3" data-primary-route="${this.escapeAttribute(stakeholder.route)}">${this.escape(stakeholder.action)}</button>`
				: "";
			return `<div class="col-sm-6 col-lg-4 mb-3">
			<div class="border rounded h-100 p-3">
				<div class="d-flex justify-content-between align-items-start mb-2"><strong>${this.escape(stakeholder.step)}. ${this.escape(stakeholder.title)}</strong><span class="badge badge-light">${this.escape(stakeholder.where)}</span></div>
				<div class="text-muted small">${this.escape(stakeholder.detail)}</div>${action}
			</div>
		</div>`;
		}).join("");
		return `<details class="card mb-4"><summary class="card-header font-weight-bold" style="cursor: pointer">${__("Who does each part?")}</summary>
			<div class="card-body"><p class="mb-3">${__("Only Company Directors use this page for business decisions. System Managers may open it for support. Staff and outlet managers use the tablet. One person may also handle buying or finance responsibilities.")}</p><div class="row">${cards}</div></div>
		</details>`;
	}

	chooseOutletNextStepHtml() {
		return `<div class="jiji-next"><div class="jiji-next__copy">
			<div class="jiji-label">${__("Your next step")}</div>
			<div class="jiji-next__title mt-1">${__("Choose an outlet above")}</div>
			<div class="jiji-next__detail">${__("JiJi will show the first action that needs a person. Refreshing only checks information; it does not change stock.")}</div>
		</div></div>`;
	}

	renderNextStep(nextStep, status, consequence) {
		const action = nextStep.route && nextStep.action
			? `<button type="button" class="btn btn-primary" data-primary-route="${this.escapeAttribute(nextStep.route)}">${this.escape(nextStep.action)}</button>`
			: `<span class="text-success font-weight-bold">✓ ${__("Nothing to do right now")}</span>`;
		// A bar, never a fill, and never an outline in the status colour: the
		// same shape the counter and every other desk page use for one step.
		this.page.body.find("[data-next-step]").html(`<div class="jiji-next">
			<div class="jiji-next__copy">
				<div class="jiji-label">${__("Your next step")}</div>
				<div class="jiji-next__title mt-1">${this.escape(nextStep.value)}</div>
				<div class="jiji-next__detail">${this.escape(nextStep.detail)}</div>
				${consequence ? `<div class="jiji-next__consequence">${this.escape(consequence)}</div>` : ""}
			</div>${action}
		</div>`);
	}

	renderCountReviews(summary) {
		const target = this.page.body.find("[data-count-reviews]");
		if (!summary || summary.status === "not_installed") {
			target.empty();
			return;
		}
		if (summary.status === "unavailable") {
			target.html(`<div class="alert alert-warning">${this.statusBadge("warning", __("Count reviews are temporarily unavailable. Refresh and try again."))}</div>`);
			return;
		}
		if (!Number(summary.open || 0)) {
			target.empty();
			return;
		}
		const cards = (Array.isArray(summary.items) ? summary.items : []).map((review) => {
			const lines = (Array.isArray(review.lines) ? review.lines : []).map((line) => {
				const packs = line.full_packs != null && line.loose_quantity != null
					? `${this.escape(line.full_packs)} ${this.escape(line.purchase_uom || __("packs"))} + ${this.escape(line.loose_quantity)} ${this.escape(line.uom)}`
					: `${this.escape(line.total_quantity)} ${this.escape(line.uom)}`;
				const difference = line.variance_percent == null ? __("difference not available") : `${this.escape(line.variance_percent)}%`;
				return `<tr><td>${this.escape(line.item_name || line.item_id)}</td><td>${packs}</td><td>${difference}</td></tr>`;
			}).join("");
			const reviewId = this.escapeAttribute(review.observation_id);
			return `<div class="card border-warning mb-3"><div class="card-body">
				<div class="d-flex justify-content-between align-items-start gap-2"><div><div class="mb-1">${this.statusBadge("warning", __("Director review required"))}</div>
					<h4 class="mb-1">${__("Help number")}: ${this.escape(review.observation_id)}</h4><p class="text-muted small mb-2">${__("The counted amount is very different from the saved amount, so a director must check it.")}</p></div>
				<button type="button" class="btn btn-sm btn-primary" data-count-review="${reviewId}">${__("Review and create draft")}</button></div>
					<table class="table table-sm mb-0"><thead><tr><th>${__("Item")}</th><th>${__("Count entered")}</th><th>${__("Difference from the saved amount")}</th></tr></thead><tbody>${lines}</tbody></table>
				<div class="text-muted small mt-2">${review.age_minutes == null ? __("Observed time unavailable") : __("Observed {0} ago", [this.ageMinutesPhrase(review.age_minutes)])}</div>
			</div></div>`;
		}).join("");
		target.html(`<h3>${__("Stock counts needing your decision")}</h3><p class="text-muted">${__("Check what staff counted below. JiJi will save a stock correction for you to review. Available stock will not change until an authorized person approves it.")}</p>${cards}`);
	}

	reviewCount(observationId) {
		const id = String(observationId || "");
		const summary = this.health?.count_director_reviews || {};
		const review = (Array.isArray(summary.items) ? summary.items : []).find((item) => String(item.observation_id || "") === id);
		if (!review) {
			frappe.msgprint({ title: __("Count review is no longer available"), message: __("Refresh the page and check the current count task.") });
			return;
		}
		frappe.prompt([
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Why are you accepting this count?"), description: __("Write a short reason. Do not enter a price or cost."), reqd: 1, default: __("I checked the physical amounts. Save this stock correction for final approval.") },
		], (values) => {
			frappe.call({
				method: "kopos_connector.api.inventory.create_count_reconciliation_after_director_review",
				type: "POST",
				args: { payload: JSON.stringify({ observation_id: id, reason: values.reason }) },
				callback: (response) => {
					const result = response.message || {};
					const message = result.status === "replayed" ? __("This saved stock correction already exists. JiJi did not create another one.") : __("Stock correction saved. Open it to review and approve the stock change.");
					frappe.show_alert({ message, indicator: "green" });
					this.refresh();
				},
				error: () => frappe.msgprint({ title: __("Count review not completed"), message: __("Nothing was changed. Refresh the latest stock information and try again.") }),
			});
		}, __("Review this stock count"), __("Save stock correction"));
	}

	/**
	 * One card of one area.
	 *
	 * ``isNextStep`` marks the area the next step came from. Six areas each
	 * shouting "Action required" is how a director ends up unable to tell which
	 * one to open first, so that area points at the step above instead of
	 * repeating the alarm, and the other five stay quiet unless they are
	 * urgent in their own right.
	 */
	sectionHtml(section, isNextStep = false) {
		const action = section.route && section.action
			? `<button type="button" class="btn btn-sm btn-default mt-auto align-self-start" data-primary-route="${this.escapeAttribute(section.route)}">${this.escape(section.action)}</button>`
			: `<span class="text-success small font-weight-bold mt-auto">✓ ${__("No action needed")}</span>`;
		const badge = isNextStep
			? `<span class="text-muted small font-weight-bold">${__("This is your next step")}</span>`
			: this.statusBadge(section.status, this.statusLabel(section.status));
		// The card stays quiet and the badge carries the status. Six coloured
		// outlines at once is the same noise as six areas all saying "Action
		// required": nothing is emphasised when everything is.
		return `<div class="col-sm-6 col-lg-4 mb-4"><div class="card h-100">
			<div class="card-body d-flex flex-column"><h4>${section.icon} ${section.title}</h4>
				<div class="mb-2">${badge}</div>
				<div class="font-weight-bold mb-1">${this.escape(section.value)}</div>
				<p class="text-muted small mb-3">${this.escape(section.detail)}</p>
				${action}
			</div></div></div>`;
	}

	renderExceptions(rows, otherReviewNeeded = false) {
		const target = this.page.body.find("[data-exceptions]");
		if (!rows.length) {
			const status = otherReviewNeeded ? "info" : "ok";
			const message = otherReviewNeeded ? __("No additional stock problems") : __("No open stock problems");
			target.html(`<div class="alert alert-${this.toneClass(status)}">${this.statusBadge(status, message)}</div>`);
			return;
		}
		const cards = rows.map((row) => {
			const severity = String(row.severity || "Warning").toLowerCase() === "critical" ? "critical" : "warning";
			const route = row.source_doctype && row.source_name
				? JSON.stringify(["Form", row.source_doctype, row.source_name])
				: JSON.stringify(["Form", "FB Inventory Exception", row.name]);
			return `<div class="border rounded p-3 mb-2"><div class="d-flex justify-content-between align-items-start gap-2">
					<div><div class="mb-1">${this.statusBadge(severity, severity === "critical" ? __("Urgent") : __("Please check"))}</div><div class="font-weight-bold">${this.escape(this.plainExceptionMessage(row.reason_code))}</div><div class="text-muted small">${__("Open this problem and follow the one next step shown.")}</div></div>
				<button type="button" class="btn btn-sm btn-default" data-exception-route='${this.escapeAttribute(route)}'>${__("Open")}</button>
			</div><div class="text-muted small mt-2">${__("Seen {0}", [this.agePhrase(row.last_seen, this.health?.as_of)])}</div></div>`;
		}).join("");
		target.html(`<h3>${__("Open stock problems needing attention")}</h3>${cards}`);
	}

	firstExceptionDetail(exceptions) {
		const row = exceptions.top && exceptions.top[0];
		if (!row) return __("Open stock problems to see what needs fixing.");
		const age = row.age_minutes == null ? __("age unavailable") : this.ageMinutesPhrase(row.age_minutes);
		return `${this.plainExceptionMessage(row.reason_code)} · ${age}`;
	}

	plainExceptionMessage(reasonCode) {
		const reason = String(reasonCode || "").toLowerCase();
		if (reason.includes("scheduler") || reason.includes("projection")) {
			return __("JiJi could not finish a stock update. Your saved information is safe.");
		}
		if (reason.includes("cost")) return __("One or more ingredient costs need checking.");
		if (reason.includes("purchase") || reason.includes("outbound")) return __("Buying settings need checking before another supplier order draft can be created.");
		if (reason.includes("routing") || reason.includes("director")) return __("Choose who should receive urgent stock problems.");
		return __("JiJi found a stock problem that needs review.");
	}

	oldestDeviceReportAge(devices, asOf) {
		const acknowledgements = Array.isArray(devices.acknowledgements) ? devices.acknowledgements : [];
		const ages = acknowledgements
			.map((row) => this.ageMinutes(row.effective_at, asOf))
			.filter((value) => value != null);
		return ages.length ? __("Oldest tablet update: {0}", [this.ageMinutesPhrase(Math.max(...ages))]) : __("No tablet update yet");
	}

	checkedAge(timestamp) {
		const age = this.ageMinutes(timestamp, new Date().toISOString());
		return age == null ? __("check time unavailable") : __("checked {0}", [this.ageMinutesPhrase(age)]);
	}

	schedulerStatus(health) {
		const critical = (health.exceptions?.critical_reasons || []).some((reason) => String(reason).includes("scheduler"));
		return critical ? "critical" : (health.exceptions?.warning_reasons || []).some((reason) => String(reason).includes("scheduler")) ? "warning" : "ok";
	}

	summaryStatus(summary) {
		if (!summary || summary.status === "not_installed" || summary.status === "unavailable") return "warning";
		return Number(summary.open || 0) > 0 ? "info" : "ok";
	}

	summaryValue(summary, singular, plural) {
		if (!summary || summary.status === "not_installed") return __("Not configured");
		if (summary.status === "unavailable") return __("Could not read");
		const count = Number(summary.open || 0);
		return `${count} ${count === 1 ? singular : plural}`;
	}

	summaryAge(summary, label) {
		if (!summary || summary.status !== "ok") return __("This area needs setup before it can be monitored");
		return summary.oldest_age_minutes == null ? __("No open work") : `${label}: ${this.ageMinutesPhrase(summary.oldest_age_minutes)}`;
	}

	statusBanner(status, message) {
		return `<div class="alert alert-${this.toneClass(status)} mb-0">${this.statusBadge(status, message)}</div>`;
	}

	statusBadge(status, label) {
		const icon = { critical: "⛔", warning: "⚠", ok: "✓", info: "ⓘ" }[status] || "ⓘ";
		return `<span class="text-${this.toneClass(status)}"><span aria-hidden="true">${icon}</span> ${this.escape(label)}</span>`;
	}

	statusLabel(status) {
		return { critical: __("Action required"), warning: __("Review"), ok: __("OK"), info: __("Information") }[status] || __("Information");
	}

	automationStateLabel(state) {
		return {
			Active: __("Automatic actions on"),
			Paused: __("Paused"),
			"Review First": __("Approval required"),
		}[String(state || "Review First")] || __("Approval required");
	}

	toneClass(status) {
		return { critical: "danger", warning: "warning", ok: "success", info: "info" }[status] || "secondary";
	}

	openRoute(key) {
		const workflows = { today: "updates", plans: "buying", ledger: "finance" };
		if (workflows[key]) {
			this.openWorkflow(workflows[key]);
			return;
		}
		const routes = {
			needs: ["List", "FB Inventory Exception"],
			stock: ["List", "Bin"],
			counts: ["List", "FB Inventory Count Task"],
			settings: ["List", "FB Inventory Policy"],
		};
		const route = routes[key];
		if (!route) return;
		frappe.set_route(...route);
	}

	openWorkflow(workflow) {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		if (!warehouse) {
			frappe.msgprint({ title: __("Choose an outlet stock location"), message: __("Choose where this outlet keeps stock first.") });
			return;
		}
		this.activeWorkflow = workflow;
		const target = this.page.body.find("[data-workflow]");
		target.html(`<div class="card"><div class="card-body"><span class="text-muted">${__("Loading this stock workflow…")}</span></div></div>`);
		frappe.call({
			method: "kopos_connector.api.inventory.get_autopilot_workflow",
			type: "GET",
			args: { warehouse, workflow, period_days: this.financePeriodDays },
			callback: (response) => {
				if (this.activeWorkflow !== workflow) return;
				target.html(this.workflowHtml(response.message || {}));
				target[0]?.scrollIntoView({ behavior: "smooth", block: "start" });
			},
			error: () => {
				if (this.activeWorkflow !== workflow) return;
				target.html(`<div class="alert alert-warning"><strong>${__("This stock workflow could not be loaded")}</strong><br>${__("Nothing was changed. Check the outlet and connection, then try again.")} <button type="button" class="btn btn-sm btn-default ml-2" data-primary-route="${this.escapeAttribute(workflow === "buying" ? "plans" : workflow === "updates" ? "today" : "ledger")}">${__("Try again")}</button></div>`);
			},
		});
	}

	workflowHtml(data) {
		const title = { buying: __("Restocking and buying"), updates: __("Stock updates"), finance: __("Stock for finance") }[data.workflow] || __("Stock work");
		const body = data.workflow === "buying"
			? this.buyingWorkflowHtml(data)
			: data.workflow === "updates"
				? this.updatesWorkflowHtml(data)
				: this.financeWorkflowHtml(data);
		return `<div class="card border-info"><div class="card-header d-flex justify-content-between align-items-center"><div><div class="text-muted small text-uppercase font-weight-bold">${__("Follow these steps")}</div><h3 class="mb-0">${this.escape(title)}</h3></div><button type="button" class="btn btn-sm btn-default" data-close-workflow>${__("Back to stock overview")}</button></div><div class="card-body"><h4>${this.escape(data.headline || __("No information available"))}</h4><p class="text-muted">${this.escape(data.next_action || __("Refresh and try again."))}</p>${body}</div></div>`;
	}

	buyingWorkflowHtml(data) {
		const draftStatusLabel = __("Draft — not sent");
		const cards = (Array.isArray(data.items) ? data.items : []).map((plan) => {
			const lines = (Array.isArray(plan.lines) ? plan.lines : []).map((line) => `<tr><td>${this.escape(line.item_name || line.item)}</td><td>${this.escape(line.action)}</td><td>${this.escape(line.quantity)} ${this.escape(line.uom)}</td><td>${this.escape(line.reason || __("JiJi calculated this from current stock and expected use."))}</td></tr>`).join("");
			const drafts = (Array.isArray(plan.draft_documents) ? plan.draft_documents : []).map((draft) => `<div class="alert alert-info mt-3 mb-0"><strong>${this.escape(draft.status || draftStatusLabel)}</strong> · ${this.escape(draft.name)} · ${this.escape(draft.supplier || __("Supplier not shown"))} · ${this.escape(this.moneyLabel(draft.total_sen, draft.currency))}<br><span class="small">${__("The Director reviews this draft, then sends it outside JiJi.")}</span> <button type="button" class="btn btn-sm btn-primary ml-2" data-doctype="Purchase Order" data-open-document="${this.escapeAttribute(draft.name)}">${__("Review draft")}</button></div>`).join("");
			return `<div class="border rounded p-3 mb-3"><div class="d-flex justify-content-between"><strong>${__("Restock plan")}</strong><span class="badge badge-light">${this.escape(plan.status)}</span></div><div class="text-muted small mb-2">${__("Help number")}: ${this.escape(plan.name)} · ${this.escape(plan.planning_date)} · ${this.escape(plan.forecast_state)}</div><table class="table table-sm"><thead><tr><th>${__("Item")}</th><th>${__("Action")}</th><th>${__("Suggested amount")}</th><th>${__("Why")}</th></tr></thead><tbody>${lines || `<tr><td colspan="4">${__("No suggested items are saved in this plan.")}</td></tr>`}</tbody></table>${drafts || `<div class="text-muted small">${__("No supplier order draft has been added yet. Nothing has been sent.")}</div>`}</div>`;
		}).join("");
		return `${cards || `<div class="alert alert-light">${__("No restock plan is waiting. Nothing was created or sent.")}</div>`}<button type="button" class="btn btn-sm btn-default" data-advanced-route="plans">${__("Advanced: detailed restock records")}</button>`;
	}

	updatesWorkflowHtml(data) {
		const scheduler = data.scheduler || {};
		const rows = (Array.isArray(data.items) ? data.items : []).map((row) => `<tr><td>${this.escape(row.name)}</td><td>${this.escape(row.state_label || __("Check status"))}</td><td>${this.escape(this.shortDate(row.last_attempt_at || row.created_at))}</td><td>${this.escape(row.failure_reason || __("No problem recorded"))}</td><td>${this.escape(row.next_action)}</td></tr>`).join("");
		return `<div class="row mb-3"><div class="col-sm-4"><strong>${__("JiJi checks stock")}</strong><br>${__("About every {0} minutes", [this.escape(scheduler.expected_interval_minutes || 5)])}</div><div class="col-sm-4"><strong>${__("Last finished")}</strong><br>${this.escape(this.shortDate(scheduler.last_success))}</div><div class="col-sm-4"><strong>${__("Last problem")}</strong><br>${scheduler.last_failure ? this.escape(this.shortDate(scheduler.last_failure)) : __("No problem recorded")}</div></div><table class="table table-sm"><thead><tr><th>${__("Help number")}</th><th>${__("Status")}</th><th>${__("Last checked")}</th><th>${__("What happened")}</th><th>${__("What to do")}</th></tr></thead><tbody>${rows || `<tr><td colspan="5">${__("JiJi has not finished a stock update for this outlet yet.")}</td></tr>`}</tbody></table><button type="button" class="btn btn-sm btn-primary" data-primary-route="today">${__("Refresh")}</button> <button type="button" class="btn btn-sm btn-default" data-advanced-route="updates">${__("Advanced: technical update history")}</button>`;
	}

	financeWorkflowHtml(data) {
		const summary = data.summary || {};
		const currency = data.currency || "";
		const periods = [7, 30, 90].map((days) => `<button type="button" class="btn btn-sm ${Number(data.period_days) === days ? "btn-primary" : "btn-default"} mr-1" data-finance-period="${days}">${days} ${__("days")}</button>`).join("");
		const balances = (Array.isArray(data.balances) ? data.balances : []).map((row) => `<tr><td>${this.escape(row.item_name || row.item)}</td><td>${this.escape(row.quantity)}</td><td>${this.escape(this.moneyLabel(row.stock_value_sen, currency))}</td></tr>`).join("");
		const movements = (Array.isArray(data.items) ? data.items : []).map((row) => `<tr><td>${this.escape(row.posting_date)}</td><td>${this.escape(row.item_name || row.item)}</td><td>${this.escape(this.movementLabel(row.kind))}</td><td>${this.escape(row.quantity)}</td><td>${this.escape(this.moneyLabel(row.value_change_sen, currency))}</td></tr>`).join("");
		return `<div class="mb-3">${periods}</div><div class="row mb-3"><div class="col-sm-4"><strong>${__("Stock value")}</strong><br>${this.escape(this.moneyLabel(summary.stock_value_sen, currency))}</div><div class="col-sm-2"><strong>${__("Received")}</strong><br>${this.escape(this.moneyLabel(summary.received_sen, currency))}</div><div class="col-sm-2"><strong>${__("Used")}</strong><br>${this.escape(this.moneyLabel(summary.used_sen, currency))}</div><div class="col-sm-2"><strong>${__("Transferred")}</strong><br>${this.escape(this.moneyLabel(summary.transferred_sen, currency))}</div><div class="col-sm-2"><strong>${__("Waste")}</strong><br>${this.escape(this.moneyLabel(summary.waste_sen, currency))}</div></div><h4>${__("Stock on hand")}</h4><table class="table table-sm"><thead><tr><th>${__("Item")}</th><th>${__("Amount on hand")}</th><th>${__("Value")}</th></tr></thead><tbody>${balances || `<tr><td colspan="3">${__("No stock balance is recorded.")}</td></tr>`}</tbody></table><h4>${__("Recent stock changes")}</h4><table class="table table-sm"><thead><tr><th>${__("Date")}</th><th>${__("Item")}</th><th>${__("Change")}</th><th>${__("Quantity")}</th><th>${__("Value change")}</th></tr></thead><tbody>${movements || `<tr><td colspan="5">${__("No stock change is recorded for this period.")}</td></tr>`}</tbody></table><button type="button" class="btn btn-sm btn-default" data-advanced-route="ledger">${__("Advanced: detailed stock records")}</button>`;
	}

	movementLabel(kind) {
		return {
			received: __("Received"),
			used: __("Used"),
			transfer: __("Transferred"),
			waste: __("Waste"),
		}[String(kind || "").toLowerCase()] || __("Stock change");
	}

	openAdvancedRoute(key) {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		const company = this.health && this.health.company;
		if (key === "ledger") frappe.route_options = { company, warehouse };
		if (key === "plans") frappe.route_options = { company, warehouse };
		if (key === "updates") frappe.route_options = { warehouse };
		const route = { plans: ["List", "FB Inventory Plan"], updates: ["List", "FB Projection Log"], ledger: ["query-report", "Stock Ledger"] }[key];
		if (route) frappe.set_route(...route);
	}

	moneyLabel(value, currency) {
		const sen = Number(value || 0);
		return `${currency || ""} ${(sen / 100).toFixed(2)}`.trim();
	}

	openException(serializedRoute) {
		try {
			const route = JSON.parse(serializedRoute);
			if (Array.isArray(route)) frappe.set_route(...route);
		} catch {
			frappe.msgprint({ title: __("Could not open the stock problem"), message: __("Refresh the page and try again.") });
		}
	}

	activateCutover() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		if (!warehouse) {
			frappe.msgprint({ title: __("Choose an outlet stock location"), message: __("Choose where this outlet keeps stock before starting stock tracking.") });
			return;
		}
		frappe.prompt([
			{ fieldname: "policy", fieldtype: "Link", options: "FB Inventory Policy", label: __("Stock settings for this outlet"), reqd: 1, get_query: () => ({ filters: { warehouse } }) },
			{ fieldname: "opening_stock_reconciliation", fieldtype: "Link", options: "Stock Reconciliation", label: __("Checked opening stock count"), description: __("This is the approved starting amount for every ingredient and supply."), reqd: 1, get_query: () => ({ filters: { docstatus: 1 } }) },
		], (values) => {
			frappe.call({
				method: "kopos_connector.api.inventory.activate_inventory_cutover",
				type: "POST",
				args: values,
				callback: (response) => {
					const result = response.message || {};
					frappe.show_alert({ message: result.status === "already_active" ? __("Stock tracking already has a recorded start point. Nothing changed.") : __("Stock tracking has started. Automatic actions still follow the approval settings."), indicator: "green" });
					this.refresh();
				},
				error: () => frappe.msgprint({ title: __("Stock tracking could not start"), message: __("Nothing was changed. Complete the missing setup shown by the system and try again.") }),
			});
		}, __("Start using stock tracking"), __("Start stock tracking"));
	}

	agePhrase(timestamp, asOf) {
		if (!timestamp) return __("time unavailable");
		const age = this.ageMinutes(timestamp, asOf);
		return this.ageMinutesPhrase(age);
	}

	ageMinutesPhrase(minutes) {
		if (minutes == null) return __("age unavailable");
		if (minutes < 1) return __("just now");
		if (minutes < 60) return minutes === 1 ? __("1 minute ago") : __("{0} minutes ago", [minutes]);
		const hours = Math.floor(minutes / 60);
		if (hours < 48) return hours === 1 ? __("1 hour ago") : __("{0} hours ago", [hours]);
		const days = Math.floor(minutes / 1440);
		if (days < 365) return days === 1 ? __("1 day ago") : __("{0} days ago", [days]);
		return __("more than a year ago");
	}

	ageMinutes(timestamp, asOf) {
		const end = asOf ? new Date(asOf).getTime() : Date.now();
		const start = new Date(timestamp).getTime();
		if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
		return Math.max(0, Math.floor((end - start) / 60000));
	}

	shortDate(timestamp) {
		if (!timestamp) return __("not recorded");
		const date = new Date(timestamp);
		return Number.isFinite(date.getTime()) ? date.toLocaleString() : __("not recorded");
	}

	escape(value) {
		return frappe.utils.escape_html(String(value == null ? "" : value));
	}

	escapeAttribute(value) {
		return this.escape(value).replace(/'/g, "&#39;");
	}
}
