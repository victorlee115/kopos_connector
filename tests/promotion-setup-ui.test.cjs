const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const source = readFileSync(join(__dirname, "../kopos_connector/public/js/kopos_promotion.js"), "utf8");

function harness() {
  const dialogs = [], messages = [], requests = [], properties = [], buttons = [];
  let handlers, saved = 0, failure = false;
  class Dialog {
    constructor(options) {
      Object.assign(this, options);
      this.values = Object.fromEntries(options.fields.filter(f => f.fieldname).map(f => [f.fieldname, f.data || f.default || ""]));
      this.disabled = false;
      this.fields_dict = Object.fromEntries(options.fields.filter(f => f.fieldtype === "Table").map(f => [f.fieldname, { grid: { wrapper: { find: () => ({ text: label => { f.add_label = label; } }) } } }]));
      dialogs.push(this);
      for (const field of options.fields) field.onchange?.();
    }
    show() { this.visible = true; }
    hide() { this.visible = false; }
    get_value(field) { return this.values[field]; }
    set_df_property(field, key, value) { this.fields.find(f => f.fieldname === field)[key] = value; }
    get_primary_btn() { return { prop: (_key, value) => { this.disabled = value; } }; }
  }
  const result = {
    fields: { promotion_name: "Tea <offer>", is_active: 0, eligible_items: [{ item_code: "TEA" }], eligible_item_groups: [], eligible_pos_profiles: [{ pos_profile: "Cafe" }] },
    example: { item: "TEA <script>", qty: 2, price_sen: 1000, subtotal_sen: 2000, discount_sen: 200, total_sen: 1800, at_time: "2030-01-01 10:00:00" },
    notice: "Example only; no publication.",
  };
  const frm = {
    doc: { name: "new-offer", is_active: 0 },
    is_new: () => frm.doc.name === "new-offer",
    is_dirty: () => false,
    set_df_property: (...args) => properties.push(args),
    set_intro() {}, add_custom_button: (label, callback) => buttons.push({ label, callback }),
    clear_table: field => { frm.doc[field] = []; },
    add_child: (field, value) => frm.doc[field].push(value),
    set_value: async values => Object.assign(frm.doc, values),
    save: async () => { saved++; frm.doc.name = "Tea offer"; },
  };
  const context = vm.createContext({
    __: text => text,
    frappe: {
      ui: { form: { on: (_name, value) => { handlers = value; } }, Dialog },
      user: { has_role: () => false },
      msgprint: value => messages.push(value), show_alert: value => messages.push(value),
      confirm: (_message, callback) => callback(),
      call: async request => { requests.push(request); if (failure) throw new Error("Offline"); return { message: result }; },
    },
  });
  vm.runInContext(source, context);
  return { dialogs, messages, requests, properties, buttons, frm, handlers, result, context,
    saved: () => saved, fail: () => { failure = true; } };
}

test("new forms route to the one full-page editor without saving or opening an old wizard", () => {
  const h = harness(), routes = [];
  h.context.frappe.set_route = (...route) => routes.push(route);
  h.handlers.refresh(h.frm);
  h.buttons.find(button => button.label === "Create an offer").callback();
  assert.equal(h.dialogs.length, 0);
  assert.deepEqual(routes, [["jiji_promotions"]]);
  assert.equal(h.context.frappe.route_options.create, true);
  assert.equal(h.saved(), 0);
});

test("inactive drafts can use cost checks without being made active", () => {
  const h = harness();
  h.context.frappe.user.has_role = () => true;
  h.frm.doc = { name: "Saved offer", is_active: 0, eligible_pos_profiles: [{pos_profile:"Cafe"}] };
  h.handlers.refresh(h.frm);
  h.buttons.find(button => button.label === "Check costs and profit").callback();
  assert.equal(h.dialogs.at(-1).values.pos_profile, "Cafe");
  assert.equal(h.frm.doc.is_active, 0);
  assert.equal(h.requests.length, 0);
});

