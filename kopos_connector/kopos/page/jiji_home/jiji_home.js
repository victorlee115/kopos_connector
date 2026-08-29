/**
 * JiJi Home.
 *
 * A director opens JiJi to answer four questions: is anything wrong, did we
 * make money, do I need to order anything, and can this person use the tablet.
 * Every one of those answers already lives somewhere; the problem was that
 * each area reported itself with equal weight, so three could claim to be the
 * most urgent thing at once.
 *
 * This page is read-only. It shows one next step, five quiet doors, and
 * whatever is queued behind that step. Every number comes from the service
 * that owns it, and every link goes to the real record.
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
		this.body = jiji.desk.page(this.page);
		this.load();
	}

	load() {
		frappe.call({
			method: "kopos_connector.api.director_home.get_director_home",
			type: "GET",
			callback: (response) => this.render(response.message || {}),
			error: () => this.renderUnavailable(),
		});
	}

	renderUnavailable() {
		this.body.empty().append(
			jiji.desk.notice(
				__("JiJi could not check your outlets just now"),
				__("Nothing is wrong with your records. Try Refresh in a moment."),
			),
		);
	}

	render(data) {
		this.body.empty();
		if (data.status === "no_company") {
			this.body.append(
				jiji.desk.notice(
					__("Choose a company to see your outlets"),
					__("Set your default company, then open JiJi Home again."),
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
			const link = jiji.desk.routeLink(tile.route).addClass("jiji-tile");
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
			head.append($("<span>").addClass("jiji-muted jiji-num").text(__("{0} taken", [week.total || ""])));
			card.append(jiji.desk.weekBars(week.days).css("margin-top", "16px"));

			// Only when there is more than one counter: a café with one does not
			// need today's total repeated back to it under another name.
			if (outlets.length) {
				const side = $("<div>").addClass("jiji-card jiji-two__side").appendTo(row);
				side.append(jiji.desk.label(__("Your outlets, today")));
				const list = $("<div>").css("margin-top", "8px").appendTo(side);
				outlets.forEach((outlet) => {
					const line = jiji.desk.routeLink(outlet.route).addClass("jiji-kv").appendTo(list);
					line.append($("<span>").addClass("jiji-kv__main").text(outlet.outlet || ""));
					line.append($("<span>").addClass("jiji-muted jiji-num").text(outlet.orders || ""));
					line.append($("<span>").addClass("jiji-num").css("font-weight", "600").text(outlet.total || ""));
				});
			}
			this.body.append(row);
		}

		this.body.append(jiji.desk.advanced(__("every number here opens the record it came from.")));
	}
}
