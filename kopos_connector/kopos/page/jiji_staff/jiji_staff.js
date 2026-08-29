/**
 * JiJi Staff.
 *
 * The form behind this page showed an email as its title, an employee code, a
 * revision counter, a grid with a Warehouse column, five red asterisks, and a
 * Comments feed on a security record. It also could not set a PIN at all: the
 * field sits at permlevel 1 and no role had permission to write it.
 *
 * A director needs three things here -- who can use a tablet, at what level,
 * and how to give someone a PIN -- and the two states the old form could not
 * say: "No PIN yet" and "Not working here now".
 *
 * The PIN is written by the doctype, which hashes it, bumps the revision and
 * invalidates every bound tablet. This page collects four digits twice and
 * never shows them again.
 *
 * Routing and the shared look live in public/js/jiji_desk.js.
 */

frappe.pages["jiji_staff"].on_page_load = function (wrapper) {
	new JiJiStaff(wrapper);
};

class JiJiStaff {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("JiJi Staff"),
			single_column: true,
		});
		this.page.set_primary_action(__("Refresh"), () => this.load(), "refresh");
		// Adding a person is the one thing this page cannot do from a row, and
		// the record is where it is correctly done.
		this.page.add_inner_button(__("Add someone"), () =>
			jiji.desk.go(["Form", "KoPOS Staff Access", "new"]),
		);
		this.body = jiji.desk.page(this.page);
		this.injectStyle();
		// Whose PIN the form at the bottom is for. Null means it is not shown.
		this.setting = null;
		this.load();
	}

	injectStyle() {
		if (document.getElementById("jiji-staff-style")) return;
		const style = document.createElement("style");
		style.id = "jiji-staff-style";
		style.textContent = `
.jiji-staff__avatar { width: 30px; height: 30px; border-radius: 15px; flex: 0 0 auto;
  display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600;
  background: var(--jiji-quiet); color: var(--jiji-ink-2); }
.jiji-staff__name { font-weight: 600; }
.jiji-staff__pin-title { font-size: 16px; font-weight: 600; }
.jiji-staff__pin-detail { color: var(--jiji-ink-2); margin: 3px 0 16px; }
.jiji-staff__pin-form { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.jiji-staff__pin-form input { width: 168px; height: 36px; border: 1px solid var(--jiji-line);
  border-radius: 8px; padding: 0 12px; background: var(--jiji-surface); color: inherit;
  font-variant-numeric: tabular-nums; letter-spacing: .3em; }
/* The spacing is for four digits. On a sentence it clipped "Type it again". */
.jiji-staff__pin-form input::placeholder { letter-spacing: normal; }
.jiji-staff__pin-error { color: var(--jiji-stop); }
`;
		document.head.appendChild(style);
	}

	load() {
		frappe.call({
			method: "kopos_connector.api.staff_desk.get_staff_desk",
			type: "GET",
			callback: (response) => this.render(response.message || {}),
			error: () =>
				this.body.empty().append(
					jiji.desk.notice(
						__("JiJi could not read your people just now"),
						__("Nobody's access has changed. Try Refresh in a moment."),
					),
				),
		});
	}

	render(data) {
		this.data = data;
		this.body.empty();
		if (data.status === "no_company") {
			this.body.append(
				jiji.desk.notice(
					__("Choose a company to see its people"),
					__("Set your default company, then open JiJi Staff again."),
				),
			);
			return;
		}
		if (data.status !== "ok") {
			this.body.append(
				jiji.desk.notice(
					__("JiJi could not read your people just now"),
					__("Nobody's access has changed. Try Refresh in a moment."),
				),
			);
			return;
		}

		const list = $("<div>").addClass("jiji-rows");
		(data.people || []).forEach((person) => list.append(this.renderPerson(person)));
		this.body.append(list);

		if (this.setting) this.body.append(this.renderPinForm(this.setting));

		if ((data.tablets || []).length) {
			const block = $("<div>");
			const head = $("<div>").addClass("jiji-head").appendTo(block);
			head.append(jiji.desk.label(__("Tablets right now")));
			head.append($("<span>").addClass("jiji-muted").text(__("A tablet is a place, not a person.")));
			const tablets = $("<div>").addClass("jiji-rows").appendTo(block);
			data.tablets.forEach((tablet) => {
				const row = jiji.desk.routeLink(tablet.route).addClass("jiji-row");
				const main = $("<span>").addClass("jiji-row__main").appendTo(row);
				main.append($("<div>").addClass("jiji-staff__name").text(tablet.tablet || ""));
				main.append($("<div>").addClass("jiji-row__note").text(tablet.detail || ""));
				row.append(jiji.desk.pill(tablet.state || "", tablet.state_level));
				tablets.append(row);
			});
			this.body.append(block);
		}

		this.body.append(jiji.desk.advanced(__("employee record, outlet stores, change history")));
	}

	renderPerson(person) {
		const row = $("<div>").addClass("jiji-row");
		row.append(
			$("<span>")
				.addClass("jiji-staff__avatar")
				.text((person.name || "?").trim().charAt(0).toUpperCase()),
		);
		const main = $("<span>").addClass("jiji-row__main").appendTo(row);
		main.append($("<div>").addClass("jiji-staff__name").text(person.name || ""));
		main.append(
			$("<div>")
				.addClass("jiji-row__note")
				.text(person.active ? person.outlets || "" : __("Not working here now")),
		);
		if (person.active) row.append(jiji.desk.pill(person.role || "", ""));
		row.append(jiji.desk.pill(person.state || "", person.state_level));

		if (person.active) {
			$("<button>")
				.addClass("btn btn-sm " + (person.has_pin ? "btn-default" : "btn-primary"))
				.text(person.action || __("Set tablet PIN"))
				.on("click", () => {
					// Re-rendering from the data already in hand keeps one
					// answer on screen; the server is asked only after a save.
					this.setting = person;
					this.render(this.data);
				})
				.appendTo(row);
		} else {
			jiji.desk.routeLink(person.route, __("Bring back")).addClass("btn btn-sm btn-default").appendTo(row);
		}
		return row;
	}

	renderPinForm(person) {
		const card = $("<div>").addClass("jiji-card");
		card.append(
			$("<div>")
				.addClass("jiji-staff__pin-title")
				.text(__("Set a PIN for {0}", [person.name || ""])),
		);
		card.append(
			$("<div>")
				.addClass("jiji-staff__pin-detail")
				.text(__("Four digits, entered twice. Tell them in person — JiJi never shows a PIN again after this.")),
		);

		const form = $("<div>").addClass("jiji-staff__pin-form").appendTo(card);
		const pin = $('<input type="password" inputmode="numeric" maxlength="4" autocomplete="new-password">')
			.attr("aria-label", __("New PIN"))
			.attr("placeholder", "····")
			.appendTo(form);
		const confirm = $('<input type="password" inputmode="numeric" maxlength="4" autocomplete="new-password">')
			.attr("aria-label", __("Type it again"))
			.attr("placeholder", __("Type it again"))
			.appendTo(form);
		const save = $("<button>").addClass("btn btn-primary").text(__("Save PIN")).appendTo(form);
		const note = $("<span>")
			.addClass("jiji-muted")
			.text(__("Their tablets will ask for the new PIN within a minute."))
			.appendTo(form);

		save.on("click", () => {
			save.prop("disabled", true);
			note.removeClass("jiji-staff__pin-error").text(__("Saving…"));
			frappe.call({
				method: "kopos_connector.api.staff_desk.set_staff_pin",
				type: "POST",
				args: { access: person.access, pin: pin.val(), confirm: confirm.val() },
				callback: (response) => {
					const result = response.message || {};
					// Clear both fields before anything else: the digits should
					// not survive in the page after they have been saved.
					pin.val("");
					confirm.val("");
					this.setting = null;
					frappe.show_alert({
						message: __("{0} can now sign in", [result.person || person.name || ""]),
						indicator: "green",
					});
					this.load();
				},
				// Frappe already shows the thrown message, which names the
				// reason. The page only has to stay usable for a second try.
				error: () => {
					save.prop("disabled", false);
					note.addClass("jiji-staff__pin-error").text(__("The PIN was not changed."));
				},
			});
		});

		$("<button>")
			.addClass("btn btn-link")
			.text(__("Cancel"))
			.on("click", () => {
				this.setting = null;
				this.render(this.data);
			})
			.appendTo(form);

		return card;
	}
}
