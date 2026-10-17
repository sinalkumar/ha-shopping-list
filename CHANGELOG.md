# Changelog

## 0.2.0

### Fixed

- The calculated route and refresh button stay available while the AI Task entity reports `unknown` or `unavailable`; only the source list gates availability. AI Task health is exposed as the sensor's `ai_available` attribute and in diagnostics.
- A `set_item_section` correction with `remember: true` now supersedes an earlier per-item (`remember: false`) correction instead of being ignored.
- `clear_cache` with `include_manual: true` now also clears per-item corrections.
- Unclassifiable items and provider failures are no longer re-sent to the AI provider on every source update. A low-confidence or explicit **Unclassified / Other** answer is cached; failures back off exponentially.
- The display prefix is only stripped from edited summaries when the prefix feature is enabled, so user text such as `Dairy · Milk` is preserved.
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
