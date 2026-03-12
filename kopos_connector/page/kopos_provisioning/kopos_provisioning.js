frappe.pages["kopos_provisioning"].on_page_load = function (wrapper) {
	new KoPOSProvisioningPage(wrapper);
};

class KoPOSProvisioningPage {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("KoPOS Provisioning"),
			single_column: true,
		});

		frappe.breadcrumbs.add("Setup");
		this.page.set_primary_action(__("Generate QR"), () => this.generate());
		this.render();
		this.bind_events();
	}

	render() {
		$(this.page.body).html(`
			<div class="kopos-provisioning-page">
				<div class="kopos-provisioning-card kopos-provisioning-intro">
					<h3>${__("Generate setup QR for a POS device")}</h3>
					<p>${__("Create a short-lived QR that opens KoPOS and auto-configures the assigned device, linked POS profile, printers, users, credentials, catalog, and promotions. Use the Device ERP URL that the tablet can actually reach.")}</p>
				</div>
				<div class="kopos-provisioning-grid">
					<div class="kopos-provisioning-card">
						<div class="kopos-field" data-field="erpnext_url"></div>
						<div class="kopos-field" data-field="device"></div>
						<div class="kopos-field" data-field="pos_profile"></div>
						<div class="kopos-field-row">
							<div class="kopos-field" data-field="device_name"></div>
							<div class="kopos-field" data-field="device_prefix"></div>
						</div>
						<div class="kopos-field-row">
							<div class="kopos-field" data-field="company"></div>
							<div class="kopos-field" data-field="warehouse"></div>
						</div>
						<div class="kopos-field-row">
							<div class="kopos-field" data-field="currency"></div>
							<div class="kopos-field" data-field="expires_in_seconds"></div>
						</div>
						<div class="kopos-field" data-field="api_key"></div>
						<div class="kopos-field" data-field="api_secret"></div>
						<div class="kopos-provisioning-actions">
							<button class="btn btn-primary kopos-generate">${__("Generate QR")}</button>
							<button class="btn btn-default kopos-copy-link" style="display:none">${__("Copy Link")}</button>
						</div>
						<p class="text-muted small kopos-status">${__("Enter the ERP token credentials used by this POS, then generate a one-time QR.")}</p>
					</div>
					<div class="kopos-provisioning-card kopos-provisioning-preview">
						<div class="kopos-preview-empty">
							<div class="kopos-preview-icon"><i class="fa fa-qrcode"></i></div>
							<h4>${__("QR preview will appear here")}</h4>
							<p>${__("Scan the generated QR from the tablet camera. The link is one-time and expires automatically.")}</p>
						</div>
						<div class="kopos-preview-filled" style="display:none">
							<img class="kopos-qr-image" alt="${__("KoPOS provisioning QR")}" />
							<div class="kopos-meta"></div>
							<div class="kopos-link"></div>
						</div>
					</div>
				</div>
			</div>
		`);

		this.add_styles();
		this.make_fields();
		this.apply_route_options();
	}

	add_styles() {
		if (document.getElementById("kopos-provisioning-styles")) return;
		const style = document.createElement("style");
		style.id = "kopos-provisioning-styles";
		style.textContent = `
			.kopos-provisioning-page { display:flex; flex-direction:column; gap:16px; padding:16px 0 24px; }
			.kopos-provisioning-grid { display:grid; grid-template-columns:minmax(380px, 1fr) minmax(320px, 420px); gap:16px; align-items:start; }
			.kopos-provisioning-card { background: var(--card-bg); border:1px solid var(--border-color); border-radius:16px; padding:20px; box-shadow: var(--shadow-sm); }
			.kopos-provisioning-intro h3 { margin:0 0 8px; }
			.kopos-provisioning-intro p { margin:0; color: var(--text-muted); }
			.kopos-field-row { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
			.kopos-field { margin-bottom:12px; }
			.kopos-provisioning-actions { display:flex; gap:8px; margin-top:8px; }
			.kopos-status { margin-top:12px; }
			.kopos-provisioning-preview { position:sticky; top:24px; min-height:520px; display:flex; align-items:center; justify-content:center; }
			.kopos-preview-empty, .kopos-preview-filled { width:100%; text-align:center; }
			.kopos-preview-icon { width:64px; height:64px; margin:0 auto 12px; border-radius:18px; display:flex; align-items:center; justify-content:center; background:rgba(245,158,11,0.12); color:#f59e0b; font-size:28px; }
			.kopos-qr-image { width:280px; height:280px; max-width:100%; border-radius:16px; border:1px solid var(--border-color); background:#fff; padding:12px; }
			.kopos-meta { margin-top:16px; color: var(--text-muted); line-height:1.6; }
			.kopos-link { margin-top:12px; word-break:break-all; font-size:12px; color: var(--text-muted); }
			.kopos-link code { white-space:pre-wrap; }
			@media (max-width: 991px) { .kopos-provisioning-grid { grid-template-columns:1fr; } .kopos-provisioning-preview { position:static; min-height:auto; } }
		`;
		document.head.appendChild(style);
	}

	make_fields() {
		this.fields = {};
		const defs = [
			{ fieldname: "erpnext_url", label: __("Device ERP URL"), fieldtype: "Data", reqd: 1, default: window.location.origin },
			{ fieldname: "device", label: __("KoPOS Device"), fieldtype: "Link", options: "KoPOS Device", reqd: 1 },
			{ fieldname: "pos_profile", label: __("POS Profile"), fieldtype: "Link", options: "POS Profile", read_only: 1 },
			{ fieldname: "device_name", label: __("Device Name"), fieldtype: "Data" },
			{ fieldname: "device_prefix", label: __("Device Prefix"), fieldtype: "Data" },
			{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
			{ fieldname: "warehouse", label: __("Warehouse"), fieldtype: "Link", options: "Warehouse" },
			{ fieldname: "currency", label: __("Currency"), fieldtype: "Link", options: "Currency" },
			{ fieldname: "expires_in_seconds", label: __("Expires In (seconds)"), fieldtype: "Int", default: 900 },
			{ fieldname: "api_key", label: __("API Key"), fieldtype: "Data", reqd: 1 },
			{ fieldname: "api_secret", label: __("API Secret"), fieldtype: "Password", reqd: 1 },
		];

		defs.forEach((df) => {
			const parent = this.page.body.find(`[data-field="${df.fieldname}"]`).get(0);
			const field = frappe.ui.form.make_control({
				parent,
				df,
				render_input: true,
			});
			field.refresh();
			if (df.default !== undefined) {
				field.set_value(df.default);
			}
			this.fields[df.fieldname] = field;
		});

		this.fields.device.$input.on("change", () => this.load_device_defaults());
	}

	bind_events() {
		this.page.body.on("click", ".kopos-generate", () => this.generate());
		this.page.body.on("click", ".kopos-copy-link", () => this.copy_link());
	}

	apply_route_options() {
		const opts = frappe.route_options || {};
		["erpnext_url", "device", "pos_profile", "company", "warehouse", "currency", "device_name", "device_prefix"].forEach((key) => {
			if (opts[key] && this.fields[key]) {
				this.fields[key].set_value(opts[key]);
			}
		});
		frappe.route_options = null;
		if (this.fields.device.get_value()) {
			void this.load_device_defaults();
		}
	}

	async load_device_defaults() {
		const deviceName = this.fields.device.get_value();
		if (!deviceName) return;

		try {
			const deviceDoc = await frappe.db.get_doc("KoPOS Device", deviceName);
			if (deviceDoc.pos_profile) this.fields.pos_profile.set_value(deviceDoc.pos_profile);
			if (!this.fields.device_name.get_value() && deviceDoc.device_name) this.fields.device_name.set_value(deviceDoc.device_name);
			if (!this.fields.device_prefix.get_value() && deviceDoc.device_prefix) this.fields.device_prefix.set_value(deviceDoc.device_prefix);

			if (deviceDoc.pos_profile) {
				const profileDoc = await frappe.db.get_doc("POS Profile", deviceDoc.pos_profile);
				if (!this.fields.company.get_value() && profileDoc.company) this.fields.company.set_value(profileDoc.company);
				if (!this.fields.warehouse.get_value() && profileDoc.warehouse) this.fields.warehouse.set_value(profileDoc.warehouse);
				if (!this.fields.currency.get_value() && profileDoc.currency) this.fields.currency.set_value(profileDoc.currency);
			}
		} catch (error) {
			frappe.show_alert({ message: __("Could not load device defaults"), indicator: "orange" });
		}
	}

	get_values() {
		const values = {};
		Object.keys(this.fields).forEach((key) => {
			values[key] = this.fields[key].get_value();
		});
		return values;
	}

	async generate() {
		const values = this.get_values();
		if (!values.device || !values.api_key || !values.api_secret) {
			frappe.msgprint({ title: __("Missing fields"), message: __("KoPOS Device, API Key, and API Secret are required."), indicator: "red" });
			return;
		}

		this.page.set_indicator(__("Generating"), "orange");
		this.page.body.find(".kopos-generate").prop("disabled", true);
		this.page.body.find(".kopos-status").text(__("Creating one-time provisioning token..."));

		try {
			const response = await frappe.call({
				method: "kopos.api.create_pos_provisioning",
				args: values,
			});
			const payload = response.message || response;
			this.current_link = payload.provisioning_link;
			this.render_preview(payload);
			this.page.set_indicator(__("Ready"), "green");
			this.page.body.find(".kopos-status").text(__("QR generated. Scan it once from the POS device before it expires."));
			frappe.show_alert({ message: __("Provisioning QR ready"), indicator: "green" });
		} catch (error) {
			const message = error?.message || __("Failed to generate provisioning QR");
			this.page.set_indicator(__("Failed"), "red");
			this.page.body.find(".kopos-status").text(message);
			frappe.msgprint({ title: __("Provisioning failed"), message, indicator: "red" });
		} finally {
			this.page.body.find(".kopos-generate").prop("disabled", false);
		}
	}

	render_preview(payload) {
		const preview = payload.setup_preview || {};
		this.page.body.find(".kopos-preview-empty").hide();
		this.page.body.find(".kopos-preview-filled").show();
		this.page.body.find(".kopos-copy-link").show();
		this.page.body.find(".kopos-qr-image").attr("src", `data:image/svg+xml;base64,${payload.provisioning_qr_svg}`);
		this.page.body.find(".kopos-meta").html(`
			<div><strong>${__("Device")}:</strong> ${this.escape_html(preview.device || "-")}</div>
			<div><strong>${__("POS Profile")}:</strong> ${this.escape_html(preview.pos_profile || "-")}</div>
			<div><strong>${__("Company")}:</strong> ${this.escape_html(preview.company || "-")}</div>
			<div><strong>${__("Warehouse")}:</strong> ${this.escape_html(preview.warehouse || "-")}</div>
			<div><strong>${__("Expires At")}:</strong> ${frappe.datetime.str_to_user(payload.expires_at)}</div>
		`);
		this.page.body.find(".kopos-link").html(`<code>${this.escape_html(payload.provisioning_link)}</code>`);
	}

	escape_html(value) {
		return String(value)
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;")
			.replace(/\"/g, "&quot;")
			.replace(/'/g, "&#39;");
	}

	async copy_link() {
		if (!this.current_link) return;
		try {
			await navigator.clipboard.writeText(this.current_link);
			frappe.show_alert({ message: __("Provisioning link copied"), indicator: "green" });
		} catch (error) {
			frappe.msgprint(this.current_link);
		}
	}
}
