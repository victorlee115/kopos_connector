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
 */

/**
 * Follow a route the way the desk does.
 *
 * The desk is served at /app on some deployments and /desk on others, and its
 * router intercepts clicks rather than following an href, so a hard-coded URL
 * either 404s or silently does nothing. Route segments work on both.
 */
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
		this.body = $('<div class="jiji-home"></div>').appendTo(this.page.body);
		this.injectStyle();
		this.load();
	}

	injectStyle() {
		if (document.getElementById("jiji-home-style")) return;
		const style = document.createElement("style");
		style.id = "jiji-home-style";
		style.textContent = `
.jiji-home { display: flex; flex-direction: column; gap: 20px; padding: 4px 0 24px; }
.jiji-home__label { font-size: 11px; letter-spacing: .09em; text-transform: uppercase; color: var(--text-muted); }
/* A bar, not a filled block: a fill would read as the button to press. */
.jiji-home__next { display: flex; gap: 20px; align-items: center; flex-wrap: wrap;
  border-left: 3px solid var(--yellow-500, #e8952a); background: var(--fg-color, #fff);
  border-radius: 0 8px 8px 0; padding: 18px 22px; box-shadow: var(--card-shadow); }
.jiji-home__next-copy { flex: 1 1 320px; }
.jiji-home__next-title { font-size: 18px; font-weight: 600; }
.jiji-home__next-detail { color: var(--text-muted); margin-top: 2px; }
.jiji-home__tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.jiji-home__tile { display: block; background: var(--fg-color, #fff); border: 1px solid var(--border-color);
  border-radius: 8px; padding: 14px 16px; text-decoration: none; color: inherit; }
.jiji-home__tile:hover { border-color: var(--yellow-500, #e8952a); text-decoration: none; color: inherit; }
.jiji-home__tile-value { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 4px; }
.jiji-home__tile-value--attention { color: var(--yellow-600, #b4640e); }
.jiji-home__tile-detail { color: var(--text-muted); font-size: 12px; }
.jiji-home__waiting { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; }
.jiji-home__waiting a { display: flex; justify-content: space-between; gap: 12px; padding: 14px 18px;
  border-bottom: 1px solid var(--border-color); text-decoration: none; color: inherit; }
.jiji-home__waiting a:last-child { border-bottom: 0; }
.jiji-home__advanced { color: var(--text-muted); font-size: 12px; }
`;
		document.head.appendChild(style);
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
			$("<div>").addClass("jiji-home__next").append(
				$("<div>").addClass("jiji-home__next-copy").append(
					$("<div>").addClass("jiji-home__next-title").text(__("JiJi could not check your outlets just now")),
					$("<div>").addClass("jiji-home__next-detail").text(__("Nothing is wrong with your records. Try Refresh in a moment.")),
				),
			),
		);
	}

	render(data) {
		this.body.empty();
		if (data.status === "no_company") {
			this.body.append(
				$("<div>").addClass("jiji-home__next").append(
					$("<div>").addClass("jiji-home__next-copy").append(
						$("<div>").addClass("jiji-home__next-title").text(__("Choose a company to see your outlets")),
						$("<div>").addClass("jiji-home__next-detail").text(__("Set your default company, then open JiJi Home again.")),
					),
				),
			);
			return;
		}
		if (data.status !== "ok") return this.renderUnavailable();

		const next = data.next_step || {};
		const nextBlock = $("<div>");
		nextBlock.append($("<div>").addClass("jiji-home__label").text(__("Your next step")));
		const card = $("<div>").addClass("jiji-home__next").appendTo(nextBlock);
		const copy = $("<div>").addClass("jiji-home__next-copy").appendTo(card);
		copy.append($("<div>").addClass("jiji-home__next-title").text(next.title || ""));
		if (next.detail) copy.append($("<div>").addClass("jiji-home__next-detail").text(next.detail));
		if (next.action && (next.route || []).length) {
			routeLink(next.route, next.action).addClass("btn btn-primary").appendTo(card);
		}
		this.body.append(nextBlock);

		const tiles = $("<div>").addClass("jiji-home__tiles");
		(data.tiles || []).forEach((tile) => {
			const link = routeLink(tile.route).addClass("jiji-home__tile");
			link.append($("<div>").addClass("jiji-home__label").text(tile.label || ""));
			link.append(
				$("<div>")
					.addClass("jiji-home__tile-value" + (tile.attention ? " jiji-home__tile-value--attention" : ""))
					.text(tile.value || ""),
			);
			link.append($("<div>").addClass("jiji-home__tile-detail").text(tile.detail || ""));
			tiles.append(link);
		});
		this.body.append(tiles);

		// Demoted on purpose: these are real, and none of them is the next step.
		if ((data.waiting || []).length) {
			const waitingBlock = $("<div>");
			waitingBlock.append($("<div>").addClass("jiji-home__label").text(__("Waiting behind that")));
			const list = $("<div>").addClass("jiji-home__waiting").appendTo(waitingBlock);
			data.waiting.forEach((row) => {
				routeLink(row.route)
					.append($("<span>").text(row.title || ""))
					.append($("<span>").text("›"))
					.appendTo(list);
			});
			this.body.append(waitingBlock);
		}

		this.body.append(
			$("<div>").addClass("jiji-home__advanced").text(
				__("Advanced details · every number here opens the record it came from."),
			),
		);
	}
}
