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
			title: __("JiJi Supplies"),
			single_column: true,
		});
		this.page.set_primary_action(__("Refresh"), () => this.load(), "refresh");
		this.body = jiji.desk.page(this.page);
		this.injectStyle();
		this.load();
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
`;
		document.head.appendChild(style);
	}

	load() {
		frappe.call({
			method: "kopos_connector.api.supplier_orders.get_supplier_orders",
			type: "GET",
			callback: (response) => this.render(response.message || {}),
			error: () =>
				this.body.empty().append(
					jiji.desk.notice(
						__("JiJi could not read your supplier orders just now"),
						__("Nothing has been sent or changed. Try Refresh in a moment."),
					),
				),
		});
	}

	render(data) {
		this.body.empty();
		if (data.status === "no_company") {
			this.body.append(
				jiji.desk.notice(
					__("Choose a company to see its supplier orders"),
					__("Set your default company, then open JiJi Supplies again."),
				),
			);
			return;
		}
		if (data.status !== "ok") {
			this.body.append(
				jiji.desk.notice(
					__("JiJi could not read your supplier orders just now"),
					__("Nothing has been sent or changed. Try Refresh in a moment."),
				),
			);
			return;
		}

		this.page.set_indicator(
			data.draft_count === 1
				? __("1 draft")
				: __("{0} drafts", [data.draft_count || 0]),
			data.draft_count ? "orange" : "green",
		);

		if (!data.draft_count) {
			this.body.append(
				jiji.desk.notice(
					__("Nothing is waiting to be ordered"),
					__("JiJi drafts an order when stock is due to run out before a delivery could arrive."),
				),
			);
		}

		(data.detailed || []).forEach((draft) => {
			this.body.append(this.renderDraft(draft));
			// The supplier's record is its own block. Folded into the decision
			// card it read as a footnote to the order rather than as the
			// evidence the decision is weighed against.
			if ((draft.history || []).length) this.body.append(this.renderHistory(draft));
		});

		if ((data.other_drafts || []).length) {
			const block = $("<div>").append(jiji.desk.label(__("Also waiting")));
			const list = $("<div>").addClass("jiji-rows").appendTo(block);
			data.other_drafts.forEach((draft) => {
				jiji.desk
					.routeLink(draft.route)
					.addClass("jiji-row")
					.append($("<span>").addClass("jiji-row__main").text(draft.supplier || ""))
					.append($("<span>").addClass("jiji-num").text(draft.total || ""))
					.append($("<span>").text("›"))
					.appendTo(list);
			});
			this.body.append(block);
		}

		if ((data.on_the_way || []).length) {
			const block = $("<div>");
			const head = $("<div>").addClass("jiji-head").appendTo(block);
			head.append(jiji.desk.label(__("Already on the way")));
			head.append($("<span>").addClass("jiji-muted").text(__("Sent, so there is nothing to decide here")));
			const list = $("<div>").addClass("jiji-rows").appendTo(block);
			data.on_the_way.forEach((order) => {
				jiji.desk
					.routeLink(order.route)
					.addClass("jiji-row")
					.append($("<span>").addClass("jiji-row__main").text(order.supplier || ""))
					.append($("<span>").addClass("jiji-row__note").text(order.when || ""))
					.append($("<span>").addClass("jiji-num jiji-supplies__history-total").text(order.total || ""))
					.appendTo(list);
			});
			this.body.append(block);
		}

		this.body.append(
			jiji.desk
				.routeLink(data.orders_route, __("Advanced details · open the draft Purchase Order to send it"))
				.addClass("jiji-advanced"),
		);
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
			const row = jiji.desk.routeLink(entry.route).addClass("jiji-kv").appendTo(list);
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
			jiji.desk
				.routeLink(line.route)
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
		// One button, and it says where it goes. Sending stays in ERPNext.
		jiji.desk.routeLink(draft.route, __("Open the order to send it")).addClass("btn btn-primary").appendTo(actions);
		actions.append($("<span>").addClass("jiji-muted").text(__("Nothing is sent from this page.")));
		if (draft.arrives) {
			actions.append($("<span>").addClass("jiji-supplies__when").text(__("Arrives {0}", [draft.arrives])));
		}

		return card;
	}
}