function editorHarness() {
  const messages = [], requests = [], controls = [], focus = [];
  const makeNode = () => ({ attr() { return this; }, prop(key, value) { this[key] = value; return this; },
    empty() { return this; }, text() { return this; }, append() { return this; }, prepend() { return this; }, appendTo() { return this; },
    addClass() { return this; }, css() { return this; }, val() { return this; }, on() { return this; },
    find() { return this; }, remove() { return this; }, trigger(event) { focus.push({event, inert: node.inert}); return this; } });
  const node = makeNode();
  const context = vm.createContext({ __: text => text, $: makeNode, Intl,
    jiji: {desk: {notice: () => node}},
    frappe: {pages: {jiji_promotions: {}}, show_alert: message => messages.push(message),
      ui: {form: {make_control: ({df}) => {
        const control = {df, value: undefined, get_value() {return this.value;}, set_value(value) {
          this.value = value;
          // Frappe's initial set_value completes asynchronously and invokes
          // the handler assigned after set_value, just like a user's edit.
          return Promise.resolve().then(() => this.df.onchange?.());
        }};
        controls.push(control);
        return control;
      }}},
      call: async request => {requests.push(request); return {message: {}};} } });
  vm.runInContext(readFileSync(join(__dirname, "../kopos_connector/kopos/page/jiji_promotions/jiji_promotions.js"), "utf8"), context);
  const page = Object.create(vm.runInContext("JiJiOffers.prototype", context));
  Object.assign(page, {body: node, panel: node, sequence:0, previewArea: node, readinessArea: node, activateButton: {prop: (_key, disabled) => {page.disabled = disabled;}},
    previewSequence: 0, editor: {id: "Offer", revision:"r1", dirty:false, previewChecked:true, basket:[{item:"TEA",quantity:1}], values: {name:"Offer",outlets:["Cafe"]}, receipt:{issues:[],expected_hash:"review"}},
    button() {return node;} });
  return {page, context, requests, messages, controls, focus};
}

test("initial asynchronous outlet hydration does not create a preview request loop", async () => {
  const {page, controls} = editorHarness(), calls = [];
  page.editor.values.items = ["TEA"];
  page.call = async (_method, args) => {
    calls.push(args);
    if (calls.length > 3) throw new Error("Detected repeated preview initialization");
    return {pos_profile:args.pos_profile, at_time:"2026-09-09T06:00:00Z", occurrences:[],
      notice:"Example only", quote:{items:[{name:"Tea",unitPriceSen:1200,quantity:1,lineTotalSen:1080}],
        appliedPromotions:[{promotionName:"Ten percent",amountSen:120}],totalDiscountSen:120}};
  };
  await page.preview();
  await new Promise(setImmediate);
  assert.equal(calls.length, 1, "rendering the outlet must not request another quote");
  assert.equal(page.editor.previewChecked, true);
  const outlet = controls.find(control => control.df.fieldname === "example_outlet");
  await outlet.set_value("Other cafe");
  await new Promise(setImmediate);
  assert.equal(calls.length, 2, "a real outlet change requests exactly one new quote");
  assert.equal(calls[1].pos_profile, "Other cafe");
});

test("initial field hydration keeps a saved draft clean but a real edit invalidates review", async () => {
  const {page} = editorHarness();
  const control = page.field(page.body, "Offer name", "name");
  await new Promise(setImmediate);
  assert.equal(page.editor.dirty, false);
  assert.equal(page.editor.previewChecked, true);
  await control.set_value("Changed offer");
  assert.equal(page.editor.values.name, "Changed offer");
  assert.equal(page.editor.dirty, true);
  assert.equal(page.editor.receipt, null);
});

test("initial weekly date hydration does not dirty a saved schedule", async () => {
  const {page, controls} = editorHarness();
  page.editor.values.schedule = {timeZone:"Asia/Kuala_Lumpur",weekdays:[1,2,3,4,5],
    startDate:null,endDate:null,startMinute:840,endMinute:1020};
  page.scheduleStep();
  await new Promise(setImmediate);
  assert.equal(page.editor.dirty, false);
  await controls.find(control => control.df.fieldname === "startDate").set_value("2026-09-10");
  assert.equal(page.editor.values.schedule.startDate, "2026-09-10");
  assert.equal(page.editor.dirty, true);
});

