/**
 * JiJi Money.
 *
 * The shift reconciliation report is honest and unreadable: one row per shift,
 * money columns off the right edge, a raw filter hint as body text, and an
 * execution time. A director looking for "did the cash match" had to read a
 * table sideways.
 *
 * Summary first, then only the shifts that need them, then the four numbers
 * for one shift. The report stays one click away and keeps its columns; it is
 * the right tool for an audit and the wrong one for a decision.
 *
 * Routing and the shared look live in public/js/jiji_desk.js.
 */

/**
 * "2026-08-23 09:04:02" is a database timestamp, not something a director
 * reads. Shifts are discussed by day and time, so that is what is shown.
 */
function readableWhen(value) {
	if (!value) return "";
	const parsed = new Date(String(value).replace(" ", "T"));
	if (Number.isNaN(parsed.getTime())) return String(value);
	return parsed.toLocaleString(undefined, {
		weekday: "short",
		day: "numeric",
		month: "short",
		hour: "numeric",
		minute: "2-digit",
	});
}

frappe.pages["jiji_money"].on_page_load = function (wrapper) {
	new JiJiMoney(wrapper);
};

class JiJiMoney {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("JiJi Money"),
			single_column: true,
		});
		this.page.set_primary_action(__("Refresh"), () => this.load(), "refresh");
		this.body = jiji.desk.page(this.page);
		this.injectStyle();
		this.load();
	}

	injectStyle() {
		if (document.getElementById("jiji-money-style")) return;
		const style = document.createElement("style");
		style.id = "jiji-money-style";
		style.textContent = `
.jiji-money__card--attention { border-left: 3px solid var(--jiji-accent); }
.jiji-money__who { font-weight: 600; flex: 1 1 240px; }
.jiji-money__amounts { color: var(--jiji-ink-3); font-variant-numeric: tabular-nums; }
.jiji-money__difference { font-weight: 600; font-variant-numeric: tabular-nums; color: var(--jiji-accent); }
`;
		document.head.appendChild(style);
	}

	load() {
		frappe.call({
			method: "kopos_connector.api.shift_money.get_shift_money",
			type: "GET",
			callback: (response) => this.render(response.message || {}),
			error: () =>
				this.body.empty().append(
					jiji.desk.notice(
						__("JiJi could not read your shifts just now"),
						__("Nothing is wrong with your records. Try Refresh in a moment."),
					),
				),
		});
	}

	render(data) {
		this.body.empty();
		if (data.status !== "ok") {
			this.body.append(
				jiji.desk.notice(
					__("Choose a company to see its shifts"),
					__("Set your default company, then open JiJi Money again."),
				),
			);
			return;
		}

		// The statement first, the counters behind it, the same shape as every
		// other page. Three zeros with the good news underneath said nothing.
		const next = data.next_step || {};
		if (next.title) {
			const block = $("<div>").append(jiji.desk.label(__("Where the cash stands")));
			jiji.desk.notice(next.title, next.detail).appendTo(block);
			this.body.append(block);
		}

		const summary = $("<div>").addClass("jiji-tiles jiji-tiles--few");
		(data.summary || []).forEach((card) => {
			const box = $("<div>").addClass("jiji-tile" + (card.attention ? " jiji-money__card--attention" : ""));
			box.append(jiji.desk.label(card.label || ""));
			box.append(
				$("<div>")
					.addClass("jiji-tile__value" + (card.attention ? " jiji-tile__value--attention" : ""))
					.text(String(card.count == null ? "" : card.count)),
			);
			summary.append(box);
		});
		this.body.append(summary);

		this.appendGroup(__("Need review"), data.needs_review || []);
		this.appendGroup(__("Never counted"), data.missing_count || []);

		this.body.append(
			jiji.desk
				.routeLink(data.report_route, __("More detail · every shift, one by one"))
				.addClass("jiji-advanced"),
		);
	}

	appendGroup(title, rows) {
		if (!rows.length) return;
		const block = $("<div>").append(jiji.desk.label(title));
		const list = $("<div>").addClass("jiji-rows").appendTo(block);
		rows.forEach((row) => {
			const link = jiji.desk.routeLink(["Form", "FB Shift", row.shift]).addClass("jiji-row");
			link.append($("<span>").addClass("jiji-money__who").text(`${row.person || ""} · ${readableWhen(row.closed_at)}`));
			link.append(
				$("<span>")
					.addClass("jiji-money__amounts")
					.text(`${__("Expected")} RM ${Number(row.expected || 0).toFixed(2)} · ${__("Counted")} RM ${Number(row.counted || 0).toFixed(2)}`),
			);
			link.append($("<span>").addClass("jiji-money__difference").text(row.difference || ""));
			list.append(link);
		});
		this.body.append(block);
	}
}
