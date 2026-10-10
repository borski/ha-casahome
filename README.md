# Casa for Home Assistant

Unofficial Home Assistant integration for [Casa](https://getcasa.com), the home membership service. It shows each Casa home as a device with sensors for membership status, unread requests and mail, and pending agreements.

Built on [python-casahome](https://github.com/borski/python-casahome). Read-only. It never creates requests, schedules visits, or changes anything on your Casa account.

## Status

Early and personal. Only one Casa endpoint (`bootstrap`) has been seen in real app traffic. Everything else comes from static analysis of the iOS app and is off by default. See [Experimental sensors](#experimental-sensors).

## Requirements

- Home Assistant 2026.10 or newer.
- A Casa refresh token. Casa signs in by SMS through Firebase Phone Auth, which can't be reproduced off the phone. You need to capture the refresh token from the Casa app's own traffic once. How to do that is outside this README.

## Installation

The SDK isn't on PyPI yet, so Home Assistant can't install the `python-casahome==0.1.1` requirement on its own. Until it's published, this integration won't load on a stock install. The two ways forward:

- **A.** Publish `python-casahome` 0.1.1 to PyPI. Nothing else changes.
- **B.** Make `borski/python-casahome` public, tag `v0.1.1`, and change the manifest requirement to `python-casahome @ git+https://github.com/borski/python-casahome.git@v0.1.1`.

Once the SDK installs:

### HACS

1. HACS, then Integrations, then the menu, then Custom repositories.
2. Add `https://github.com/borski/ha-casahome` as an Integration.
3. Install **Casa** and restart Home Assistant.

### Manual

Copy `custom_components/casahome` into your Home Assistant `config/custom_components/` folder and restart.

## Configuration

Settings, then Devices and services, then Add integration, then **Casa**.

| Field | Description |
|-------|-------------|
| Refresh token | Your Casa (Firebase) refresh token. Stored in the config entry. Never logged. |

The integration signs in once to check the token, then creates the entry titled with your first name. Only one Casa account is supported.

Each setup registers Home Assistant as a device on your Casa account, the same way the app does on a new phone. The device id is generated once and reused across restarts and reauth, so it doesn't pile up.

### Options

| Option | Default | Description |
|--------|---------|-------------|
| Enable experimental sensors | Off | Calls Casa endpoints that haven't been seen in real traffic. See below. |

Changing the option reloads the integration.

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

### Experimental sensors

With the option on, each home also gets these, disabled by default in the entity registry:

| Entity | Notes |
|--------|-------|
| Weather temperature (experimental) | Unavailable if the unit string isn't recognized |
| Weather condition (experimental) | |
| Credits available (experimental) | |
| Handyman hourly rate (experimental) | Assumes cents in USD |

These endpoints' paths and methods come from disassembling the app. They may return 404 or 405. If one does, the integration logs one warning and stops calling it until the next reload.

## Data updates

The integration polls Casa's bootstrap endpoint every 15 minutes. Experimental endpoints, when enabled, poll every 15 minutes per home. There's no push.

If the refresh token stops working, Home Assistant starts a reauth flow. Paste a new token and the entry reloads. If Casa rotates the token on refresh, the integration saves the new one.

## Privacy

Diagnostics redact contact details, address, coordinates, payment and membership billing fields, the Matterport model and password, agreement text, the refresh token, and the device id. Fields Casa adds later are dropped from diagnostics rather than passed through.

## Known limitations

- The SDK isn't installable from PyPI yet. See Installation.
- Only `bootstrap` is confirmed against real traffic. Experimental sensors may never work.
- New homes appear after a reload. Removed homes go unavailable and their device stays until you delete it.
- No actions, no writes, no Casa AI chat.
- This is unofficial and not supported by Casa.

## Removal

Settings, then Devices and services, then **Casa**, then the menu, then Delete. If you installed through HACS, remove it there too. Removing the integration doesn't sign Home Assistant out of Casa on the server side.

## Development

```bash
uv venv --python 3.14
uv pip install -p .venv/bin/python -r requirements_test.txt -e ../python-casahome
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy custom_components tests
```

Tests mock the SDK. Nothing calls Casa.

CI: `validate.yml` runs HACS and hassfest. Hassfest fails until the SDK is on PyPI. `test.yml` installs the SDK from its git tag and fails until that repo is public or has a deploy key.

## License

MIT. See [LICENSE](LICENSE).