test("offer validation identifies the specific missing or invalid field", () => {
  const {page} = editorHarness(), errors = [];
  page.error = message => errors.push(message);
  page.editor.step = 0;
  page.editor.values = {name:"",kind:"percentage",value:"10"};
  assert.equal(page.validateStep(), false);
  assert.match(errors.at(-1), /^Give this offer a name/);
  page.editor.values.name = "Tea offer";
  page.editor.values.value = "101";
  assert.equal(page.validateStep(), false);
  assert.match(errors.at(-1), /^Enter a percent/);
  page.editor.values = {name:"Tea offer",kind:"amount",value:"0"};
  assert.equal(page.validateStep(), false);
  assert.match(errors.at(-1), /^Enter an amount/);
});

test("leaving Review invalidates an in-flight sample without accepting its late response", async () => {
  const {page, controls} = editorHarness();
  let finish;
  page.call = () => new Promise(resolve => {finish = resolve;});
  const pending = page.preview();
  page.editor.step = 2;
  page.render();
  finish({pos_profile:"Cafe",at_time:"2026-09-09T06:00:00Z",occurrences:[],quote:{items:[],appliedPromotions:[],totalDiscountSen:0}});
  await pending;
  assert.equal(page.editor.previewChecked, false);
  assert.equal(controls.some(control => control.df.fieldname === "example_outlet"), false);
  assert.equal(page.disabled, true);
});

test("validation focus is restored only after the form is no longer inert", async () => {
  const {page, focus} = editorHarness();
  await page.run(() => page.error("Give this offer a name."));
  assert.deepEqual(focus, [{event:"focus",inert:false}]);
  assert.equal(page.body.inert, false);
});

test("retrying the sample clears its old quantity warning without dismissing unrelated errors", async () => {
  const {page} = editorHarness();
  let removed = 0;
  page.editor.sampleQuantityError = {remove() {removed++;}};
  page.body.find = () => {assert.fail("must not dismiss unrelated save or conflict errors");};
  page.call = async () => {throw new Error("Offline");};
  await page.preview();
  assert.equal(removed, 1);
  assert.equal(page.editor.sampleQuantityError, null);
  assert.equal(page.editor.previewChecked, false);
});

test("activation needs a successful current sample and a clean reviewed draft", () => {
  const {page} = editorHarness();
  page.updateActivation(); assert.equal(page.disabled, false);
  page.editor.previewChecked = false; page.updateActivation(); assert.equal(page.disabled, true);
  page.editor.previewChecked = true; page.change("name", "Changed");
  assert.equal(page.disabled, true);
  assert.equal(page.editor.receipt, null);
  assert.equal(page.editor.previewChecked, false);
});

test("a late cost review cannot enable activation after sample pricing failed", async () => {
  const {page} = editorHarness();
  page.call = async () => {throw new Error("No exact price");};
  await page.preview();
  page.editor.receipt = {issues:[]};
  page.readiness(page.editor.receipt);
  assert.equal(page.disabled, true);
  assert.equal(page.editor.previewChecked, false);
  let publications = 0;
  page.call = async () => {publications++;};
  await page.activate();
  assert.equal(publications, 0);
});

test("failed sample can be reset without editing or publishing the saved offer", async () => {
  const {page} = editorHarness(), buttons = [];
  page.editor.values = {name:"Offer",items:["TEA"],outlets:["Cafe"],kind:"half_second"};
  page.editor.basket = [{item:"TEA",quantity:999}];
  page.editor.previewTime = "invalid";
  page.editor.previewOutlet = "Old outlet";
  const saved = JSON.stringify(page.editor.values);
  page.button = (_area, label, callback) => {buttons.push({label,callback}); return page.body;};
  page.call = async () => {throw new Error("No exact price");};
  await page.preview();
  let retried = 0;
  page.preview = async () => {retried++;};
  await buttons.find(button => button.label === "Reset sample basket").callback();
  assert.equal(JSON.stringify(page.editor.values), saved);
  assert.equal(JSON.stringify(page.editor.basket), JSON.stringify([{item:"TEA",quantity:2}]));
  assert.equal(page.editor.previewTime, null);
  assert.equal(page.editor.previewOutlet, "Cafe");
  assert.equal(retried, 1);
  assert.equal(page.disabled, true);
});

