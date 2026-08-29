/**
 * The JiJi desk: one routing rule and one visual vocabulary.
 *
 * The tablet is a counter -- dark, one hand, one task. The desk is a desk --
 * light, seated, comparing things. What travels between them is the status
 * model, the money format, and the refusal to show anyone a primary key.
 *
 * Every director-facing page needs the same three things, and before this file
 * each one carried its own copy: how the desk routes a click, what a tile
 * looks like, and what to show when a service cannot answer. Three copies of a
 * rule is three answers to the same question the first time one of them is
 * edited alone.
 *
 * Loaded through the `page_js` hook, which inlines it into each page script.
 * That is deliberate: it needs no bundler, so a bench without node still
 * serves it.
 */

frappe.provide("jiji.desk");

(function (desk) {
	if (desk.routeLink) return;

	const STYLE_ID = "jiji-desk-style";

	/**
	 * Follow a route the way the desk does.
	 *
	 * The desk is served at /app on some deployments and /desk on others, and
	 * its router intercepts clicks rather than following an href, so a
	 * hard-coded URL either 404s or silently does nothing. Route segments work
	 * on both. The href is still written so middle-click and "copy link" behave
	 * like links, and only the string segments go into it -- a filter object is
	 * meaningful to the router and meaningless in a path.
	 */
	desk.routeLink = function routeLink(segments, label) {
		const route = Array.isArray(segments) ? segments : [];
		const path = route.filter((part) => typeof part === "string").join("/");
		const link = $("<a>").attr("href", path ? `/app/${path}` : "#");
		if (label) link.text(label);
		link.on("click", (event) => {
			if (!route.length) return;
			event.preventDefault();
			frappe.set_route.apply(null, route);
		});
		return link;
	};

	/**
	 * The one thing a page must never do is go blank.
	 *
	 * A director who opens JiJi and sees nothing cannot tell an empty café from
	 * a broken page, so every failure says which of the two it is.
	 */
	desk.notice = function notice(title, detail) {
		const card = $("<div>").addClass("jiji-next");
		const copy = $("<div>").addClass("jiji-next__copy").appendTo(card);
		copy.append($("<div>").addClass("jiji-next__title").text(title));
		if (detail) copy.append($("<div>").addClass("jiji-next__detail").text(detail));
		return card;
	};

	/** The uppercase line that names a block without competing with it. */
	desk.label = function label(text) {
		return $("<div>").addClass("jiji-label").text(text);
	};

	/** "Advanced details -- <where the real record is>", demoted to a footnote. */
	desk.advanced = function advanced(text) {
		return $("<div>").addClass("jiji-advanced").text(__("Advanced details · {0}", [text]));
	};

	/**
	 * A status pill. Four levels, the same four the tablet uses:
	 * "" quiet, "attention" needs you, "ok" done, "stop" blocked.
	 */
	desk.pill = function pill(text, level) {
		const suffix = level ? ` jiji-pill--${level}` : "";
		return $("<span>").addClass(`jiji-pill${suffix}`).text(text);
	};

	/**
	 * A week of takings, drawn.
	 *
	 * Seven numbers in a row are a table; seven bars are a shape, and the
	 * shape is the answer to "compared with what". Heights are relative to the
	 * best day, so an empty week draws nothing rather than seven full bars.
	 */
	desk.weekBars = function weekBars(days) {
		const rows = Array.isArray(days) ? days : [];
		const peak = rows.reduce((high, row) => Math.max(high, Number(row.total) || 0), 0);
		// A week with no sales has no shape. Seven empty columns look like a
		// chart that failed to load; a sentence says which it is.
		if (peak <= 0) {
			return $("<div>").addClass("jiji-muted").text(__("Nothing was taken in the last seven days."));
		}
		const chart = $("<div>").addClass("jiji-week");
		rows.forEach((row) => {
			const column = $("<div>").addClass("jiji-week__day").appendTo(chart);
			const height = peak > 0 ? Math.round(((Number(row.total) || 0) / peak) * 100) : 0;
			$("<div>")
				.addClass("jiji-week__bar" + (row.today ? " jiji-week__bar--today" : ""))
				.css("height", `${height}%`)
				.attr("title", `${row.day}: ${row.total}`)
				.appendTo(column);
			$("<div>").addClass("jiji-week__label").text(row.day || "").appendTo(column);
		});
		return chart;
	};

	desk.injectStyle = function injectStyle() {
		if (document.getElementById(STYLE_ID)) return;
		const style = document.createElement("style");
		style.id = STYLE_ID;
		style.textContent = `
.jiji-page { display: flex; flex-direction: column; gap: 20px; padding: 4px 0 24px; }
.jiji-label { font-size: 11px; letter-spacing: .09em; text-transform: uppercase; color: var(--text-muted); }
.jiji-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.jiji-head .jiji-label { flex: 1 1 auto; }
.jiji-muted { color: var(--text-muted); font-size: 12px; }

/* One next step: a bar, never a fill. A fill would read as the button to press. */
.jiji-next { display: flex; gap: 20px; align-items: center; flex-wrap: wrap;
  border-left: 3px solid var(--yellow-500, #e8952a); background: var(--fg-color, #fff);
  border-radius: 0 8px 8px 0; padding: 18px 22px; box-shadow: var(--card-shadow); }
.jiji-next__copy { flex: 1 1 320px; }
.jiji-next__title { font-size: 18px; font-weight: 600; }
.jiji-next__detail { color: var(--text-muted); margin-top: 2px; }

.jiji-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.jiji-tile { display: block; background: var(--fg-color, #fff); border: 1px solid var(--border-color);
  border-radius: 8px; padding: 14px 16px; text-decoration: none; color: inherit; }
.jiji-tile:hover { border-color: var(--yellow-500, #e8952a); text-decoration: none; color: inherit; }
.jiji-tile__value { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 4px; }
.jiji-tile__value--attention { color: var(--yellow-600, #b4640e); }
.jiji-tile__detail { color: var(--text-muted); font-size: 12px; }

.jiji-rows { background: var(--fg-color, #fff); border: 1px solid var(--border-color);
  border-radius: 8px; overflow: hidden; }
.jiji-row { display: flex; align-items: center; gap: 16px; padding: 13px 18px;
  border-bottom: 1px solid var(--border-color); text-decoration: none; color: inherit; }
.jiji-row:last-child { border-bottom: 0; }
a.jiji-row:hover { text-decoration: none; color: inherit; background: var(--bg-color, #f7f7f8); }
.jiji-row__main { flex: 1 1 auto; min-width: 0; }
.jiji-row__note { color: var(--text-muted); font-size: 12px; }
.jiji-num { font-variant-numeric: tabular-nums; }

.jiji-card { background: var(--fg-color, #fff); border: 1px solid var(--border-color);
  border-radius: 8px; padding: 16px 18px; }
.jiji-kv { display: flex; align-items: center; gap: 14px; padding: 9px 0;
  border-bottom: 1px solid var(--border-color); }
.jiji-kv:last-child { border-bottom: 0; padding-bottom: 0; }
.jiji-kv__main { flex: 1 1 auto; min-width: 0; }

.jiji-pill { display: inline-flex; align-items: center; height: 24px; padding: 0 10px;
  border-radius: 12px; font-size: 12px; font-weight: 600; white-space: nowrap;
  background: var(--bg-gray, #f2f2f4); color: var(--text-muted); }
.jiji-pill--attention { background: var(--bg-yellow, #fbf2e7); color: var(--yellow-600, #b4640e); }
.jiji-pill--ok { background: var(--bg-green, #eaf5ef); color: var(--green-600, #1b6e45); }
.jiji-pill--stop { background: var(--bg-red, #fdecea); color: var(--red-600, #b3261e); }

/* How long something lasts, drawn rather than only counted. */
.jiji-meter { width: 96px; height: 6px; border-radius: 3px; overflow: hidden;
  background: var(--bg-gray, #ededf0); flex: 0 0 auto; }
.jiji-meter > span { display: block; height: 100%; border-radius: 3px; background: var(--yellow-600, #b4640e); }
.jiji-meter--calm > span { background: var(--gray-400, #cfcfd6); }

.jiji-week { display: flex; align-items: flex-end; gap: 10px; height: 132px;
  border-bottom: 1px solid var(--border-color); }
.jiji-week__day { flex: 1 1 0; display: flex; flex-direction: column; justify-content: flex-end;
  align-items: center; gap: 8px; height: 100%; }
.jiji-week__bar { width: 100%; max-width: 44px; min-height: 2px; border-radius: 4px 4px 0 0;
  background: var(--bg-gray, #e7e2da); }
.jiji-week__bar--today { background: var(--yellow-600, #b4640e); }
.jiji-week__label { font-size: 12px; color: var(--text-muted); }

.jiji-advanced { color: var(--text-muted); font-size: 12px; }
`;
		document.head.appendChild(style);
	};
})(jiji.desk);
