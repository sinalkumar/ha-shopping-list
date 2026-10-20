# Changelog

## 0.8.13

- Stop a rule-out from voting for the product it rules out. "Centrum advance
  Adult 100pk (not 50+)" scored *higher* for the 50+ formulation than for the
  plain adult one, because `not` counted as content and `50` counted as a match.
  A negated quantity is now a hard exclusion, and the words that express it no
  longer take part in scoring.
- Keep a trailing `+` in the token, so `50+` and `50` are different products.
- Stop counting the pack size in the similarity score. `best_match()` already
  enforces it exactly, so counting it again only diluted the score: a size
  printed on the pack rather than in the title pushed correct matches below the
  threshold while wrong variants stayed above it.
- Add an optional AI tie-break for the items scoring genuinely cannot separate.
  It only ever sees candidates that already passed the hard constraints, may
  only answer with one of them or "none", and its answer is remembered as an
  automatic pin so a price cannot flip between refreshes and rewrite the price
  history. Off by default: it sends item and product names to the configured AI
  provider, which the anonymous retailer lookups never do. A profile that
  cannot resolve an automatic pin drops it and matches afresh.

## 0.8.12

- Stop an unavailable source from emptying the route. The card and the price
  cache already refused to read "unavailable" as "empty", but the coordinator
  and the price coordinator still adopted the empty reading, so a restart or a
  Refresh during an outage blanked the derived list, dropped every section
  correction on the next save, and cleared the quotes, store history and product
  matches the household had collected. The last known items now stand until an
  available reading arrives, and only an available reading prunes.
- Drop the numeric keypad hint from the editor's grocer.nz field. The label
  offers an ID or a product URL, and a digits-only keypad on a phone made the
  URL half of that impossible to type.

## 0.8.11

- Keep an expanded "Product & history" section expanded when the source briefly
  reports itself unavailable. An unavailable source reports no items, and the
  pruning that keeps that set from growing treated it as a removal, collapsing
  what the household had open for the next poll.
- Keep pricing backoff when the source is briefly unavailable, for the same
  reason. An unavailable source reports no items, so treating that as a removal
  cleared the retry timer for every item and immediately retried lookups that
  had just failed.

Both are the same mistake in two places: an unavailable source is not an empty
list. Only an available source is treated as authoritative now, matching the
fix 0.8.10 made for stored section corrections.

## 0.8.10

- Release the source subscription when setup fails. `async_setup` subscribes to
  the source before it can fail later (a price store that will not load, say),
  and the error path raised `ConfigEntryNotReady` without shutting the
  coordinator down. Home Assistant retries the entry, so one route ended up with
  two listeners and ran classification and price lookups twice for every source
  change. The retry path itself was already correct.
- Stop the card contradicting itself when the source is unavailable. It warned
  that the source list was unavailable and then also said the list was empty,
  because an unavailable source reports no items. The empty message now appears
  only when the source is actually available.
- Resolve the configured history store's name through the shared `store_name()`
  helper in the config flow too, so every store-name lookup degrades the same
  way instead of the flow keeping its own raw dictionary lookup.

## 0.8.9

- Follow the source to-do entity across a reload. A to-do integration can
  unload and re-create its entity under the same entity ID, and the replacement
  is a different object with its own listener list. The route had subscribed
  once at setup, so after such a reload it kept showing the removed entity's
  items while still reporting itself available. The subscription now follows the
  entity ID, replays the replacement's items immediately, and is released on
  unload.
- Stop a card refresh from eating an unsaved item name. Every reload rebuilt the
  add row, so a background price update or the 60-second timer could clear what
  was being typed. The draft and its focus now survive a render, an expanded
  "Product & history" section stays expanded, and the text is cleared only after
  the source list actually shows the added item.
- Keep the editor open when a section move, pin, or pin reset succeeds. Those
  actions are independent of the name and notes fields, but they used to close
  the whole dialog and silently discard any unsaved text. Closing (or pressing
  Escape) with unsaved name or notes now asks once before discarding them.
- Delete a removed profile's pins and price history through the store instance
  that owns them. Home Assistant's `Store` cancels its final-write listener per
  instance, so removing through a fresh instance could delete the file and then
  let a queued shutdown write recreate it.
- Re-import grocer.nz history for an item that is removed and added again. The
  "already backfilled" flag outlived the history it described, so a re-added
  Woolworths item showed no in-store history until the next mirror sync or
  restart.
- Prune stored section overrides when the source list becomes empty. The last
  item leaving was treated as "nothing to prune", leaving overrides for deleted
  item IDs behind. An unavailable source that reports an empty list is still
  left alone, because that is not a removal.

## 0.8.8

