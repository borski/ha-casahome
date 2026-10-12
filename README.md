# Casa for Home Assistant

A Home Assistant integration for [Casa](https://getcasa.com), the home membership service.

Unofficial. Not affiliated with or endorsed by Casa.

Each Casa home becomes a device with membership, badge, weather, credit, inventory, attention, and maintenance sensors, a read-only suggested task list, and separate onboarding and regular service calendars. Selected inventory records become virtual devices linked to the home. The integration is built on [python-casahome](https://github.com/borski/python-casahome). It reads account data and never creates requests or schedules visits.

## Status

Alpha. Casa has no public API, so endpoints and response shapes can change without notice. Expect breaking changes until 1.0.

Version 0.3.1 improves inventory names, keeps duplicate names stable, and lets eligible virtual devices follow Casa room changes in Home Assistant. It pins python-casahome 0.3.1. See the [releases](https://github.com/borski/ha-casahome/releases) for installable versions.

## Requirements

- Home Assistant 2026.10 or newer.
- A Casa account.
- A Casa refresh token. See [Getting a refresh token](#getting-a-refresh-token).

Version 0.3.1 pins [python-casahome](https://pypi.org/project/python-casahome/) 0.3.1 from PyPI. Earlier releases keep their own dependency pins.

## Installation

### HACS

1. Open HACS and go to the menu, then Custom repositories.
2. Add `https://github.com/borski/ha-casahome` with category Integration.
3. Install **Casa**, select the latest release, and restart Home Assistant.

Existing users can select v0.3.1 from the Casa HACS page using Update or Redownload. Restart Home Assistant after the download. The update doesn't rename existing entity IDs or change account configuration.

### Manual

Download `casahome.zip` from a [release](https://github.com/borski/ha-casahome/releases), extract its contents into `config/custom_components/casahome/`, and restart Home Assistant. `manifest.json` should be directly inside that directory. The ZIP contains integration files, not the entire repository.

Alternatively, copy `custom_components/casahome` from the tagged source into your Home Assistant `config/custom_components/` folder and restart.

## Getting a refresh token

Casa has no official token export or login API for third parties. You need a refresh token from your own Casa app session. The [python-casahome README](https://github.com/borski/python-casahome#authentication) describes how to get one.

Treat the refresh token like a password. Anyone who has it can access your Casa account as you. Home Assistant stores it in the config entry, so your configuration directory and backups contain it too.

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

Devices have home-scoped stable identifiers, a home parent link, make, model, and serial number when supplied. Their single diagnostic sensor reports Casa's record status as bounded text. They never merge identities with physical devices.

In v0.3.1, default names use a single known room, such as `Garage Backup Battery`. If the room is unknown or shared, they use the make when available, such as `Acme Air Purifier`, then the item type. Names no longer start with `Casa inventory`. Make, model, and serial stay in device metadata.

When names collide, differing models or makes distinguish them. Identical labels get numbers, such as `Garage Backup Battery 1` and `Garage Backup Battery 2`. Existing numbers and useful qualifiers survive response reordering, missing records, new records, and reloads while the registry devices remain. The names don't imply physical placement beyond a known room.

Names use bounded source text with URLs and control characters removed. Changes to Casa room, type, model, or make can change the generated default. Deleting a registry device discards its reserved number.

On the next successful inventory refresh, including one triggered by a reload, older `Casa inventory` defaults change to the new names. Existing entity IDs, unique IDs, device identities, custom names, and disabled choices stay intact.

Room references must match rooms in that Casa home. A record linked to multiple rooms stays unassigned. The diagnostic sensor retains a `suggested_room` attribute for manual placement.

Automatic area placement requires one known room and one existing Home Assistant area with a matching name or alias. Matching ignores case and extra whitespace. The integration never creates an area or guesses a match.

New virtual devices and devices with migrating `Casa inventory` defaults can start following Casa rooms. An existing area, custom device name, or disabled device at that first placement prevents this. Managed devices move to a matching area on the next inventory refresh when their Casa room changes. An unmatched or shared room clears the area the integration set. A later match can place the device again.

To opt out, change or clear the device's area in Home Assistant. Deleting its assigned area also ends automatic management. The integration won't take over again on reload. A custom name added after placement doesn't opt out. Disabled managed devices pause following until re-enabled. Existing devices without a migrating default aren't enrolled in area following.

If you set the area back to exactly the one the integration last assigned before the next refresh, it can't distinguish your choice from no change.

New records are discovered dynamically while the inventory family is active. Removed records become unavailable without deleting registry entries. Reappearing records use fresh metadata. Missing make/model/serial stays missing, never inferred from candidate blobs. No static property sensors, model summaries, manuals, images, detail requests, or attachment downloads are created.

### Attention, mail, and maintenance

Request attention, mail, package, maintenance, and service history counts carry `scope: fetched_response` and describe only the fetched response. They aren't account-wide totals. A successful empty response reports zero. A failed read makes only that family unavailable.

### Regular service visits

The regular calendar is separate from onboarding. It refreshes every 15 minutes and caches 31 days of UTC coverage beginning at the previous UTC midnight. Calendar queries use that cache and fail when the requested range falls outside it. Events are titled `Casa service visit`. Vendor details, locations, descriptions, and links aren't exposed. Casa's populated event schema and timestamp units remain provisional.

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

The original weather and credit probe remains in setup. Up to seven aggregate reads per home start concurrently after enabled entities register. A slow aggregate endpoint doesn't block setup or the other aggregate families. Each read times out after 30 seconds. Onboarding and suggestions keep their existing setup flow. These budgets exclude token refreshes and SDK retries.

Disabled entities don't poll. A new family is active only when it has an enabled entity. To stop inventory reads, disable its home count and all its record-status entities. Discovery itself isn't a polling context. Disabling a calendar removes its respective request. Weather and credits are still fetched once per home at setup, even if their sensors are disabled. A 404/405 disables only the failing endpoint until reload. Other failures retry on that family's normal interval.

If bootstrap fails or a home disappears, its entities become unavailable, cached data is cleared, and polling pauses. Results from older aggregate reads are discarded after the home becomes unavailable or a read family loses all enabled entities. Recovery requires a fresh response. Unloading the integration cancels active reads and clears its timers and listeners. Casa doesn't provide push updates.

## Privacy

Diagnostics redact contact details, address, coordinates, payment and billing fields, Matterport credentials, agreement text, the refresh token, and the device ID. Fields Casa adds later are dropped rather than passed through. Diagnostics don't include inventory metadata, new read-family payloads, suggested tasks, or calendars.

The home address, pending agreement names, task titles, visit times, inventory make, model, serial number, and suggested rooms are visible to Home Assistant users with access. Count entities don't expose sender or addressee details, previews, tracking numbers, QR codes, summaries, invoice or payment data, prices, signed URLs, or source extras. Errors are logged without tokens or raw exception details.

## Known limitations

- Unofficial. Casa can change or block its API at any time.
- The handyman rate assumes USD.
- New homes appear after a reload. Removed homes go unavailable, and their device stays until you delete it.
- Suggested tasks only map one known status.
- Regular service calendar queries must fit its explicit rolling cache coverage. Its item schema and epoch-seconds interpretation remain provisional.
- Request, mail, package, and service history populated item schemas remain unverified against live responses. Counts apply only to fetched items.
- No Home Assistant write actions. The integration can't create requests, accept suggestions, or book visits. Setup still registers Home Assistant as a Casa device, as described above.
- Virtual inventory devices aren't physical device connections. Unknown categories don't create devices, and identity candidates aren't guessed.
- Billing, purchases, and Casa AI chat aren't supported.

## Removal

Go to Settings, then Devices and services, then **Casa**, then the menu, then Delete. If you installed through HACS, remove it there too. Removing the integration doesn't sign Home Assistant out of Casa on the server side.

## Development

Install the pinned SDK and run the local checks:

```bash
uv venv --python 3.14
uv pip install -p .venv/bin/python -r requirements_test.txt "python-casahome==0.3.1"
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy custom_components tests
```

Tests mock the SDK boundary and use synthetic fixtures. Nothing calls Casa or Firebase.

## License

MIT. See [LICENSE](LICENSE).
