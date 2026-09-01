/**
 * JiJi Supplies.
 *
 * A Purchase Order form asks a director to approve five tabs, a truncated item
 * code and a Submit button. This page shows the five facts the decision needs
 * -- what, how much, what it costs, when it lands, whether they deliver on time
 * -- and the one sentence explaining why JiJi suggested it.
 *
 * Nothing is sent from here. The Purchase Order is the accounting document and
 * the only place it is submitted, so the page ends at a link to it. Saying so
 * on the button is the point: a control should say exactly what happens.
 *
 * Routing and the shared look live in public/js/jiji_desk.js.
 */

frappe.pages["jiji_supplies"].on_page_load = function (wrapper) {
	new JiJiSupplies(wrapper);
};

class JiJiSupplies {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Supplies"),
			single_column: true,
		});
		this.page.set_primary_action(__("Refresh"), () => this.loadChoices(), "refresh");
		this.body = jiji.desk.page(this.page);
		this.outletBar = $("<div>").addClass("jiji-supplies__outletbar row").appendTo(this.body);
		this.outletBar.append(`<div class="col-sm-6"><label class="control-label" for="jiji-supplies-cafe">${__("Café")}</label><select class="form-control" id="jiji-supplies-cafe"><option value="">${__("Choose a café")}</option></select></div>`);
		this.outletBar.append(`<div class="col-sm-6"><label class="control-label" for="jiji-supplies-outlet">${__("Outlet")}</label><select class="form-control" id="jiji-supplies-outlet"><option value="">${__("Choose an outlet")}</option></select></div>`);
		this.cafeInput = this.outletBar.find("#jiji-supplies-cafe");
		this.outletInput = this.outletBar.find("#jiji-supplies-outlet");
		this.cafeInput.on("change", () => this.loadChoices());
		this.outletInput.on("change", () => this.load());
		this.content = $("<div>").appendTo(this.body);
		this.injectStyle();
		this.loadChoices();
	}

	injectStyle() {
		if (document.getElementById("jiji-supplies-style")) return;
		const style = document.createElement("style");
		style.id = "jiji-supplies-style";
		style.textContent = `
.jiji-supplies__head { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; }
.jiji-supplies__supplier { font-size: 21px; font-weight: 600; letter-spacing: -.01em; }
.jiji-supplies__total { margin-left: auto; font-size: 29px; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
.jiji-supplies__why { border-left: 3px solid var(--jiji-accent); padding: 12px 0 12px 16px; margin: 16px 0; }
.jiji-supplies__why-title { font-weight: 600; }
.jiji-supplies__why-detail { color: var(--jiji-ink-2); margin-top: 2px; }
.jiji-supplies__actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 16px; }
.jiji-supplies__when { margin-left: auto; color: var(--jiji-ink-3); font-size: 12px; }
.jiji-supplies__amount { color: var(--jiji-ink-2); }
.jiji-supplies__cost { font-variant-numeric: tabular-nums; color: var(--jiji-ink-2); min-width: 92px; text-align: right; }
.jiji-supplies__date { color: var(--jiji-ink-3); font-size: 12px; width: 74px; flex: 0 0 auto; }
.jiji-supplies__why + .jiji-supplies__actions { margin-top: 0; }
.jiji-supplies__history-total { font-weight: 600; font-variant-numeric: tabular-nums; min-width: 100px; text-align: right; }
	.jiji-supplies__outletbar { max-width: 760px; margin-bottom: 16px; }
`;
		document.head.appendChild(style);
	}

	setSelectOptions(select, rows, prompt, selected) {
		select.empty().append($("<option>").val("").text(prompt));
		(rows || []).forEach((row) => select.append($("<option>").val(row.value || "").text(row.label || row.value || "")));
		select.val(selected || "");
	}

	loadChoices() {
		const requestedCafe = String(this.cafeInput.val() || "");
		frappe.call({
			method: "kopos_connector.api.supplier_orders.get_supplier_page_choices",
			type: "GET",
			args: requestedCafe ? { company: requestedCafe } : {},
			callback: (response) => {
				const choices = response.message || {};
				const selectedCafe = requestedCafe || choices.selected_cafe || "";
				const currentOutlet = String(this.outletInput.val() || "");
				this.setSelectOptions(this.cafeInput, choices.cafes, __("Choose a café"), selectedCafe);
				const outlet = (choices.outlets || []).some((row) => row.value === currentOutlet)
					? currentOutlet
					: choices.selected_outlet || "";
				this.setSelectOptions(this.outletInput, choices.outlets, __("Choose an outlet"), outlet);
				this.load();
			},
			error: () => this.content.empty().append(jiji.desk.notice(__("JiJi could not load your outlets"), __("Nothing changed. Refresh and try again."))),
		});
	}

	load() {
		const company = String(this.cafeInput.val() || "");
		const warehouse = String(this.outletInput.val() || "");
		if (!company || !warehouse) {
			this.content.empty().append(jiji.desk.notice(__("Choose a café and outlet"), __("Supplier orders are shown one outlet at a time.")));
			return;
		}
		frappe.call({
			method: "kopos_connector.api.supplier_orders.get_supplier_orders",
			type: "GET",
			args: { company, warehouse },
			callback: (response) => {
				const data = response.message || {};
				this.render(data);
			},
			error: () =>
				this.content.empty().append(
					jiji.desk.notice(
						__("JiJi could not read your supplier orders just now"),
						__("Nothing has been ordered or changed. Try Refresh in a moment."),
					),
				),
		});
	}

	render(data) {
		this.content.empty();
		if (data.status === "permission_denied") {
			this.content.append(
				jiji.desk.notice(
					__("You do not have access to supplier orders"),
						__("Ask a director to check your café and outlet access."),
				),
			);
			return;
		}
		if (data.status === "selection_required") {
			this.content.append(
				jiji.desk.notice(
					__("Choose an outlet"),
					__("Supplier orders and deliveries are shown one outlet at a time."),
				),
			);
			return;
		}
		if (data.status !== "ok") {
			this.content.append(
				jiji.desk.notice(
					__("JiJi could not read your supplier orders just now"),
					__("Nothing has been ordered or changed. Try Refresh in a moment."),
				),
			);
			return;
		}

		this.page.set_indicator(
			data.draft_count === 1
				? __("1 waiting")
				: __("{0} waiting", [data.draft_count || 0]),
			data.draft_count ? "orange" : "green",
		);

		if (!data.draft_count) {
			this.content.append(
				jiji.desk.notice(
					__("Nothing is waiting to be ordered"),
					__("JiJi writes one when something will run out before a delivery could arrive."),
				),
			);
		}

		if ((data.detailed || []).length) {
			// Every other page names its first block. This one began mid-air.
			this.content.append(jiji.desk.label(__("Waiting for your approval")));
		}
		(data.detailed || []).forEach((draft) => {
			this.content.append(this.renderDraft(draft));
			// The supplier's record is its own block. Folded into the decision
			// card it read as a footnote to the order rather than as the
			// evidence the decision is weighed against.
			if ((draft.history || []).length) this.content.append(this.renderHistory(draft));
		});

		if ((data.other_drafts || []).length) {
				const block = $("<div>").append(jiji.desk.label(__("Also waiting")));
				const list = $("<div>").addClass("jiji-rows").appendTo(block);
				data.other_drafts.forEach((draft) => {
					$("<div>")
						.addClass("jiji-row")
						.append($("<span>").addClass("jiji-row__main").text(draft.supplier || ""))
						.append($("<span>").addClass("jiji-num").text(draft.total || ""))
						.appendTo(list);
				});
				block.append($("<p>").addClass("jiji-muted mt-2").text(__("Finish the first order to check the next one.")));
			this.content.append(block);
		}

		if ((data.on_the_way || []).length) {
			const block = $("<div>");
			const head = $("<div>").addClass("jiji-head").appendTo(block);
			head.append(jiji.desk.label(__("Already on the way")));
			head.append($("<span>").addClass("jiji-muted").text(__("Sent, so there is nothing to decide here")));
				const list = $("<div>").addClass("jiji-rows").appendTo(block);
				data.on_the_way.forEach((order) => {
					$("<div>")
						.addClass("jiji-row")
					.append($("<span>").addClass("jiji-row__main").text(order.supplier || ""))
					.append($("<span>").addClass("jiji-row__note").text(order.when || ""))
					.append($("<span>").addClass("jiji-num jiji-supplies__history-total").text(order.total || ""))
					.appendTo(list);
			});
			this.content.append(block);
		}

	}

	renderHistory(draft) {
		const card = $("<div>").addClass("jiji-card");
		const head = $("<div>").addClass("jiji-head").appendTo(card);
		head.append(jiji.desk.label(__("Last orders from {0}", [draft.supplier || ""])));
		// The reliability line belongs beside the decision, not repeated here.
		// This says what the record covers, which the line above does not.
		head.append(
			$("<span>")
				.addClass("jiji-muted")
				.text(
					draft.history.length === 1
						? __("1 finished order")
						: __("{0} finished orders", [draft.history.length]),
				),
		);
		const list = $("<div>").css("margin-top", "8px").appendTo(card);
		draft.history.forEach((entry) => {
			const row = $("<div>").addClass("jiji-kv").appendTo(list);
			row.append($("<span>").addClass("jiji-supplies__date").text(entry.when || ""));
			row.append($("<span>").addClass("jiji-kv__main"));
			row.append(jiji.desk.pill(entry.arrival || "", entry.on_time ? "ok" : ""));
			row.append($("<span>").addClass("jiji-supplies__history-total").text(entry.total || ""));
		});
		return card;
	}

	renderDraft(draft) {
		const card = $("<div>").addClass("jiji-card");

		const head = $("<div>").addClass("jiji-supplies__head").appendTo(card);
		head.append($("<span>").addClass("jiji-supplies__supplier").text(draft.supplier || ""));
		if (draft.reliability) head.append($("<span>").addClass("jiji-muted").text(draft.reliability));
		head.append($("<span>").addClass("jiji-supplies__total").text(draft.total || ""));

		const lines = $("<div>").addClass("jiji-rows").appendTo(card);
		(draft.lines || []).forEach((line) => {
			$("<div>")
				.addClass("jiji-row")
				.append($("<span>").addClass("jiji-row__main").text(line.item || ""))
				.append($("<span>").addClass("jiji-supplies__amount").text(line.amount || ""))
				.append($("<span>").addClass("jiji-supplies__cost").text(line.cost || ""))
				.appendTo(lines);
		});

		if (draft.why) {
			const why = $("<div>").addClass("jiji-supplies__why").appendTo(card);
			why.append($("<div>").addClass("jiji-supplies__why-title").text(__("Why this is suggested")));
			why.append($("<div>").addClass("jiji-supplies__why-detail").text(draft.why));
		}

		const actions = $("<div>").addClass("jiji-supplies__actions").appendTo(card);
		$("<button type=\"button\">")
			.addClass("btn btn-primary")
			.text(__("Approve order"))
			.on("click", () => this.approveOrder(draft))
			.appendTo(actions);
		actions.append($("<span>").addClass("jiji-muted").text(__("Approval marks it as ordered. Send it to the supplier outside JiJi.")));
		if (draft.arrives) {
			actions.append($("<span>").addClass("jiji-supplies__when").text(__("Arrives {0}", [draft.arrives])));
		}

		return card;
	}

	approveOrder(draft) {
		const outlet = String(this.outletInput.val() || "");
		if (!draft.order || !draft.revision || !outlet) {
			frappe.msgprint({ title: __("Refresh this order"), message: __("The order may have changed. Refresh and check it again.") });
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Approve this order?"),
			fields: [{
				fieldtype: "HTML",
				fieldname: "explanation",
				options: `<p>${__("This records the order here. JiJi will not send anything to the supplier. Send it yourself.")}</p>`,
			}],
			primary_action_label: __("Approve order"),
			primary_action: () => {
				dialog.hide();
				frappe.call({
				method: "kopos_connector.api.supplier_orders.approve_supplier_order",
				type: "POST",
				args: { order: draft.order, outlet, expected_modified: draft.revision },
				callback: (response) => {
					const result = response.message || {};
					frappe.show_alert({ message: result.message || __("Order approved"), indicator: "green" });
					this.load();
				},
				error: () => frappe.msgprint({ title: __("Order not approved"), message: __("Nothing was sent. Refresh and try again.") }),
				});
			},
			secondary_action_label: __("Not now"),
			secondary_action: () => dialog.hide(),
		});
		dialog.show();
	}
}