- Name the store a quote came from. Profiles written before the names were
  stored only kept the ids, so the card labelled its own header
  "Current-price store" even though it knew the store. A migration now
  backfills both names from ids the entry already holds, and the card falls
  back to the same derivation for any entry that still lacks them.
- Keep the price block to three lines with a short digest. Home Assistant's own
  item editor shows the machine-readable block as plain text, and the old
  four-line form with a full 64-character digest wrapped onto extra lines. Both
  forms are still accepted on save, so an item left open in an older dialog
  does not fail after the upgrade.
- Say "no price match — refresh, rename or pin this item" on the card, matching
  the native list instead of the vaguer "waiting for a confident match".
- Stop the shopping card from reading the coordinator's private history
  archive; the condition is now a named property.
- Drop the stale `?v=0.8.0` from the documented card resource URL. The file is
  served without long-lived cache headers, so the suffix only invited
  hand-editing on every release.

## 0.8.7

- Re-price every 6 hours rather than every 3, halving the retailer requests
  (one lookup per distinct item per cycle). Staleness stays decoupled from the
  interval, so this is only about request rate.

## 0.8.6

- Re-price the list every 3 hours instead of every 12. Each chain publishes
  new prices roughly weekly, so this mostly shortens how long a mid-week
  change or a missed match stays wrong. It costs four times the retailer
  requests: one lookup per distinct item per cycle.
- Give staleness its own 24-hour threshold instead of deriving it from the
  refresh interval. With the two coupled, a single missed cycle would have
  marked every row `STALE — refresh prices`.
- Sample unchanged prices every 12 hours rather than every 6, so the bounded
  per-item series still spans weeks at the shorter interval.

## 0.8.5

- Re-price the whole list at 06:00 every Monday, and force the grocer.nz mirror
  to re-sync at the same time. Both chains publish the new week's prices
  between Sunday morning and Monday 05:00 NZ time — 93% of Pak'nSave and 98% of
  Woolworths price changes observed in 2026 land on a Monday, recorded by
  grocer.nz at 05:00 — so the 12-hour interval could be half a day late on
  exactly the morning the list changed.

## 0.8.4

- Make a manual or interval refresh retry an item whose last search found no
  match. The one-hour failure backoff used to swallow the request, so pressing
  Refresh could silently skip the very row being looked at.

## 0.8.3

- Keep the retailer's top 20 search candidates instead of 8. A qualifier
  variant that ranked below the cut matched nothing at all: `Tuna 185g (olive
  oil)` was silently unpriced even though the store stocks it.
- Say so when a searched item has no confident match, instead of leaving the
  row silent: `no price match — refresh, rename or pin this item`.

## 0.8.2

- Drop the `Price info (automatic; edit notes below):` label from the derived
  list. The price line is shown on its own, so rows no longer wrap onto an extra
  line in the native to-do list. The block stays integrity-checked and invisible
  to readers, and editing the price is still rejected with an explanation.

## 0.8.1

- Show paper comparisons per 100 sheets rather than rounding each sheet to
  `$0.00`. Other positive sub-cent comparisons display `<$0.01` instead of zero.
- Use consistent half-up unit-price rounding and safely reject unrepresentable
  comparative prices.

## 0.8.0

### Fixed

- Preserve real source notes, including whitespace, through derived-list renames,
  completion and editing. Automatic price blocks are marked and integrity-checked;
  editing generated text or unsupported notes now returns an explicit error.
- Strip only the item's actual view prefix, including overlapping section names.
- Scope price histories to retailer/store/product and history-store/product IDs.
  Re-pinning invalidates previous comparisons and in-flight results. Legacy
  unscoped histories are backed up, not used to manufacture discounts.
- Keep Woolworths physical-store history separate from non-store-scoped online
  quotes. Historical references are dated, limited to 30 days, and never labelled
  as the retailer's normal price.
- Read every Parquet row group, validate column/row counts, normalize malformed
  data errors, and validate downloads before replacing a last-good mirror.
- Use one refresh lifecycle for scheduled/manual/source updates, including sync
  and persistence. Expire history files after 12 hours, retry missing/ambiguous
  matches, bound mirror retention to active matches/pins, and queue additions
  received during an in-flight price request. Mirror cleanup errors defer cleanup
  rather than skipping retailer pricing or price persistence.
- Use exact retailer pack metadata; handle multipacks and counts, normalize
  metric unit prices, and refuse unlabelled Woolworths multi-variant guesses.
- Distinguish partial, stale, pending and unavailable basket estimates from a
  genuinely empty zero-dollar basket. Stale prices do not claim current savings.

### Added

