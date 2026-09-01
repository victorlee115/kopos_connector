/**
 * JiJi Staff.
 *
 * Business users stay on this page. The security record, internal identifiers,
 * and outlet links remain behind the scoped API.
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
				title: __("Staff"),
			single_column: true,
		});
		this.page.set_primary_action(__("Add someone"), () => this.openAddPerson());
		this.page.add_inner_button(__("Refresh"), () => this.load());
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
						__("Nobody has been changed. Try Refresh in a moment."),
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
						__("Choose a café to see its people"),
						__("Ask Support to choose the café for your account."),
				),
			);
			return;
		}
		if (data.status !== "ok") {
			this.body.append(
				jiji.desk.notice(
					__("JiJi could not read your people just now"),
					__("Nobody has been changed. Try Refresh in a moment."),
				),
			);
			return;
		}

		// Every other block on the desk is named. This one started mid-air.
		const people = $("<div>").append(
			jiji.desk.label(
				data.needs_pin
					? __("Who can use a tablet · {0} waiting for a PIN", [data.needs_pin])
					: __("Who can use a tablet"),
			),
		);
		const list = $("<div>").addClass("jiji-rows").css("margin-top", "8px").appendTo(people);
		if ((data.people || []).length) {
			data.people.forEach((person) => list.append(this.renderPerson(person)));
		} else {
			list.append(
				$("<div>")
					.addClass("jiji-row jiji-muted")
					.text(__("No one can use a tablet yet. Add someone to get started.")),
			);
		}
		this.body.append(people);

		if (this.setting) this.body.append(this.renderPinForm(this.setting));

		if ((data.tablets || []).length) {
			const block = $("<div>");
			const head = $("<div>").addClass("jiji-head").appendTo(block);
			head.append(jiji.desk.label(__("Tablets right now")));
			head.append($("<span>").addClass("jiji-muted").text(__("Where each tablet is, and who is on it")));
			const tablets = $("<div>").addClass("jiji-rows").appendTo(block);
			data.tablets.forEach((tablet) => {
					const row = $("<div>").addClass("jiji-row");
				const main = $("<span>").addClass("jiji-row__main").appendTo(row);
				main.append($("<div>").addClass("jiji-staff__name").text(tablet.tablet || ""));
				main.append($("<div>").addClass("jiji-row__note").text(tablet.detail || ""));
				row.append(jiji.desk.pill(tablet.state || "", tablet.state_level));
				tablets.append(row);
			});
			this.body.append(block);
		}

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
			$("<button>")
				.addClass("btn btn-sm btn-link text-danger")
				.text(__("Remove access"))
				.on("click", () => this.setStaffActive(person, false))
				.appendTo(row);
		} else {
			$("<button>")
				.addClass("btn btn-sm btn-default")
				.text(__("Bring back"))
				.on("click", () => this.setStaffActive(person, true))
				.appendTo(row);
		}
		return row;
	}

	openAddPerson() {
		frappe.call({
			method: "kopos_connector.api.staff_desk.get_staff_setup_choices",
			type: "GET",
			args: { company: this.data?.company || "" },
			callback: (response) => {
				const choices = response.message || {};
				if (choices.status !== "ok") {
					frappe.msgprint({
						title: __("Choose a café first"),
						message: __("Ask Support to choose the café for your account."),
					});
					return;
				}
				if (!(choices.people || []).length) {
					const message = choices.people_missing_sign_in
						? __("Someone's staff details are missing a sign-in email. Ask Support to finish them first.")
						: __("No one else is ready to add. Ask Support if the person you need is missing.");
					frappe.msgprint({ title: __("Nobody to add right now"), message });
					return;
				}
				const people = choices.people.map((row) => `<option value="${this.escape(row.value)}">${this.escape(row.label)}</option>`).join("");
				const outlets = (choices.outlets || []).map((row) => `<option value="${this.escape(row.value)}">${this.escape(row.label)}</option>`).join("");
				const dialog = new frappe.ui.Dialog({
					title: __("Add someone"),
					fields: [
						{ fieldtype: "HTML", fieldname: "person_picker", options: `<label class="control-label">${__("Person")}</label><select class="form-control" data-new-staff-person><option value="">${__("Choose someone")}</option>${people}</select>` },
						{ fieldtype: "HTML", fieldname: "outlet_picker", options: `<label class="control-label">${__("Outlet")}</label><select class="form-control" data-new-staff-outlet><option value="">${__("Choose an outlet")}</option>${outlets}</select>` },
						{ fieldtype: "Select", fieldname: "level", label: __("What can they do?"), options: [__("Staff"), __("Manager")], default: __("Staff"), reqd: 1, description: __("Managers can approve the actions that need a manager on the tablet.") },
						{ fieldtype: "Password", fieldname: "pin", label: __("Four-digit tablet PIN"), reqd: 1 },
						{ fieldtype: "Password", fieldname: "confirm", label: __("Type the PIN again"), reqd: 1 },
					],
					primary_action_label: __("Give tablet access"),
					primary_action: (values) => {
						const employee = dialog.$wrapper.find("[data-new-staff-person]").val() || "";
						const outlet = dialog.$wrapper.find("[data-new-staff-outlet]").val() || "";
						if (!employee) return frappe.msgprint(__("Choose someone."));
						if (!outlet) return frappe.msgprint(__("Choose an outlet."));
						frappe.call({
							method: "kopos_connector.api.staff_desk.add_staff_access",
							type: "POST",
							args: {
								company: choices.company,
								employee,
								outlet,
								access_level: values.level === __("Manager") ? "Manager" : "Staff",
								pin: values.pin,
								confirm: values.confirm,
							},
							callback: (saveResponse) => {
								const result = saveResponse.message || {};
								dialog.hide();
								frappe.show_alert({
									message: __("{0} can now sign in", [result.person || __("This person")]),
									indicator: "green",
								});
								this.load();
							},
							error: () => frappe.msgprint({
								title: __("This person was not added"),
								message: __("Nothing changed. Check the details and try again."),
							}),
						});
					},
				});
				dialog.show();
			},
			error: () => frappe.msgprint({
				title: __("JiJi could not load the staff list"),
				message: __("Nothing changed. Try again."),
			}),
		});
	}

	setStaffActive(person, active) {
		const save = () => frappe.call({
			method: "kopos_connector.api.staff_desk.set_staff_active",
			type: "POST",
			args: { access: person.access, active: active ? 1 : 0 },
			callback: () => {
				frappe.show_alert({
					message: active ? __("Tablet access restored") : __("Tablet access removed"),
					indicator: "green",
				});
				this.load();
			},
			error: () => frappe.msgprint({
				title: __("Access was not changed"),
				message: __("Nothing changed. Try again."),
			}),
		});
		if (active) return save();
		frappe.confirm(
			__("Remove tablet access for {0}? They will not be able to sign in.", [person.name || __("this person")]),
			save,
		);
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

	escape(value) {
		return String(value ?? "").replace(/[&<>"']/g, (character) => ({
			"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
		})[character]);
	}
}
