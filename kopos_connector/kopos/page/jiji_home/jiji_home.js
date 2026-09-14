/**
 * JiJi Home.
 *
 * A director opens JiJi to answer four questions: is anything wrong, did we
 * make money, do I need to order anything, and can this person use the tablet.
 * Every one of those answers already lives somewhere; the problem was that
 * each area reported itself with equal weight, so three could claim to be the
 * most urgent thing at once.
 *
	 * This page is read-only. It shows one next step, four quiet doors, today's
	 * sales at a glance, and
	 * whatever is waiting behind that step. Every number comes from the service
	 * that owns it, and actions stay inside the simple JiJi pages.
 *
 * Routing and the shared look live in public/js/jiji_desk.js, so the five
 * director pages cannot drift into five answers about how the desk behaves.
 */

frappe.pages["jiji_home"].on_page_load = function (wrapper) {
	new JiJiHome(wrapper);
};

class JiJiHome {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("JiJi Home"),
			single_column: true,
		});
		this.page.set_primary_action(__("Refresh"), () => this.load(), "refresh");
		this.page.add_inner_button(__("Offers"), () => jiji.desk.go(["jiji_promotions"]));
		this.body = jiji.desk.page(this.page);
		this.load();
	}

	load() {
		frappe.call({
			method: "kopos_connector.api.director_home.get_director_home",
			type: "GET",
			args: this.company ? { company: this.company } : {},
			callback: (response) => this.render(response.message || {}),
			error: () => this.renderUnavailable(),
		});
	}

	renderUnavailable() {
		this.body.empty().append(
			jiji.desk.notice(
				__("JiJi could not check your outlets just now"),
				__("This page has not confirmed anything yet. Try Refresh in a moment."),
			),
		);
	}

	render(data) {
		this.body.empty();
		if (data.status === "selection_required" && (data.companies || []).length) {
			const card = jiji.desk.notice(
				__("Which café do you want to see?"),
				__("Choose one to continue."),
			);
			(data.companies || []).forEach((company) => {
				$("<button>")
					.addClass("btn btn-primary")
					.css("margin", "12px 8px 0 0")
					.text(company)
					.on("click", () => {
						this.company = company;
						this.load();
					})
					.appendTo(card);
			});
			this.body.append(card);
			return;
		}
		if (data.status === "permission_denied") {
			this.body.append(
				jiji.desk.notice(
					__("This page is not linked to your account yet"),
					__("Ask the person who set up JiJi to add your café."),
				),
			);
			return;
		}
		if (data.status !== "ok") return this.renderUnavailable();

		const next = data.next_step || {};
		const nextBlock = $("<div>").append(jiji.desk.label(__("Your next step")));
		const card = jiji.desk.notice(next.title || "", next.detail).appendTo(nextBlock);
		if (next.action && (next.route || []).length) {
			jiji.desk.routeLink(next.route, next.action).addClass("btn btn-primary").appendTo(card);
		}
		this.body.append(nextBlock);

			const tiles = $("<div>").addClass("jiji-tiles");
			(data.tiles || []).forEach((tile) => {
				const route = Array.isArray(tile.route) ? tile.route : [];
				const link = route.length
					? jiji.desk.routeLink(route).addClass("jiji-tile")
					: $("<div>").addClass("jiji-tile");
			link.append(jiji.desk.label(tile.label || ""));
			link.append(
				$("<div>")
					.addClass("jiji-tile__value" + (tile.attention ? " jiji-tile__value--attention" : ""))
					.text(tile.value || ""),
			);
			link.append($("<div>").addClass("jiji-tile__detail").text(tile.detail || ""));
			tiles.append(link);
		});
		this.body.append(tiles);

		// Demoted on purpose: these are real, and none of them is the next step.
		if ((data.waiting || []).length) {
			const waitingBlock = $("<div>").append(jiji.desk.label(__("Waiting behind that")));
			const list = $("<div>").addClass("jiji-rows").appendTo(waitingBlock);
			data.waiting.forEach((row) => {
				jiji.desk
					.routeLink(row.route)
					.addClass("jiji-row")
					.append($("<span>").addClass("jiji-row__main").text(row.title || ""))
					.append($("<span>").text("›"))
					.appendTo(list);
			});
			this.body.append(waitingBlock);
		}

		// The shape behind today's number: a week is what "compared with what"
		// means, and it is the same query today already came out of.
		const week = data.week || {};
		const outlets = data.outlets || [];
		if ((week.days || []).length) {
			const row = $("<div>").addClass("jiji-two");
			const card = $("<div>").addClass("jiji-card").appendTo(row);
			const head = $("<div>").addClass("jiji-head").appendTo(card);
			head.append(jiji.desk.label(__("Last seven days")));
			const weekSummary = [__("{0} collected", [week.total || ""])];
			if (Number(week.complimentary_orders || 0) > 0) {
				weekSummary.push(__("{0} given free", [week.complimentary_value || ""]));
			}
			if (week.busiest) weekSummary.push(__("busiest {0}", [week.busiest]));
			head.append(
				$("<span>")
					.addClass("jiji-muted jiji-num")
					.text(weekSummary.join(" · ")),
			);
			card.append(jiji.desk.weekBars(week.days).css("margin-top", "16px"));

			// Only when there is more than one counter: a café with one does not
			// need today's total repeated back to it under another name.
			if (outlets.length) {
				const side = $("<div>").addClass("jiji-card jiji-two__side").appendTo(row);
				side.append(jiji.desk.label(__("Your outlets, today")));
				const list = $("<div>").css("margin-top", "8px").appendTo(side);
				outlets.forEach((outlet) => {
					const line = $("<div>").addClass("jiji-kv").appendTo(list);
					line.append($("<span>").addClass("jiji-kv__main").text(outlet.outlet || ""));
					const outletOrders = Number(outlet.complimentary_orders_count || 0) > 0
						? __("{0} · {1} given free", [outlet.orders || "", outlet.complimentary_orders_count])
						: outlet.orders || "";
					line.append($("<span>").addClass("jiji-muted jiji-num").text(outletOrders));
					const outletMoney = Number(outlet.complimentary_orders_count || 0) > 0
						? __("{0} in · {1} free", [outlet.total || "", outlet.complimentary_value || ""])
						: outlet.total || "";
					line.append($("<span>").addClass("jiji-num").css("font-weight", "600").text(outletMoney));
				});
			}
			this.body.append(row);
		}
	}
}
