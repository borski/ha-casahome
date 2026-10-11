# Casa for Home Assistant

A Home Assistant integration for [Casa](https://getcasa.com), the home membership service.

Unofficial. Not affiliated with or endorsed by Casa.

Each Casa home becomes one device with membership, badge, weather, and credit sensors, a read-only suggested task list, and a read-only onboarding visit calendar. The integration is built on [python-casahome](https://github.com/borski/python-casahome). It reads account data and never creates requests or schedules visits.

## Status

Alpha. Casa has no public API, so endpoints and response shapes can change without notice. Expect breaking changes until 1.0.

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
| Casa Collective member | Binary sensor | Diagnostic, disabled by default |
| Weather temperature | Sensor (temperature) | Unavailable if the unit isn't recognized |
| Weather condition | Sensor | Casa's condition string as sent, such as `clear` |
| Credits available | Sensor | Service credits on the home |
| Handyman hourly rate | Sensor (monetary) | Diagnostic. Casa sends cents. USD is assumed |
| Suggested tasks | Todo list | Read-only |
| Onboarding visits | Calendar | Read-only |

That's 19 entities per home: 11 sensors, 6 binary sensors, 1 todo list, and 1 calendar. 18 are enabled by default.

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

The integration polls every 15 minutes. Each cycle makes one shared bootstrap request, plus up to four requests per home: weather, credits, suggestions, and onboarding. For N homes that's at most `1 + 4N` requests, not counting token refreshes and retries.

Disabled entities don't poll. Disabling the todo list or calendar removes that request for the home. Weather and credits are still fetched once per home at setup, even if their sensors are disabled.

If bootstrap fails or a home disappears, its entities go unavailable and polling resumes on recovery. Stale data isn't shown after a home comes back. There's no push from Casa.

## Privacy

Diagnostics redact contact details, address, coordinates, payment and billing fields, Matterport credentials, agreement text, the refresh token, and the device ID. Fields Casa adds later are dropped rather than passed through. Diagnostics don't include suggested tasks or onboarding visits.

Task titles and visit times are visible to any Home Assistant user who can see those entities. Errors are logged without tokens or raw exception details.

## Known limitations

- Unofficial. Casa can change or block its API at any time.
- The handyman rate assumes USD.
- New homes appear after a reload. Removed homes go unavailable, and their device stays until you delete it.
- Suggested tasks only map one known status.
- The calendar covers onboarding visits only, not regular service visits.
- No write actions. The integration can't create requests, accept suggestions, or book visits.
- Inventory, billing, purchases, and Casa AI chat aren't supported.

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
