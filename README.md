# Smoke Chamber

Local web dashboard and sensor recorder for a Raspberry Pi 5 fruit fly smoke
chamber experiment. The Pi reads two gas-sensor banks and three PurpleAir devices,
writes timestamped CSV sessions, and displays live charts in a browser.

## What is included

- Flask/Socket.IO application (`app.py`) and sensor collector (`collector.py`).
- Runtime hardware drivers in `lib/` and standalone hardware examples in `dfrobot/`.
- Dashboard template, CSS, Chart.js, and other static assets.
- Pinned direct Python dependencies and a reference systemd service.
- [Agent context](AGENTS.md), [GitHub setup](docs/GITHUB_SETUP.md), and
  [website update instructions](docs/UPDATES.md).

Experiment CSVs, logs, virtual environments, private keys, and local settings
are excluded from Git. The `data/` and `logs/` directories are kept through
placeholder files. Source control is not a backup of experimental results;
export and back those up separately.

## Existing device

The current installation lives at `/home/smoke/smokechamber`, uses `env/`, and
runs as `smokechamber.service`. Preparing this repository does not install
dependencies, replace the installed service, or restart collection.

The configured server listens on port 5000. Open `http://<PI_ADDRESS>:5000/`
from the chamber's local network. It provides collection start/stop, a 1–60
second sampling interval, Bank 1 / Bank 2 / Control tabs, CSV/ZIP downloads, and a live log.
Sampling includes hardware/network overhead. The website also exposes hardware
address changes, service restart, and Pi reboot controls. It currently has no
authentication; keep its deployment on the intended local network.

## Hardware and configuration

Each bank has O₂, NO₂, SO₂, O₃, and CO on I²C bus 1, an MH-Z16 CO₂ sensor over
UART, and a PurpleAir local HTTP JSON feed. Exact addresses, serial paths,
units, timing, and data formats are documented in [AGENTS.md](AGENTS.md).
The separate Control group contains only a third PurpleAir feed at
`http://192.168.0.101/json?live=true`. Its CSV columns follow Bank 2 and start
with `control_` (for example, `control_pm2_5_atm (µg/m³)`).

All three PurpleAir devices use the same cache policy. Each device waits at
least 10 seconds after a completed request before another attempt, including
after failures. Between requests, the last successful reading may be reused for
up to **15 seconds after the Pi receives it**. This allows a five-second buffer
beyond the request interval; a failed refresh can briefly reuse the previous
reading. Consequently, consecutive CSV rows can contain the same cached
measurement. The age limit measures time since receipt, rather than the age of
the measurement inside the PurpleAir sensor.

Once that cached reading is older than 15 seconds, its fields become blank CSV
cells and JSON `null` values in the live data. An expiry warning is logged once
per outage. Requests continue with the same 10-second spacing as the collection
loop permits, and successful responses automatically restore readings. Fields
are also missing when no successful reading has been received yet.

The limit is `PURPLEAIR_MAX_AGE_SECONDS` in `collector.py`; request spacing is
`PURPLEAIR_CACHE_SECONDS`. Changes take effect after restarting the website
service while collection is stopped.

Device-local PurpleAir addresses, enable flags, and CO₂ UART paths are loaded
from ignored `config.local.json`, with defaults in `device_config.py`. Copy
`config.local.example.json` and adapt it before installing on different hardware.
The default private-LAN addresses and serial paths in the source are not passwords
or API credentials. Adapt the service paths for other deployments. External Socket.IO CDN
JavaScript and Google Fonts are currently used by the dashboard.

## Installing on a separate Pi

The baseline host is Debian 13 on a Raspberry Pi 5, with Python 3.13.5. This
project requires actual sensor hardware, enabled I²C/UART interfaces, device
permissions, and reachability of the configured PurpleAir devices. There is no
simulator mode. Hardware interface and OS package provisioning are not automated.
The gas-address helper also requires the `i2cdetect` utility from `i2c-tools`.

On a separate device, once those prerequisites are configured, create a Python
environment and install the direct dependency snapshot:

```sh
python3 -m venv env
./env/bin/python -m pip install -r requirements.txt
mkdir -p data logs
```

These versions were collected from the existing environment; installation and
hardware operation on a fresh OS have not been tested. Native Python packages
may need compiler tools and OS development headers. Do not run the installation
commands against an active experiment as routine validation.

To run manually when no existing service is using the sensors:

```sh
./env/bin/python app.py
```

Run from the project root because data, logs, and export paths are relative.
Importing the app or collector opens hardware connections. `flask run` does not
start the same watchdog setup as the application's main entry point.

`deploy/smokechamber.service` is a reference copy of the current service. Review
its user and absolute paths before installing it on another device. Do not run
two app instances against the same sensors.

## Development and validation

Run the updater/configuration/route tests without real sensor access:

```sh
./env/bin/python -m unittest discover -s tests -v
```

These tests use temporary Git repositories and mocked hardware/service controls.
Use the syntax-check command in [AGENTS.md](AGENTS.md) for other Python changes. Files in
`dfrobot/`, including `test_co2.py`, communicate with devices and must not be
treated as safe test discovery targets.

Before changing measurement fields, keep collector definitions, units, CSV
column order, precision, and browser metadata in sync. Avoid modifying stored
recordings while developing code.

## GitHub and device updates

See [GitHub setup](docs/GITHUB_SETUP.md) for the initial upload and separate
read-only device connection. Public repositories can be read through HTTPS
without a key; private repositories can use a repository-specific read-only
deploy key kept outside this folder.

The left-hand menu includes **Update from GitHub**, directly below **Show Live
Log**. It checks public `main`, preserves recordings and local configuration,
blocks updates during collection, and restores the previous code if the updated
website fails its startup check. Updates that change dependencies or service
setup need maintainer assistance. See [the update guide](docs/UPDATES.md) for
one-time service installation, usage, and recovery.

## Licensing

The original application code is available under the
[PolyForm Noncommercial License 1.0.0](LICENSE). Anyone may use, modify, and share
it for purposes permitted by that license, including noncommercial use. The
license also expressly permits personal research without anticipated commercial
application and use by educational institutions, public research organizations,
and the other organizations listed in its Noncommercial Organizations section.
Commercial purposes outside those permissions require separate permission from
the copyright holder.

When sharing the software, include the license terms or their URL and preserve
the required attribution in [NOTICE](NOTICE). The software is provided without
warranty. This is source-available software with noncommercial restrictions.

Third-party components retain their own licenses and notices; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
