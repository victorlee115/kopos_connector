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
 */

/**
 * "2026-08-23 09:04:02" is a database timestamp, not something a director
 * reads. Shifts are discussed by day and time, so that is what is shown.
 */
/** Route segments, not URLs: the desk router intercepts clicks. */
function routeLink(segments, label) {
	const route = Array.isArray(segments) ? segments : [];
	const link = $("<a>").attr("href", route.length ? `/app/${route.filter((part) => typeof part === "string").join("/")}` : "#");
	if (label) link.text(label);
	link.on("click", (event) => {
		if (!route.length) return;
		event.preventDefault();
		frappe.set_route.apply(null, route);
	});
	return link;
}

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
		this.body = $('<div class="jiji-money"></div>').appendTo(this.page.body);
		this.injectStyle();
		this.load();
	}

	injectStyle() {
		if (document.getElementById("jiji-money-style")) return;
		const style = document.createElement("style");
		style.id = "jiji-money-style";
		style.textContent = `
.jiji-money { display: flex; flex-direction: column; gap: 20px; padding: 4px 0 24px; }
.jiji-money__label { font-size: 11px; letter-spacing: .09em; text-transform: uppercase; color: var(--text-muted); }
.jiji-money__summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.jiji-money__card { background: var(--fg-color, #fff); border: 1px solid var(--border-color); border-radius: 8px; padding: 14px 16px; }
.jiji-money__card--attention { border-left: 3px solid var(--yellow-500, #e8952a); }
.jiji-money__count { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 4px; }
.jiji-money__count--attention { color: var(--yellow-600, #b4640e); }
.jiji-money__rows { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; }
.jiji-money__row { display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap;
  padding: 14px 18px; border-bottom: 1px solid var(--border-color); text-decoration: none; color: inherit; }
.jiji-money__row:last-child { border-bottom: 0; }
.jiji-money__row:hover { background: var(--control-bg, #f8f8f8); text-decoration: none; color: inherit; }
.jiji-money__who { font-weight: 600; flex: 1 1 240px; }
.jiji-money__amounts { color: var(--text-muted); font-variant-numeric: tabular-nums; }
.jiji-money__difference { font-weight: 600; font-variant-numeric: tabular-nums; color: var(--yellow-600, #b4640e); }
.jiji-money__advanced { color: var(--text-muted); font-size: 12px; }
.jiji-money__clear { color: var(--text-muted); }
`;
		document.head.appendChild(style);
	}

	load() {
		frappe.call({
			method: "kopos_connector.api.shift_money.get_shift_money",
			type: "GET",
			callback: (response) => this.render(response.message || {}),
			error: () => this.body.empty().append($("<div>").text(__("JiJi could not read the shifts just now. Try Refresh in a moment."))),
		});
	}

	render(data) {
		this.body.empty();
		if (data.status !== "ok") {
			this.body.append($("<div>").text(__("Choose a company to see its shifts.")));
			return;
		}

		const summary = $("<div>").addClass("jiji-money__summary");
		(data.summary || []).forEach((card) => {
			const box = $("<div>").addClass("jiji-money__card" + (card.attention ? " jiji-money__card--attention" : ""));
			box.append($("<div>").addClass("jiji-money__label").text(card.label || ""));
			box.append(
				$("<div>")
					.addClass("jiji-money__count" + (card.attention ? " jiji-money__count--attention" : ""))
					.text(String(card.count == null ? "" : card.count)),
			);
			summary.append(box);
		});
		this.body.append(summary);

		this.appendGroup(__("Need review"), data.needs_review || []);
		this.appendGroup(__("Never counted"), data.missing_count || []);

		if (!(data.needs_review || []).length && !(data.missing_count || []).length) {
			this.body.append($("<div>").addClass("jiji-money__clear").text(__("Every closed shift matched. Nothing to review.")));
		}

		this.body.append(
			routeLink(data.report_route, __("Advanced details · the full shift reconciliation report"))
				.addClass("jiji-money__advanced"),
		);
	}

	appendGroup(title, rows) {
		if (!rows.length) return;
		const block = $("<div>");
		block.append($("<div>").addClass("jiji-money__label").text(title));
		const list = $("<div>").addClass("jiji-money__rows").appendTo(block);
		rows.forEach((row) => {
			const link = routeLink(["Form", "FB Shift", row.shift]).addClass("jiji-money__row");
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
