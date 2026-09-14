const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const asset = join(__dirname, "../kopos_connector/public/js/pos_profile_setup.js");
function harness() {
  const requests = [], messages = [], dialogs = [], libraries = [], scans = [], cleanups = [];
  let refresh, dirty = false, reloads = 0, transport, decoder = async () => "SYNTHETIC-DECODED-QR";
  let loadLibrary = async () => {}, clean = () => {};
  const buttons = {}, frm = { doc: { name: "Counter", company: "Company", currency: "MYR", modified: "reviewed-revision",
    custom_kopos_manual_qr_suspense_account: "QR Suspense", custom_kopos_automatic_qr_enabled: 0 },
    is_new: () => false, is_dirty: () => dirty, add_custom_button(label, callback) { buttons[label] = callback; },
    async reload_doc() { reloads += 1; },
  };
  function node() {
    return { handlers: {}, content: "", properties: {}, children: new Map(),
      on(event, callback) { this.handlers[event] = callback; return this; },
      one(event, callback) { this.handlers[event] = callback; return this; },
      remove() { this.removed = true; },
      html(value) { this.content = value; return this; }, text(value) { this.content = value; return this; },
      prop(key, value) { this.properties[key] = value; return this; },
      find(selector) { if (!this.children.has(selector)) this.children.set(selector, node()); return this.children.get(selector); },
    };
  }
  class Dialog {
    constructor(options) {
      this.options = options; this.fields_dict = {}; this.values = {}; this.primary = node(); this.$wrapper = node(); this.alert = null;
      for (const field of options.fields) if (field.fieldname) {
        this.fields_dict[field.fieldname] = { df: { ...field }, $wrapper: node() };
        this.values[field.fieldname] = field.default;
      }
      this.set_primary_action(options.primary_action_label, options.primary_action); dialogs.push(this);
    }
    show() { this.visible = true; }
    hide() { this.visible = false; this.options.on_hide?.(); this.$wrapper.handlers["hidden.bs.modal"]?.(); }
    get_value(name) { return this.values[name]; }
    async set_value(name, value) { this.values[name] = value; if (this.fields_dict[name].df.fieldtype === "HTML") this.fields_dict[name].$wrapper.html(value); this.fields_dict[name].df.onchange?.(); }
    refresh_sections() { this.visibleReview = !this.fields_dict.review.df.hidden; }
    set_df_property(name, key, value) { this.fields_dict[name].df[key] = value; }
    set_primary_action(label, callback) { this.label = label; this.action = callback; }
    get_primary_btn() { return this.primary; }
    set_alert(message) { this.alert = message; }
    clear_alert() { this.alert = null; }
  }
  function preview(config = {}) {
    return { profile: frm.doc.name, profile_modified: "reviewed-revision", company: "Company", currency: "MYR",
      cash: { ready: true }, static_qr: { ready: Boolean(config.custom_kopos_static_qr_enabled), reason_code: "static_qr_disabled" },
      automatic_qr: { ready: false, reason_code: "automatic_qr_disabled" },
      requires_merchant_confirmation: Boolean(config.custom_kopos_static_qr_payload),
      static_qr_merchant: config.custom_kopos_static_qr_payload ? { merchant_name: "<script>store</script>", merchant_id: "123", acquirer_id: "Bank", payload_sha256: "exact-payload-hash" } : null,
    };
  }
  const context = vm.createContext({ Error, __: value => value, Html5QrcodeSupportedFormats: { QR_CODE: 0 },
    Html5Qrcode: class {
      constructor(id, config) { scans.push({ id, config }); }
      scanFile(file, show) { scans.push({ file, show }); return decoder(file); }
      clear() { cleanups.push(true); clean(); }
    },
    frappe: { user_roles: ["Accounts Manager"], utils: {
      get_random: () => "synthetic123", escape_html: value => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    }, ui: { form: { on(name, handlers) { assert.equal(name, "POS Profile"); refresh = handlers.refresh; } }, Dialog },
    async call(request) { requests.push(structuredClone(request)); return transport ? transport(request) : { message: { preview: preview(request.args.config) } }; },
    async require(url) { libraries.push(url); await loadLibrary(); }, msgprint: value => messages.push(value),
    set_route() {},
    },
  });
  vm.runInContext(readFileSync(asset, "utf8"), context); refresh(frm);
  return { frm, buttons, requests, messages, dialogs, libraries, scans, cleanups, preview,
    set dirty(value) { dirty = value; }, get reloads() { return reloads; },
    set transport(value) { transport = value; }, set decoder(value) { decoder = value; },
    set loadLibrary(value) { loadLibrary = value; }, set cleanup(value) { clean = value; },
    open() { buttons["Set Up QR Payments"](); return dialogs.at(-1); },
    import(dialog, file = { type: "image/png", size: 100 }) {
      const input = dialog.fields_dict.image.$wrapper.find("input");
      return input.handlers.change({ target: { files: file ? [file] : [], disabled: false } });
    },
  };
}

