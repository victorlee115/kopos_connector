// Installed as the existing POS Profile Client Script. Frappe owns the form,
// dialog, Link controls and request lifecycle; html5-qrcode owns image decoding.
(() => {
  const prefix = "custom_kopos_";
  const fields = ["static_qr_enabled", "manual_qr_suspense_account", "automatic_qr_enabled",
    "maybank_qrpaybiz_account", "maybank_outlet_id", "qr_clearing_account", "qr_settlement_bank_account"];
  const escape = (value) => frappe.utils.escape_html(String(value ?? ""));
  const laneState = (lane) => lane?.ready ? __("Ready") :
    (lane?.reason_code?.endsWith("_disabled") ? __("Off") : __("Needs setup"));

  function review(preview, config) {
    const merchant = preview.static_qr_merchant;
    return `<p>${__("Applies to this POS Profile and its linked tablets. Company-wide payment accounts are not changed.")}</p>`
      + `<p><strong>${escape(preview.profile)}</strong> — ${escape(preview.company)}</p>`
      + `<ul><li>${__("Cash")}: ${escape(laneState(preview.cash))}</li>`
      + `<li>${__("Store QR")}: ${escape(laneState(preview.static_qr))}</li>`
      + `<li>${__("Automatic bank confirmation")}: ${escape(laneState(preview.automatic_qr))}</li></ul>`
      + (config?.custom_kopos_manual_qr_suspense_account ? `<p>${__("Selected account for QR payments awaiting confirmation")}: <strong>${escape(config.custom_kopos_manual_qr_suspense_account)}</strong></p>` : "")
      + (merchant ? `<p><strong>${__("QR merchant")}: ${escape(merchant.merchant_name)}</strong><br>`
        + `${__("Merchant ID")}: ${escape(merchant.merchant_id)}<br>${__("Bank / acquirer ID")}: ${escape(merchant.acquirer_id)}</p>` : "");
  }

  function requireSavedForm(frm, profile) {
    if (frm.is_dirty()) throw new Error(__("Save your changes to this POS Profile first, then reopen QR setup. Nothing has been saved by setup."));
    if (profile && (frm.doc.name !== profile.name || frm.doc.modified !== profile.modified)) {
      throw new Error(__("This POS Profile changed. Close setup, reload the profile, and review again."));
    }
  }

  async function requestPreview(method, profile, config) {
    const response = await frappe.call({ method: "kopos_connector.api." + method,
      args: { profile_name: profile.name, ...(config ? { config } : {}) } });
    const preview = response.message?.preview || response.preview;
    if (!preview || preview.profile !== profile.name || preview.company !== profile.company) {
      throw new Error(__("Setup could not be confirmed. Reload this POS Profile to check its saved QR settings."));
    }
    return preview;
  }

  function openSetup(frm) {
    requireSavedForm(frm);
    if (frm.__kopos_qr_setup) return frm.__kopos_qr_setup.show();
    const profile = { name: frm.doc.name, company: frm.doc.company, currency: frm.doc.currency, modified: frm.doc.modified };
    let payload = frm.doc.custom_kopos_static_qr_payload || "";
    let dialog, reviewed = null, generation = 0, busy = false, closed = false, importFailed = false;
    const original = Object.fromEntries(fields.map(field => [field, frm.doc[prefix + field] ?? (field.endsWith("_enabled") ? 0 : "")]));
    const decoderId = "kopos-qr-image-" + frappe.utils.get_random(12);
    const tell = (text, kind = "danger") => { if (!closed) dialog.set_alert(escape(text), kind); };
    const invalidate = () => {
      generation += 1;
      reviewed = null;
      if (!dialog || closed) return;
      dialog.set_df_property("review", "hidden", 1);
      dialog.set_df_property("merchant_confirmed", "hidden", 1);
      dialog.refresh_sections();
      dialog.set_primary_action(__("Review settings"), prepare);
      dialog.get_primary_btn().prop("disabled", busy);
    };
    const settings = () => JSON.stringify(fields.map(field => dialog.get_value(prefix + field) ?? original[field]));
    let savedSettings = null;
    // A click that blurs a Link control makes Frappe validate it again and fire
    // onchange with an unchanged value. Only a real change invalidates work.
    const settingChanged = () => {
      if (!dialog || closed || settings() === savedSettings) return;
      savedSettings = settings();
      invalidate();
    };
    const configValues = () => ({
      ...Object.fromEntries(fields.map(field => [prefix + field, dialog.get_value(prefix + field) ?? original[field]])),
      custom_kopos_static_qr_payload: payload,
      expected_modified: profile.modified,
    });
    const enabled = (value) => value === 1 || value === true || value === "1";
    const accountField = (field, label, extra = {}) => ({
      fieldtype: "Link", fieldname: prefix + field, label: __(label), options: "Account", default: original[field],
      only_select: true, onchange: settingChanged,
      get_query: () => ({ filters: { company: profile.company, is_group: 0, disabled: 0,
        root_type: "Asset", account_currency: profile.currency, ...extra } }),
    });

    async function prepare() {
      if (busy || closed) return;
      try {
        requireSavedForm(frm, profile);
        const config = configValues();
        if (enabled(config.custom_kopos_static_qr_enabled) && (!payload || importFailed)) {
          throw new Error(__("Choose a clear image of your bank's store QR before continuing."));
        }
        if ((enabled(config.custom_kopos_static_qr_enabled) || enabled(config.custom_kopos_automatic_qr_enabled)) && !config.custom_kopos_manual_qr_suspense_account) {
          throw new Error(__("Choose the account for QR payments awaiting confirmation. If you are unsure, ask whoever set up your accounts."));
        }
        busy = true;
        dialog.get_primary_btn().prop("disabled", true);
        const current = generation;
        const staleReview = () => { if (current !== generation) throw new Error(__("Your settings changed while they were being checked. Review them again.")); };
        const preview = await requestPreview("get_qr_setup_preview", profile, config);
        if (closed) return;
        staleReview();
        requireSavedForm(frm, profile);
        if (preview.profile_modified !== config.expected_modified) {
          throw new Error(__("Someone changed this POS Profile. Close setup, reload the profile, and review again."));
        }
        if (enabled(config.custom_kopos_static_qr_enabled) && !preview.static_qr?.ready) {
          throw new Error(__("Store QR is not ready. Check the QR image and the selected payment account, then review again."));
        }
        if (enabled(config.custom_kopos_automatic_qr_enabled) && !preview.automatic_qr?.ready) {
          throw new Error(__("Automatic bank confirmation is not ready. Check the optional bank settings below, or leave automatic confirmation off."));
        }
        dialog.clear_alert();
        await dialog.set_value("review", review(preview, config));
        if (closed) return;
        staleReview();
        dialog.set_df_property("review", "hidden", 0);
        await dialog.set_value("merchant_confirmed", 0);
        if (closed) return;
        staleReview();
        reviewed = { config, preview };
        dialog.set_df_property("merchant_confirmed", "hidden", !enabled(config.custom_kopos_static_qr_enabled));
        dialog.refresh_sections();
        dialog.set_primary_action(__("Save QR settings"), save);
      } catch (error) {
        tell(error instanceof Error ? error.message : __("Could not review these settings. Check the QR image and accounts, then try again."));
      } finally {
        busy = false;
        if (!closed) dialog.get_primary_btn().prop("disabled", false);
      }
    }

    async function save() {
      if (busy || closed || !reviewed) return;
      try {
        requireSavedForm(frm, profile);
        if (JSON.stringify(configValues()) !== JSON.stringify(reviewed.config)) {
          invalidate();
          throw new Error(__("Your settings changed. Review them again before saving."));
        }
        if (enabled(reviewed.config.custom_kopos_static_qr_enabled) && !enabled(dialog.get_value("merchant_confirmed"))) {
          throw new Error(__("Check the merchant against your bank's QR, then tick the confirmation box."));
        }
      } catch (error) { tell(error.message); return; }
      busy = true;
      dialog.get_primary_btn().prop("disabled", true);
      const config = { ...reviewed.config };
      if (reviewed.preview.requires_merchant_confirmation) {
        config.confirmed_static_qr_sha256 = reviewed.preview.static_qr_merchant.payload_sha256;
      }
      try {
        const applied = await requestPreview("apply_qr_configuration", profile, config);
        dialog.hide();
        frappe.msgprint({ title: __("QR settings saved"), message: review(applied, config), indicator: "green" });
        // Edits made while the request was in flight still belong to the user.
        if (!frm.is_dirty() && frm.doc.name === profile.name && frm.doc.modified === profile.modified) await frm.reload_doc();
        else frappe.msgprint(__("QR settings were saved. Your unsaved form changes were kept; review them before saving this POS Profile."));
      } catch {
        dialog.hide();
        frappe.msgprint({ title: __("Check saved settings before retrying"), indicator: "orange",
          message: __("The save could not be confirmed. Keep any unsaved form changes, then reload this POS Profile and check QR settings before trying again.") });
      }
    }

    dialog = new frappe.ui.Dialog({
      title: __("Set up QR payments"), size: "large",
      fields: [
        { fieldtype: "HTML", fieldname: "intro", options: `<p><strong>${escape(profile.name)}</strong> — ${escape(profile.company)}</p>`
          + `<p>${__("Use the reusable store QR issued by your bank. Your image is read on this device, not uploaded; the decoded payment code is checked and saved in ERP.")}</p>` },
        { fieldtype: "Check", fieldname: prefix + "static_qr_enabled", label: __("Accept payments with my store QR"), default: original.static_qr_enabled, onchange: settingChanged },
        { fieldtype: "HTML", fieldname: "image", options:
          `<label>${__("1. Choose your bank QR image")}<input type="file" class="form-control" accept="image/png,image/jpeg,image/webp"></label>`
          + `<p class="text-muted">${__("PNG, JPG or WebP, up to 5 MB. Use a clear image containing only your store's QR code. No camera permission is needed.")}</p>`
          + `<p role="status" aria-live="polite" class="qr-image-status">${payload ? __("A saved QR is available. Choose an image only to replace it.") : __("No store QR has been added yet.")}</p><div id="${decoderId}"></div>` },
        { fieldtype: "Section Break", label: __("2. Choose the payment account") },
        { ...accountField("manual_qr_suspense_account", "QR payments awaiting confirmation", { account_type: ["in", ["Bank", ""]] }),
          description: __("Keep the existing account unless your accounts adviser asks you to change it. This does not change company-wide payment accounts.") },
        { fieldtype: "Section Break", label: __("Automatic bank confirmation (optional)"), collapsible: 1 },
        { fieldtype: "HTML", fieldname: "automatic_help", options: `<p>${__("Leave this off to use your store QR with manual payment confirmation. Enable only after your Maybank merchant login and accounts have been set up.")}</p>` },
        { fieldtype: "Check", fieldname: prefix + "automatic_qr_enabled", label: __("Confirm payments automatically with Maybank"), default: original.automatic_qr_enabled, onchange: settingChanged },
        { fieldtype: "Link", fieldname: prefix + "maybank_qrpaybiz_account", label: __("Maybank merchant login"), options: "Maybank QRPayBiz Account", only_select: true,
          default: original.maybank_qrpaybiz_account, get_query: () => ({ filters: { enabled: 1 } }), onchange: settingChanged },
        { fieldtype: "Data", fieldname: prefix + "maybank_outlet_id", label: __("Maybank outlet ID"), default: original.maybank_outlet_id, onchange: settingChanged },
        accountField("qr_clearing_account", "Confirmed QR payment account", { account_type: ["in", ["Bank", ""]] }),
        accountField("qr_settlement_bank_account", "Bank account receiving settlements", { account_type: "Bank" }),
        { fieldtype: "Section Break", label: __("3. Review before saving") },
        { fieldtype: "HTML", fieldname: "review", hidden: 1 },
        { fieldtype: "Check", fieldname: "merchant_confirmed", hidden: 1,
          label: __("I checked: this merchant name and ID match my store's bank QR") },
      ],
      primary_action_label: __("Review settings"), primary_action: prepare,
      secondary_action_label: __("Cancel"), secondary_action: () => dialog.hide(),
      on_hide() { closed = true; generation += 1; payload = ""; reviewed = null; frm.__kopos_qr_setup = null; },
    });
    // Each opening takes a fresh profile revision. Release the old file input
    // and handlers after Frappe finishes hiding this one-use dialog.
    dialog.$wrapper.one("hidden.bs.modal", () => dialog.$wrapper.remove());
    savedSettings = settings();
    frm.__kopos_qr_setup = dialog;
    dialog.fields_dict.image.$wrapper.find("input").on("change", async (event) => {
      const file = event.target.files?.[0];
      if (!file || busy || closed) return;
      invalidate();
      importFailed = true;
      const current = generation;
      busy = true;
      dialog.get_primary_btn().prop("disabled", true);
      event.target.disabled = true;
      const status = dialog.fields_dict.image.$wrapper.find(".qr-image-status");
      status.text(__("Reading your QR image…"));
      let scanner;
      try {
        if (!["image/png", "image/jpeg", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024) {
          throw new Error(__("Choose a PNG, JPG or WebP image no larger than 5 MB."));
        }
        await frappe.require("/assets/frappe/node_modules/html5-qrcode/html5-qrcode.min.js");
        if (closed) return;
        scanner = new Html5Qrcode(decoderId, { formatsToSupport: [Html5QrcodeSupportedFormats.QR_CODE], verbose: false });
        const decoded = await scanner.scanFile(file, false);
        if (closed) return;
        if (current !== generation) {
          status.text(__("Settings changed while reading. Choose the QR image again before reviewing."));
          return;
        }
        payload = decoded;
        importFailed = false;
        await dialog.set_value(prefix + "static_qr_enabled", 1);
        invalidate();
        dialog.clear_alert();
        status.text(__("Image read. Choose the payment account, then review the merchant before saving."));
      } catch (error) {
        if (!closed) {
          status.text(__("Image not accepted. The saved QR has not been replaced. Choose a clearer image and try again."));
          tell(error instanceof Error ? error.message : __("Could not read a QR in this image. Crop around the QR and try again."));
        }
      } finally {
        try { if (scanner) scanner.clear(); }
        catch {
          importFailed = true;
          tell(__("The QR image reader could not reset. Close setup and reopen it before choosing another image."));
        } finally {
          busy = false;
          if (!closed) { event.target.disabled = false; dialog.get_primary_btn().prop("disabled", false); }
        }
      }
    });
    dialog.show();
  }

  frappe.ui.form.on("POS Profile", {
    refresh(frm) {
      if (frm.is_new() || !["System Manager", "Accounts Manager"].some(role => (frappe.user_roles || []).includes(role))) return;
      frm.add_custom_button(__("Generate KoPOS Setup QR"), () => {
        frappe.route_options = { pos_profile: frm.doc.name, company: frm.doc.company || undefined,
          warehouse: frm.doc.warehouse || undefined, currency: frm.doc.currency || undefined };
        frappe.set_route("kopos_provisioning");
      }, __("KoPOS"));
      frm.add_custom_button(__("Validate Configuration"), async () => {
        try { requireSavedForm(frm); frappe.msgprint({ title: __("QR configuration"), message: review(await requestPreview("get_qr_setup_preview", frm.doc)), wide: true }); }
        catch (error) { frappe.msgprint({ title: __("Could not check QR settings"), message: escape(error.message), indicator: "red" }); }
      }, __("KoPOS"));
      frm.add_custom_button(__("Set Up QR Payments"), () => {
        try { openSetup(frm); }
        catch (error) { frappe.msgprint({ title: __("Before you start"), message: escape(error.message), indicator: "orange" }); }
      }, __("KoPOS"));
    },
  });
})();