- Optional dependency-free shopping card with read-only price/size/unit rows,
  source notes, expandable product/history details, UID-free section corrections,
  product/history URL pins, pin reset, and confirmed deletion. Errors retain input;
  mutations are read back before success. Register the resource manually as
  documented in README; no dashboard configuration is changed automatically.
- Permission-checked card data endpoint and source-control checks for mutations,
  including device, area, floor and label service targets. Item data is not added
  to new recorder attributes.
- Named public history-store choices with retailer/store consistency validation;
  no runtime DuckDB dependency. Public catalogue release tooling is development-only.
- Regression/reference tests, frontend DOM tests in CI, and an isolated synthetic
  browser harness covering desktop/mobile, edit/status/create/delete, corrections,
  pins, failures, read-only controls, and price-quality states.

## 0.7.0

### Fixed

- **Renaming an item from the derived list failed** with "Entity does not support
  setting field: description". The price line lives in the item description, so
  the to-do dialog sent `description` back on save, and Home Assistant rejects the
  whole call when a field is not advertised. The view now advertises description
  (it is derived) and discards the stale copy instead of forwarding it.
- Size and unit price are no longer dropped when a quote is rebuilt during
  matching.

### Added

- Item descriptions now show size and unit price as well as price, for example
  `$7.99 · 1.2kg · $0.67/100g · Sanitarium Weet-Bix Breakfast Cereal`.
  Woolworths states the size in the product name; Foodstuffs usually omits it, so
  the size is derived from their own comparative price (739c at 148c/100g implies
  a 500g pack). Countable units are only shown when they mean something to a
  shopper, so a sheet count is omitted.
- A `was` figure is shown whenever a real reference price exists, from the
  retailer's own was-price or from a price previously observed. The vaguer
  "on special" label is gone.

## 0.6.1

### Fixed

- The integration failed to load entirely on Home Assistant OS: `duckdb` was a
  manifest requirement, HAOS is Alpine/musl, no wheel matched, pip fell back to a
  source build and failed, so Home Assistant refused to load the integration and
  the route, classification and sensors all stopped. It is no longer a
  requirement, and no third-party package is needed at all.
- Parquet files are now read by a small built-in reader (thrift metadata,
  definition levels, PLAIN and RLE_DICTIONARY, gzip and the standard-library
  Zstandard in Python 3.14). Verified byte-for-byte against pyarrow across six
  real files and 2.9 million rows.
- Product names come from grocer.nz's search service instead of the DuckDB
  catalogue, which also ranks results by popularity, so matching is better than
  the previous flat catalogue scan.

### Changed

- **Price history store** is now the numeric **grocer.nz store ID**, because the
  store list is rendered in the browser only and resolving names would have
  needed a dependency.
- Product names come from grocer.nz's sitemap (114k public URLs) rather than a
  search key, so nothing credential-like ships in the repository.
- `pin_item_product` gains `grocer_product_id`, which pins the exact product used
  for history. Slugs carry no pack size or variant, so automatic matching can
  pick a sibling variant at the same price; pinning is the correction.

## 0.6.0

### Added

- Optional multi-year price history from grocer.nz, which publishes its New
  Zealand grocery dataset as public files. Daily per-store history goes back to
  2022, which is the only way to see Pak'nSave specials because Foodstuffs
  publishes no was-price at all.
- The dataset is mirrored locally (catalogue weekly, the profile's store every
  12 hours) so an outage falls back to the local copy and downloaded history is
  never lost. Diagnostics report `grocer_history` and `grocer_mirror_bytes`.
- History is imported only for self-validating matches: the catalogue price must
  agree with the price the retailer just quoted, so a similar-looking name alone
  cannot attach another product's history. Our own readings are kept on top of
  the import because they are fresher.
- Store names resolve against the grocer.nz catalogue, so "PakNSave Ormiston"
  and "PAK'nSAVE Ormiston" both work.

### Notes

- `duckdb` is added as a requirement for reading the dataset. The integration
  works without it; history is simply unavailable.

## 0.5.0

### Added

- Per-item price history: one point per price change, sampled every six hours
  otherwise, capped at 60 points per item. This is what makes a drop or a
  "cheapest seen" claim possible for a chain that publishes no was-price.
- Pak'nSave promotions are now detected. The search payload carries the same
  promotion records as the product detail endpoint (verified across a sample),
  so detection costs no extra requests. Because most of those promotions are
  price locks whose reward value equals the shelf price, they are reported as
  `promotions` context and never counted as savings.
- The Specials sensor reports `items` (real was-price specials), `price_drops`
  (cheaper than a previously observed price) and `promotions` (retailer flag,
  no reduction known) separately, each with `lowest_seen` and `at_lowest`.

### Changed

