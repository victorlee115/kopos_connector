const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");

function harness() {
  const nodes = [], requests = [];
  function element(value) {
    if (typeof value === "object") return value;
    const node = {
      label: "", children: [], handlers: {},
      empty() { this.children = []; return this; },
      append(child) { this.children.push(child); return this; },
      appendTo(parent) { parent.append(this); return this; },
      addClass() { return this; },
      text(label) { this.label = label; return this; },
      on(event, callback) { this.handlers[event] = callback; return this; },
      prop(key, value) { this[key] = value; return this; },
    };
    nodes.push(node);
    return node;
  }
  const context = vm.createContext({
    $: element, __: value => value,
    frappe: { pages: { jiji_money: {} }, call: request => requests.push(request) },
    jiji: { desk: { label: value => element("label").text(value), notice: (title, detail) => element("notice").text(`${title} ${detail}`) } },
  });
  vm.runInContext(readFileSync(join(__dirname, "../kopos_connector/kopos/page/jiji_money/jiji_money.js"), "utf8"), context);
  const page = Object.create(vm.runInContext("JiJiMoney.prototype", context));
  page.body = element("body");
  return { page, nodes, requests };
}

test("older responses and errors cannot overwrite a newer cash page", () => {
  const { page, requests } = harness(), rendered = [];
  page.render = value => rendered.push(value);
  page.load(50);
  page.load(0);
  assert.equal(requests[0].args.offset, 50);
  requests[1].callback({ message: { version: "latest" } });
  requests[0].callback({ message: { version: "stale" } });
  requests[0].error();
  assert.deepEqual(rendered, [{ version: "latest" }]);
  assert.equal(page.body.children.length, 0);
});

test("paging preserves complete counts and offers both navigation directions", () => {
  const { page, nodes } = harness(), offsets = [];
  page.load = offset => offsets.push(offset);
  page.render({ status: "ok", offset: 50, next_offset: 100,
    summary: [{ label: "Did not match", count: 120, attention: true }],
    needs_review: [{ person: "Cashier", difference: "RM 0.01 short" }], missing_count: [] });
  assert.ok(nodes.some(node => node.label === "120"));
  for (const label of ["Older shifts", "Newer shifts"]) {
    const button = nodes.find(node => node.label === label);
    assert.ok(button);
    button.handlers.click({ currentTarget: button });
    assert.equal(button.disabled, true);
  }
  assert.deepEqual(offsets, [100, 0]);
});