test("save records the returned revision and reviews it, without publishing", async () => {
  const {page} = editorHarness(), calls = [];
  page.editor.dirty = true;
  page.call = async (method, args) => {
    calls.push({method,args});
    return method === "save_offer_draft" ? {id:"Offer",revision:"r2",status:"draft_saved"} : {issues:[],expected_hash:"h"};
  };
  await page.save();
  assert.deepEqual(calls.map(call => call.method), ["save_offer_draft", "review_saved_offer"]);
  assert.equal(calls[1].args.revision, "r2");
  assert.equal(page.editor.dirty, false);
});

test("failed saves and navigation retain input and cannot overwrite another editor", async () => {
  const {page} = editorHarness();
  const old = page.editor;
  page.call = async () => {throw new Error("Offline");};
  await assert.rejects(page.save(), /Offline/);
  assert.equal(page.editor, old);
  page.call = async () => {page.editor = {id:"Other"}; return {id:"Offer",revision:"r2"};};
  await page.save();
  assert.equal(page.editor.id, "Other");
  assert.equal(old.revision, "r1");
});

test("Malaysia receipt time is independent of the browser timezone", () => {
  const {page} = editorHarness();
  assert.match(page.date("2026-09-09T06:00:00Z"), /2:00/);
});

test("schedule time controls are not restricted to checkbox size and ended offers can be stopped", () => {
  const pageSource = readFileSync(join(__dirname, "../kopos_connector/kopos/page/jiji_promotions/jiji_promotions.js"), "utf8");
  assert.match(pageSource, /input\[type=checkbox\] \{ width:20px/);
  assert.match(pageSource, /input\[type=time\] \{ width:150px; min-height:44px/);
  assert.match(pageSource, /\.jiji-offer-receipt-row > span:last-child \{ white-space:nowrap; \}/);
  assert.match(pageSource, /\.jiji-offer-panel \.btn \{ min-height:44px; \}/);
  assert.doesNotMatch(pageSource, /\.jiji-offer-choice input \{/);
  assert.match(pageSource, /if \(offer\.published_outlets\.length\) this\.button\(actions, "Stop offer"/);
});

test("a persisted cost result reloads the saved form before showing its outcome", async () => {
  const h = harness();
  h.context.frappe.user.has_role = () => true;
  h.frm.doc = {name:"Saved offer",is_active:1,eligible_pos_profiles:[{pos_profile:"Cafe"}]};
  let reloaded = 0;
  h.frm.reload_doc = async () => { reloaded++; assert.equal(h.messages.length, 0); };
  h.handlers.refresh(h.frm);
  h.buttons.find(b => b.label === "Check costs and profit").callback();
  h.dialogs.at(-1).primary_action({low:10,base:25,high:50,pos_profile:"Cafe"});
  await h.requests[0].callback({message:{status:"blocked",reason:"Cost support is not available"}});
  assert.equal(reloaded, 1);
  assert.equal(h.messages.at(-1).title, "Promotion blocked");
  assert.equal(h.dialogs.at(-1).visible, false);
});

test("cost-result refresh preserves unsaved edits and does not target another offer", async () => {
  for (const changed of ["dirty", "navigated", "reload_failed"]) {
    const h = harness();
    h.context.frappe.user.has_role = () => true;
    h.frm.doc = {name:"Saved offer",is_active:1};
    h.frm.reload_doc = async () => { if (changed === "reload_failed") throw new Error("Offline"); assert.fail("must not reload another or dirty form"); };
    h.handlers.refresh(h.frm);
    h.buttons.find(b => b.label === "Check costs and profit").callback();
    h.dialogs.at(-1).primary_action({low:10,base:25,high:50,pos_profile:"Cafe"});
    if (changed === "dirty") h.frm.is_dirty = () => true;
    if (changed === "navigated") h.frm.doc.name = "Other offer";
    await h.requests[0].callback({message:{status:"blocked",reason:"Missing cost support",economics_hash:"reviewed-hash"}});
    assert.equal(h.messages.at(-1).title, "Promotion blocked");
    if (changed === "dirty") assert.match(h.messages[0], /unsaved edits were kept/);
    if (changed === "reload_failed") assert.match(h.messages[0], /could not be reloaded/);
    if (changed === "navigated") assert.equal(h.frm._kopos_economics_hash, undefined);
  }
});
