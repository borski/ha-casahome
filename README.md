# Casa for Home Assistant

A Home Assistant integration for [Casa](https://getcasa.com), the home membership service.

Unofficial. Not affiliated with or endorsed by Casa.

Each Casa home becomes a device with membership, badge, weather, credit, inventory, attention, and maintenance sensors, a read-only suggested task list, and separate onboarding and regular service calendars. Selected inventory records become virtual devices linked to the home. The integration is built on [python-casahome](https://github.com/borski/python-casahome). It reads account data and never creates requests or schedules visits.

## Status

Alpha. Casa has no public API, so endpoints and response shapes can change without notice. Expect breaking changes until 1.0.

The local manifest is 0.3.0, a release candidate for the five-family expansion. This doesn't indicate a published release or deployment. The SDK pin remains PyPI 0.2.0.

## Requirements

- Home Assistant 2026.10 or newer.
- A Casa account.
- A Casa refresh token. See [Getting a refresh token](#getting-a-refresh-token).

Home Assistant installs [python-casahome 0.2.0](https://pypi.org/project/python-casahome/0.2.0/) from PyPI. The dependency is pinned to that version.

## Installation

### HACS

1. Open HACS and go to the menu, then Custom repositories.
2. Add `https://github.com/borski/ha-casahome` with category Integration.
3. Install **Casa** and restart Home Assistant.

### Manual

Copy `custom_components/casahome` into your Home Assistant `config/custom_components/` folder and restart.

## Getting a refresh token

Casa has no official token export or login API for third parties. You need a refresh token from your own Casa app session. The [python-casahome README](https://github.com/borski/python-casahome#authentication) describes how to get one.

Treat the refresh token like a password. Anyone who has it can read your Casa account. Home Assistant stores it in the config entry, so your configuration directory and backups contain it too.

Tokens can be revoked or stop working. When that happens, Home Assistant starts a reauth flow and asks for a new token.

## Configuration

Go to Settings, then Devices and services, then Add integration, then **Casa**.

| Field | Description |
|-------|-------------|
| Refresh token | Your Casa refresh token. It's validated before the entry is created. |

The entry is titled with your first name. One Casa account is supported per Home Assistant instance. There are no options.

Setup registers Home Assistant as a device on your Casa account, the same way the app does on a new phone. The device ID is generated once and reused across restarts and reauth, so repeated restarts don't add new devices.

Reauth checks that the new token belongs to the same account. A token for a different account is rejected. If Casa rotates the token, the integration saves the new one.

### Upgrading from 0.1.0

0.1.0 hid the weather and credits sensors behind an "experimental" option. The first start after upgrading removes that option and enables those sensors, unless you disabled them yourself. If you had the option on, those sensors keep their entity IDs ending in `_experimental`. Rename them in entity settings if you like.

Config entry minor version 3 also enables the Casa Collective diagnostic by default. Collective is Casa's beta program, not a subscription tier. Migration changes only integration-disabled Collective entities belonging to this entry. User-disabled entities and existing unique IDs and entity IDs stay unchanged. It doesn't enroll anyone in the beta program or change token values.

## Entities

One device per Casa home, named after the home's address.

| Entity | Type | Notes |
|--------|------|-------|
| Status | Sensor (enum) | `onboarding`, `active`, `inactive` |
| Plan state | Sensor | Diagnostic |
| Unread requests | Sensor | Unavailable if Casa sends no badge counts |
| Unread recurring vendors | Sensor | Same |
| Unread mail | Sensor | Same |
| Unread total | Sensor | Same |
| Pending agreements | Sensor | Diagnostic. Agreement names as an attribute, never the text |
| Agreement needs signature | Binary sensor | Problem |
| Membership active | Binary sensor | |
| Payment method on file | Binary sensor | Diagnostic |
| Post-onboarding steps pending | Binary sensor | Diagnostic |
| Calendar sync | Binary sensor | Diagnostic. Unavailable if Casa doesn't say |
| Casa Collective member | Binary sensor | Diagnostic, enabled by default. `program: beta` |
| Weather temperature | Sensor (temperature) | Unavailable if the unit isn't recognized |
| Weather condition | Sensor | Casa's condition string as sent, such as `clear` |
| Credits available | Sensor | Service credits on the home |
| Handyman hourly rate | Sensor (monetary) | Diagnostic. Casa sends cents. USD is assumed |
| Suggested tasks | Todo list | Read-only |
| Onboarding visits | Calendar | Read-only |
| Inventory records | Sensor | All fetched unique inventory entities, including non-device categories |
| Fetched requests needing review | Sensor | Unique cards in `needs_review` |
| Fetched unread requests | Sensor | Explicit `has_unread_messages` |
| Fetched requests awaiting reply | Sensor | Explicit `concierge_awaiting_reply` |
| Fetched requests with scheduling prompts | Sensor | Cards with positive `pending_schedule_prompt_count`, not number of prompts |
| Fetched requests need attention | Binary sensor | Union of the four explicit request attention sets |
| Enabled maintenance plans | Sensor | Eligible plans with `enabled: true` |
| Eligible maintenance plans | Sensor | Unique plans in categories' `plans` lists, not `ineligible_plans` |
| Fetched service history records | Sensor | Typed home-level `history_items` list length |
| Fetched unread nonarchived mail | Sensor | `read_at` and `archived_at` are both null |
| Fetched unread mail present | Binary sensor | The fetched unread nonarchived mail count is positive |
| Fetched nonarchived packages | Sensor | Packages with explicit `archived: false` |
| Fetched nonarchived packages present | Binary sensor | Not a delivery or pickup-ready assertion |
| Service visits | Calendar | Read-only, provisional schema and bounded rolling coverage |

That's 33 entities per home: 21 sensors, 9 binary sensors, 1 todo list, and 2 calendars. All are enabled by default. Each selected inventory device adds one diagnostic record-status sensor. These are inventory records, not live device controls or telemetry.

### Inventory devices

One `home_details` GET fetches stories, rooms, and all inventory entities. The inventory count includes unique IDs across all categories. Appliances, Electronics, Utility/Utilities, and Safety categories create virtual Casa inventory devices. Matching is case-insensitive and exact. Other and future categories, including bulbs, paint, and structure, still contribute to the total but don't create individual devices.

Devices have home-scoped stable identifiers, a home parent link, make, model, and the named `serial_number` property when supplied. Their single diagnostic sensor reports Casa's record status as bounded text, including new statuses without reinterpretation. The `suggested_room` attribute is a room-name suggestion only. HA's `suggested_area` creates areas, so the integration doesn't use it or assign rooms automatically. It doesn't merge identities with physical devices or overwrite user names or areas.

Room references accept exact names or IDs from that home's stories. For shared items, the first valid reference in Casa's ordered `rooms` list wins, followed by the first valid `room_entity_map` entry as fallback. The displayed name is bounded and sanitized. Unknown names aren't exposed or turned into areas.

New records are discovered dynamically while the inventory family is active. Removed records become unavailable without deleting registry entries. Reappearing records use fresh metadata. Missing make/model/serial stays missing, never inferred from candidate blobs. No static property sensors, model summaries, manuals, images, detail requests, or attachment downloads are created.

On reload, enabled existing inventory-status entities are restored as unavailable placeholders before polling starts. They own normal polling contexts even when the home inventory count is disabled. They don't carry cached record values or add a hidden discovery context. Disabling the count and every status still stops all inventory I/O.

### Attention, mail, and maintenance

All count and attention entities carry `scope: fetched_response`. Counts describe fetched cards or lists, not account-wide totals, section display counts, or badge totals. Request cards are deduplicated by ID across `needs_review` and sections. Positive explicit flags are combined across duplicates. Invoice/purchase counts, task lanes, vendor contact, message content, and titles don't affect attention.

Mail requires both null `read_at` and null `archived_at`. Packages use the SDK's separate `archived` boolean. Unknown package statuses aren't mapped to delivered or pickup-ready. Maintenance counts use category `plans` and explicit `plan.enabled`, not associations, quoted prices, or opaque plan metadata. Service history uses one home-target aggregate GET with the home ID as the SDK-required target ID. There's no per-device history fanout, guessed overdue date, or latest-service timestamp sensor.

An empty successful response reads as zero. A failed response is unavailable, not zero. One family's failure never makes another unavailable.

### Regular service visits

The regular calendar is separate from onboarding. Its cache spans exactly 31 UTC days, starting at the previous UTC midnight. It refreshes every 15 minutes. `coverage_start` and `coverage_end` describe the actual fetched half-open window. Calendar queries are local only. A query outside coverage fails explicitly instead of fabricating an empty answer. Historical views and a full calendar month may fall outside coverage.

Events use validated IDs, deduplication, UTC instant ordering, and the generic title `Casa service visit`. No vendor, event kind, location, description, links, or opaque payloads are exported. Invalid IDs or time bounds invalidate that family's whole response. The provisional codec assumes integer epoch seconds between 2000 and 2100 and requires end after start. Milliseconds and implausible/reversed values are rejected. Aware query datetimes are compared in UTC, including DST folds.

The calendar exposes `item_schema: provisional` and `timestamp_codec: epoch_seconds_assumed`. An empty live envelope doesn't verify populated items or server interpretation of timestamp units and boundary inclusivity.

### Suggested tasks

The todo list shows the tasks Casa suggests for your home. These are suggestions, not accepted work. Items with the `suggestion_concierge_approved` status show as needing action. Other statuses are shown without a status rather than guessed.

The list is read-only. Creating, editing, completing, or deleting items is rejected. Only the item title is shown. Descriptions, briefs, attachments, and links aren't exposed, and URLs and control characters are stripped from titles.

### Onboarding visits

The calendar shows scheduled onboarding visits only. Each event is titled `Casa onboarding visit` with its start and end time. No location, attendees, or description are added. The calendar is read-only.

Calendar queries use the cached data and don't make extra Casa requests. If Casa returns invalid times, the calendar goes unavailable instead of showing wrong events.

### Weather and credits

Temperature units accept `F`, `fahrenheit`, `°F`, `imperial`, and their Celsius equivalents. Any other unit makes the temperature unavailable. The weather condition is passed through as text because Casa hasn't published its full list of values.

If one of these endpoints returns 404 or 405, the integration logs one warning and stops calling it until reload. Other entities keep working.

## Data updates

Bootstrap, weather, credits, suggestions, onboarding, requests, service history, mail, packages, and regular service calendar poll every 15 minutes when active. Inventory metadata and the maintenance catalog poll every 24 hours. Their slower rate is intentional because these are descriptive records, not telemetry. Manual refresh, reload, re-enabling, and home recovery can trigger a fresh read sooner.

| Phase | Maximum aggregate requests for N homes |
|-------|------------------------------------------|
| Initial setup and background reads | `1 + 11N` |
| Normal 15-minute updates | `1 + 9N` |
| When both daily catalogs are also due | Up to `1 + 11N` |

The original weather and credit probe remains in setup. Seven new endpoint reads per home start concurrently as entry-owned background tasks after platforms register contexts, so their latency doesn't serialize or block bootstrap setup. Each is independently bounded by the existing request timeout. Onboarding and suggestions keep their existing setup flow. Calls aren't guaranteed to land at the same wall-clock instant. These budgets exclude token refreshes and SDK retries.

Disabled entities don't poll. A new family is active only when it has an enabled entity. To stop inventory reads, disable its home count and all its record-status entities. Discovery itself isn't a polling context. Disabling a calendar removes its respective request. Weather and credits are still fetched once per home at setup, even if their sensors are disabled. A 404/405 disables only the failing endpoint until reload. Other failures retry on that family's normal interval.

If bootstrap fails or a home disappears, its entities immediately go unavailable, caches are cleared, and timers pause. In-flight results from the old home/context generation are discarded, including stale auth failures and 404/405 responses. Recovery requires a fresh response. Unload cancels and awaits active SDK child reads from setup and HA timer dispatch, and clears debounce timers and listeners without closing HA's shared SDK session. There's no push from Casa.

### Wire evidence and provisional contracts

Evidence here comes from the SDK's documented contracts, not additional live probes by this integration change. Inventory entities and populated maintenance catalog variants are VERIFIED in SDK evidence. Requests, mail, packages, service history, and regular calendar have VERIFIED GET paths and empty response envelopes. Their populated item fields remain UNVERIFIED, modeled from static app metadata. Empty lists don't establish a populated contract. Synthetic HA tests establish projection and lifecycle behavior, not server compatibility with every populated account. New fields are dropped rather than exported. Known SDK schema errors make only that family unavailable without logging raw values.

## Privacy

Diagnostics redact contact details, address, coordinates, payment and billing fields, Matterport credentials, agreement text, the refresh token, and the device ID. Fields Casa adds later are dropped rather than passed through. Diagnostics don't include inventory metadata, new read-family payloads, suggested tasks, or calendars.

Task titles, visit times, inventory make/model/serial, and suggested rooms are visible to Home Assistant users with access. New count entities don't expose sender/addressee, previews, tracking numbers, QR codes, summaries, invoice/payment data, prices, signed URLs, or source extras. Errors are logged without tokens or raw exception details.

## Known limitations

- Unofficial. Casa can change or block its API at any time.
- The handyman rate assumes USD.
- New homes appear after a reload. Removed homes go unavailable, and their device stays until you delete it.
- Suggested tasks only map one known status.
- Regular service calendar queries must fit its explicit rolling cache coverage. Its item schema and epoch-seconds interpretation remain provisional.
- Request, mail, package, and service history populated item schemas remain unverified against live responses. Counts apply only to fetched items.
- No write actions. The integration can't create requests, accept suggestions, or book visits.
- Virtual inventory devices aren't physical device connections. Unknown categories don't create devices, and identity candidates aren't guessed.
- Billing, purchases, and Casa AI chat aren't supported.

## Removal

Go to Settings, then Devices and services, then **Casa**, then the menu, then Delete. If you installed through HACS, remove it there too. Removing the integration doesn't sign Home Assistant out of Casa on the server side.

## Development

```bash
uv venv --python 3.14
uv pip install -p .venv/bin/python -r requirements_test.txt "python-casahome==0.2.0"
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy custom_components tests
```

Tests mock the SDK boundary and use synthetic fixtures. Nothing calls Casa or Firebase.

## License

MIT. See [LICENSE](LICENSE).
