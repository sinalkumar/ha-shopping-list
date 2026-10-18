# Changelog

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