test("unsaved profile is neither saved, reloaded nor sent to setup", async () => {
  const h = harness(); h.dirty = true; h.open(); await h.buttons["Validate Configuration"]();
  assert.equal(h.dialogs.length, 0); assert.equal(h.requests.length, 0); assert.equal(h.reloads, 0);
  assert.match(h.messages[0].message, /Save your changes/);
});

test("setup reuses the open dialog and does not mutate the underlying profile", async () => {
  const h = harness(), before = structuredClone(h.frm.doc), d = h.open(); h.open();
  await h.import(d);
  assert.equal(h.dialogs.length, 1); assert.deepEqual(h.frm.doc, before); assert.equal(h.requests.length, 0);
  assert.deepEqual(h.libraries, ["/assets/frappe/node_modules/html5-qrcode/html5-qrcode.min.js"]);
  assert.equal(h.scans[1].show, false); assert.equal(h.cleanups.length, 1);
});

test("image review escapes merchant data and requires explicit confirmation before exact-hash save", async () => {
  const h = harness(), d = h.open(); await h.import(d); await d.action();
  assert.equal(d.label, "Save QR settings");
  assert.equal(d.visibleReview, true);
  assert.match(d.fields_dict.review.$wrapper.content, /&lt;script&gt;/);
  assert.doesNotMatch(d.fields_dict.review.$wrapper.content, /<script>/);
  await d.action(); assert.equal(h.requests.length, 1); assert.match(d.alert, /tick the confirmation/);
  await d.set_value("merchant_confirmed", 1); await d.action();
  assert.equal(h.requests.length, 2);
  const config = h.requests[1].args.config;
  assert.equal(config.confirmed_static_qr_sha256, "exact-payload-hash");
  assert.equal(config.custom_kopos_static_qr_payload, "SYNTHETIC-DECODED-QR");
  assert.equal(config.expected_modified, "reviewed-revision");
  assert.equal(h.reloads, 1); assert.equal(d.visible, false);
  assert.equal(d.$wrapper.removed, true);
});

test("changing an account invalidates the prior review and confirmation", async () => {
  const h = harness(), d = h.open(); await h.import(d); await d.action(); await d.set_value("merchant_confirmed", 1);
  await d.set_value("custom_kopos_manual_qr_suspense_account", "Other Account");
  assert.equal(d.label, "Review settings"); assert.equal(d.fields_dict.review.df.hidden, 1);
  assert.equal(d.visibleReview, false);
  await d.action(); assert.equal(d.get_value("merchant_confirmed"), 0);
  assert.equal(h.requests.at(-1).args.config.custom_kopos_manual_qr_suspense_account, "Other Account");
});

test("closing during library load cannot decode, request or reopen the dialog", async () => {
  const h = harness(), d = h.open(), waiting = deferred(); h.loadLibrary = () => waiting.promise;
  const work = h.import(d); d.hide(); waiting.resolve(); await work;
  assert.equal(h.scans.length, 0); assert.equal(h.requests.length, 0); assert.equal(d.visible, false);
});

