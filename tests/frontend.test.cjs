const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { JSDOM } = require("jsdom");
const source = readFileSync("custom_components/smart_shopping_route/frontend/route-card.js", "utf8");

function fixture(t, changes = {}) {
  const dom = new JSDOM("<!doctype html><body></body>", { runScripts: "outside-only", url: "http://localhost/" });
  const { window } = dom;
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  window.eval(source);
  const now = Date.now() / 1000;
  const data = {
    name: "Example store", available: true, can_control: true, source_features: 71,
    sections: ["Dairy", "Drinks"], prices_enabled: true, price_store: "Example online store",
    history_store: "Example history store", total: 4,
    quality: { price_status: "current", total_items: 1, priced_items: 1, oldest_quote_at: now },
    items: [{ uid: "1", summary: "Milk", notes: "Keep chilled", completed: false, section: "Dairy", stale: false, observed_at: now,
      price: { price: 4, product_name: "Example milk 2L", size: "2L", unit_price: "$2.00/L" }, history: [], store_history: [], pin: null }], ...changes,
  };
  const calls = [];
  const hass = {
    states: { "todo.example": {} },
    callWS: async (message) => { assert.equal(message.type, "smart_shopping_route/get_route"); return structuredClone(data); },
    callService: async (domain, service, fields) => {
      calls.push({ domain, service, fields });
      const item = data.items.find((row) => row.uid === (fields.item || fields.item_uid));
      if (service === "update_item") {
        if (fields.rename !== undefined) item.summary = fields.rename;
        if (fields.description !== undefined) item.notes = fields.description;
        if (fields.status !== undefined) item.completed = fields.status === "completed";
      } else if (service === "set_item_section") item.section = fields.section;
      else if (service === "pin_item_product") item.pin = { query: fields.query, product_id: fields.product_id, grocer_product_id: fields.grocer_product_id };
      else if (service === "unpin_item_product") item.pin = null;
      else if (service === "remove_item") data.items = data.items.filter((row) => row.uid !== fields.item);
      else if (service === "add_item") data.items.push({ uid: "new", summary: fields.item, notes: "", completed: false, section: "Dairy", price: null });
    },
  };
  const card = window.document.createElement("smart-shopping-route-card");
  card.setConfig({ entity: "todo.example" });
  card.hass = hass;
  window.document.body.append(card);
  t.after(() => { card.remove(); dom.window.close(); });
  return { card, data, hass, calls, window };
}
async function settle(card) { for (let i = 0; i < 40; i++) await Promise.resolve(); assert.equal(card._busy || false, false); }
const button = (root, text) => [...root.querySelectorAll("button")].find((element) => element.textContent === text);
const field = (root, label) => [...root.querySelectorAll("label")].find((element) => element.querySelector("span")?.textContent === label)?.querySelector("input,textarea,select");


test("price hierarchy and source notes are distinct; names cannot inject HTML", async (t) => {
  const { card, data } = fixture(t); await settle(card);
  data.items[0].summary = '<img src=x onerror="alert(1)">';
  await card._load();
  assert.match(card.shadowRoot.textContent, /Estimated total/);
  assert.equal(card.shadowRoot.querySelector(".money").textContent, "$4.00");
  assert.equal(card.shadowRoot.querySelector(".notes").textContent, "Keep chilled");
  assert.equal(card.shadowRoot.querySelectorAll("img").length, 0);
  assert.match(card.shadowRoot.querySelector(".name").textContent, /<img/);
});

test("read-only users have no mutating controls", async (t) => {
  const { card } = fixture(t, { can_control: false }); await settle(card);
  assert.equal(button(card.shadowRoot, "Edit"), undefined);
  assert.equal(button(card.shadowRoot, "Add"), undefined);
  assert.equal(card.shadowRoot.querySelector("input[type=checkbox]").disabled, true);
});

test("rename and notes use the source UID and confirm the resulting state", async (t) => {
  const { card, calls, data } = fixture(t); await settle(card);
  button(card.shadowRoot, "Edit").click();
  field(card._dialog, "Item name").value = "Oat milk";
  field(card._dialog, "Notes").value = "Buy organic";
  button(card._dialog, "Save item").click(); await settle(card);
  assert.equal(data.items[0].summary, "Oat milk");
  assert.equal(data.items[0].notes, "Buy organic");
  assert.equal(calls[0].fields.item, "1");
  assert.equal(card._dialog.open, false);
});

