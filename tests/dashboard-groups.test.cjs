const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

// Minimal DOM for testing the actual renderer and handlers without a browser.
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.events = {}; }
  append(...elements) { this.children.push(...elements); }
  replaceChildren(...elements) { this.children = elements; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { this.events[name] = handler; }
  set innerHTML(_) { throw Error("Untrusted names must use textContent"); }
  focus() {}
}

function setup(fail = false) {
  const nodes = Object.fromEntries(["#managed-api-list", "#managed-api-empty", ".nav-api-count", "#api-group-feedback"].map((key) => [key, new Element("div")]));
  const group = { id: "grp_test", name: "<img onerror=alert(1)>", collapsed: true, api_ids: [1] };
  const calls = [];
  const context = vm.createContext({
    document: { querySelector: (key) => nodes[key], createElement: (tag) => new Element(tag), createTextNode: (text) => ({ textContent: text }) },
    managedApis: [1, 2].map((id) => ({ id, name: `Key ${id}`, slug: `key-${id}`, status: "active", provider: "codex", model: "fixture", reasoning_effort: "low" })),
    managedApiGroups: [group],
    tr: (key) => key, currentLocale: () => "it-IT",
    option: (value, label) => Object.assign(new Element("option"), { value, textContent: label }),
    toggleManagedApi() {}, openApiConfigDialog() {}, deleteManagedApi() {},
    dashboardRequest: async (url, options) => { calls.push([url, JSON.parse(options.body)]); if (fail) throw Error("Save failed"); },
  });
  const source = readFileSync("app/static/dashboard.js", "utf8");
  const renderer = source.slice(source.indexOf("function renderManagedApis()"), source.indexOf("async function loadManagedApis()"));
  vm.runInContext(renderer + '\nfunction setGroupFeedback(message) { document.querySelector("#api-group-feedback").textContent = message; }\nrenderManagedApis();', context);
  return { nodes, group, calls };
}

test("group hides its keys while ungrouped keys stay visible, and expands on a saved toggle", async () => {
  const { nodes, group, calls } = setup();
  const [section, ungrouped] = nodes["#managed-api-list"].children;
  const [heading, , body] = section.children;
  const toggle = heading.children[0];
  assert.equal(body.hidden, true);
  assert.equal(body.children.length, 1);
  assert.equal(ungrouped.children.length, 2); // Ungrouped heading + second key.
  assert.equal(toggle.attributes["aria-expanded"], "false");
  assert.equal(toggle.children[0].textContent, group.name);
  await toggle.events.click();
  assert.equal(body.hidden, false);
  assert.equal(toggle.attributes["aria-expanded"], "true");
  assert.equal(group.collapsed, false);
  assert.deepEqual(calls, [["/api/gateway-apis/groups/grp_test", { collapsed: false }]]);
});

test("failed collapse save leaves the group state intact and exposes feedback", async () => {
  const { nodes, group } = setup(true);
  const section = nodes["#managed-api-list"].children[0];
  const toggle = section.children[0].children[0];
  await toggle.events.click();
  assert.equal(group.collapsed, true);
  assert.equal(section.children[2].hidden, true);
  assert.equal(toggle.disabled, false);
  assert.equal(nodes["#api-group-feedback"].textContent, "Save failed");
});
