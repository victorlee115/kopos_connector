/* One editor for a saved draft, a new offer, and a copy. The server owns prices,
 * scope, review receipts and publication. This page never sets an active flag. */
frappe.pages.jiji_promotions.on_page_load = function (wrapper) {
	wrapper.jijiOffers = new JiJiOffers(wrapper);
};
frappe.pages.jiji_promotions.on_page_show = function (wrapper) {
	const options = frappe.route_options || {};
	if ((options.offer || options.create) && wrapper.jijiOffers) {
		frappe.route_options = null;
		wrapper.jijiOffers.run(async () => {
			if (!wrapper.jijiOffers.data) await wrapper.jijiOffers.load();
			if (options.offer) await wrapper.jijiOffers.open(options.offer, Boolean(options.copy));
			else if (wrapper.jijiOffers.data) wrapper.jijiOffers.create();
		});
	}
};

class JiJiOffers {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({ parent: wrapper, title: __("Offers"), single_column: true });
		this.body = jiji.desk.page(this.page);
		this.sequence = 0;
		this.previewSequence = 0;
		this.busy = false;
		this.editor = null;
		this.templates = [
			["percentage", "Percent off", "For example, 10% off drinks", "Percent off", "%"],
			["amount", "Amount off each", "For example, RM1 off every coffee", "Amount off each item (RM)", "RM"],
			["price", "Special price", "For example, any selected tea for RM5", "New price for each item (RM)", "RM"],
			["free_second", "Buy 1, get 1 free", "The cheaper item in each pair is free"],
			["half_second", "Second item half price", "The cheaper item in each pair is 50% off"],
		];
		this.style();
		this.load();
	}

	style() {
		if (document.getElementById("jiji-offers-style")) return;
		$("<style>").attr("id", "jiji-offers-style").text(`
.jiji-offers-head { display:flex; justify-content:space-between; align-items:center; gap:16px; flex-wrap:wrap; }
.jiji-offers-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }
.jiji-offer-card, .jiji-offer-panel { background:var(--jiji-surface); border:1px solid var(--jiji-line); border-radius:12px; padding:22px; }
.jiji-offer-card h2 { font-size:18px; margin:0 0 12px; }
.jiji-offer-help { color:var(--jiji-ink-2); margin:8px 0 16px; line-height:1.6; }
.jiji-offer-steps { display:flex; flex-wrap:wrap; gap:8px; padding:0; list-style:none; }
.jiji-offer-steps button { min-height:44px; padding:8px 16px; border:1px solid var(--jiji-line); border-radius:22px; background:var(--jiji-surface); color:var(--jiji-ink); }
.jiji-offer-steps [aria-current=step] { border-color:var(--jiji-accent); background:var(--jiji-accent-soft); font-weight:650; }
.jiji-offer-template { display:block; text-align:left; padding:18px; min-height:100px; border:1px solid var(--jiji-line); border-radius:10px; background:var(--jiji-surface); color:var(--jiji-ink); }
.jiji-offer-template[aria-pressed=true] { border:2px solid var(--jiji-accent); padding:17px; background:var(--jiji-accent-soft); }
.jiji-offer-template strong { display:block; margin-bottom:8px; }
.jiji-offer-template span { color:var(--jiji-ink-2); }
.jiji-offer-panel { max-width:960px; }
.jiji-offer-panel h2 { font-size:23px; margin:0 0 8px; }
.jiji-offer-panel h3 { font-size:17px; margin:20px 0 12px; }
.jiji-offer-fields { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-top:20px; }
.jiji-offer-panel input:not([type=checkbox]), .jiji-offer-panel select { min-height:44px; }
.jiji-offer-products { max-height:330px; overflow:auto; border:1px solid var(--jiji-line); border-radius:8px; }
.jiji-offer-choice { display:flex; align-items:center; gap:12px; padding:12px; margin:0; min-height:48px; border-bottom:1px solid var(--jiji-line-soft); font-weight:400; cursor:pointer; }
.jiji-offer-choice input[type=checkbox] { width:20px; height:20px; flex-shrink:0; accent-color:var(--jiji-accent); }
.jiji-offer-choice input[type=time] { width:150px; min-height:44px; margin-left:auto; }
.jiji-offer-choice small { margin-left:auto; white-space:nowrap; color:var(--jiji-ink-2); }
.jiji-offer-actions { display:flex; gap:10px; flex-wrap:wrap; justify-content:space-between; margin-top:24px; }
.jiji-offer-actions .btn, .jiji-offers-head .btn, .jiji-offer-card .btn { min-height:44px; }
.jiji-offer-panel .btn { min-height:44px; }
.jiji-offer-receipt { border:1px solid var(--jiji-line); border-radius:10px; padding:20px; margin:16px 0; max-width:560px; font-variant-numeric:tabular-nums; }
.jiji-offer-receipt-row { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:10px 0; }
.jiji-offer-receipt-row > span:last-child { white-space:nowrap; }
.jiji-offer-receipt-total { border-top:1px solid var(--jiji-line); font-weight:700; font-size:19px; }
.jiji-offer-error { color:var(--jiji-stop); background:var(--jiji-stop-soft); border-radius:8px; padding:14px; margin-bottom:16px; }
.jiji-offer-days { display:flex; gap:6px; flex-wrap:wrap; }
.jiji-offer-days .jiji-offer-choice { border:1px solid var(--jiji-line); border-radius:8px; }
.jiji-offer-panel :focus-visible, .jiji-offer-card :focus-visible { outline:3px solid var(--jiji-accent); outline-offset:3px; }
.jiji-offer-panel [aria-invalid=true] { border-color:var(--jiji-stop); }
@media(max-width:600px) { .jiji-offer-panel { padding:16px; } .jiji-offers-grid { grid-template-columns:1fr; } }
`).appendTo(document.head);
	}

	async call(method, args = {}, type = "POST") {
		const response = await frappe.call({ method: `kopos_connector.api.promotion_setup.${method}`, args, type });
		return response.message;
	}

	async run(action) {
		if (this.busy) return;
		this.busy = true;
		this.body.attr("aria-busy", "true").prop("inert", true);
		try { await action(); }
		catch (error) {
			this.error(this.errorMessage(error, __("That step could not be completed. Your draft is still here. Reconnect and try again.")));
			// Frappe displays the server's field/domain explanation. Do not swallow
			// transport failures or turn an uncertain publication into a success.
			if (!error?.responseJSON?._server_messages) frappe.show_alert({ message: __("Could not reach ERP. Reconnect, then try again."), indicator: "red" });
		} finally {
			this.busy = false;
			this.body.attr("aria-busy", "false").prop("inert", false);
			this.focusAfterRun?.trigger("focus");
			this.focusAfterRun = null;
		}
	}

	focus(target) {
		// Browsers refuse focus inside an inert form. Navigation and validation
		// must announce their destination after the current action unlocks it.
		if (this.busy) this.focusAfterRun = target;
		else target.trigger("focus");
	}

	errorMessage(error, fallback) {
		try {
			const messages = JSON.parse(error?.responseJSON?._server_messages || "[]");
			const last = messages.at(-1);
			const value = typeof last === "string" ? JSON.parse(last) : last;
			// Server messages may contain markup. Render only their text.
			if (value?.message) return String(value.message).replace(/<[^>]*>/g, "");
		} catch (_invalidMessage) { /* Use the explicit transport explanation. */ }
		return fallback;
	}

	error(message) {
		this.body.find(".jiji-offer-error").remove();
		const box = $("<div>").addClass("jiji-offer-error").attr({ role: "alert", tabindex: "-1" }).text(message);
		(this.panel || this.body).prepend(box);
		this.focus(box);
		return box;
	}

	button(parent, label, action, primary = false) {
		return $("<button>").attr("type", "button").addClass(`btn ${primary ? "btn-primary" : "btn-default"}`)
			.text(__(label)).on("click", () => this.run(action)).appendTo(parent);
	}

	async load() {
		try {
			this.data = await this.call("get_promotion_desk", {}, "GET");
			if (!this.editor) this.home();
		} catch (_error) { this.error(__("Offers could not be loaded. Nothing has changed. Refresh to try again.")); }
	}

	home() {
		this.editor = null;
		this.panel = null;
		this.body.empty();
		const head = $("<div>").addClass("jiji-offers-head").appendTo(this.body);
		$("<div>").append($("<h2>").text(__("Offers for your café")), $("<p>").addClass("jiji-offer-help").text(__("Give customers the best available price. Set it up here; tills calculate it automatically."))).appendTo(head);
		this.button(head, "Create an offer", () => this.create(), true).prop("disabled", !this.data.outlets?.length);
		if (!this.data.outlets?.length) this.body.append(jiji.desk.notice(__("No outlet is available to your account"), __("Ask the owner to give you access to your café before creating an offer.")));
		const grid = $("<div>").addClass("jiji-offers-grid").appendTo(this.body);
		if (!this.data.offers?.length) grid.append(jiji.desk.notice(__("Your first offer starts here"), __("Try a simple percent-off offer. You can save a draft and check the price before switching it on.")));
		(this.data.offers || []).forEach((offer) => {
			const card = $("<section>").addClass("jiji-offer-card").appendTo(grid);
			$("<h2>").text(offer.name).appendTo(card);
			card.append(jiji.desk.pill(__(offer.state), offer.state === "Published" ? "ok" : ""));
			if (offer.has_draft_changes) card.append(jiji.desk.pill(__("Unpublished changes"), "attention"));
			$("<p>").addClass("jiji-offer-help").text(offer.outlets.join(" · ")).appendTo(card);
			$("<p>").text(offer.delivery).appendTo(card);
			if (offer.tablets?.length) {
				const detail = $("<details>").append($("<summary>").text(__("Which tills?"))).appendTo(card);
				offer.tablets.forEach((tablet) => $("<p>").text(`${tablet.name} · ${tablet.outlet} · ${__(tablet.received ? "Received" : "Waiting to sync")}`).appendTo(detail));
			}
			const actions = $("<div>").addClass("jiji-offer-actions").appendTo(card);
			this.button(actions, offer.editable ? "Edit offer" : "View details", () => this.open(offer.id));
			if (offer.editable) this.button(actions, "Make a copy", () => this.open(offer.id, true));
			if (offer.published_outlets.length) this.button(actions, "Stop offer", () => this.stop(offer));
		});
		this.button(this.body, "Refresh tablet status", () => this.load());
	}

	create() {
		this.editor = { id: null, revision: null, dirty: true, step: 0, receipt: null, basket: [],
			values: { name: "", kind: "percentage", value: "10", items: [],
				outlets: this.data.outlets.length === 1 ? [this.data.outlets[0].name] : [],
				repeat: "repeat", activation: "automatic", start: "", end: "", schedule: null } };
		this.render();
	}

	async open(id, copy = false) {
		const result = await this.call("get_offer_editor", { name: id }, "GET");
		if (!result.editable) { frappe.msgprint({ title: __("Advanced offer"), message: __(result.message), indicator: "orange" }); return; }
		if (!this.data) this.data = await this.call("get_promotion_desk", {}, "GET");
		this.editor = { id: copy ? null : result.id, revision: copy ? null : result.revision,
			dirty: copy, step: 0, receipt: null, basket: [], values: result.values };
		if (copy) this.editor.values.name = __("{0} (copy)", [result.values.name]);
		this.render();
	}

	change(key, value) {
		this.editor.values[key] = value;
		this.editor.dirty = true;
		this.editor.receipt = null;
		this.editor.previewChecked = false;
		this.previewSequence++;
		this.updateActivation();
	}

	updateActivation() {
		const editor = this.editor;
		this.activateButton?.prop("disabled", !editor?.previewChecked || !editor.receipt || editor.dirty || editor.receipt.issues.length > 0);
	}

	field(parent, label, key, type = "Data", extra = {}) {
		if (type === "Datetime") return this.datetimeInput(parent, label, this.editor.values[key], value => this.change(key, value));
		const editor = this.editor;
		const holder = $("<div>").appendTo(parent);
		const control = frappe.ui.form.make_control({ parent: holder, render_input: true,
			df: { fieldname: key, label: __(label), fieldtype: type, ...extra } });
		control.set_value(this.editor.values[key]);
		control.df.onchange = () => {
			const value = control.get_value() ?? "";
			if (editor === this.editor && value !== (editor.values[key] ?? "")) this.change(key, value);
		};
		return control;
	}

	datetimeInput(parent, label, value, change) {
		// datetime-local has no account/browser timezone conversion. The API
		// interprets this wall time only as Asia/Kuala_Lumpur.
		const row = $("<label>").text(__(label)).appendTo(parent);
		return $("<input>").attr({type: "datetime-local", step: 60}).addClass("form-control")
			.val(value ? String(value).replace(" ", "T").slice(0, 16) : "")
			.on("change", event => change(event.target.value ? event.target.value.replace("T", " ").slice(0, 16) + ":00" : "")).appendTo(row);
	}

	check(parent, label, checked, change, detail) {
		const row = $("<label>").addClass("jiji-offer-choice").appendTo(parent);
		$("<input>").attr("type", "checkbox").prop("checked", checked).on("change", (event) => change(event.target.checked)).appendTo(row);
		$("<span>").text(__(label)).appendTo(row);
		if (detail) $("<small>").text(detail).appendTo(row);
	}

	render() {
		this.sequence++;
		this.previewSequence++;
		this.body.empty();
		const editor = this.editor;
		const steps = ["Offer", "Products & outlets", "Schedule", "Review"];
		const nav = $("<ol>").addClass("jiji-offer-steps").attr("aria-label", __("Offer setup progress")).appendTo(this.body);
		steps.forEach((label, index) => {
			const item = $("<li>").appendTo(nav);
			$("<button>").attr({ type: "button", "aria-current": index === editor.step ? "step" : null })
				.prop("disabled", index > editor.step).text(`${index + 1}. ${__(label)}`)
				.on("click", () => { editor.step = index; this.render(); }).appendTo(item);
		});
		this.panel = $("<section>").addClass("jiji-offer-panel").appendTo(this.body);
		this.focus($("<h2>").attr("tabindex", "-1").text(__(["What would you like to offer?", "Where should it work?", "When should it run?", "Check before you switch it on"][editor.step])).appendTo(this.panel));
		if (editor.id) $("<p>").addClass("jiji-offer-help").text(__("Editing this draft does not change the offer already on tills." )).appendTo(this.panel);
		[this.offerStep, this.productsStep, this.scheduleStep, this.reviewStep][editor.step].call(this);
		const actions = $("<div>").addClass("jiji-offer-actions").appendTo(this.panel);
		this.button(actions, editor.step ? "Back" : "All offers", async () => {
			if (editor.step) { editor.step--; this.render(); }
			else if (editor.dirty) frappe.confirm(__("Leave this editor without saving your changes? Your last saved draft will stay."), () => { this.editor = null; this.load(); });
			else { this.editor = null; await this.load(); }
		});
		if (editor.step < 3) this.button(actions, `Continue to ${steps[editor.step + 1]}`, () => {
			if (!this.validateStep()) return;
			editor.step++; this.render();
		}, true);
		else {
			this.button(actions, "Save draft", () => this.save(), true);
			this.activateButton = this.button(actions, "Activate offer", () => this.activate());
			this.updateActivation();
		}
	}

	validateStep() {
		const { step, values } = this.editor;
		let message = "";
		if (step === 0 && !values.name.trim()) message = "Give this offer a name, such as Afternoon coffee treat.";
		else if (step === 0 && (!/^\d{1,7}(\.\d{1,2})?$/.test(String(values.value))
			|| (!values.kind.endsWith("second") && Number(values.value) <= 0)
			|| (values.kind === "percentage" && Number(values.value) > 100))) message = values.kind === "percentage"
			? "Enter a percent greater than 0 and no more than 100, such as 10."
			: "Enter an amount greater than RM0, with no more than two decimal places, such as 1.50.";
		if (step === 1 && (!values.items.length || !values.outlets.length)) message = "Choose at least one product and one outlet.";
		if (step === 2 && values.schedule && (!values.schedule.weekdays.length || values.schedule.startMinute === values.schedule.endMinute)) message = "Choose at least one day and different opening and closing times.";
		if (message) { this.error(__(message)); return false; }
		return true;
	}

	offerStep() {
		const values = this.editor.values;
		$("<p>").addClass("jiji-offer-help").text(__("Choose a starting point. If offers overlap, customers receive the best total price; discounts are not stacked on the same item." )).appendTo(this.panel);
		const grid = $("<div>").addClass("jiji-offers-grid").appendTo(this.panel);
		this.templates.forEach(([key, title, detail]) => {
			$("<button>").attr({ type: "button", "aria-pressed": values.kind === key }).addClass("jiji-offer-template")
				.append($("<strong>").text(__(title)), $("<span>").text(__(detail))).on("click", () => {
					this.change("kind", key); this.change("value", key === "free_second" ? "0" : key === "half_second" ? "50" : key === "percentage" ? "10" : "1"); this.render();
				}).appendTo(grid);
		});
		const fields = $("<div>").addClass("jiji-offer-fields").appendTo(this.panel);
		this.field(fields, "Offer name", "name", "Data", { reqd: 1, description: __("For example: Afternoon coffee treat" ) });
		const template = this.templates.find((row) => row[0] === values.kind);
		if (template[3]) this.field(fields, template[3], "value", "Data", { reqd: 1 });
		$("<p>").addClass("jiji-offer-help").text(__("Applies automatically. Paid extras stay full price." )).appendTo(this.panel);
		const advanced = $("<details>").append($("<summary>").text(__("More options"))).appendTo(this.panel);
		this.check(advanced, "Staff choose this offer at checkout", values.activation === "manual_selectable", (checked) => this.change("activation", checked ? "manual_selectable" : "automatic"));
		if (values.kind.endsWith("second")) this.check(advanced, "Repeat for every qualifying pair", values.repeat === "repeat", (checked) => this.change("repeat", checked ? "repeat" : "once"));
	}

	productsStep() {
		const values = this.editor.values;
		$("<h3>").text(__("Choose outlets" )).appendTo(this.panel);
		this.data.outlets.forEach((outlet) => this.check(this.panel, outlet.name, values.outlets.includes(outlet.name), (checked) => {
			this.change("outlets", checked ? [...values.outlets, outlet.name] : values.outlets.filter((name) => name !== outlet.name));
			this.render();
		}, outlet.company));
		$("<h3>").text(__("Choose products" )).appendTo(this.panel);
		if (!values.outlets.length) { $("<p>").text(__("Choose an outlet first to see its current menu." )).appendTo(this.panel); return; }
		$("<p>").addClass("jiji-offer-help").text(__("Prices shown for {0}. We also check availability at every other selected outlet.", [values.outlets[0]])).appendTo(this.panel);
		const search = $("<input>").attr({ type: "search", placeholder: __("Search product name"), "aria-label": __("Search products") }).addClass("form-control").appendTo(this.panel);
		const count = $("<p>").attr("aria-live", "polite").appendTo(this.panel);
		const list = $("<div>").addClass("jiji-offer-products").appendTo(this.panel).text(__("Loading current menu prices…"));
		const sequence = this.sequence;
		this.call("get_offer_products", { pos_profile: values.outlets[0] }, "GET").then((result) => {
			if (sequence !== this.sequence) return;
			this.menu = result.items;
			const draw = () => {
				list.empty(); count.text(__("{0} selected", [values.items.length]));
				const matches = this.menu.filter((item) => item.label.toLowerCase().includes(String(search.val()).toLowerCase()));
				matches.forEach((item) => this.check(list, item.label, values.items.includes(item.id), (checked) => {
					this.change("items", checked ? [...values.items, item.id] : values.items.filter((id) => id !== item.id));
					count.text(__("{0} selected", [values.items.length]));
				}, this.money(item.price_sen)));
				if (!matches.length) list.text(__("No products match. Try another name."));
			};
			search.on("input", draw); draw();
		}).catch(() => { if (sequence === this.sequence) list.text(__("Menu prices could not be loaded. Go Back, then try again.")); });
	}

	scheduleStep() {
		const values = this.editor.values;
		$("<p>").addClass("jiji-offer-help").text(__("All dates and times below use Malaysia time (UTC+8), including when you travel." )).appendTo(this.panel);
		this.check(this.panel, "Repeat on selected days every week", Boolean(values.schedule), (checked) => {
			this.change("schedule", checked ? { timeZone: "Asia/Kuala_Lumpur", weekdays: [1, 2, 3, 4, 5], startDate: null, endDate: null, startMinute: 14 * 60, endMinute: 17 * 60 } : null);
			this.change("start", ""); this.change("end", ""); this.render();
		});
		const fields = $("<div>").addClass("jiji-offer-fields").appendTo(this.panel);
		if (!values.schedule) {
			this.field(fields, "Starts (leave empty for now)", "start", "Datetime");
			this.field(fields, "Ends (optional)", "end", "Datetime");
			return;
		}
		const schedule = values.schedule;
		const change = (key, value) => { schedule[key] = value; this.change("schedule", schedule); };
		const days = $("<div>").addClass("jiji-offer-days").appendTo(this.panel);
		["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].forEach((day, index) => this.check(days, day, schedule.weekdays.includes(index + 1), (checked) => change("weekdays", checked ? [...schedule.weekdays, index + 1].sort() : schedule.weekdays.filter((value) => value !== index + 1))));
		for (const [key, label] of [["startDate", "First date (optional)"], ["endDate", "Last starting date (optional)"]]) {
			const field = frappe.ui.form.make_control({ parent: $("<div>").appendTo(fields), render_input: true, df: { fieldname: key, label: __(label), fieldtype: "Date" } });
			field.set_value(schedule[key]); field.df.onchange = () => {
				const value = field.get_value() || null;
				if (this.editor?.values.schedule === schedule && value !== schedule[key]) change(key, value);
			};
		}
		this.check(this.panel, "All day", schedule.startMinute === 0 && schedule.endMinute === 1440, (checked) => {
			change("startMinute", checked ? 0 : 840); change("endMinute", checked ? 1440 : 1020); this.render();
		});
		if (!(schedule.startMinute === 0 && schedule.endMinute === 1440)) {
			for (const [key, label] of [["startMinute", "From"], ["endMinute", "Until"]]) {
				const row = $("<label>").addClass("jiji-offer-choice").text(__(label)).appendTo(this.panel);
				$("<input>").attr("type", "time").addClass("form-control").val(`${String(Math.floor(schedule[key] / 60) % 24).padStart(2, "0")}:${String(schedule[key] % 60).padStart(2, "0")}`)
					.on("change", (event) => { const [hours, minutes] = event.target.value.split(":").map(Number); change(key, hours * 60 + minutes); note(); }).appendTo(row);
			}
		}
		const detail = $("<p>").addClass("jiji-offer-help").attr("aria-live", "polite").appendTo(this.panel);
		const note = () => detail.text(__(schedule.endMinute < schedule.startMinute ? "Continues into the next morning. The chosen weekday is the day the offer starts." : "The offer stops at the Until time. We show its next three runs on Review."));
		note();
	}

	money(sen) { return new Intl.NumberFormat("en-MY", { style: "currency", currency: "MYR" }).format(sen / 100); }
	date(value) { return new Intl.DateTimeFormat("en-MY", { timeZone: "Asia/Kuala_Lumpur", weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }).format(new Date(value)); }

	reviewStep() {
		const editor = this.editor;
		$("<p>").addClass("jiji-offer-help").text(__("Try a basket at real menu prices. Save a draft to run the final cost and tablet checks. Only Activate sends it to tills." )).appendTo(this.panel);
		$("<h3>").text(editor.values.name).appendTo(this.panel);
		$("<p>").text(editor.values.outlets.join(" · ")).appendTo(this.panel);
		const template = this.templates.find((row) => row[0] === editor.values.kind);
		$("<p>").text(__(template[1]) + (template[3] ? ` · ${editor.values.kind === "percentage" ? editor.values.value + "%" : "RM" + editor.values.value}` : "")
			+ " · " + __(editor.values.activation === "automatic" ? "Automatic at checkout" : "Staff choose at checkout")
			+ (editor.values.kind.endsWith("second") ? " · " + __(editor.values.repeat === "repeat" ? "Every qualifying pair" : "One pair per order") : "")).appendTo(this.panel);
		if (!editor.values.schedule) $("<p>").text(__("Starts: {0}. Ends: {1}. Malaysia time.", [editor.values.start || __("When activated"), editor.values.end || __("When you stop it")])).appendTo(this.panel);
		if (!editor.basket.length) editor.basket = [{ item: editor.values.items[0], quantity: editor.values.kind.endsWith("second") ? 2 : 1 }];
		this.previewArea = $("<div>").appendTo(this.panel);
		this.readinessArea = $("<div>").attr("aria-live", "polite").appendTo(this.panel);
		this.preview();
		if (editor.receipt) this.readiness(editor.receipt);
	}

	async preview() {
		const editor = this.editor;
		editor.sampleQuantityError?.remove();
		editor.sampleQuantityError = null;
		const sequence = ++this.previewSequence;
		editor.previewChecked = false;
		this.updateActivation();
		const area = this.previewArea;
		area.empty().text(__("Checking the basket…"));
		try {
			const result = await this.call("preview_offer", { payload: editor.values, name: editor.id, pos_profile: editor.previewOutlet || editor.values.outlets[0], basket: editor.basket, at_time: editor.previewTime || null });
			if (editor !== this.editor || sequence !== this.previewSequence) return;
			area.empty();
			const controls = $("<div>").addClass("jiji-offer-fields").appendTo(area);
			const outlet = frappe.ui.form.make_control({ parent: controls, render_input: true, df: { fieldname: "example_outlet", label: __("Sample outlet"), fieldtype: "Select", options: editor.values.outlets.join("\n") } });
			outlet.set_value(result.pos_profile); outlet.df.onchange = () => {
				const value = outlet.get_value();
				// Initial set_value can fire onchange asynchronously. Only an actual
				// selection change on the current receipt requests another quote.
				if (editor !== this.editor || sequence !== this.previewSequence || value === result.pos_profile) return;
				editor.previewOutlet = value; this.preview();
			};
			this.datetimeInput(controls, "Try a different date & time (Malaysia)", editor.previewTime || "", value => { editor.previewTime = value || null; this.preview(); });
			const receipt = $("<div>").addClass("jiji-offer-receipt").appendTo(area);
			$("<h3>").text(__("Sample receipt" )).appendTo(receipt);
			$("<p>").text(this.date(result.at_time) + " · " + __("Malaysia time")).appendTo(receipt);
			result.quote.items.forEach((line, index) => {
				const row = $("<div>").addClass("jiji-offer-receipt-row").appendTo(receipt);
				$("<span>").text(`${line.name} · ${this.money(line.unitPriceSen)} each`).appendTo(row);
				$("<input>").attr({ type: "number", min: 1, max: 100, step: 1, "aria-label": __("Quantity of {0}", [line.name]) }).css("width", "70px").val(line.quantity)
					.on("change", (event) => {
						const quantity = Number(event.target.value);
						if (!Number.isInteger(quantity) || quantity < 1 || quantity > 100) {
							event.target.value = String(editor.basket[index].quantity);
							editor.sampleQuantityError = this.error(__("Use a whole quantity from 1 to 100. Your previous sample quantity was kept."));
							return;
						}
						editor.basket[index].quantity = quantity; this.preview();
					}).appendTo(row);
				if (editor.basket.length > 1) this.button(row, "Remove", () => { editor.basket.splice(index, 1); return this.preview(); }).attr("aria-label", __("Remove {0}", [line.name]));
			});
			const append = (label, amount, total = false) => $("<div>").addClass("jiji-offer-receipt-row" + (total ? " jiji-offer-receipt-total" : ""))
				.append($("<span>").text(label), $("<span>").text(this.money(amount))).appendTo(receipt);
			append(__("Before offers"), result.quote.items.reduce((sum, line) => sum + line.unitPriceSen * line.quantity, 0));
			result.quote.appliedPromotions.forEach((offer) => append(offer.promotionName, -offer.amountSen));
			if (!result.quote.appliedPromotions.length) $("<p>").text(__("No offer qualifies for this basket at this time." )).appendTo(receipt);
			append(__("Customer pays, before tax"), result.quote.items.reduce((sum, line) => sum + line.lineTotalSen, 0), true);
			$("<p>").text(__("You save {0}", [this.money(result.quote.totalDiscountSen)])).appendTo(receipt);
			if (editor.values.items.length > 1) this.button(area, "Try all selected products", () => { editor.basket = editor.values.items.map((item) => ({ item, quantity: 1 })); return this.preview(); });
			if (result.sample_products?.length && editor.basket.length < 50) {
				const sample = frappe.ui.form.make_control({ parent: area, render_input: true, df: { fieldname: "sample_product", label: __("Add a product to this sample"), fieldtype: "Select", options: result.sample_products.map((item) => ({ value: item.id, label: `${item.label} · ${this.money(item.price_sen)}` })) } });
				this.button(area, "Add to sample", () => { editor.basket.push({ item: sample.get_value(), quantity: 1 }); return this.preview(); });
			}
			$("<p>").addClass("jiji-offer-help").text(result.notice).appendTo(area);
			if (result.occurrences.length) {
				$("<h3>").text(__("Next three runs · Malaysia time" )).appendTo(area);
				const list = $("<ul>").appendTo(area);
				result.occurrences.forEach((run) => $("<li>").text(`${this.date(run.start)} – ${this.date(run.end)}`).appendTo(list));
			}
			editor.previewChecked = true;
			this.updateActivation();
		} catch (error) {
			if (editor === this.editor && sequence === this.previewSequence) {
				area.empty().append(jiji.desk.notice(__("This basket could not be checked"), this.errorMessage(error, __("Check the offer details, products and quantities. No price has been accepted and nothing has been activated."))));
				this.button(area, "Try again", () => this.preview());
				this.button(area, "Reset sample basket", () => {
					editor.basket = [{ item: editor.values.items[0], quantity: editor.values.kind.endsWith("second") ? 2 : 1 }];
					editor.previewTime = null; editor.previewOutlet = editor.values.outlets[0];
					return this.preview();
				});
				this.button(area, "Change offer details", () => { editor.step = 0; this.render(); });
				this.updateActivation();
			}
		}
	}

	async save() {
		const editor = this.editor;
		const saved = await this.call("save_offer_draft", { payload: editor.values, name: editor.id, revision: editor.revision });
		if (editor !== this.editor) return;
		editor.id = saved.id; editor.revision = saved.revision; editor.dirty = false;
		frappe.show_alert({ message: __("Draft saved. It has not been sent to tills."), indicator: "green" });
		editor.receipt = await this.call("review_saved_offer", { name: editor.id, revision: editor.revision });
		if (editor !== this.editor) return;
		this.readiness(editor.receipt);
	}

	readiness(receipt) {
		this.readinessArea.empty();
		if (!receipt.issues.length) this.readinessArea.append(jiji.desk.notice(__("Ready for your approval"), __("Current menu prices and costs have been checked. Review the sample, then activate.")));
		for (const issue of receipt.issues) {
			const card = jiji.desk.notice(issue.title, issue.message).appendTo(this.readinessArea);
			if (issue.route?.length) card.append(jiji.desk.routeLink(issue.route, issue.action || __("Fix this")));
		}
		this.updateActivation();
	}

	async activate() {
		const editor = this.editor;
		if (!editor.previewChecked || !editor.receipt || editor.dirty || editor.receipt.issues.length) return;
		await this.call("activate_reviewed_offer", { name: editor.id, revision: editor.revision, expected_hash: editor.receipt.expected_hash });
		this.editor = null;
		await this.load();
		frappe.msgprint({ title: __("Offer published"), message: __("Connected tills will receive it on their next sync. Offline tills are still waiting; check tablet status here."), indicator: "green" });
	}

	async stop(offer) {
		frappe.confirm(__("Stop {0} at {1}? Connected tills stop after syncing. An offline till may keep using its saved offer until it reconnects.", [offer.name, offer.published_outlets.join(", ")]), () => this.run(async () => {
			const review = await this.call("review_offer_stop", { name: offer.id });
			await this.call("stop_reviewed_offer", { name: offer.id, expected_hash: review.expected_hash });
			await this.load();
			frappe.show_alert({ message: __("Stop published. Check which tills have received it."), indicator: "green" });
		}));
	}
}