test("source failures keep the editor open and preserve user input", async (t) => {
  const { card, hass } = fixture(t); await settle(card);
  hass.callService = async () => { throw new Error("Simulated source failure"); };
  button(card.shadowRoot, "Edit").click();
  field(card._dialog, "Item name").value = "Oat milk";
  button(card._dialog, "Save item").click(); await settle(card);
  assert.equal(card._dialog.open, true);
  assert.equal(field(card._dialog, "Item name").value, "Oat milk");
  assert.match(card._dialogError.textContent, /Simulated source failure/);
});

test("accepted but unobserved updates are not claimed successful or resubmitted", async (t) => {
  const { card, hass } = fixture(t); await settle(card);
  let calls = 0;
  hass.callService = async () => { calls++; };
  button(card.shadowRoot, "Edit").click();
  field(card._dialog, "Item name").value = "Oat milk";
  button(card._dialog, "Save item").click(); await settle(card);
  assert.equal(card._dialog.open, true);
  assert.match(card._dialogError.textContent, /not yet confirmed/);
  assert.equal(calls, 1);
});

test("section corrections require no UID copying", async (t) => {
  const { card, data, calls } = fixture(t); await settle(card);
  button(card.shadowRoot, "Edit").click();
  field(card._dialog, "Section").value = "Drinks";
  button(card._dialog, "Move to section").click(); await settle(card);
  assert.equal(data.items[0].section, "Drinks");
  assert.equal(calls[0].fields.item_uid, "1");
  assert.equal(calls[0].fields.remember, true);
});

test("product pins accept a grocer product URL and can be reset", async (t) => {
  const { card, calls, data } = fixture(t); await settle(card);
  button(card.shadowRoot, "Edit").click();
  field(card._dialog, "Product search").value = "Example oat milk 2L";
  field(card._dialog, "Exact grocer.nz product ID or URL (optional)").value = "https://grocer.nz/product/123/example-product";
  button(card._dialog, "Pin product").click(); await settle(card);
  assert.equal(calls[0].fields.grocer_product_id, 123);
  assert.equal(data.items[0].pin.query, "Example oat milk 2L");
  button(card.shadowRoot, "Edit").click(); button(card._dialog, "Reset product match").click(); await settle(card);
  assert.equal(data.items[0].pin, null);
});

test("history pins need a configured history store", async (t) => {
  const { card, calls } = fixture(t, { history_store: "" }); await settle(card);
  button(card.shadowRoot, "Edit").click();
  field(card._dialog, "Exact grocer.nz product ID or URL (optional)").value = "123";
  button(card._dialog, "Pin product").click(); await settle(card);
  assert.match(card._dialogError.textContent, /Choose a price-history store/);
  assert.equal(calls.length, 0);
});

test("deletion requires confirmation and verifies removal", async (t) => {
  const { card, calls, data } = fixture(t); await settle(card);
  button(card.shadowRoot, "Edit").click();
  button(card._dialog, "Delete item").click(); await settle(card);
  assert.equal(calls.length, 0);
  card._dialog.querySelector('input[type=checkbox]').checked = true;
  button(card._dialog, "Delete item").click(); await settle(card);
  assert.equal(data.items.length, 0);
  assert.equal(card._dialog.open, false);
});

test("duplicate names complete only the selected UID", async (t) => {
  const { card, data } = fixture(t); await settle(card);
  data.items.push({ ...structuredClone(data.items[0]), uid: "2" }); await card._load();
  const checks = card.shadowRoot.querySelectorAll('input[type=checkbox]');
  checks[1].click(); await settle(card);
  assert.equal(data.items[0].completed, false);
  assert.equal(data.items[1].completed, true);
});

