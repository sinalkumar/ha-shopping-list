"""Disposable synthetic UI harness. Never connects to a real Home Assistant.

Run from the repository with tests and test dependencies on PYTHONPATH:
  python3 tests/frontend_fixture.py
Then open http://127.0.0.1:8766/ . Uses real entity/coordinator/payload code,
with an in-memory source adapter and deterministic classifier/retailer fixtures.
"""

import asyncio
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.components.todo import TodoItem, TodoItemStatus

import conftest
from smart_shopping_route.models import RouteSection
from smart_shopping_route.prices import PriceQuote
from smart_shopping_route.todo import SmartShoppingRouteTodo
from smart_shopping_route.ui import CARD_URL, route_payload
from test_coordinator import item, make_coordinator
from test_prices import make_price_coordinator

ROOT = Path(__file__).resolve().parents[1]
PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Shopping route — synthetic review fixture</title>
<style>body{margin:0;padding:16px;font:16px system-ui;background:#f0f2f5;color:#202020}main{max-width:640px;margin:auto}nav{display:flex;gap:12px;flex-wrap:wrap;padding:12px 0}a{min-height:32px}h1{font-size:18px}</style></head><body><main><h1>Synthetic shopping route — no household data</h1>
<nav><a href="/">Current</a><a href="/quality">Quality comparison</a><a href="/?mode=partial">Partial</a><a href="/?mode=stale">Stale</a><a href="/?mode=unavailable">Unavailable</a><a href="/?mode=readonly">Read only</a><a href="/?mode=failure">Save failure</a></nav><div id="card"></div><details><summary>Verified fixture actions</summary><pre id="events" style="white-space:pre-wrap"></pre></details></main>
<script type="module" src="/smart_shopping_route/route-card.js"></script><script type="module">
await customElements.whenDefined('smart-shopping-route-card');
const mode=new URLSearchParams(location.search).get('mode')||'';
const card=document.createElement('smart-shopping-route-card');card.setConfig({entity:'todo.demo'});
const request=async(path,options={})=>{const r=await fetch(path+'?mode='+mode,options);const result=await r.json();if(!r.ok)throw new Error(result.error);return result;};
const hass={states:{'todo.demo':{}},callWS:async()=>{const data=await request('/api/route');document.querySelector('#events').textContent=(data._fixture_events||[]).join('\\n');return data;},callService:async(domain,service,data)=>{await request('/api/service',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({domain,service,data})});hass.states['todo.demo']={};card.hass=hass;}};
card.hass=hass;document.querySelector('#card').append(card);
</script></body></html>"""


async def main():
    patch = pytest.MonkeyPatch()
    conftest._fake_price_store.__wrapped__(patch)
    coordinator = make_coordinator(
        [
            item("milk", "Milk"),
            item("juice", "Juice multipack"),
            item("vitamins", "Vitamins"),
        ],
        patch,
    )
    coordinator.sections += (RouteSection("Drinks"), RouteSection("Pantry"))
    coordinator.hass.loop = asyncio.get_running_loop()
    coordinator.hass.is_stopping = False
    quotes = {
        "Milk": PriceQuote(
            "paknsave",
            "m1",
            "Example fresh milk 2L",
            4,
            size="2L",
            unit_price="$2.00/L",
        ),
        "Juice multipack": PriceQuote(
            "paknsave",
            "j1",
            "Example juice 6 x 250ml",
            9,
            was_price=11,
            size="6 x 250ml",
            unit_price="$6.00/L",
        ),
        "Vitamins": PriceQuote(
            "paknsave",
            "v1",
            "Example gummies 110 Pack",
            22,
            size="110Pack",
            unit_price="$0.20/ea",
        ),
    }
    prices, _ = make_price_coordinator(patch, [], quotes)
    prices.hass.loop = asyncio.get_running_loop()
    prices.hass.is_stopping = False
    prices._summaries = coordinator._priced_summaries
    prices.entry.data.update({"grocer_store_id": 228})
    prices.grocer_store_id = 228
    # No network history in a deterministic frontend fixture.
    prices.async_sync_grocer = _noop
    prices._async_backfill = _noop
    coordinator.prices = prices
    coordinator.entry.data.update({
        "price_store_name": "Example current-price store",
        "show_completed": True,
    })

    class Source:
        entity_id = "todo.synthetic_source"
        supported_features = 71
        available = True

        def __init__(self):
            self.rows = copy.deepcopy(coordinator.items)
            self.rows[0].description = "Keep chilled\nSource notes stay editable."

        def items(self):
            return self.rows

        def publish(self):
            coordinator._source_updated(copy.deepcopy(self.rows))

        async def async_update(self, updated):
            self.rows = [
                copy.deepcopy(updated) if row.uid == updated.uid else row
                for row in self.rows
            ]
            self.publish()

        async def async_create(self, new):
            new.uid = f"new-{len(self.rows)}"
            new.status = TodoItemStatus.NEEDS_ACTION
            self.rows.append(copy.deepcopy(new))
            self.publish()

        async def async_delete(self, uids):
            self.rows = [row for row in self.rows if row.uid not in uids]
            self.publish()

    coordinator.source = Source()
    coordinator.items = copy.deepcopy(coordinator.source.rows)
    entity = SmartShoppingRouteTodo(coordinator.entry, coordinator)
    await prices.async_refresh()
    for uid, section in (
        ("milk", "Dairy"),
        ("juice", "Drinks"),
        ("vitamins", "Pantry"),
    ):
        await coordinator.async_set_section(uid, section, remember=False)

    async def settle():
        while coordinator._tasks:
            await asyncio.gather(*list(coordinator._tasks), return_exceptions=True)

    events = []

    async def payload(mode):
        await settle()
        original, rows = prices._snapshot, coordinator.items
        prices._snapshot = copy.deepcopy(original)
        try:
            if mode == "stale":
                prices.snapshot.errors.update(prices.snapshot.quotes)
            elif mode == "unavailable":
                prices.snapshot.quotes.clear()
            elif mode == "partial":
                prices.snapshot.active["unmatched item"] = 1
                prices.snapshot.failed.add("unmatched item")
                coordinator.items = [*rows, item("missing", "Unmatched item")]
            result = route_payload(coordinator, can_control=mode != "readonly")
            result["_fixture_events"] = events[-12:]
            return result
        finally:
            prices._snapshot, coordinator.items = original, rows

    async def service(message, mode):
        if mode == "readonly":
            raise ValueError("Read-only fixture: mutations denied")
        if mode == "failure":
            raise ValueError("Simulated source failure; your changes were not saved")
        domain, name, fields = message["domain"], message["service"], message["data"]
        uid = fields.get("item") or fields.get("item_uid")
        if domain == "todo":
            if name == "add_item":
                await entity.async_create_todo_item(TodoItem(summary=fields["item"]))
            elif name == "remove_item":
                await entity.async_delete_todo_items([uid])
            elif name == "update_item":
                update = copy.deepcopy(
                    next(row for row in entity.todo_items if row.uid == uid)
                )
                if "rename" in fields:
                    update.summary = fields["rename"]
                if "description" in fields:
                    update.description = fields["description"]
                if "status" in fields:
                    update.status = TodoItemStatus(fields["status"])
                await entity.async_update_todo_item(update)
        elif name == "refresh":
            await prices.async_refresh(force=True)
        elif name == "set_item_section":
            await coordinator.async_set_section(
                uid, fields["section"], remember=fields.get("remember", True)
            )
        elif name == "pin_item_product":
            raw = next(row.summary for row in coordinator.items if row.uid == uid)
            await prices.async_pin(
                raw,
                query=fields.get("query", ""),
                product_id=fields.get("product_id"),
                grocer_product_id=fields.get("grocer_product_id"),
            )
        elif name == "unpin_item_product":
            raw = next(row.summary for row in coordinator.items if row.uid == uid)
            await prices.async_unpin(raw)
        else:
            raise ValueError("Unknown fixture action")
        await settle()
        if name == "pin_item_product":
            events.append(
                f"Verified saved product pin: {prices.pin_for(raw).as_dict()}"
            )
        elif name == "unpin_item_product":
            events.append(f"Verified reset product pin: {prices.pin_for(raw)!r}")
        else:
            events.append(
                f"Verified {name}; source UIDs: {[row.uid for row in coordinator.source.rows]}"
            )
        return {"ok": True}

    loop = asyncio.get_running_loop()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, content, kind="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content.encode() if isinstance(content, str) else content)

        def do_GET(self):
            parsed = urlparse(self.path)
            mode = parse_qs(parsed.query).get("mode", [""])[0]
            if parsed.path == "/quality":
                comparison = (
                    '<!doctype html><title>Synthetic quality comparison</title><h1>Actual card quality states: synthetic backend</h1><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">'
                    + "".join(
                        f'<section><h2>{state.title()}</h2><iframe title="{state}" src="/?mode={state}" style="width:100%;height:920px;border:1px solid #bbb"></iframe></section>'
                        for state in ("partial", "stale", "unavailable")
                    )
                    + "</div>"
                )
                self.respond(200, comparison, "text/html; charset=utf-8")
            elif parsed.path == "/":
                self.respond(200, PAGE, "text/html; charset=utf-8")
            elif parsed.path == CARD_URL:
                self.respond(
                    200,
                    (
                        ROOT
                        / "custom_components/smart_shopping_route/frontend/route-card.js"
                    ).read_bytes(),
                    "text/javascript",
                )
            elif parsed.path == "/api/route":
                try:
                    result = asyncio.run_coroutine_threadsafe(
                        payload(mode), loop
                    ).result(timeout=15)
                    self.respond(200, json.dumps(result))
                except Exception as err:
                    self.respond(500, json.dumps({"error": str(err)}))
            else:
                self.respond(404, "{}")

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/api/service" or self.headers.get("Origin") not in (
                None,
                "http://127.0.0.1:8766",
            ):
                self.respond(403, "{}")
                return
            mode = parse_qs(parsed.query).get("mode", [""])[0]
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length < 10000:
                    raise ValueError("Invalid request size")
                message = json.loads(self.rfile.read(length))
                result = asyncio.run_coroutine_threadsafe(
                    service(message, mode), loop
                ).result(timeout=15)
                self.respond(200, json.dumps(result))
            except Exception as err:
                self.respond(400, json.dumps({"error": str(err)}))

    server = ThreadingHTTPServer(("127.0.0.1", 8766), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    print("Synthetic fixture: http://127.0.0.1:8766/", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        server.shutdown()
        patch.undo()


async def _noop(*_args, **_kwargs):
    return None


if __name__ == "__main__":
    asyncio.run(main())