- The `was` figure for a chain with no was-price now comes from an observed
  price rather than a stored scalar, so it is only shown when a higher price was
  actually recorded.
- Diagnostics report `items_with_history`.

## 0.4.0

### Added

- `pin_item_product` and `unpin_item_product` actions. A pin can replace the
  search text, so household shorthand reaches a real product name, and can pin
  an exact retailer product ID so variants of the same product are never
  confused. An exact pin that the retailer no longer stocks shows no price
  rather than substituting a near-miss. Pins persist across restarts.
- Price-drop detection for chains that publish no was-price. Pak'nSave exposes
  no was-price at all, so the Specials sensor now also reports items that are
  cheaper than our own previous reading, listed separately under
  `price_drops` with the previous price and the drop.
- Price history and pins are persisted in
  `.storage/smart_shopping_route.prices.<entry_id>` and removed with the profile.

## 0.3.0

### Added

- Optional price and specials indicator, per profile and off by default. Choose a
  price source while configuring:
  - **Woolworths**: price per item plus a real was-price and savings for items on
    special. Anonymous product search is not store-scoped, so this is the online
    price.
  - **Pak'nSave**: per-store price from the Foodstuffs API, which exposes no
    was-price at all, so no specials are claimed for this chain.
- Priced items carry a description such as `$8.50 · was $9.70 · Anchor Butter
  500g`, which the standard to-do card already renders.
- New **Basket total** and **Specials** sensors; the latter lists each discounted
  item with its product, price, was-price, savings, and validity text.
- Prices refresh every 12 hours on an independent coordinator and never block the
  route. Each item is priced once and reused, and failures back off rather than
  retrying every cycle.

### Changed

- Item names are now sent to the chosen supermarket when a price source is
  enabled, in addition to the AI Task provider. This is opt-in per profile and
  documented in the privacy section; choosing **No prices** keeps item names
  away from retailers entirely.
- Product matching compares words, so `Weetbix` finds `Weet-Bix` and
  `Toilet paper` finds `Toilet Tissues`, and a bracketed qualifier is retried
  separately so `Tuna (olive oil)` does not resolve to a bottle of olive oil.
  A candidate must match enough of the item's words or no price is shown, which
  means a supplement the store does not stock stays unpriced instead of being
  estimated from an unrelated product.

## 0.2.0

### Fixed

- The calculated route and refresh button stay available while the AI Task entity reports `unknown` or `unavailable`; only the source list gates availability. AI Task health is exposed as the sensor's `ai_available` attribute and in diagnostics.
- A `set_item_section` correction with `remember: true` now supersedes an earlier per-item (`remember: false`) correction instead of being ignored.
- `clear_cache` with `include_manual: true` now also clears per-item corrections.
- Unclassifiable items and provider failures are no longer re-sent to the AI provider on every source update. A low-confidence or explicit **Unclassified / Other** answer is cached; failures back off exponentially.
- The display prefix is only stripped from edited summaries when the prefix feature is enabled, so user text such as `Dairy · Milk` is preserved.
- The refresh button no longer stays `unavailable` after a restart. It derives availability but never re-wrote its state, so it kept whatever it computed during platform setup, when the source entity was often not yet resolvable. It now subscribes to coordinator updates and source state changes, like the sensor already did.
- Section definitions tolerate unknown persisted keys instead of failing the profile with a permanent retry loop.
- Source feature bits unknown to this version are masked out of the calculated entity.

### Changed

- Actions now use the standard `target` selector instead of a required `route_entity_id` field, so entity, device, area, floor, and label targets all work; an ambiguous selection is refused. `item_uid` is required, and the undocumented `item` alias was removed.
- Profile names are unique and trimmed; reconfiguring renames the config entry. Uniqueness is enforced against current profile names rather than a name-derived unique ID, so renaming a profile does not keep blocking its old name.
- Background classification writes are coalesced into a delayed storage write.
- Removing a config entry deletes its cache file, including after a failed unload, and any pending delayed write is cancelled first.
- Entities of one profile are grouped under a single service device.
- `iot_class` is `cloud_polling`, reflecting that classification depends on the configured AI provider.
- Added `loggers` to the manifest for debug logging.

### Added

- Config entry migration hook.
- Missing `already_in_progress` abort translation.
- Brand assets at `custom_components/smart_shopping_route/brand/`, as required by HACS.
- Coordinator, entity, config-flow, and behavioural privacy tests; coverage gate at 85%.

## 0.1.0

- Initial HACS-ready integration.
- Push-driven calculated Todo route with UID-preserving write-through.
- Structured Home Assistant AI Task classification and bounded local cache.
- Multiple profiles, reconfiguration, manual corrections, refresh button, sensor, and privacy-safe diagnostics.
