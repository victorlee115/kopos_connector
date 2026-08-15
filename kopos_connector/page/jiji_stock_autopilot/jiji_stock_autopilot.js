frappe.pages["jiji_stock_autopilot"].on_page_load = function (wrapper) {
	new JiJiStockAutopilotPage(wrapper);
};

class JiJiStockAutopilotPage {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("JiJi Stock Autopilot"),
			single_column: true,
		});
		this.loadedWarehouse = "";
		this.health = null;
		this.render();
		this.warehouseControl = frappe.ui.form.make_control({
			parent: this.page.body.find("[data-warehouse]")[0],
			df: {
				fieldtype: "Link",
				fieldname: "warehouse",
				options: "Warehouse",
				label: __("Outlet warehouse"),
				description: __("All information below is for this warehouse only."),
				reqd: 1,
			},
		});
		this.warehouseControl.refresh();
		this.warehouseControl.$input.on("change awesomplete-selectcomplete", () => this.onWarehouseChanged());
		this.page.set_primary_action(__("Refresh"), () => this.refresh());
		this.page.add_inner_button(__("Activate cutover"), () => this.activateCutover());
		this.page.body.on("click", "[data-primary-route]", (event) => {
			event.preventDefault();
			this.openRoute($(event.currentTarget).attr("data-primary-route"));
		});
		this.page.body.on("click", "[data-exception-route]", (event) => {
			event.preventDefault();
			this.openException($(event.currentTarget).attr("data-exception-route"));
		});
	}

	render() {
		$(this.page.body).html(`
			<div class="jiji-stock-autopilot">
				<div class="alert alert-info mb-4">
					${__("This page tells directors what needs attention. It does not replace ERPNext forms: use the guided POS tasks for physical work and standard forms for approvals and documents.")}
				</div>
				<div class="row align-items-end mb-4">
					<div class="col-sm-5"><div data-warehouse></div></div>
					<div class="col-sm-7"><p data-warehouse-help class="text-muted mb-0"></p></div>
				</div>
				<div data-overall class="mb-4"></div>
				<div data-sections class="row"></div>
				<div data-exceptions class="mt-4"></div>
			</div>`);
		this.updateWarehouseHelp();
	}

	onWarehouseChanged() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		this.updateWarehouseHelp();
		if (!warehouse || warehouse === this.loadedWarehouse) return;
		this.loadedWarehouse = warehouse;
		this.refresh();
	}

	updateWarehouseHelp() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		const message = warehouse
			? __("Showing one safe, warehouse-scoped view for {0}. Refresh only reads state.", [warehouse])
			: __("Select an outlet warehouse to see its six work areas.");
		this.page.body.find("[data-warehouse-help]").text(message);
	}

	refresh() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		this.updateWarehouseHelp();
		if (!warehouse) {
			frappe.msgprint({ title: __("Choose an outlet warehouse"), message: __("Select a warehouse before refreshing.") });
			return;
		}
		this.page.set_indicator(__("Loading"), "orange");
		frappe.call({
			method: "kopos_connector.api.inventory.get_autopilot_health",
			type: "GET",
			args: { warehouse },
			callback: (response) => {
				this.health = response.message || {};
				this.loadedWarehouse = warehouse;
				this.renderHealth();
			},
			error: () => {
				this.page.set_indicator(__("Could not read health"), "red");
				frappe.msgprint({
					title: __("Health check failed"),
					message: __("No stock or document was changed. Check the warehouse and try again."),
				});
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
		const stockReportAge = this.oldestDeviceReportAge(devices, health.as_of);
		const overallCritical = critical > 0 || (exceptions.critical_reasons || []).length > 0 || health.draft_purchase_order_safety === "unsafe";
		const overallWarning = warningCount > 0 || Number(devices.stale || 0) > 0 || Number(devices.dirty || 0) > 0;
		this.page.set_indicator(
			overallCritical ? __("Action needed") : overallWarning ? __("Review needed") : __("All clear"),
			overallCritical ? "red" : overallWarning ? "orange" : "green",
		);
		this.page.body.find("[data-overall]").html(this.statusBanner(
			overallCritical ? "critical" : overallWarning ? "warning" : "ok",
			overallCritical ? __("Inventory automation is stopped for review until the critical issue is resolved.") : overallWarning ? __("Inventory is running, but one or more items need review.") : __("No critical inventory issue is reported."),
		));

		const sections = [
			{
				key: "needs",
				title: __("Needs you"),
				icon: "⚠",
				status: overallCritical ? "critical" : overallWarning ? "warning" : "ok",
				value: critical ? __("{0} critical exception(s)", [critical]) : __("No critical exceptions"),
				detail: this.firstExceptionDetail(exceptions),
				action: __("Open exceptions"),
				route: "needs",
			},
			{
				key: "today",
				title: __("Today"),
				icon: "◷",
				status: this.schedulerStatus(health),
				value: scheduler.last_success ? __("Last scheduler success {0} ago", [this.agePhrase(scheduler.last_success, health.as_of)]) : __("No scheduler success recorded"),
				detail: scheduler.next_success_deadline ? __("Next success expected by {0}", [this.shortDate(scheduler.next_success_deadline)]) : __("Scheduler must report one successful run before rollout"),
				action: __("Open projection logs"),
				route: "today",
			},
			{
				key: "stock",
				title: __("Stock"),
				icon: "▣",
				status: Number(devices.stale || 0) || Number(devices.dirty || 0) || !health.overlay?.acknowledged ? "warning" : "ok",
				value: __("{0} current · {1} stale · {2} need sync", [Number(devices.current || 0), Number(devices.stale || 0), Number(devices.dirty || 0)]),
				detail: `${health.overlay?.acknowledged ? __("Every reported catalog and stock overlay is acknowledged") : __("{0} device(s) have not acknowledged the latest catalog or overlay", [Number(health.overlay?.unacknowledged_devices || 0)])} · ${stockReportAge}`,
				action: __("Open stock"),
				route: "stock",
			},
			{
				key: "counts",
				title: __("Counts"),
				icon: "▤",
				status: this.summaryStatus(health.counts),
				value: this.summaryValue(health.counts, __("open count task(s)")),
				detail: this.summaryAge(health.counts, __("Oldest count")),
				action: __("Open count tasks"),
				route: "counts",
			},
			{
				key: "plans",
				title: __("Plans & buying"),
				icon: "▥",
				status: health.draft_purchase_order_safety === "unsafe" ? "critical" : this.summaryStatus(health.planning),
				value: health.draft_purchase_order_safety === "unsafe" ? __("Draft PO creation is paused") : this.summaryValue(health.planning, __("open plan(s)")),
				detail: health.draft_purchase_order_safety === "unsafe" ? __("Review the outbound-safety configuration before buying automation") : this.summaryAge(health.planning, __("Oldest plan")),
				action: __("Open plans"),
				route: "plans",
			},
			{
				key: "settings",
				title: __("Settings"),
				icon: "⚙",
				status: runtime.status === "verified" ? "info" : "critical",
				value: health.automation_state || __("Review First"),
				detail: runtime.status === "verified" ? `${__("Runtime artifact identity verified · source freshness {0} minutes", [Number(health.max_source_age_minutes || 30)])} · ${this.checkedAge(health.as_of)}` : __("Runtime artifact identity is unavailable; rollout is blocked"),
				action: __("Open inventory policy"),
				route: "settings",
			},
		];
		this.page.body.find("[data-sections]").html(sections.map((section) => this.sectionHtml(section)).join(""));
		this.renderExceptions(exceptions.top || []);
	}

	sectionHtml(section) {
		return `<div class="col-sm-6 col-lg-4 mb-4"><div class="card h-100 border-${this.toneClass(section.status)}">
			<div class="card-body d-flex flex-column"><h4>${section.icon} ${section.title}</h4>
				<div class="mb-2">${this.statusBadge(section.status, this.statusLabel(section.status))}</div>
				<div class="font-weight-bold mb-1">${this.escape(section.value)}</div>
				<p class="text-muted small mb-3">${this.escape(section.detail)}</p>
				<button type="button" class="btn btn-sm btn-default mt-auto align-self-start" data-primary-route="${section.route}">${this.escape(section.action)}</button>
			</div></div></div>`;
	}

	renderExceptions(rows) {
		const target = this.page.body.find("[data-exceptions]");
		if (!rows.length) {
			target.html(`<div class="alert alert-success">${this.statusBadge("ok", __("No open exceptions"))}</div>`);
			return;
		}
		const cards = rows.map((row) => {
			const severity = String(row.severity || "Warning").toLowerCase() === "critical" ? "critical" : "warning";
			const route = row.source_doctype && row.source_name
				? JSON.stringify(["Form", row.source_doctype, row.source_name])
				: JSON.stringify(["Form", "FB Inventory Exception", row.name]);
			return `<div class="border rounded p-3 mb-2"><div class="d-flex justify-content-between align-items-start gap-2">
				<div><div class="mb-1">${this.statusBadge(severity, row.severity || __("Warning"))}</div><div class="font-weight-bold">${this.escape(row.summary || row.reason_code)}</div><div class="text-muted small">${this.escape(row.next_action || __("Open the exception and follow the recommended action."))}</div></div>
				<button type="button" class="btn btn-sm btn-default" data-exception-route='${this.escapeAttribute(route)}'>${__("Open")}</button>
			</div><div class="text-muted small mt-2">${this.escape(row.reason_code)} · ${this.agePhrase(row.last_seen, this.health?.as_of)}</div></div>`;
		}).join("");
		target.html(`<h3>${__("Open exceptions needing attention")}</h3>${cards}`);
	}

	firstExceptionDetail(exceptions) {
		const row = exceptions.top && exceptions.top[0];
		if (!row) return __("No director action is currently required");
		const age = row.age_minutes == null ? __("age unavailable") : this.ageMinutesPhrase(row.age_minutes);
		return `${row.next_action || row.summary || row.reason_code} · ${age}`;
	}

	oldestDeviceReportAge(devices, asOf) {
		const acknowledgements = Array.isArray(devices.acknowledgements) ? devices.acknowledgements : [];
		const ages = acknowledgements
			.map((row) => this.ageMinutes(row.effective_at, asOf))
			.filter((value) => value != null);
		return ages.length ? __("Oldest report: {0}", [this.ageMinutesPhrase(Math.max(...ages))]) : __("No device report yet");
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

	summaryValue(summary, label) {
		if (!summary || summary.status === "not_installed") return __("Not configured");
		if (summary.status === "unavailable") return __("Could not read");
		return `${Number(summary.open || 0)} ${label}`;
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
		const routes = {
			needs: ["List", "FB Inventory Exception"],
			today: ["List", "FB Projection Log"],
			stock: ["List", "Bin"],
			counts: ["List", "FB Inventory Count Task"],
			plans: ["List", "FB Inventory Plan"],
			settings: ["List", "FB Inventory Policy"],
		};
		const route = routes[key];
		if (route) frappe.set_route(...route);
	}

	openException(serializedRoute) {
		try {
			const route = JSON.parse(serializedRoute);
			if (Array.isArray(route)) frappe.set_route(...route);
		} catch {
			frappe.msgprint({ title: __("Could not open exception"), message: __("Refresh the page and try again.") });
		}
	}

	activateCutover() {
		const warehouse = this.warehouseControl && this.warehouseControl.get_value();
		if (!warehouse) {
			frappe.msgprint({ title: __("Choose an outlet warehouse"), message: __("Select a warehouse before recording cutover.") });
			return;
		}
		frappe.prompt([
			{ fieldname: "policy", fieldtype: "Link", options: "FB Inventory Policy", label: __("Inventory Policy"), reqd: 1, get_query: () => ({ filters: { warehouse } }) },
			{ fieldname: "opening_stock_reconciliation", fieldtype: "Link", options: "Stock Reconciliation", label: __("Submitted opening Stock Reconciliation"), reqd: 1, get_query: () => ({ filters: { docstatus: 1 } }) },
		], (values) => {
			frappe.call({
				method: "kopos_connector.api.inventory.activate_inventory_cutover",
				type: "POST",
				args: values,
				callback: (response) => {
					const result = response.message || {};
					frappe.show_alert({ message: result.status === "already_active" ? __("Cutover is already recorded; its identity was preserved.") : __("Cutover recorded. Automation remains in Review First."), indicator: "green" });
					this.refresh();
				},
				error: () => frappe.msgprint({ title: __("Cutover blocked"), message: __("No cutover identity was changed. Fix the prerequisite shown by ERPNext and try again.") }),
			});
		}, __("Record inventory cutover"), __("Record cutover"));
	}

	agePhrase(timestamp, asOf) {
		if (!timestamp) return __("time unavailable");
		const age = this.ageMinutes(timestamp, asOf);
		return this.ageMinutesPhrase(age);
	}

	ageMinutesPhrase(minutes) {
		if (minutes == null) return __("age unavailable");
		if (minutes < 1) return __("just now");
		if (minutes < 60) return __("{0} min ago", [minutes]);
		return __("{0} hr ago", [Math.floor(minutes / 60)]);
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