test("an add is read back before success", async (t) => {
  const { card, data } = fixture(t); await settle(card);
  const input = card.shadowRoot.querySelector('input[aria-label="New item name"]'); input.value = "Bread";
  card.shadowRoot.querySelector("form.add").dispatchEvent(new card.ownerDocument.defaultView.Event("submit", { bubbles: true, cancelable: true }));
  await settle(card);
  assert.ok(data.items.some((row) => row.summary === "Bread"));
});

for (const status of ["pending", "unavailable"]) test(`${status} is not shown as a zero-dollar basket`, async (t) => {
  const { card } = fixture(t, { total: 0, items: [], quality: { price_status: status, total_items: 3, priced_items: 0 } }); await settle(card);
  assert.match(card.shadowRoot.querySelector(".status").textContent, /No usable basket estimate/);
  assert.doesNotMatch(card.shadowRoot.querySelector(".status").textContent, /\$0\.00/);
});

test("stale prices do not claim a current was-price", async (t) => {
  const { card, data } = fixture(t); await settle(card);
  data.items[0].stale = true; data.items[0].price.was_price = 9;
  data.quality.price_status = "stale"; await card._load();
  assert.match(card.shadowRoot.textContent, /Stale estimate/);
  assert.doesNotMatch(card.shadowRoot.textContent, /Was \$/);
});

test("a pre-mutation read is drained before verifying a successful change", async (t) => {
  const { card, data, hass } = fixture(t); await settle(card);
  const stale = structuredClone(data);
  let release, first = true;
  hass.callWS = async () => {
    if (first) { first = false; return new Promise((resolve) => { release = resolve; }); }
    return structuredClone(data);
  };
  const pending = card._load();
  button(card.shadowRoot, "Edit").click(); field(card._dialog, "Item name").value = "Oat milk";
  button(card._dialog, "Save item").click();
  for (let i = 0; i < 5; i++) await Promise.resolve();
  release(stale); await pending; await settle(card);
  assert.equal(card._dialog.open, false);
  assert.equal(data.items[0].summary, "Oat milk");
  assert.equal(card._dialogError.textContent, "");
});

test("unclassified section shows an explicit choice rather than a blank select", async (t) => {
  const { card, data } = fixture(t); await settle(card);
  data.items[0].section = "Unclassified / Other"; await card._load();
  button(card.shadowRoot, "Edit").click();
  const select = field(card._dialog, "Section");
  assert.equal(select.getAttribute("aria-label"), "Section");
  assert.match(select.selectedOptions[0].textContent, /Current: Unclassified/);
  button(card._dialog, "Move to section").click(); await settle(card);
  assert.match(card._dialogError.textContent, /Choose a section/);
});

test("raw names beginning with a section label are forwarded with one view prefix", async (t) => {
  const { card, data, hass } = fixture(t); await settle(card);
  data.items[0].display_prefix = "Dairy · "; await card._load();
  const original = hass.callService; let incoming;
  hass.callService = async (domain, service, fields) => {
    incoming = fields.rename;
    await original(domain, service, { ...fields, rename: fields.rename.replace(/^Dairy · /, "") });
  };
  button(card.shadowRoot, "Edit").click(); field(card._dialog, "Item name").value = "Dairy · Milk";
  button(card._dialog, "Save item").click(); await settle(card);
  assert.equal(incoming, "Dairy · Dairy · Milk");
  assert.equal(data.items[0].summary, "Dairy · Milk");
  assert.equal(card._dialog.open, false);
});

test("background price refresh does not erase unsaved notes", async (t) => {
  const { card, data } = fixture(t); await settle(card);
  button(card.shadowRoot, "Edit").click(); field(card._dialog, "Notes").value = "Unsaved text";
  data.items[0].price.price = 3.5; await card._load();
  assert.equal(field(card._dialog, "Notes").value, "Unsaved text");
});

test("an unmatched row explains itself the same way the native list does", async (t) => {
  const { card, data } = fixture(t); await settle(card);
  data.items[0].price = null; data.items[0].stale = false;
  data.quality.unmatched_items = 1; data.quality.priced_items = 0;
  await card._load();
  assert.match(card.shadowRoot.textContent, /no price match — refresh, rename or pin this item/);
  assert.doesNotMatch(card.shadowRoot.textContent, /Unpriced/);
});
