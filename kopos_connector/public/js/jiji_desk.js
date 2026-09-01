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

	/** Follow a route from something that is not a link, such as a button. */
	desk.go = function go(segments) {
		const route = Array.isArray(segments) ? segments : [];
		if (route.length) frappe.set_route.apply(null, route);
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

	/**
	 * Take over a desk page and hand back the container to draw in.
	 *
	 * Frappe hands a page an unpadded white canvas that runs flush to the
	 * sidebar and off the right edge. A director's page is a desk: a calm
	 * ground with white cards on it, room around the edges, and one accent
	 * colour. Claiming the wrapper is what lets that reach the header button
	 * too, so the page has one primary colour rather than two.
	 */
	desk.page = function page(frappePage) {
		desk.injectStyle();
		$(frappePage.wrapper).addClass("jiji-desk");
		return $('<div class="jiji-page"></div>').appendTo(frappePage.body);
	};

	/** The uppercase line that names a block without competing with it. */
	desk.label = function label(text) {
		return $("<div>").addClass("jiji-label").text(text);
	};

	/** "Advanced details -- <where the real record is>", demoted to a footnote. */
	desk.advanced = function advanced(text) {
		return $("<div>").addClass("jiji-advanced").text(__("More detail · {0}", [text]));
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
		const peak = rows.reduce((high, row) => Math.max(high, Number(row.total_sen) || 0), 0);
		// A week with no sales has no shape. Seven empty columns look like a
		// chart that failed to load; a sentence says which it is.
		if (peak <= 0) {
			return $("<div>").addClass("jiji-muted").text(__("You have not sold anything in the last seven days."));
		}
		const chart = $("<div>").addClass("jiji-week");
		rows.forEach((row) => {
			const column = $("<div>").addClass("jiji-week__day").appendTo(chart);
			const height = peak > 0 ? Math.round(((Number(row.total_sen) || 0) / peak) * 100) : 0;
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
/* ---------------------------------------------------------------------------
   The desk palette.

   Frappe's own tokens cannot carry this: --bg-color is white in the default
   theme, so a ground drawn from it is white on white, and --yellow-600 is a
   mustard rather than the amber the counter uses. The desk owns its six
   colours and states them for both themes, so the two surfaces of one product
   agree on what amber means.
   --------------------------------------------------------------------------- */
.jiji-desk {
  --jiji-ground: #F6F6F7;
  --jiji-surface: #FFFFFF;
  --jiji-line: #E6E6E9;
  --jiji-line-soft: #F0F0F2;
  --jiji-ink: #17181A;
  --jiji-ink-2: #62636A;
  --jiji-ink-3: #97989F;
  --jiji-accent: #B4640E;
  --jiji-accent-soft: #FBF2E7;
  --jiji-done: #1B6E45;
  --jiji-done-soft: #EAF5EF;
  --jiji-stop: #B3261E;
  --jiji-stop-soft: #FDECEA;
  --jiji-quiet: #F2F2F4;
  --jiji-lift: 0 1px 2px rgba(23, 24, 26, .05);
}
[data-theme="dark"] .jiji-desk {
  --jiji-ground: #16171A;
  --jiji-surface: #1D1E22;
  --jiji-line: #2C2E33;
  --jiji-line-soft: #232529;
  --jiji-ink: #EFEAE1;
  --jiji-ink-2: #A6A7AE;
  --jiji-ink-3: #74757C;
  --jiji-accent: #E8952A;
  --jiji-accent-soft: #2A2116;
  --jiji-done: #6FCf9A;
  --jiji-done-soft: #17251D;
  --jiji-stop: #F09B94;
  --jiji-stop-soft: #2A1A18;
  --jiji-quiet: #24262A;
  --jiji-lift: 0 1px 2px rgba(0, 0, 0, .35);
}

/* The page is a ground with cards on it, not a wall of white. The ground is
   painted on the page's own containers rather than on the block inside them,
   because a min-height guess leaves a white strip under short content. */
.jiji-desk .page-content,
.jiji-desk .layout-main,
.jiji-desk .layout-main-section-wrapper,
.jiji-desk .layout-main-section { background: var(--jiji-ground); }
/* The first breadcrumb opens Frappe's technical desktop. Business users land
   on JiJi and navigate through its six plain-language pages, so that escape
   hatch is both confusing and unnecessary here. */
.jiji-desk .navbar-breadcrumbs li:first-child { display: none; }
.jiji-desk .navbar-breadcrumbs li:nth-child(2)::before,
.jiji-desk .navbar-breadcrumbs li:nth-child(2) > a::before { display: none; }
.jiji-page,
.jiji-stock-autopilot,
.jiji-menu-page {
  background: var(--jiji-ground); padding: 26px 30px 44px; color: var(--jiji-ink);
  /* Frappe's header is 49px, measured. If that ever changes the ground ends
     short or scrolls a little; it can never leave a white seam mid-page,
     because the containers above are painted too. */
  min-height: calc(100vh - 49px);
}
.jiji-page { display: flex; flex-direction: column; gap: 20px; }
/* Frappe's current Bootstrap build has no gap utilities. These two classes
   are used by the JiJi pages, so define them once instead of letting controls
   and money figures run into each other. */
.jiji-desk .gap-2 { gap: 8px; }
.jiji-desk .gap-3 { gap: 16px; }
/* Frappe moves dialogs outside the page root, so money shown in a recipe
   check needs its own globally scoped JiJi class. */
.jiji-money-line { gap: 16px; }

/* One accent, reaching the header button so the page is not two-coloured. */
.jiji-desk .btn-primary,
.jiji-desk .btn-primary:hover,
.jiji-desk .btn-primary:focus,
.jiji-desk .btn-primary:active {
  background-color: var(--jiji-accent); border-color: var(--jiji-accent);
  color: #fff; box-shadow: none;
}
.jiji-desk .btn-primary:hover { filter: brightness(1.08); }

.jiji-label { font-size: 11px; letter-spacing: .09em; text-transform: uppercase;
  color: var(--jiji-ink-3); font-weight: 600; }
.jiji-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.jiji-head .jiji-label { flex: 1 1 auto; }
.jiji-muted { color: var(--jiji-ink-3); font-size: 12px; }
.jiji-num { font-variant-numeric: tabular-nums; }

/* One next step: a bar, never a fill. A fill would read as the button to press. */
.jiji-next { display: flex; gap: 20px; align-items: center; flex-wrap: wrap;
  border: 1px solid var(--jiji-line); border-left: 3px solid var(--jiji-accent);
  background: var(--jiji-surface); border-radius: 0 10px 10px 0; padding: 20px 24px;
  box-shadow: var(--jiji-lift); }
.jiji-next__copy { flex: 1 1 320px; }
.jiji-next__title { font-size: 22px; line-height: 30px; font-weight: 600; letter-spacing: -.01em; }
.jiji-next__detail { color: var(--jiji-ink-2); margin-top: 3px; }
/* What follows from not doing it, said once and with the step it belongs to. */
.jiji-next__consequence { color: var(--jiji-ink-3); font-size: 12px; margin-top: 8px; }

.jiji-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
/* Three tiles stretched across a full row read as three empty boxes. Cap them
   so a short row keeps a tile's natural size instead of inflating it. */
.jiji-tiles--few { grid-template-columns: repeat(auto-fit, minmax(170px, 260px)); }
.jiji-tile { display: block; background: var(--jiji-surface); border: 1px solid var(--jiji-line);
  border-radius: 10px; padding: 15px 17px; text-decoration: none; color: inherit;
  box-shadow: var(--jiji-lift); }
.jiji-tile:hover { border-color: var(--jiji-accent); text-decoration: none; color: inherit; }
.jiji-tile__value { font-size: 27px; line-height: 34px; font-weight: 600;
  font-variant-numeric: tabular-nums; margin-top: 3px; letter-spacing: -.01em; }
.jiji-tile__value--attention { color: var(--jiji-accent); }
.jiji-tile__detail { color: var(--jiji-ink-3); font-size: 12px; }

.jiji-rows { background: var(--jiji-surface); border: 1px solid var(--jiji-line);
  border-radius: 10px; overflow: hidden; box-shadow: var(--jiji-lift); }
.jiji-row { display: flex; align-items: center; gap: 16px; padding: 14px 18px;
  border-bottom: 1px solid var(--jiji-line-soft); text-decoration: none; color: inherit; }
.jiji-row:last-child { border-bottom: 0; }
a.jiji-row:hover { text-decoration: none; color: inherit; background: var(--jiji-ground); }
.jiji-row__main { flex: 1 1 auto; min-width: 0; }
.jiji-row__note { color: var(--jiji-ink-3); font-size: 12px; }

.jiji-card { background: var(--jiji-surface); border: 1px solid var(--jiji-line);
  border-radius: 10px; padding: 17px 19px; box-shadow: var(--jiji-lift); }
.jiji-desk .card { border-radius: 10px; border-color: var(--jiji-line);
  box-shadow: var(--jiji-lift); }
.jiji-kv { display: flex; align-items: center; gap: 14px; padding: 10px 0;
  border-bottom: 1px solid var(--jiji-line-soft); }
.jiji-kv:last-child { border-bottom: 0; padding-bottom: 0; }
.jiji-kv__main { flex: 1 1 auto; min-width: 0; }

.jiji-two { display: flex; gap: 16px; align-items: stretch; flex-wrap: wrap; }
.jiji-two > .jiji-card { flex: 1 1 340px; }
.jiji-two > .jiji-two__side { flex: 0 1 320px; }
a.jiji-kv { text-decoration: none; color: inherit; }
a.jiji-kv:hover { text-decoration: none; color: inherit; }

/* Four levels, the same four the counter uses. */
.jiji-pill { display: inline-flex; align-items: center; height: 24px; padding: 0 10px;
  border-radius: 12px; font-size: 12px; font-weight: 600; white-space: nowrap;
  background: var(--jiji-quiet); color: var(--jiji-ink-2); }
.jiji-pill--attention { background: var(--jiji-accent-soft); color: var(--jiji-accent); }
.jiji-pill--ok { background: var(--jiji-done-soft); color: var(--jiji-done); }
.jiji-pill--stop { background: var(--jiji-stop-soft); color: var(--jiji-stop); }

/* How long something lasts, drawn rather than only counted. */
.jiji-meter { width: 96px; height: 6px; border-radius: 3px; overflow: hidden;
  background: var(--jiji-quiet); flex: 0 0 auto; }
.jiji-meter > span { display: block; height: 100%; border-radius: 3px; background: var(--jiji-accent); }
.jiji-meter--calm > span { background: var(--jiji-ink-3); }

/* A week of takings: bars against the best day, today picked out. */
.jiji-week { display: flex; align-items: flex-end; gap: 10px; height: 132px;
  border-bottom: 1px solid var(--jiji-line-soft); }
.jiji-week__day { flex: 1 1 0; display: flex; flex-direction: column; justify-content: flex-end;
  align-items: center; gap: 8px; height: 100%; }
.jiji-week__bar { width: 100%; max-width: 44px; min-height: 2px; border-radius: 4px 4px 0 0;
  background: var(--jiji-quiet); }
.jiji-week__bar--today { background: var(--jiji-accent); }
.jiji-week__label { font-size: 12px; color: var(--jiji-ink-3); }

/* Help a first-timer needs once should not be furniture a director reads
   every day. Offered the same way on every page: folded, and openable. */
.jiji-desk details.jiji-help > summary { cursor: pointer; list-style: none; }
.jiji-desk details.jiji-help > summary::-webkit-details-marker { display: none; }
.jiji-desk details.jiji-help > summary::before { content: "▸"; margin-right: 8px; color: var(--jiji-ink-3); }
.jiji-desk details.jiji-help[open] > summary::before { content: "▾"; }

/* Where you are, on one line, instead of a form asking again every visit. */
.jiji-outletbar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.jiji-outletbar__sp { flex: 1 1 auto; }
.jiji-outletbar [data-warehouse] { min-width: 260px; }
.jiji-outletbar .frappe-control { margin-bottom: 0 !important; }
.jiji-outletbar .control-label, .jiji-outletbar .help-box { display: none !important; }

.jiji-drink__title { font-size: 21px; font-weight: 600; letter-spacing: -.01em; flex: 1 1 auto; }
.jiji-drink__amount { color: var(--jiji-ink); }
.jiji-drink__cost { color: var(--jiji-ink-2); min-width: 92px; text-align: right; }

.jiji-advanced { color: var(--jiji-ink-3); font-size: 12px; margin-top: 4px; }
`;
		document.head.appendChild(style);
	};
})(jiji.desk);
