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
						<p>${__("Create a short-lived QR that opens KoPOS and auto-configures the assigned device, linked POS profile, printers, users, credentials, catalog, and promotions. Use the Device ERP URL that the tablet can actually reach. The one-time setup link is hidden after creation; scan the QR or copy it only while this page is open.")}</p>
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
						<div class="kopos-provisioning-actions">
							<button class="btn btn-primary kopos-generate">${__("Generate QR")}</button>
							<button class="btn btn-default kopos-copy-link" style="display:none">${__("Copy Link")}</button>
						</div>
						<p class="text-muted small kopos-status">${__("Generate a one-time QR using a dedicated per-device API identity.")}</p>
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
				<div class="kopos-provisioning-card kopos-recovery-card">
					<h3>${__("Safe reset and credential recovery")}</h3>
					<p class="text-muted">${__("Use the support code shown by the tablet after its emergency export is complete and every local sync queue is drained. The raw reset proof and replacement credentials never appear in this code.")}</p>
					<div class="kopos-field" data-recovery-field="recovery_code"></div>
					<div class="kopos-field-row">
						<div class="kopos-field" data-recovery-field="recovery_confirmation"></div>
						<div class="kopos-field" data-recovery-field="allow_stale_export"></div>
					</div>
					<div class="kopos-field" data-recovery-field="stale_export_override_reason"></div>
					<div class="kopos-field" data-recovery-field="migration_recovery_confirmation"></div>
					<div class="kopos-field" data-recovery-field="migration_recovery_acknowledgement_reason"></div>
					<div class="kopos-provisioning-actions">
						<button class="btn btn-danger kopos-recovery-generate">${__("Register and Generate Approval QR")}</button>
					</div>
					<p class="text-muted small kopos-recovery-status">${__("A System Manager must type the exact RECOVER confirmation. Approval does not restore the API user, rotate credentials, or change config; that happens only after the tablet rehashes its retained archive and redeems the challenge.")}</p>
				</div>
				<div class="kopos-provisioning-card kopos-authorization-card">
					<h3>${__("Authorize an existing safe reset")}</h3>
					<p class="text-muted">${__("Use this for normal device-authenticated requests and QR reissues. Load the immutable ERP record before authorization; recovery evidence, archive hash, and any prior acknowledgement are shown below.")}</p>
					<div class="kopos-field" data-authorization-field="reset_id"></div>
					<div class="kopos-provisioning-actions">
						<button class="btn btn-default kopos-authorization-load">${__("Load Safe Reset")}</button>
					</div>
					<div class="kopos-authorization-evidence" style="display:none"></div>
					<div class="kopos-field" data-authorization-field="migration_recovery_confirmation"></div>
					<div class="kopos-field" data-authorization-field="migration_recovery_acknowledgement_reason"></div>
					<div class="kopos-provisioning-actions">
						<button class="btn btn-danger kopos-authorization-generate">${__("Generate or Reissue Approval QR")}</button>
					</div>
					<p class="text-muted small kopos-authorization-status">${__("Load a safe reset ID to verify its immutable evidence before issuing a QR.")}</p>
					<div class="kopos-cancellation-panel">
						<h4>${__("Abandon before credential rotation")}</h4>
						<p class="text-muted small">${__("Use this when a requested or authorized reset will not be completed, including credential recovery where the old tablet API credential is unavailable. This never rotates credentials or changes the device configuration.")}</p>
						<div class="kopos-field" data-authorization-field="cancellation_confirmation"></div>
						<div class="kopos-field" data-authorization-field="cancellation_reason"></div>
						<div class="kopos-provisioning-actions">
							<button class="btn btn-danger kopos-authorization-cancel">${__("Cancel Safe Reset")}</button>
						</div>
						<p class="text-muted small kopos-cancellation-status">${__("Load the record, then type the exact cancellation confirmation and a reason.")}</p>
					</div>
				</div>
			</div>
		`);

		this.add_styles();
		this.make_fields();
		this.make_recovery_fields();
		this.make_authorization_fields();
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
			.kopos-recovery-card { border-color:rgba(220,53,69,0.35); }
			.kopos-authorization-card { border-color:rgba(220,53,69,0.35); }
			.kopos-recovery-card h3, .kopos-authorization-card h3 { margin:0 0 8px; }
			.kopos-recovery-card > p, .kopos-authorization-card > p { margin-bottom:16px; }
			.kopos-authorization-evidence { margin:16px 0; padding:14px; border:1px solid var(--border-color); border-radius:10px; background:var(--subtle-fg); line-height:1.6; overflow-wrap:anywhere; }
			.kopos-cancellation-panel { margin-top:20px; padding-top:18px; border-top:1px solid var(--border-color); }
			.kopos-cancellation-panel h4 { margin:0 0 8px; }
			.kopos-preview-empty, .kopos-preview-filled { width:100%; text-align:center; }
			.kopos-preview-icon { width:64px; height:64px; margin:0 auto 12px; border-radius:18px; display:flex; align-items:center; justify-content:center; background:rgba(245,158,11,0.12); color:#f59e0b; font-size:28px; }
			.kopos-qr-image { width:280px; height:280px; max-width:100%; border-radius:16px; border:1px solid var(--border-color); background:#fff; padding:12px; }
			.kopos-meta { margin-top:16px; color: var(--text-muted); line-height:1.6; }
			.kopos-link { margin-top:12px; font-size:12px; color: var(--text-muted); }
			@media (max-width: 991px) { .kopos-provisioning-grid { grid-template-columns:1fr; } .kopos-provisioning-preview { position:static; min-height:auto; } }
		`;
		document.head.appendChild(style);
	}

	make_recovery_fields() {
		this.recovery_fields = {};
		const defs = [
			{ fieldname: "recovery_code", label: __("Tablet Credential Recovery Code"), fieldtype: "Long Text", reqd: 1 },
			{ fieldname: "recovery_confirmation", label: __("Type RECOVER + Device ID"), fieldtype: "Data", reqd: 1 },
			{ fieldname: "allow_stale_export", label: __("Override 30-minute export window"), fieldtype: "Check", default: 0 },
			{ fieldname: "stale_export_override_reason", label: __("Stale Export Override Reason (20-500 characters)"), fieldtype: "Small Text" },
			{ fieldname: "migration_recovery_confirmation", label: __("Migration Recovery ACK (required only when recovery points exist)"), fieldtype: "Data" },
			{ fieldname: "migration_recovery_acknowledgement_reason", label: __("Migration Recovery Reconciliation Reason (20-500 characters)"), fieldtype: "Small Text" },
		];

		defs.forEach((df) => {
			const parent = this.page.body.find(`[data-recovery-field="${df.fieldname}"]`).get(0);
			const field = frappe.ui.form.make_control({ parent, df, render_input: true });
			field.refresh();
			if (df.default !== undefined) field.set_value(df.default);
			this.recovery_fields[df.fieldname] = field;
		});
	}

	make_authorization_fields() {
		this.authorization_fields = {};
		const defs = [
			{ fieldname: "reset_id", label: __("Safe Reset ID"), fieldtype: "Data", reqd: 1 },
			{ fieldname: "migration_recovery_confirmation", label: __("Exact Migration Recovery ACK"), fieldtype: "Data" },
			{ fieldname: "migration_recovery_acknowledgement_reason", label: __("Reconciliation Reason (20-500 characters)"), fieldtype: "Small Text" },
			{ fieldname: "cancellation_confirmation", label: __("Type CANCEL SAFE RESET + Safe Reset ID"), fieldtype: "Data" },
			{ fieldname: "cancellation_reason", label: __("Cancellation Reason (1-500 characters)"), fieldtype: "Small Text" },
		];
		defs.forEach((df) => {
			const parent = this.page.body.find(`[data-authorization-field="${df.fieldname}"]`).get(0);
			const field = frappe.ui.form.make_control({ parent, df, render_input: true });
			field.refresh();
			this.authorization_fields[df.fieldname] = field;
		});
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
		this.page.body.on("click", ".kopos-recovery-generate", () => this.register_recovery());
		this.page.body.on("click", ".kopos-authorization-load", () => this.load_safe_reset());
		this.page.body.on("click", ".kopos-authorization-generate", () => this.authorize_safe_reset());
		this.page.body.on("click", ".kopos-authorization-cancel", () => this.cancel_safe_reset_as_manager());
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
		if (opts.reset_id && this.authorization_fields.reset_id) {
			this.authorization_fields.reset_id.set_value(opts.reset_id);
			void this.load_safe_reset();
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
		if (!values.device) {
			frappe.msgprint({ title: __("Missing fields"), message: __("KoPOS Device is required."), indicator: "red" });
			return;
		}

		this.page.set_indicator(__("Generating"), "orange");
		this.page.body.find(".kopos-generate").prop("disabled", true);
		this.page.body.find(".kopos-status").text(__("Creating one-time provisioning token..."));

		try {
			const response = await frappe.call({
				method: "kopos_connector.api.create_device_provisioning_qr",
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

	validate_migration_recovery_evidence(payload, sourceLabel = __("Migration recovery evidence")) {
		const countKeys = [
			"migration_recovery_point_count",
			"migration_recovery_valid_point_count",
			"migration_recovery_invalid_point_count",
			"migration_recovery_captured_pending_total",
		];
		if (countKeys.some((key) => !Number.isSafeInteger(payload[key]) || payload[key] < 0 || payload[key] > 2147483647)) {
			throw new Error(__("{0} contains an invalid or out-of-range count.", [sourceLabel]));
		}
		if (typeof payload.migration_recovery_review_required !== "boolean") {
			throw new Error(__("{0} review requirement must be a boolean.", [sourceLabel]));
		}
		if (payload.migration_recovery_valid_point_count + payload.migration_recovery_invalid_point_count !== payload.migration_recovery_point_count) {
			throw new Error(__("{0} valid and invalid point counts do not equal its total.", [sourceLabel]));
		}
		if (payload.migration_recovery_review_required !== (payload.migration_recovery_point_count > 0)) {
			throw new Error(__("{0} review requirement does not match whether recovery points exist.", [sourceLabel]));
		}
		if (payload.migration_recovery_point_count === 0 && payload.migration_recovery_captured_pending_total !== 0) {
			throw new Error(__("{0} captured pending total must be zero when no recovery points exist.", [sourceLabel]));
		}
		return countKeys.reduce((evidence, key) => {
			evidence[key] = payload[key];
			return evidence;
		}, { migration_recovery_review_required: payload.migration_recovery_review_required });
	}

	parse_recovery_code(value) {
		const prefix = "KOPOS-ERP-CREDENTIAL-RECOVERY-V2.";
		const rawValue = String(value || "");
		if (rawValue.length > 8192) {
			throw new Error(__("Recovery code exceeds the 8 KiB safety limit."));
		}
		const encoded = rawValue.trim();
		if (!encoded.startsWith(prefix)) {
			throw new Error(__("Recovery code must use the KOPOS-ERP-CREDENTIAL-RECOVERY-V2 format."));
		}
		const body = encoded.slice(prefix.length);
		if (!body || !/^[A-Za-z0-9_-]+$/.test(body)) {
			throw new Error(__("Recovery code payload is not valid unpadded base64url."));
		}

		let decoded;
		try {
			const padding = "=".repeat((4 - (body.length % 4)) % 4);
			const binary = window.atob(body.replace(/-/g, "+").replace(/_/g, "/") + padding);
			const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
			decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
		} catch (error) {
			throw new Error(__("Recovery code payload is not valid UTF-8."));
		}

		let payload;
		try {
			payload = JSON.parse(decoded);
		} catch (error) {
			throw new Error(__("Recovery code payload is not valid JSON."));
		}
		if (!payload || Array.isArray(payload) || typeof payload !== "object" || JSON.stringify(payload) !== decoded) {
			throw new Error(__("Recovery code JSON must be canonical and contain no duplicate or reordered fields."));
		}

		const exactKeys = [
			"safe_reset_protocol_version", "confirmation", "request_id", "device_id", "reason", "erp_base_url",
			"company", "currency", "pos_profile", "warehouse", "export_sha256",
			"export_content_sha256", "export_byte_length", "exported_at", "drained_row_count", "queue_evidence",
			"migration_recovery_point_count", "migration_recovery_valid_point_count",
			"migration_recovery_invalid_point_count", "migration_recovery_captured_pending_total",
			"migration_recovery_review_required",
			"previous_config_version", "reset_proof_sha256",
		];
		if (JSON.stringify(Object.keys(payload)) !== JSON.stringify(exactKeys)) {
			throw new Error(__("Recovery code contains missing, unknown, or reordered fields."));
		}
		const queueKeys = ["pending_count", "failed_count", "syncing_count", "dead_letter_count"];
		if (!payload.queue_evidence || Array.isArray(payload.queue_evidence) || JSON.stringify(Object.keys(payload.queue_evidence)) !== JSON.stringify(queueKeys)) {
			throw new Error(__("Recovery queue evidence is incomplete or contains unknown fields."));
		}

		const stringKeys = [
			"confirmation", "request_id", "device_id", "reason", "erp_base_url", "company",
			"currency", "pos_profile", "warehouse", "export_sha256", "export_content_sha256",
			"exported_at", "reset_proof_sha256",
		];
		if (stringKeys.some((key) => typeof payload[key] !== "string" || !payload[key])) {
			throw new Error(__("Recovery code contains an empty or invalid text field."));
		}
		if (!/^[0-9a-f]{64}$/.test(payload.export_sha256) || !/^[0-9a-f]{64}$/.test(payload.export_content_sha256) || !/^[0-9a-f]{64}$/.test(payload.reset_proof_sha256)) {
			throw new Error(__("Recovery code SHA-256 evidence is invalid."));
		}
		if (payload.safe_reset_protocol_version !== 2) {
			throw new Error(__("Recovery code must use safe reset protocol version 2."));
		}
		if (!Number.isSafeInteger(payload.export_byte_length) || payload.export_byte_length <= 0 || payload.export_byte_length > 8657043456) {
			throw new Error(__("Recovery archive byte length is invalid or outside the supported range."));
		}
		if (!Number.isInteger(payload.drained_row_count) || payload.drained_row_count < 0 || !Number.isInteger(payload.previous_config_version) || payload.previous_config_version <= 0) {
			throw new Error(__("Recovery row count or configuration version is invalid."));
		}
		if (queueKeys.some((key) => !Number.isInteger(payload.queue_evidence[key]) || payload.queue_evidence[key] !== 0)) {
			throw new Error(__("Recovery requires pending, failed, syncing, and dead-letter queue counts to all be zero."));
		}
		this.validate_migration_recovery_evidence(payload, __("Recovery code migration evidence"));
		if (!/(Z|[+-]\d{2}:\d{2})$/.test(payload.exported_at) || Number.isNaN(Date.parse(payload.exported_at))) {
			throw new Error(__("Recovery export timestamp must be a timezone-aware ISO datetime."));
		}
		const expectedConfirmation = `RECOVER ${payload.device_id}`;
		if (payload.confirmation !== expectedConfirmation) {
			throw new Error(__("Recovery code confirmation does not match its device ID."));
		}
		return payload;
	}

	async register_recovery() {
		const button = this.page.body.find(".kopos-recovery-generate");
		button.prop("disabled", true);
		this.page.body.find(".kopos-recovery-status").text(__("Validating tablet recovery evidence..."));
		try {
			const payload = this.parse_recovery_code(this.recovery_fields.recovery_code.get_value());
			const typedConfirmation = String(this.recovery_fields.recovery_confirmation.get_value() || "");
			const expectedConfirmation = `RECOVER ${payload.device_id}`;
			if (typedConfirmation !== expectedConfirmation) {
				throw new Error(__("Type {0} exactly to continue.", [expectedConfirmation]));
			}
			payload.confirmation = typedConfirmation;
			const allowStaleValue = this.recovery_fields.allow_stale_export.get_value();
			const allowStale = allowStaleValue === true || allowStaleValue === 1 || allowStaleValue === "1";
			const staleReason = String(this.recovery_fields.stale_export_override_reason.get_value() || "").trim();
			if (allowStale) {
				if (staleReason.length < 20 || staleReason.length > 500) {
					throw new Error(__("A stale export override requires a 20-500 character justification."));
				}
				payload.allow_stale_export = true;
				payload.stale_export_override_reason = staleReason;
			} else if (staleReason) {
				throw new Error(__("Enable the stale export override before entering a justification."));
			}

			this.page.body.find(".kopos-recovery-status").text(__("Registering immutable recovery evidence..."));
			const registrationResponse = await frappe.call({
				method: "kopos_connector.api.register_device_credential_recovery",
				args: payload,
			});
			const registration = registrationResponse.message || registrationResponse;
			if (!registration.reset_id) {
				throw new Error(registration.message || __("ERP did not return a safe reset ID."));
			}
			const submittedRecoveryEvidence = this.validate_migration_recovery_evidence(payload, __("Submitted migration evidence"));
			const registeredRecoveryEvidence = this.validate_migration_recovery_evidence(registration, __("ERP migration evidence"));
			if (Object.keys(submittedRecoveryEvidence).some((key) => submittedRecoveryEvidence[key] !== registeredRecoveryEvidence[key])) {
				throw new Error(__("ERP did not acknowledge the exact migration recovery evidence from the tablet."));
			}
			if (registration.safe_reset_protocol_version !== 2 || registration.export_byte_length !== payload.export_byte_length || registration.export_sha256 !== payload.export_sha256 || registration.export_content_sha256 !== payload.export_content_sha256) {
				throw new Error(__("ERP did not acknowledge the exact protocol and retained-archive evidence from the tablet."));
			}

			const authorizationArgs = { reset_id: registration.reset_id };
			const recoveryAck = String(this.recovery_fields.migration_recovery_confirmation.get_value() || "");
			const recoveryReasonRaw = String(this.recovery_fields.migration_recovery_acknowledgement_reason.get_value() || "");
			if (registration.migration_recovery_review_required && registration.lifecycle_status === "requested") {
				const expectedAck = ["ACK RECOVERY", registration.reset_id, registration.export_sha256].join(" ");
				if (recoveryAck !== expectedAck) {
					throw new Error(__("Type {0} exactly to acknowledge the archived migration recovery points.", [expectedAck]));
				}
				const recoveryReason = recoveryReasonRaw.trim();
				if (recoveryReasonRaw !== recoveryReason || recoveryReason.length < 20 || recoveryReason.length > 500) {
					throw new Error(__("Migration recovery reconciliation reason must contain 20-500 characters without leading or trailing spaces."));
				}
				authorizationArgs.migration_recovery_confirmation = recoveryAck;
				authorizationArgs.migration_recovery_acknowledgement_reason = recoveryReason;
			} else if (!registration.migration_recovery_review_required && (recoveryAck || recoveryReasonRaw)) {
				throw new Error(__("Clear migration recovery acknowledgement fields because this archive contains no recovery points."));
			} else if (registration.lifecycle_status !== "requested" && (recoveryAck || recoveryReasonRaw)) {
				throw new Error(__("This recovery acknowledgement is already immutable; clear the ACK and reason fields to reissue the QR."));
			}

			this.page.body.find(".kopos-recovery-status").text(__("Verifying ERP business state and issuing an approval-only challenge QR..."));
			const authorizationResponse = await frappe.call({
				method: "kopos_connector.api.authorize_device_safe_reset",
				args: authorizationArgs,
			});
			const authorization = authorizationResponse.message || authorizationResponse;
			if (!authorization.approval_qr_svg || !authorization.approval_link || authorization.provisioning_mode !== "safe_reset_approval" || authorization.safe_reset_protocol_version !== 2) {
				throw new Error(authorization.message || __("ERP did not issue a safe-reset approval QR."));
			}
			this.current_link = authorization.approval_link;
			this.render_approval_preview(authorization);
			this.page.set_indicator(__("Approval QR Ready"), "green");
			this.page.body.find(".kopos-recovery-status").text(__("Approval QR issued without changing credentials. The trusted tablet app must now rehash the retained archive and redeem the challenge before ERP rotates anything."));
			frappe.show_alert({ message: __("Safe-reset approval QR ready"), indicator: "green" });
		} catch (error) {
			const message = error?.message || __("Failed to register credential recovery");
			this.page.set_indicator(__("Recovery Failed"), "red");
			this.page.body.find(".kopos-recovery-status").text(message);
			frappe.msgprint({ title: __("Credential recovery failed"), message: this.escape_html(message), indicator: "red" });
		} finally {
			button.prop("disabled", false);
		}
	}

	async load_safe_reset() {
		const resetId = String(this.authorization_fields.reset_id.get_value() || "").trim();
		if (!/^[A-Za-z0-9._:-]{8,128}$/.test(resetId)) {
			frappe.msgprint({ title: __("Safe reset ID required"), message: __("Enter a valid safe reset ID."), indicator: "red" });
			return null;
		}
		this.page.body.find(".kopos-authorization-load").prop("disabled", true);
		this.page.body.find(".kopos-authorization-status").text(__("Loading immutable safe-reset evidence from ERP..."));
		try {
			const doc = await frappe.db.get_doc("KoPOS Device Safe Reset", resetId);
			if (String(doc.reset_id || doc.name || "") !== resetId) {
				throw new Error(__("ERP returned a different safe reset record."));
			}
			const evidence = this.validate_migration_recovery_evidence({
				migration_recovery_point_count: Number(doc.migration_recovery_point_count),
				migration_recovery_valid_point_count: Number(doc.migration_recovery_valid_point_count),
				migration_recovery_invalid_point_count: Number(doc.migration_recovery_invalid_point_count),
				migration_recovery_captured_pending_total: Number(doc.migration_recovery_captured_pending_total),
				migration_recovery_review_required: doc.migration_recovery_review_required === true || doc.migration_recovery_review_required === 1 || doc.migration_recovery_review_required === "1",
			}, __("ERP safe-reset migration evidence"));
			const protocolVersion = Number(doc.safe_reset_protocol_version);
			const archiveByteLength = Number(doc.export_byte_length);
			if (protocolVersion !== 2) {
				throw new Error(__("ERP safe reset is not protocol version 2."));
			}
			if (!/^[0-9a-f]{64}$/.test(String(doc.export_sha256 || "")) || !/^[0-9a-f]{64}$/.test(String(doc.export_content_sha256 || ""))) {
				throw new Error(__("ERP safe reset has invalid archive hash evidence."));
			}
			if (!Number.isSafeInteger(archiveByteLength) || archiveByteLength <= 0 || archiveByteLength > 8657043456) {
				throw new Error(__("ERP safe reset has invalid archive byte-length evidence."));
			}
			this.authorization_reset = { ...doc, ...evidence, safe_reset_protocol_version: protocolVersion, export_byte_length: archiveByteLength };
			this.authorization_fields.migration_recovery_confirmation.set_value("");
			this.authorization_fields.migration_recovery_acknowledgement_reason.set_value("");
			this.authorization_fields.cancellation_confirmation.set_value("");
			this.authorization_fields.cancellation_reason.set_value("");
			const hasAcknowledgement = Boolean(String(doc.migration_recovery_ack_fingerprint || "").trim());
			const expectedAck = ["ACK RECOVERY", resetId, doc.export_sha256].join(" ");
			this.page.body.find(".kopos-authorization-evidence").html(
				"<div><strong>" + __("Status") + ":</strong> " + this.escape_html(doc.status || "-") + "</div>" +
				"<div><strong>" + __("Request Origin") + ":</strong> " + this.escape_html(doc.request_origin || "-") + "</div>" +
				"<div><strong>" + __("Protocol") + ":</strong> v" + this.escape_html(protocolVersion) + "</div>" +
				"<div><strong>" + __("Archive SHA-256") + ":</strong> " + this.escape_html(doc.export_sha256) + "</div>" +
				"<div><strong>" + __("Archive Content SHA-256") + ":</strong> " + this.escape_html(doc.export_content_sha256) + "</div>" +
				"<div><strong>" + __("Archive Bytes") + ":</strong> " + this.escape_html(archiveByteLength) + "</div>" +
				"<div><strong>" + __("Recovery Points") + ":</strong> " + this.escape_html(evidence.migration_recovery_point_count) +
				" (" + this.escape_html(evidence.migration_recovery_valid_point_count) + " " + __("valid") + ", " +
				this.escape_html(evidence.migration_recovery_invalid_point_count) + " " + __("invalid") + ")</div>" +
				"<div><strong>" + __("Captured Pending Work Aggregate") + ":</strong> " + this.escape_html(evidence.migration_recovery_captured_pending_total) + "</div>" +
				"<div><strong>" + __("Review") + ":</strong> " + this.escape_html(
					evidence.migration_recovery_review_required
						? (hasAcknowledgement ? __("Acknowledged") : __("Required"))
						: __("Not required")
				) + "</div>" +
				(evidence.migration_recovery_review_required && !hasAcknowledgement
					? "<div><strong>" + __("Exact acknowledgement") + ":</strong> " + this.escape_html(expectedAck) + "</div>"
					: "") +
				(["requested", "authorized"].includes(String(doc.status || "").toLowerCase())
					? "<div><strong>" + __("Exact cancellation") + ":</strong> " + this.escape_html(`CANCEL SAFE RESET ${resetId}`) + "</div>"
					: "")
			).show();
			this.page.body.find(".kopos-authorization-status").text(
				evidence.migration_recovery_review_required && !hasAcknowledgement
					? __("Review the archive, then type the exact ACK and a reconciliation reason before authorization.")
					: __("Immutable evidence loaded. Generating approval will not restore users, rotate credentials, or increment config.")
			);
			return this.authorization_reset;
		} catch (error) {
			this.authorization_reset = null;
			this.page.body.find(".kopos-authorization-evidence").hide().empty();
			const message = error?.message || __("Failed to load safe reset evidence");
			this.page.body.find(".kopos-authorization-status").text(message);
			frappe.msgprint({ title: __("Safe reset load failed"), message: this.escape_html(message), indicator: "red" });
			return null;
		} finally {
			this.page.body.find(".kopos-authorization-load").prop("disabled", false);
		}
	}

	get_cancellation_idempotency_key(resetId) {
		const storageKey = `kopos-safe-reset-cancel:${resetId}`;
		const existing = window.sessionStorage.getItem(storageKey);
		if (/^[A-Za-z0-9_-]{43}$/.test(String(existing || ""))) {
			return existing;
		}
		if (!window.crypto || typeof window.crypto.getRandomValues !== "function") {
			throw new Error(__("Secure browser randomness is unavailable; cancellation was not sent."));
		}
		const bytes = new Uint8Array(32);
		window.crypto.getRandomValues(bytes);
		const encoded = window.btoa(String.fromCharCode(...bytes))
			.replace(/\+/g, "-")
			.replace(/\//g, "_")
			.replace(/=+$/g, "");
		if (!/^[A-Za-z0-9_-]{43}$/.test(encoded)) {
			throw new Error(__("Could not generate a valid cancellation retry key."));
		}
		window.sessionStorage.setItem(storageKey, encoded);
		return encoded;
	}

	async cancel_safe_reset_as_manager() {
		const resetId = String(this.authorization_fields.reset_id.get_value() || "").trim();
		let resetDoc = this.authorization_reset;
		if (!resetDoc || String(resetDoc.reset_id || resetDoc.name || "") !== resetId) {
			resetDoc = await this.load_safe_reset();
		}
		if (!resetDoc) return;

		const lifecycleStatus = String(resetDoc.status || "").toLowerCase();
		if (lifecycleStatus === "cancelled") {
			frappe.msgprint({ title: __("Already cancelled"), message: __("This safe reset is already terminal and cannot rotate credentials."), indicator: "blue" });
			return;
		}
		if (!["requested", "authorized"].includes(lifecycleStatus)) {
			frappe.msgprint({ title: __("Cancellation unavailable"), message: __("Only a requested or authorized safe reset can be cancelled before rotation."), indicator: "red" });
			return;
		}
		const expectedConfirmation = `CANCEL SAFE RESET ${resetId}`;
		const confirmation = String(this.authorization_fields.cancellation_confirmation.get_value() || "");
		if (confirmation !== expectedConfirmation) {
			frappe.msgprint({ title: __("Exact confirmation required"), message: this.escape_html(__("Type {0} exactly.", [expectedConfirmation])), indicator: "red" });
			return;
		}
		const reasonRaw = String(this.authorization_fields.cancellation_reason.get_value() || "");
		const reason = reasonRaw.trim();
		if (!reason || reasonRaw !== reason || reason.length > 500) {
			frappe.msgprint({ title: __("Cancellation reason required"), message: __("Enter 1-500 characters without leading or trailing spaces."), indicator: "red" });
			return;
		}

		const button = this.page.body.find(".kopos-authorization-cancel");
		button.prop("disabled", true);
		this.page.body.find(".kopos-cancellation-status").text(__("Cancelling the audit before any credential rotation..."));
		try {
			const response = await frappe.call({
				method: "kopos_connector.api.cancel_device_safe_reset_as_system_manager",
				args: {
					safe_reset_protocol_version: 2,
					reset_id: resetId,
					confirmation,
					reason,
					idempotency_key: this.get_cancellation_idempotency_key(resetId),
				},
			});
			const cancellation = response.message || response;
			if (!["cancelled", "already_cancelled"].includes(cancellation.status) || cancellation.lifecycle_status !== "cancelled" || cancellation.credentials_rotated !== false || cancellation.cancellation_origin !== "system_manager" || !/^[0-9a-f]{64}$/.test(String(cancellation.cancellation_idempotency_sha256 || ""))) {
				throw new Error(cancellation.message || __("ERP did not return a valid immutable cancellation acknowledgement."));
			}
			this.authorization_reset = { ...resetDoc, ...cancellation, status: "cancelled" };
			this.current_link = null;
			this.page.body.find(".kopos-copy-link").hide();
			this.page.body.find(".kopos-preview-filled").hide();
			this.page.body.find(".kopos-qr-image").removeAttr("src");
			this.page.body.find(".kopos-preview-empty").show();
			this.page.set_indicator(__("Safe Reset Cancelled"), "blue");
			this.page.body.find(".kopos-cancellation-status").text(__("Cancelled before rotation. Any previously issued approval QR is now rejected by ERP."));
			frappe.show_alert({ message: __("Safe reset cancelled before rotation"), indicator: "blue" });
		} catch (error) {
			const message = error?.message || __("Failed to cancel safe reset");
			this.page.set_indicator(__("Cancellation Failed"), "red");
			this.page.body.find(".kopos-cancellation-status").text(message);
			frappe.msgprint({ title: __("Safe reset cancellation failed"), message: this.escape_html(message), indicator: "red" });
		} finally {
			button.prop("disabled", false);
		}
	}

	async authorize_safe_reset() {
		const resetId = String(this.authorization_fields.reset_id.get_value() || "").trim();
		let resetDoc = this.authorization_reset;
		if (!resetDoc || String(resetDoc.reset_id || resetDoc.name || "") !== resetId) {
			resetDoc = await this.load_safe_reset();
		}
		if (!resetDoc) return;

		const args = { reset_id: resetId };
		const firstAuthorization = String(resetDoc.status || "").toLowerCase() === "requested";
		const reviewRequired = resetDoc.migration_recovery_review_required === true;
		const confirmation = String(this.authorization_fields.migration_recovery_confirmation.get_value() || "");
		const reasonRaw = String(this.authorization_fields.migration_recovery_acknowledgement_reason.get_value() || "");
		if (firstAuthorization && reviewRequired) {
			const expectedAck = ["ACK RECOVERY", resetId, resetDoc.export_sha256].join(" ");
			if (confirmation !== expectedAck) {
				frappe.msgprint({ title: __("Acknowledgement required"), message: this.escape_html(__("Type {0} exactly.", [expectedAck])), indicator: "red" });
				return;
			}
			const reason = reasonRaw.trim();
			if (reasonRaw !== reason || reason.length < 20 || reason.length > 500) {
				frappe.msgprint({ title: __("Reconciliation reason required"), message: __("Enter 20-500 characters without leading or trailing spaces."), indicator: "red" });
				return;
			}
			args.migration_recovery_confirmation = confirmation;
			args.migration_recovery_acknowledgement_reason = reason;
		} else if (!reviewRequired && (confirmation || reasonRaw)) {
			frappe.msgprint({ title: __("Acknowledgement not applicable"), message: __("Clear the acknowledgement fields because no migration recovery points exist."), indicator: "red" });
			return;
		} else if (!firstAuthorization && reviewRequired && (confirmation || reasonRaw)) {
			frappe.msgprint({ title: __("Acknowledgement is immutable"), message: __("Clear the ACK and reason fields to reissue using the stored acknowledgement."), indicator: "red" });
			return;
		}

		const button = this.page.body.find(".kopos-authorization-generate");
		button.prop("disabled", true);
		this.page.body.find(".kopos-authorization-status").text(__("Verifying business state and issuing an approval-only challenge QR..."));
		try {
			const response = await frappe.call({
				method: "kopos_connector.api.authorize_device_safe_reset",
				args,
			});
			const authorization = response.message || response;
			if (!authorization.approval_qr_svg || !authorization.approval_link || authorization.provisioning_mode !== "safe_reset_approval" || authorization.safe_reset_protocol_version !== 2) {
				throw new Error(authorization.message || __("ERP did not issue a safe-reset approval QR."));
			}
			const acknowledgedEvidence = this.validate_migration_recovery_evidence(authorization, __("ERP authorization migration evidence"));
			if (Object.keys(acknowledgedEvidence).some((key) => acknowledgedEvidence[key] !== resetDoc[key])) {
				throw new Error(__("ERP authorization did not echo the loaded migration recovery evidence."));
			}
			if (authorization.export_sha256 !== resetDoc.export_sha256 || authorization.export_content_sha256 !== resetDoc.export_content_sha256 || authorization.export_byte_length !== resetDoc.export_byte_length) {
				throw new Error(__("ERP approval did not echo the loaded retained-archive evidence."));
			}
			this.current_link = authorization.approval_link;
			this.render_approval_preview(authorization);
			this.page.set_indicator(__("Safe Reset Approval Ready"), "green");
			this.page.body.find(".kopos-authorization-status").text(__("Approval QR issued without changing credentials. Rotation occurs only after the trusted tablet app rehashes its retained archive and redeems this challenge."));
			frappe.show_alert({ message: __("Safe-reset approval QR ready"), indicator: "green" });
		} catch (error) {
			const message = error?.message || __("Failed to authorize safe reset");
			this.page.set_indicator(__("Authorization Failed"), "red");
			this.page.body.find(".kopos-authorization-status").text(message);
			frappe.msgprint({ title: __("Safe reset authorization failed"), message: this.escape_html(message), indicator: "red" });
		} finally {
			button.prop("disabled", false);
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
			<div><strong>${__("Provisioning User")}:</strong> ${this.escape_html(preview.provisioning_user || frappe.session.user || "-")}</div>
			<div><strong>${__("Company")}:</strong> ${this.escape_html(preview.company || "-")}</div>
			<div><strong>${__("Warehouse")}:</strong> ${this.escape_html(preview.warehouse || "-")}</div>
			<div><strong>${__("Expires At")}:</strong> ${frappe.datetime.str_to_user(payload.expires_at)}</div>
		`);
		this.page.body.find(".kopos-link").text(__("The one-time setup link is hidden after creation. Scan the QR or use Copy Link only while this page is open."));
	}

	render_approval_preview(payload) {
		this.page.body.find(".kopos-preview-empty").hide();
		this.page.body.find(".kopos-preview-filled").show();
		this.page.body.find(".kopos-copy-link").show();
		this.page.body.find(".kopos-qr-image").attr("src", `data:image/svg+xml;base64,${payload.approval_qr_svg}`);
		this.page.body.find(".kopos-meta").html(`
			<div><strong>${__("Safe Reset ID")}:</strong> ${this.escape_html(payload.reset_id || "-")}</div>
			<div><strong>${__("Request ID")}:</strong> ${this.escape_html(payload.request_id || "-")}</div>
			<div><strong>${__("Protocol")}:</strong> v${this.escape_html(payload.safe_reset_protocol_version || "-")}</div>
			<div><strong>${__("Approval Generation")}:</strong> ${this.escape_html(payload.approval_generation || "-")}</div>
			<div><strong>${__("Archive SHA-256")}:</strong> ${this.escape_html(payload.export_sha256 || "-")}</div>
			<div><strong>${__("Archive Content SHA-256")}:</strong> ${this.escape_html(payload.export_content_sha256 || "-")}</div>
			<div><strong>${__("Archive Bytes")}:</strong> ${this.escape_html(payload.export_byte_length || "-")}</div>
			<div><strong>${__("Approval Expires At")}:</strong> ${this.escape_html(frappe.datetime.str_to_user(payload.approval_expires_at) || "-")}</div>
		`);
		this.page.body.find(".kopos-link").text(__("This approval link contains no API credentials or raw reset proof. Keep it private and scan it only on the matching tablet."));
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
			frappe.msgprint(__("Copy failed. Scan the QR while this page is open."));
		}
	}
}
