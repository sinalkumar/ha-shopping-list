/* Optional, dependency-free Lovelace card. All untrusted content is textContent. */
const money = (amount) => new Intl.NumberFormat("en-NZ", { style: "currency", currency: "NZD" }).format(amount);
const date = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString() : "not yet checked";
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};

class SmartShoppingRouteCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    const style = node("style");
    style.textContent = `
      :host { display:block; color:var(--primary-text-color,#202020); }
      ha-card { display:block; padding:16px; background:var(--ha-card-background,var(--card-background-color,#fff)); border-radius:12px; }
      h2 { font-size:20px; margin:0; } h3 { font-size:16px; margin:20px 0 4px; }
      .header,.row,.actions,.price { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
      .header { justify-content:space-between; } .row { flex-wrap:nowrap; align-items:flex-start; padding:12px 0; border-bottom:1px solid var(--divider-color,#ddd); }
      .body { flex:1; min-width:0; } .name { font-size:16px; overflow-wrap:anywhere; }
      .price { margin-top:6px; font-size:15px; } .money { font-weight:700; font-size:18px; }
      .muted { font-size:13px; color:var(--secondary-text-color,#555); overflow-wrap:anywhere; }
      .status { background:var(--secondary-background-color,#eee); padding:12px; border-radius:8px; margin:12px 0; }
      .warning { color:var(--error-color,#a00); font-weight:600; }
      .notes { white-space:pre-wrap; font-size:14px; margin-top:6px; overflow-wrap:anywhere; }
      button,input,select,textarea { font:inherit; box-sizing:border-box; color:inherit; }
      button { min-height:44px; min-width:44px; padding:8px 12px; border:1px solid var(--divider-color,#aaa); border-radius:8px; background:var(--secondary-background-color,#eee); cursor:pointer; }
      button:disabled { opacity:.55; cursor:default; } input[type=checkbox] { width:24px; height:24px; margin:10px; flex-shrink:0; }
      input:not([type=checkbox]),textarea,select { width:100%; min-height:44px; padding:8px; border:1px solid var(--divider-color,#aaa); border-radius:6px; background:var(--card-background-color,#fff); }
      /* A disabled field must read as disabled. Without this the border and
         background above outrank the browser's own styling and an unwritable
         notes box looks exactly like one that saves. */
      input:disabled,select:disabled,textarea:disabled { opacity:.6; background:var(--secondary-background-color,#eee); color:var(--secondary-text-color,#555); cursor:not-allowed; }
      label { display:block; margin:12px 0; } label span { display:block; margin-bottom:6px; } textarea { min-height:88px; }
      form.add { display:flex; gap:8px; } form.add input { flex:1; min-width:0; }
      dialog { color:inherit; background:var(--card-background-color,#fff); border:1px solid var(--divider-color,#aaa); border-radius:12px; width:520px; max-width:calc(100vw - 24px); max-height:calc(100dvh - 32px); padding:16px; box-sizing:border-box; }
      dialog::backdrop { background:#0008; } .close { float:right; } details { margin-top:12px; } summary { min-height:36px; cursor:pointer; }
      .completed .name { text-decoration:line-through; } .error:empty { display:none; }
      .status-note { font-size:14px; margin:8px 0; color:var(--secondary-text-color,#555); } .status-note:empty { display:none; }
      .metadata { margin-top:4px; } .metadata summary { min-height:36px; display:list-item; }
      .status details { margin-top:4px; } .status details summary { min-height:32px; display:list-item; }
      .price { row-gap:2px; } .identity { margin-top:2px; }
      .error { white-space:pre-wrap; margin:8px 0; color:var(--error-color,#a00); }
      @media(max-width:440px) { ha-card {padding:12px;} .row {gap:4px;} .edit {padding:6px;} }
    `;
    this._content = node("div");
    this._dialog = node("dialog"); this._dialog.setAttribute("aria-label", "Edit shopping item");
    this.shadowRoot.append(style, this._content, this._dialog);
    // Render-surviving state. A refresh rebuilds the card, so anything the
    // household has typed but not yet submitted has to live outside the DOM.
    this._draft = { add: "" };
    this._openDetails = new Set();
    this._dialogBaseline = null;
    this._confirmDiscard = false;
    this._dialog.addEventListener("cancel", (event) => {
      if (this._dialogDirty()) { event.preventDefault(); this._warnDiscard(); }
    });
  }

  setConfig(config) {
    if (!config.entity || !config.entity.startsWith("todo.")) throw new Error("Choose a Smart Shopping Route todo entity");
    this._config = { ...config };
    this._data = null;
    this._attempted = false;
    this._lastState = null;
    this._draft = { add: "" };
    this._openDetails = new Set();
    this._load();
  }
  static getStubConfig() { return { entity: "todo.shopping_route" }; }
  getCardSize() { return Math.max(3, (this._data?.items.length || 4) * 1.4); }
  set hass(hass) {
    this._hass = hass;
    const state = hass.states?.[this._config?.entity];
    if (state !== this._lastState || (!this._data && !this._attempted)) {
      this._lastState = state;
      this._load();
    }
  }
  connectedCallback() {
    if (!this._timer) this._timer = setInterval(() => this._load(), 60000);
    this._load();
  }
  disconnectedCallback() { clearInterval(this._timer); this._timer = null; }

  async _load() {
    if (!this._hass || !this._config || !this.isConnected) return;
    this._again = true;
    if (this._loading) return this._loading;
    this._attempted = true;
    // All callers await the entire drain, including a fresh read queued after
    // an in-flight pre-mutation response. Never verify against stale data.
    this._loading = (async () => {
      do {
        this._again = false;
        const entity = this._config.entity;
        try {
          const data = await this._hass.callWS({ type: "smart_shopping_route/get_route", entity_id: entity });
          if (entity === this._config.entity) { this._data = data; this._error = ""; this._disconnected = false; }
        } catch (error) { this._disconnected = true; this._error = error.message || "Cannot load route. Check permissions and integration availability."; }
        this._render();
      } while (this._again && this.isConnected);
    })().finally(() => { this._loading = null; });
    return this._loading;
  }

  _button(text, action, className) {
    const button = node("button", text, className);
    button.type = "button";
    button.disabled = this._busy || this._disconnected || !this._data?.can_control;
    button.addEventListener("click", action);
    return button;
  }
  _can(feature) { return !this._disconnected && this._data?.can_control && (this._data.source_features & feature) !== 0; }
  async _action(action, { close = false, verify = null } = {}) {
    if (this._busy) return;
    this._busy = true;
    this._dialogError && (this._dialogError.textContent = "");
    this._dialogStatus && (this._dialogStatus.textContent = "");
    this.shadowRoot.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    let success = false;
    try {
      await action();
      await this._load();
      if (this._error || (verify && !verify(this._data))) throw new Error("Change accepted, but the source update is not yet confirmed. Refresh before repeating the action.");
      success = true;
      if (close) this._dialog.close();
    } catch (error) {
      const text = error.message || "Action failed. Your changes have not been confirmed.";
      if (this._dialog.open) this._dialogError.textContent = text;
      else this._error = text;
    } finally {
      this._busy = false;
      // Re-enable controls in an open editor without discarding user input.
      this._dialog.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      this._render();
    }
    return success;
  }
  _service(service, data = {}) {
    return this._hass.callService("smart_shopping_route", service, { entity_id: this._config.entity, ...data });
  }

  _render() {
    const focused = this.shadowRoot.activeElement;
    const addHadFocus = focused !== null && focused === this._content.querySelector("form.add input");
    const card = node("ha-card");
    const header = node("div", undefined, "header");
    header.append(node("h2", this._data?.name || "Shopping route"), this._button("Refresh", () => this._action(() => this._service("refresh"))));
    card.append(header);
    const error = node("div", this._error || "", "error"); error.setAttribute("role", "alert"); card.append(error);
    const data = this._data;
    if (!data) { card.append(node("p", this._error ? "Route unavailable" : "Loading route…")); this._content.replaceChildren(card); return; }
    const quality = data.quality;
    if (data.prices_enabled) {
      let label = `${quality.priced_items} of ${quality.total_items} items priced`;
      if (quality.price_status === "current" || quality.price_status === "empty") label += ` · Estimated total ${money(data.total)}`;
      else if (quality.price_status === "partial") label += ` · Partial estimate ${money(data.total)} — unpriced items excluded`;
      else if (quality.price_status === "stale") label += ` · Stale estimate ${money(data.total)} — refresh needed`;
      else label += " · No usable basket estimate yet";
      const status = node("div", undefined, "status"); status.setAttribute("role", "status");
      status.append(node("div", label), node("div", `${data.price_store} · Oldest quote: ${date(quality.oldest_quote_at)}`, "muted"));
      if (data.history_store) status.append(node("div", `History: ${data.history_store}`, "muted"));
      if (data.legacy_history_preserved) {
        // Kept, not dropped: it is a one-time explanation, so it collapses to a
        // single line instead of pushing the first item off the visible card.
        const legacy = node("details", undefined, "metadata");
        legacy.append(node("summary", "Older price history is kept but not compared", "muted"));
        legacy.append(node("p", "Its product or store identity is unverified, so it is not plotted against current prices. It stays until newer history replaces it.", "muted"));
        status.append(legacy);
      }
      card.append(status);
    }
    if (!data.available) card.append(node("p", "Source list unavailable. Saved prices may be stale.", "warning"));
    if (this._can(1)) {
      const form = node("form", undefined, "add");
      const input = node("input"); input.required = true; input.placeholder = "Add item"; input.setAttribute("aria-label", "New item name");
      input.value = this._draft.add;
      input.addEventListener("input", () => { this._draft.add = input.value; });
      const add = this._button("Add", () => form.requestSubmit());
      form.append(input, add);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const summary = input.value.trim();
        if (!summary) return;
        const oldCount = data.items.filter((item) => item.summary === summary).length;
        const added = await this._action(() => this._hass.callService("todo", "add_item", { entity_id: this._config.entity, item: summary }), { verify: (updated) => updated.items.filter((item) => item.summary === summary).length > oldCount });
        // Keep the text when the add was not confirmed so it can be retried,
        // and clear it only once the source list actually shows the new item.
        if (added) { this._draft.add = ""; this._render(); }
      });
      card.append(form);
    }
    let section;
    for (const item of data.items) {
      const nextSection = item.completed ? "Completed" : item.section;
      if (section !== nextSection) { card.append(node("h3", nextSection)); section = nextSection; }
      const row = node("div", undefined, `row${item.completed ? " completed" : ""}`);
      const check = node("input"); check.type = "checkbox"; check.checked = item.completed;
      check.disabled = this._busy || !this._can(4); check.setAttribute("aria-label", `${item.completed ? "Reopen" : "Complete"} ${item.summary}`);
      check.addEventListener("change", async () => {
        const completed = check.checked;
        const ok = await this._action(() => this._hass.callService("todo", "update_item", { entity_id: this._config.entity, item: item.uid, status: completed ? "completed" : "needs_action" }), { verify: (updated) => updated.items.some((row) => row.uid === item.uid && row.completed === completed) || (completed && !updated.items.some((row) => row.uid === item.uid)) });
        if (!ok) check.checked = item.completed;
      });
      const body = node("div", undefined, "body"); body.append(node("div", item.summary, "name"));
      if (item.pin) body.append(node("div", "Pinned product match", "muted"));
      if (item.price) {
        const price = node("div", undefined, "price"); price.append(node("span", money(item.price.price), "money"));
        if (item.price.size) price.append(node("span", item.price.size));
        if (item.price.unit_price) price.append(node("span", item.price.unit_price));
        body.append(price);
        // The matched product belongs on the row, not behind a disclosure: it is
        // what the price is for, and the native list already shows it.
        if (item.price.product_name) {
          const identity = node("div", `${item.price.product_name}`, "muted identity");
          if (item.price.from_store_file) identity.append(" · store price file");
          body.append(identity);
        }
        if (item.stale) body.append(node("div", `Stale · checked ${date(item.observed_at)}`, "warning"));
        else if (item.price.was_price > item.price.price) body.append(node("div", `Was ${money(item.price.was_price)}`, "muted"));
        else if (item.previous?.price > item.price.price) body.append(node("div", `Previously seen ${money(item.previous.price)} · ${date(item.previous.ts)}`, "muted"));
        if (item.alternatives?.length > 1) {
          // Same ranking the native list prints, shown rather than described: a
          // shopper deciding on the card should not have to open another view.
          const better = node("details", undefined, "metadata");
          better.append(node("summary", "Better value per unit"));
          const list = node("ul");
          for (const option of item.alternatives) {
            const unit = option.unit_price || money(option.price);
            const chosen = option.product_id === item.price.product_id ? " (this item)" : "";
            list.append(node("li", `${unit}${chosen} · ${option.product_name}`));
          }
          better.append(list);
          body.append(better);
        }
        const details = node("details", undefined, "metadata");
        if (this._openDetails.has(item.uid)) details.open = true;
        details.addEventListener("toggle", () => { if (details.open) this._openDetails.add(item.uid); else this._openDetails.delete(item.uid); });
        details.append(node("summary", "Price history"));
        for (const [points, label] of [[item.history, "Observed price history"], [item.store_history, `Physical-store history: ${data.history_store} (not compared with online quotes)`]]) {
          if (points?.length) {
            details.append(node("p", label, "muted"));
            const list = node("ul");
            for (const point of points.slice(-10).reverse()) list.append(node("li", `${date(point.ts)} · ${money(point.price)}`));
            details.append(list);
          }
        }
        body.append(details);
      } else if (data.prices_enabled && !item.completed) {
        // Name only what this card can actually do. Pinning is a service, and
        // telling someone here to "pin it" points at a control that is not on
        // the screen.
        body.append(node("div", "no price match — refresh, or edit the item to the words the store uses", "muted"));
        const last = item.history?.[item.history.length - 1];
        if (last) body.append(node("div", `Last seen ${money(last.price)} · ${date(last.ts)}`, "muted"));
      }
      if (item.notes) body.append(node("div", item.notes, "notes"));
      row.append(check, body);
      if (this._can(4)) {
        const edit = this._button("Edit", () => this._edit(item), "edit"); edit.setAttribute("aria-label", `Edit ${item.summary}`); row.append(edit);
      }
      card.append(row);
    }
    // An unavailable source reports nothing, which is not the same as an empty
    // list: saying "empty" there contradicts the warning above it.
    if (!data.items.length && data.available) card.append(node("p", "Your shopping list is empty."));
    // Only remember expansions for rows that still exist, so a long session
    // cannot accumulate every item the household ever opened. An unavailable
    // source reports no items, which is not a removal, so keep them then.
    if (data.available) {
      const uids = new Set(data.items.map((item) => item.uid));
      for (const uid of this._openDetails) if (!uids.has(uid)) this._openDetails.delete(uid);
    }
    this._content.replaceChildren(card);
    if (addHadFocus) {
      const next = this._content.querySelector("form.add input");
      if (next) { next.focus(); const end = next.value.length; next.setSelectionRange?.(end, end); }
    }
  }

  _field(label, value = "", tag = "input", container = null) {
    const wrapper = node("label"); wrapper.append(node("span", label));
    const input = node(tag); input.setAttribute("aria-label", label); input.value = value; wrapper.append(input); (container || this._dialog).append(wrapper);
    return input;
  }
  _edit(item) {
    this._dialog.replaceChildren();
    this._confirmDiscard = false;
    const close = this._button("Close", () => this._closeDialog(), "close");
    this._dialog.append(close, node("h2", "Edit shopping item"));
    this._dialogError = node("div", "", "error"); this._dialogError.setAttribute("role", "alert"); this._dialog.append(this._dialogError);
    this._dialogStatus = node("div", "", "status-note"); this._dialogStatus.setAttribute("role", "status"); this._dialog.append(this._dialogStatus);
    const name = this._field("Item name", item.summary);
    const notes = this._field("Notes", item.notes, "textarea");
    notes.disabled = !this._can(64);
    this._dialogBaseline = { name: item.summary, notes: item.notes || "" };
    for (const typed of [name, notes]) typed.addEventListener("input", () => { this._confirmDiscard = false; this._dialogStatus.textContent = ""; });
    if (!this._can(64)) this._dialog.append(node("p", "This source list does not support notes. Price information is read-only.", "muted"));
    this._dialog.append(this._button("Save item", () => {
      if (!name.value.trim()) { this._dialogError.textContent = "An item name is required."; return; }
      const summary = name.value.trim();
      const update = { entity_id: this._config.entity, item: item.uid, rename: (item.display_prefix || "") + summary };
      if (this._can(64)) update.description = notes.value;
      this._action(() => this._hass.callService("todo", "update_item", update), { close: true, verify: (data) => data.items.some((row) => row.uid === item.uid && row.summary === summary && (!this._can(64) || row.notes === update.description)) });
    }));
    const section = this._field("Section", "", "select");
    const placeholder = node("option", `Current: ${item.section} — choose section`); placeholder.value = ""; placeholder.disabled = true; section.append(placeholder);
    for (const value of this._data.sections) { const option = node("option", value); option.value = value; section.append(option); }
    // Left unchosen on purpose. Preselecting the current section makes the
    // placeholder unreachable and turns "Move to section" into a no-op move the
    // household never asked for.
    section.value = "";
    this._dialog.append(this._button("Move to section", async () => {
      if (!section.value) { this._dialogError.textContent = "Choose a section first."; return; }
      const target = section.value;
      const moved = await this._action(() => this._service("set_item_section", { item_uid: item.uid, section: target, remember: true }), { verify: (data) => data.items.some((row) => row.uid === item.uid && row.section === target) });
      if (!moved) return;
      // Independent of the name/notes draft, so the editor stays open rather
      // than silently throwing those edits away.
      placeholder.textContent = `Current: ${target} — choose section`;
      section.value = "";
      this._dialogStatus.textContent = this._dialogDirty() ? `Moved to ${target}. Your unsaved name and notes are still here.` : `Moved to ${target}.`;
    }));
    if (this._data.prices_enabled) {
      // Collapsed by default: product matching is a power-user correction, and
      // the editor is a long scroll on a phone.
      const advanced = node("details", undefined, "advanced");
      if (item.pin) advanced.open = true;
      advanced.append(node("summary", "Wrong product?"));
      advanced.append(node("p", "Pins apply to every occurrence of this item name in this profile. They do not rename the source item.", "muted"));
      const query = this._field("Product search", item.pin?.query || item.summary, "input", advanced);
      const product = this._field("Exact retailer product ID (optional)", item.pin?.product_id || "", "input", advanced);
      const history = this._field("Exact grocer.nz product ID or URL (optional)", item.pin?.grocer_product_id || "", "input", advanced);
      // No numeric keypad hint: the field takes an ID or a grocer.nz URL, and
      // a digits-only keypad would make the URL the label offers untypeable.
      advanced.append(this._button("Pin product", async () => {
        const historyMatch = history.value.trim().match(/^(?:https:\/\/grocer\.nz\/product\/)?([1-9]\d*)(?:\/[^\s]*)?$/);
        if (history.value.trim() && !historyMatch) { this._dialogError.textContent = "Enter a positive history product ID or a grocer.nz product URL."; return; }
        if (historyMatch && !this._data.history_store) { this._dialogError.textContent = "Choose a price-history store in integration settings before pinning history."; return; }
        const pin = { item_uid: item.uid, query: query.value.trim() };
        if (product.value.trim()) pin.product_id = product.value.trim();
        if (historyMatch) pin.grocer_product_id = Number(historyMatch[1]);
        const pinned = await this._action(() => this._service("pin_item_product", pin), { verify: (data) => data.items.some((row) => row.uid === item.uid && row.pin?.query === pin.query && (!pin.product_id || row.pin.product_id === pin.product_id) && (!pin.grocer_product_id || row.pin.grocer_product_id === pin.grocer_product_id)) });
        if (pinned) this._dialogStatus.textContent = this._dialogDirty() ? "Product match pinned. Your unsaved name and notes are still here." : "Product match pinned.";
      }), this._button("Reset product match", async () => {
        const cleared = await this._action(() => this._service("unpin_item_product", { item_uid: item.uid }), { verify: (data) => data.items.some((row) => row.uid === item.uid && !row.pin) });
        if (!cleared) return;
        query.value = item.summary; product.value = ""; history.value = "";
        this._dialogStatus.textContent = this._dialogDirty() ? "Product match cleared. Your unsaved name and notes are still here." : "Product match cleared.";
      }));
      this._dialog.append(advanced);
    }
    if (this._can(2)) {
      const confirm = node("label"); const check = node("input"); check.type = "checkbox"; confirm.append(check, document.createTextNode("Confirm deletion from the source list")); this._dialog.append(confirm);
      this._dialog.append(this._button("Delete item", () => {
        if (!check.checked) { this._dialogError.textContent = "Tick the deletion confirmation first."; return; }
        this._action(() => this._hass.callService("todo", "remove_item", { entity_id: this._config.entity, item: item.uid }), { close: true, verify: (data) => !data.items.some((row) => row.uid === item.uid) });
      }));
    }
    this._dialog.showModal(); name.focus();
  }

  _dialogDirty() {
    if (!this._dialog?.open || !this._dialogBaseline) return false;
    const name = this._dialog.querySelector('[aria-label="Item name"]');
    const notes = this._dialog.querySelector('[aria-label="Notes"]');
    return (name !== null && name.value !== this._dialogBaseline.name) || (notes !== null && notes.value !== this._dialogBaseline.notes);
  }
  _warnDiscard() {
    this._confirmDiscard = true;
    this._dialogStatus.textContent = "Unsaved changes. Save item to keep them, or choose Close again to discard them.";
  }
  _closeDialog() {
    // Ask once instead of dropping typed notes on a stray Close or Escape.
    if (this._dialogDirty() && !this._confirmDiscard) { this._warnDiscard(); return; }
    this._dialog.close();
  }
}

if (!customElements.get("smart-shopping-route-card")) customElements.define("smart-shopping-route-card", SmartShoppingRouteCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "smart-shopping-route-card")) window.customCards.push({ type: "smart-shopping-route-card", name: "Smart Shopping Route", description: "Shopping route with read-only prices, notes, section corrections and product pins." });