test("changing settings or closing during decode cannot install a stale QR result", async () => {
  for (const close of [false, true]) {
    const h = harness(), d = h.open(), waiting = deferred(); h.decoder = () => waiting.promise;
    const work = h.import(d); await Promise.resolve(); await Promise.resolve();
    if (close) d.hide(); else await d.set_value("custom_kopos_manual_qr_suspense_account", "New Account");
    waiting.resolve("STALE-QR"); await work;
    assert.equal(h.frm.doc.custom_kopos_static_qr_payload, undefined);
    if (!close) { await d.set_value("custom_kopos_static_qr_enabled", 1); await d.action(); assert.equal(h.requests.length, 0); }
  }
});

test("rejected or unreadable images do not replace the existing QR or silently reuse it", async () => {
  for (const file of [{ type: "image/svg+xml", size: 10 }, { type: "image/png", size: 6 * 1024 * 1024 }, { type: "image/jpeg", size: 200 }]) {
    const h = harness(); h.frm.doc.custom_kopos_static_qr_payload = "SAVED-QR"; h.frm.doc.custom_kopos_static_qr_enabled = 1;
    h.decoder = async () => { throw new Error("No QR found"); };
    const d = h.open(); await h.import(d, file); await d.action();
    assert.equal(h.requests.length, 0); assert.equal(h.frm.doc.custom_kopos_static_qr_payload, "SAVED-QR");
    assert.match(d.alert, /Choose a clear image/);
  }
});

test("old preview responses cannot restore a review invalidated by a new account", async () => {
  const h = harness(), d = h.open(), waiting = deferred(); h.transport = () => waiting.promise;
  const work = d.action(); await d.set_value("custom_kopos_manual_qr_suspense_account", "New Account");
  waiting.resolve({ message: { preview: h.preview() } }); await work;
  assert.equal(d.label, "Review settings"); assert.equal(d.fields_dict.review.df.hidden, 1);
});

test("a changed server revision or unavailable enabled lane cannot be saved", async () => {
  for (const change of [p => { p.profile_modified = "newer"; }, p => { p.static_qr.ready = false; }]) {
    const h = harness(), d = h.open(); await h.import(d);
    h.transport = async request => { const p = h.preview(request.args.config); change(p); return { message: { preview: p } }; };
    await d.action(); assert.equal(d.label, "Review settings"); assert.equal(h.requests.length, 1); assert.ok(d.alert);
  }
});

test("dirty changes made during save are retained without an automatic form reload", async () => {
  const h = harness(), d = h.open(); await d.action(); const waiting = deferred(); h.transport = () => waiting.promise;
  const work = d.action(); h.dirty = true; waiting.resolve({ message: { preview: h.preview() } }); await work;
  assert.equal(h.reloads, 0); assert.match(h.messages.at(-1), /unsaved form changes were kept/);
});

test("repeated save clicks send one request and an uncertain response never reports success", async () => {
  const h = harness(), d = h.open(); await d.action(); const waiting = deferred(); h.transport = () => waiting.promise;
  const work = d.action(); await d.action(); assert.equal(h.requests.length, 2);
  waiting.reject(new Error("Connection lost")); await work;
  assert.equal(h.reloads, 0); assert.equal(d.visible, false); assert.equal(h.messages.at(-1).title, "Check saved settings before retrying");
});

test("account selectors use native Link controls scoped to current company and currency", () => {
  const h = harness(), d = h.open();
  for (const name of ["manual_qr_suspense_account", "qr_clearing_account", "qr_settlement_bank_account"]) {
    const field = d.fields_dict["custom_kopos_" + name].df;
    assert.equal(field.fieldtype, "Link"); assert.equal(field.only_select, true);
    const { filters } = field.get_query();
    assert.equal(filters.company, "Company"); assert.equal(filters.account_currency, "MYR");
    assert.equal(filters.is_group, 0); assert.equal(filters.disabled, 0); assert.equal(filters.root_type, "Asset");
  }
});

test("a decoder cleanup failure leaves a visible recoverable state instead of an unhandled rejection", async () => {
  const h = harness(), d = h.open(); h.cleanup = () => { throw new Error("Synthetic cleanup failure"); };
  await h.import(d);
  assert.equal(d.primary.properties.disabled, false); assert.match(d.alert, /reader/);
  await d.action(); assert.equal(h.requests.length, 0);
});
