frappe.pages["jiji_stock_autopilot"].on_page_load = function (wrapper) {
	new JiJiStockAutopilotPage(wrapper);
};

class JiJiStockAutopilotPage {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
				title: __("Stock"),
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
			const outletInput = this.page.body.find("[data-warehouse-select]");
			this.warehouseControl = {
				$input: outletInput,
				get_value: () => String(outletInput.val() || ""),
				set_value: (value) => {
					outletInput.val(value || "");
					return Promise.resolve();
				},
			};
		this.warehouseControl.$input.on("change awesomplete-selectcomplete", () => this.onWarehouseChanged());
		this.chooseOutletForDirector();
		jiji.desk.injectStyle();
		$(wrapper).addClass("jiji-desk");
		this.page.set_primary_action(__("Check now"), () => this.refresh({ showMissingOutlet: true }));
		this.page.body.on("click", "[data-primary-route]", (event) => {
			event.preventDefault();
			this.openRoute($(event.currentTarget).attr("data-primary-route"));
		});
		this.page.body.on("click", "[data-count-review]", (event) => {
			event.preventDefault();
			this.reviewCount($(event.currentTarget).attr("data-count-review"));
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
						<div><label class="control-label" for="jiji-stock-outlet">${__("Outlet")}</label><select class="form-control" id="jiji-stock-outlet" data-warehouse-select><option value="">${__("Choose an outlet")}</option></select></div>
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
		frappe.call({
			method: "kopos_connector.api.director_scope.get_director_outlet",
			type: "GET",
			callback: (response) => {
				const data = response.message || {};
				const current = this.warehouseControl.get_value();
				const outlets = Array.isArray(data.outlets)
					? data.outlets
					: (data.warehouses || []).map((value) => ({ value, label: this.placeName(value) }));
				const options = outlets.map((row) => `<option value="${this.escapeAttribute(row.value)}">${this.escape(row.label)}</option>`).join("");
				this.warehouseControl.$input.html(`<option value="">${__("Choose an outlet")}</option>${options}`);
				const warehouse = outlets.some((row) => row.value === current)
					? current
					: data.warehouse || "";
				Promise.resolve(this.warehouseControl.set_value(warehouse)).then(() => {
					if (warehouse) this.warehouseControl.$input.trigger("change");
					else this.updateWarehouseHelp();
				});
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

	/**
	 * The name of a place, without the code the ERP appends to it.
	 *
	 * "Matcha & Bean Café - MBC" is a warehouse id. The café is called Matcha
	 * & Bean Café, and the person reading this named it.
	 */
	placeName(value) {
		return String(value || "").replace(/\s+-\s+[A-Z0-9]{2,6}$/, "");
	}

	updateWarehouseHelp() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		const message = warehouse
				? __("Showing {0}.", [this.placeName(warehouse)])
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
					const card = $('<div class="jiji-tile"></div>');
					card.append(jiji.desk.label(tile.label || ""));
					card.append(
						$("<div>")
						.addClass("jiji-tile__value" + (tile.attention ? " jiji-tile__value--attention" : ""))
						.text(tile.value || ""),
				);
					card.append($("<div>").addClass("jiji-tile__detail").text(tile.detail || ""));
					grid.append(card);
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
				this.page.body.find("[data-auto-status]").text(__("JiJi could not check for new stock changes."));
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
				this.page.body.find("[data-auto-status]").text(__("JiJi could not check for new stock changes."));
			},
		});
	}

	renderHealth() {
		const health = this.health || {};
		const exceptions = health.exceptions || {};
		const critical = Number(exceptions.open_critical || 0);
		const warningCount = (exceptions.warning_reasons || []).length;
		const devices = health.devices || {};
		const countReviews = health.count_director_reviews || {};
		const exceptionRouting = health.exception_routing || {};
		const stockReportAge = this.oldestDeviceReportAge(devices, health.as_of);
		const schedulerState = this.schedulerStatus(health);
		const stockNeedsReview = Number(devices.stale || 0) > 0 || Number(devices.dirty || 0) > 0 || !health.overlay?.acknowledged;
		const setupPending = !health.stock_tracking_started && exceptionRouting.status === "configured";
		const overallCritical = ["missing", "policy_missing"].includes(exceptionRouting.status) || (!setupPending && (critical > 0 || (exceptions.critical_reasons || []).length > 0 || health.draft_purchase_order_safety === "unsafe"));
		const overallWarning = setupPending || warningCount > 0 || stockNeedsReview || Number(countReviews.open || 0) > 0 || Number(health.planning?.open || 0) > 0;
		let needs = {
			value: __("Nothing needs your attention"),
			detail: __("JiJi can carry on. Look again later."),
			action: null,
			route: null,
			consequence: __("No urgent stock problem was found."),
		};
		if (["missing", "policy_missing"].includes(exceptionRouting.status)) {
			needs = exceptionRouting.status === "policy_missing"
				? { value: __("Stock settings are not finished"), detail: __("Finish the stock settings for this outlet before starting stock tracking."), action: __("Open stock settings"), route: "settings", consequence: __("Stock tracking cannot start until the setup is finished.") }
				: { value: __("No director is assigned to stock problems"), detail: __("Ask the person who set up JiJi to choose who receives urgent stock problems."), action: __("Open stock settings"), route: "settings", consequence: __("Urgent stock problems cannot reach the right person yet.") };
		} else if (setupPending) {
			needs = { value: __("Start stock tracking"), detail: __("JiJi will use the opening stock already saved for this outlet."), action: __("Start now"), route: "start", consequence: __("Until you start, JiJi will not estimate what is running low or suggest new orders.") };
		} else if (critical > 0) {
			needs = { value: critical === 1 ? __("1 urgent stock problem") : __("{0} urgent stock problems", [critical]), detail: this.firstExceptionDetail(exceptions), action: __("Open stock problems"), route: "needs", consequence: __("Fix this before relying on automatic updates.") };
		} else if (Number(countReviews.open || 0) > 0) {
			const openReviews = Number(countReviews.open || 0);
			needs = { value: openReviews === 1 ? __("1 stock count needs your check") : __("{0} stock counts need your check", [openReviews]), detail: __("Check what your staff counted, then tell JiJi which amount is right."), action: __("See the stock counts"), route: "counts", consequence: __("The count stays saved until you choose the right amount.") };
		} else if (schedulerState === "critical" || schedulerState === "warning") {
			needs = { value: __("JiJi has not finished its first automatic update"), detail: __("JiJi keeps your stock in step with your sales on its own. It has to finish that once before you can rely on it."), action: __("See the automatic updates"), route: "today", consequence: __("Stock may not be current until that update finishes.") };
		} else if (health.draft_purchase_order_safety === "unsafe") {
			needs = { value: __("Your buying limits need checking"), detail: __("Check the minimum and maximum amounts this outlet is allowed to buy. JiJi never sends orders on its own."), action: __("Open stock settings"), route: "settings", consequence: __("Suggested orders stay paused until this is fixed.") };
		} else if ((exceptions.critical_reasons || []).length > 0 || warningCount > 0) {
			needs = { value: __("Something in your stock needs you"), detail: this.firstExceptionDetail(exceptions), action: __("Open stock problems"), route: "needs", consequence: __("Open the problem before relying on the affected automatic update.") };
		} else if (stockNeedsReview) {
			needs = { value: __("A tablet's stock report needs you"), detail: stockReportAge, action: __("See the tablets"), route: "stock", consequence: __("JiJi will use the last saved information until the tablet updates.") };
		} else if (Number(health.planning?.open || 0) > 0) {
			const openPlans = Number(health.planning.open || 0);
			needs = { value: openPlans === 1 ? __("1 suggested order needs your check") : __("{0} suggested orders need your check", [openPlans]), detail: __("Check the amounts and the order before anything is sent."), action: __("See the suggested orders"), route: "plans", consequence: __("Nothing will be ordered until someone approves it.") };
		}
		this.page.set_indicator(
			overallCritical ? __("Action needed") : setupPending ? __("Setup needed") : overallWarning ? __("Review needed") : __("All clear"),
			overallCritical ? "red" : overallWarning ? "orange" : "green",
		);
		// The consequence sits inside the one next step rather than above it.
		// Two full-width alarms stacked on one screen is the pattern this page
		// was redrawn to remove; the reader cannot tell which to act on.
		this.page.body.find("[data-overall]").empty();
		this.renderNextStep(
			needs,
			overallCritical ? "critical" : overallWarning ? "warning" : "ok",
			needs.consequence || (overallCritical
				? __("Fix this before relying on automatic updates.")
				: overallWarning
					? __("JiJi is updating your stock, but some things need you.")
					: __("No urgent stock problem was found.")),
		);

			// The business page keeps only the three decisions a director makes.
			// Update machinery, tablet diagnostics, and policy records belong to the
			// separate support workspace; problems from them still become one plain
			// next step above.
			const sections = [
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
					title: __("Ordering"),
				icon: "▥",
				status: health.draft_purchase_order_safety === "unsafe" ? "critical" : this.summaryStatus(health.planning),
				value: health.draft_purchase_order_safety === "unsafe" ? __("Suggested orders paused") : this.summaryValue(health.planning, __("suggested order waiting"), __("suggested orders waiting")),
				detail: health.draft_purchase_order_safety === "unsafe" ? __("Check your buying limits before JiJi writes another suggestion") : this.summaryAge(health.planning, __("Oldest suggested order")),
					action: __("See suggested orders"),
				route: "plans",
			},
			{
				key: "finance",
				title: __("Cost and value"),
				icon: "◫",
				status: "info",
				value: __("Stock value and changes"),
				detail: __("See what was received, used, transferred, or wasted over 7, 30, or 90 days."),
				action: __("See cost and value"),
				route: "ledger",
			},
			];
			this.page.body.find("[data-sections]").html(
				`<div class="col-12"><div class="jiji-head mb-2"><div class="jiji-label">${__("What do you want to check?")}</div></div></div>` +
				sections.map((section) => this.sectionHtml(section, section.route === needs.route)).join(""),
			);
		this.renderCountReviews(countReviews);
		this.renderExceptions(exceptions.top || [], overallCritical || overallWarning);
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
			target.html(`<div class="alert alert-warning">${this.statusBadge("warning", __("JiJi cannot show the counts just now. Refresh and try again."))}</div>`);
			return;
		}
		if (!Number(summary.open || 0)) {
			target.empty();
			return;
		}
			const cards = (Array.isArray(summary.items) ? summary.items : []).map((review) => {
			const lines = (Array.isArray(review.lines) ? review.lines : []).map((line) => {
				const counted = line.pack_breakdown_display || line.counted_display || __("Count is not ready yet");
				const difference = line.difference_display || __("Difference is not ready yet");
				return `<tr><td>${this.escape(line.item_name || line.item_id)}</td><td>${this.escape(counted)}</td><td>${this.escape(difference)}</td></tr>`;
				}).join("");
				const reviewId = this.escapeAttribute(review.observation_id);
				const when = review.age_minutes == null
					? __("Count waiting for you")
					: __("Count from {0} ago", [this.ageMinutesPhrase(review.age_minutes)]);
				return `<div class="card border-warning mb-3"><div class="card-body">
					<div class="d-flex justify-content-between align-items-start gap-2"><div><div class="mb-1">${this.statusBadge("warning", __("You need to check this"))}</div>
					<h4 class="mb-1">${this.escape(when)}</h4><p class="text-muted small mb-2">${__("This count is a long way from what JiJi expected, so you need to look at it.")}</p></div>
				<button type="button" class="btn btn-sm btn-primary" data-count-review="${reviewId}">${__("Check it and write the order")}</button></div>
					<table class="table table-sm mb-0"><thead><tr><th>${__("Item")}</th><th>${__("Count entered")}</th><th>${__("Difference from what JiJi expected")}</th></tr></thead><tbody>${lines}</tbody></table>
				<div class="text-muted small mt-2">${review.age_minutes == null ? __("Observed time unavailable") : __("Observed {0} ago", [this.ageMinutesPhrase(review.age_minutes)])}</div>
			</div></div>`;
		}).join("");
		target.html(`<h3>${__("Stock counts waiting for you")}</h3><p class="text-muted">${__("Check what staff counted below. JiJi will save the correction for you to check. What staff can sell does not change until someone approves it.")}</p>${cards}`);
	}

	reviewCount(observationId) {
		const id = String(observationId || "");
		const summary = this.health?.count_director_reviews || {};
		const review = (Array.isArray(summary.items) ? summary.items : []).find((item) => String(item.observation_id || "") === id);
		if (!review) {
			frappe.msgprint({ title: __("This count is no longer waiting"), message: __("Refresh the page and check the current count task.") });
			return;
		}
		frappe.prompt([
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Why are you accepting this count?"), description: __("Write a short reason. Do not enter a price or cost."), reqd: 1, default: __("I have checked the real amounts. Save this correction for approval.") },
		], (values) => {
			frappe.call({
				method: "kopos_connector.api.inventory.create_count_reconciliation_after_director_review",
				type: "POST",
				args: { payload: JSON.stringify({ observation_id: id, reason: values.reason }) },
				callback: (response) => {
					const result = response.message || {};
					const message = result.status === "replayed" ? __("This correction already exists. JiJi did not make a second one.") : __("Correction saved. Open it to check and approve the change.");
					frappe.show_alert({ message, indicator: "green" });
					this.refresh();
				},
				error: () => frappe.msgprint({ title: __("The count was not saved"), message: __("Nothing was changed. Refresh the latest stock information and try again.") }),
			});
		}, __("Check this stock count"), __("Save the corrected amount"));
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
		const visibleRows = !this.health?.stock_tracking_started
			? rows.filter((row) => {
				const reason = String(row.reason_code || "").toLowerCase();
				return !reason.includes("cutover_identity")
					&& !reason.includes("before_cutover")
					&& !reason.includes("plan_gate");
			})
			: rows;
		if (!visibleRows.length) {
			const status = otherReviewNeeded ? "info" : "ok";
			const message = otherReviewNeeded ? __("No additional stock problems") : __("No open stock problems");
			target.html(`<div class="alert alert-${this.toneClass(status)}">${this.statusBadge(status, message)}</div>`);
			return;
		}
		// One cause raised four times is one thing to fix, not four alarms.
		// Grouped by what the reader is told, not by the code behind it: two
		// codes that produce the same sentence are one thing to a person.
		const grouped = new Map();
		visibleRows.forEach((row) => {
			const key = this.plainExceptionMessage(row.reason_code);
			const seen = grouped.get(key);
			if (seen) {
				seen.count += 1;
				return;
			}
			grouped.set(key, { row, count: 1 });
		});
		const cards = [...grouped.values()].map(({ row, count }) => {
			const severity = String(row.severity || "Warning").toLowerCase() === "critical" ? "critical" : "warning";
				const again = count > 1 ? ` · ${__("{0} times", [count])}` : "";
			return `<div class="jiji-row"><span class="jiji-row__main">
					<span class="jiji-pill jiji-pill--${severity === "critical" ? "stop" : "attention"}">${severity === "critical" ? __("Urgent") : __("Please check")}</span>
					<div class="mt-2" style="font-weight:600">${this.escape(this.plainExceptionMessage(row.reason_code))}</div>
						<div class="jiji-row__note">${__("Found {0}", [this.agePhrase(row.last_seen, this.health?.as_of)])}${again}</div>
					</span>
				</div>`;
		}).join("");
		target.html(`<div class="jiji-head mb-2"><div class="jiji-label">${__("Stock problems open now")}</div>
			<span class="jiji-muted">${__("Each problem appears once, with what to do.")}</span></div>
			<div class="jiji-rows">${cards}</div>`);
	}

	firstExceptionDetail(exceptions) {
		const row = exceptions.top && exceptions.top[0];
		if (!row) return __("Open stock problems to see what needs fixing.");
		const age = row.age_minutes == null ? __("age unavailable") : this.ageMinutesPhrase(row.age_minutes);
		return `${this.plainExceptionMessage(row.reason_code)} · ${age}`;
	}

	plainExceptionMessage(reasonCode) {
		const reason = String(reasonCode || "").toLowerCase();
		if (reason === "operating_day_evidence_missing") {
			return __("JiJi needs a few more recent sales before it can suggest what to order.");
		}
		if (reason === "inventory_count_schedule_unconfigured") {
			return __("Choose how often this outlet should count stock.");
		}
		if (reason === "inventory_count_missing_uom_conversion") {
			return __("One item's pack size is missing, so JiJi cannot turn packs into the amount on hand.");
		}
		if (reason === "inventory_count_stale_watermark") {
			return __("Stock changed while this count was being checked. Open the count and choose the right amount.");
		}
		if (reason === "inventory_count_director_review") {
			return __("A stock count needs you to choose the right amount.");
		}
		if (reason.includes("scheduler") || reason.includes("projection")) {
			return __("JiJi could not finish updating your stock. Nothing you saved is lost.");
		}
		if (reason.includes("cutover_identity") || reason.includes("before_cutover")) {
			return __("Stock tracking has not been started for this outlet, so JiJi cannot do this yet.");
		}
		if (reason.includes("plan_gate")) {
			return __("JiJi could not finish a suggested order because one of its safety checks did not pass.");
		}
		if (reason.includes("cost")) return __("One or more ingredient costs need checking.");
		if (reason.includes("purchase") || reason.includes("outbound")) return __("Your buying limits need checking before JiJi can write another order.");
		if (reason.includes("routing") || reason.includes("director")) return __("Choose who should receive urgent stock problems.");
		// The saved exception keeps the support code. The beginner page never
		// makes a director decode it just to decide what to do next.
		return __("JiJi found a stock problem. Open it to see what needs fixing.");
	}

	oldestDeviceReportAge(devices, asOf) {
		const acknowledgements = Array.isArray(devices.acknowledgements) ? devices.acknowledgements : [];
		const ages = acknowledgements
			.map((row) => this.ageMinutes(row.effective_at, asOf))
			.filter((value) => value != null);
		return ages.length ? __("Oldest tablet update: {0}", [this.ageMinutesPhrase(Math.max(...ages))]) : __("No tablet update yet");
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

	toneClass(status) {
		return { critical: "danger", warning: "warning", ok: "success", info: "info" }[status] || "secondary";
	}

	openRoute(key) {
		if (key === "start") {
			this.activateCutover();
			return;
		}
		const workflows = { today: "updates", plans: "buying", ledger: "finance" };
		if (workflows[key]) {
			this.openWorkflow(workflows[key]);
			return;
		}
		if (key === "needs") {
			this.page.body.find("[data-exceptions]")[0]?.scrollIntoView({ behavior: "smooth", block: "start" });
			return;
		}
		if (key === "counts") {
			this.page.body.find("[data-count-reviews]")[0]?.scrollIntoView({ behavior: "smooth", block: "start" });
			return;
		}
		if (key === "stock") {
			jiji.desk.go(["jiji_staff"]);
			return;
		}
		if (key === "supplies") {
			jiji.desk.go(["jiji_supplies"]);
				return;
			}
		if (key === "settings") {
			frappe.msgprint({
				title: __("Ask Support to check this setup"),
				message: __("Tell Support which outlet you chose and what this page says. Your saved stock will not be removed."),
			});
		}
	}

	openWorkflow(workflow) {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		if (!warehouse) {
			frappe.msgprint({ title: __("Choose an outlet stock location"), message: __("Choose where this outlet keeps stock first.") });
			return;
		}
		this.activeWorkflow = workflow;
		const target = this.page.body.find("[data-workflow]");
		target.html(`<div class="card"><div class="card-body"><span class="text-muted">${__("Opening this…")}</span></div></div>`);
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
				target.html(`<div class="alert alert-warning"><strong>${__("This could not be opened")}</strong><br>${__("Nothing was changed. Check the outlet and connection, then try again.")} <button type="button" class="btn btn-sm btn-default ml-2" data-primary-route="${this.escapeAttribute(workflow === "buying" ? "plans" : workflow === "updates" ? "today" : "ledger")}">${__("Try again")}</button></div>`);
			},
		});
	}

	workflowHtml(data) {
		const title = { buying: __("Ordering"), updates: __("Stock updates"), finance: __("Stock for finance") }[data.workflow] || __("Stock");
		const body = data.workflow === "buying"
			? this.buyingWorkflowHtml(data)
			: data.workflow === "updates"
				? this.updatesWorkflowHtml(data)
				: this.financeWorkflowHtml(data);
		return `<div class="card border-info"><div class="card-header d-flex justify-content-between align-items-center"><div><div class="text-muted small text-uppercase font-weight-bold">${__("Follow these steps")}</div><h3 class="mb-0">${this.escape(title)}</h3></div><button type="button" class="btn btn-sm btn-default" data-close-workflow>${__("Back to stock overview")}</button></div><div class="card-body"><h4>${this.escape(data.headline || __("No information available"))}</h4><p class="text-muted">${this.escape(data.next_action || __("Refresh and try again."))}</p>${body}</div></div>`;
	}

	buyingWorkflowHtml(data) {
		const draftStatusLabel = __("Written — not sent");
		const cards = (Array.isArray(data.items) ? data.items : []).map((plan) => {
			const lines = (Array.isArray(plan.lines) ? plan.lines : []).map((line) => `<tr><td>${this.escape(line.item_name || __("Ingredient or supply"))}</td><td>${this.escape(line.action_label || this.stockActionLabel(line.action))}</td><td>${this.escape(line.quantity_display || line.quantity)}</td><td>${this.escape(line.reason || __("JiJi calculated this from current stock and expected use."))}</td></tr>`).join("");
				const drafts = (Array.isArray(plan.draft_documents) ? plan.draft_documents : []).map((draft) => {
					const details = [draft.supplier, Number.isInteger(draft.total_sen) ? this.moneyLabel(draft.total_sen, draft.currency) : ""].filter(Boolean).map((value) => this.escape(value)).join(" · ");
					return `<div class="alert alert-info mt-3 mb-0"><strong>${this.escape(draft.document_label || __("Supplier order"))}</strong> · ${this.escape(draft.status || draftStatusLabel)}${details ? ` · ${details}` : ""}<br><span class="small">${this.escape(draft.next_action || __("Open Supplies to continue."))}</span> <button type="button" class="btn btn-sm btn-primary ml-2" data-primary-route="supplies">${__("Open Supplies")}</button></div>`;
			}).join("");
			return `<div class="border rounded p-3 mb-3"><strong>${__("Suggested order")}</strong><div class="text-muted small mb-2">${this.escape(plan.planning_date || "")}</div><table class="table table-sm"><thead><tr><th>${__("Ingredient or supply")}</th><th>${__("What to do")}</th><th>${__("Suggested amount")}</th><th>${__("Why")}</th></tr></thead><tbody>${lines || `<tr><td colspan="4">${__("Nothing is suggested in this order.")}</td></tr>`}</tbody></table>${drafts || `<div class="text-muted small">${__("No order has been written yet. Nothing has been sent.")}</div>`}</div>`;
		}).join("");
		return cards || `<div class="alert alert-light">${__("No order is suggested. Nothing was created or sent.")}</div>`;
	}

	updatesWorkflowHtml(data) {
		const scheduler = data.scheduler || {};
			const rows = (Array.isArray(data.items) ? data.items : []).map((row) => `<tr><td>${this.escape(row.state_label || __("Check status"))}</td><td>${this.escape(this.shortDate(row.checked_at))}</td><td>${this.escape(row.failure_reason || __("No problem recorded"))}</td><td>${this.escape(row.next_action)}</td></tr>`).join("");
		return `<div class="row mb-3"><div class="col-sm-4"><strong>${__("JiJi checks stock")}</strong><br>${__("About every {0} minutes", [this.escape(scheduler.expected_interval_minutes || 5)])}</div><div class="col-sm-4"><strong>${__("Last finished")}</strong><br>${this.escape(this.shortDate(scheduler.last_success))}</div><div class="col-sm-4"><strong>${__("Last problem")}</strong><br>${scheduler.last_failure ? this.escape(this.shortDate(scheduler.last_failure)) : __("No problem recorded")}</div></div><table class="table table-sm"><thead><tr><th>${__("Status")}</th><th>${__("Last checked")}</th><th>${__("What happened")}</th><th>${__("What to do")}</th></tr></thead><tbody>${rows || `<tr><td colspan="4">${__("JiJi has not updated this outlet's stock yet.")}</td></tr>`}</tbody></table><button type="button" class="btn btn-sm btn-primary" data-primary-route="today">${__("Refresh")}</button>`;
	}

	financeWorkflowHtml(data) {
		const summary = data.summary || {};
		const currency = data.currency || "";
		const periods = [7, 30, 90].map((days) => `<button type="button" class="btn btn-sm ${Number(data.period_days) === days ? "btn-primary" : "btn-default"} mr-1" data-finance-period="${days}">${days} ${__("days")}</button>`).join("");
			const balances = (Array.isArray(data.balances) ? data.balances : []).map((row) => `<tr><td>${this.escape(row.item_name || __("Ingredient or supply"))}</td><td>${this.escape(row.quantity_display)}</td><td>${this.escape(this.moneyLabel(row.stock_value_sen, currency))}</td></tr>`).join("");
			const movements = (Array.isArray(data.items) ? data.items : []).map((row) => `<tr><td>${this.escape(row.posting_date)}</td><td>${this.escape(row.item_name || __("Ingredient or supply"))}</td><td>${this.escape(this.movementLabel(row.kind))}</td><td>${this.escape(row.quantity_display)}</td><td>${this.escape(this.moneyLabel(row.value_change_sen, currency))}</td></tr>`).join("");
		const balancesNote = data.balances_truncated ? `<div class="alert alert-light small">${__("The total includes everything. This table shows the first 100 ingredients and supplies.")}</div>` : "";
		const movementsNote = data.items_truncated ? `<div class="alert alert-light small">${__("The totals include every stock change. This table shows the latest 100 changes.")}</div>` : "";
		return `<div class="mb-3">${periods}</div><div class="row mb-3"><div class="col-sm-4"><strong>${__("Stock value")}</strong><br>${this.escape(this.moneyLabel(summary.stock_value_sen, currency))}</div><div class="col-sm-2"><strong>${__("Received")}</strong><br>${this.escape(this.moneyLabel(summary.received_sen, currency))}</div><div class="col-sm-2"><strong>${__("Used")}</strong><br>${this.escape(this.moneyLabel(summary.used_sen, currency))}</div><div class="col-sm-2"><strong>${__("Transferred")}</strong><br>${this.escape(this.moneyLabel(summary.transferred_sen, currency))}</div><div class="col-sm-2"><strong>${__("Waste")}</strong><br>${this.escape(this.moneyLabel(summary.waste_sen, currency))}</div></div><h4>${__("Stock on hand")}</h4>${balancesNote}<table class="table table-sm"><thead><tr><th>${__("Ingredient or supply")}</th><th>${__("Amount on hand")}</th><th>${__("Value")}</th></tr></thead><tbody>${balances || `<tr><td colspan="3">${__("No stock balance is recorded.")}</td></tr>`}</tbody></table><h4>${__("Recent stock changes")}</h4>${movementsNote}<table class="table table-sm"><thead><tr><th>${__("Date")}</th><th>${__("Ingredient or supply")}</th><th>${__("Change")}</th><th>${__("Amount")}</th><th>${__("Value change")}</th></tr></thead><tbody>${movements || `<tr><td colspan="5">${__("No stock change is recorded for this period.")}</td></tr>`}</tbody></table>`;
	}

	stockActionLabel(action) {
		return {
			Purchase: __("Order from supplier"),
			Manufacture: __("Prepare in the café"),
			Transfer: __("Move from another outlet"),
		}[String(action || "")] || __("Check this");
	}

	movementLabel(kind) {
		return {
			received: __("Received"),
			used: __("Used"),
			transfer: __("Transferred"),
			waste: __("Waste"),
		}[String(kind || "").toLowerCase()] || __("Stock change");
	}

	moneyLabel(value, currency) {
		const sen = Number(value || 0);
		return `${currency || ""} ${(sen / 100).toFixed(2)}`.trim();
	}

	activateCutover() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		if (!warehouse) {
			frappe.msgprint({ title: __("Choose an outlet stock location"), message: __("Choose where this outlet keeps stock before starting stock tracking.") });
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Start stock tracking?"),
			fields: [{
				fieldtype: "HTML",
				fieldname: "explanation",
				options: `<p>${__("JiJi will use the opening stock already saved for this outlet as the starting amount for every ingredient and supply.")}</p>`,
			}],
			primary_action_label: __("Start stock tracking"),
			primary_action: () => {
				dialog.hide();
				frappe.call({
					method: "kopos_connector.api.inventory.start_inventory_tracking",
					type: "POST",
					args: { warehouse },
					callback: (response) => {
						const result = response.message || {};
						frappe.show_alert({ message: result.message || __("Stock tracking has started."), indicator: "green" });
						this.refresh();
					},
					error: () => frappe.msgprint({ title: __("Stock tracking could not start"), message: __("Nothing was changed. Follow the message above or ask Support to check this outlet.") }),
				});
			},
			secondary_action_label: __("Not now"),
			secondary_action: () => dialog.hide(),
		});
		dialog.show();
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
		if (!timestamp) return null;
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
