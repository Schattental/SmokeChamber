# Smoke Chamber

Local web dashboard and sensor recorder for a Raspberry Pi 5 fruit fly smoke
chamber experiment. The Pi reads two gas-sensor banks and two PurpleAir devices,
writes timestamped CSV sessions, and displays live charts in a browser.

## What is included

- Flask/Socket.IO application (`app.py`) and sensor collector (`collector.py`).
- Runtime hardware drivers in `lib/` and standalone hardware examples in `dfrobot/`.
- Dashboard template, CSS, Chart.js, and other static assets.
- Pinned direct Python dependencies and a reference systemd service.
- [Agent context](AGENTS.md) and [GitHub setup instructions](docs/GITHUB_SETUP.md).

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
second sampling interval, two bank tabs, CSV/ZIP downloads, and a live log.
Sampling includes hardware/network overhead. The website also exposes hardware
address changes, service restart, and Pi reboot controls. It currently has no
authentication; keep its deployment on the intended local network.

## Hardware and configuration

Each bank has O₂, NO₂, SO₂, O₃, and CO on I²C bus 1, an MH-Z16 CO₂ sensor over
UART, and a PurpleAir local HTTP JSON feed. Exact addresses, serial paths,
units, timing, and data formats are documented in [AGENTS.md](AGENTS.md).

Current deployment values are hardcoded in `collector.py`, including the
PurpleAir private-LAN addresses and UART paths. They are included in this source
snapshot and are not passwords or API credentials. Adapt these values and the
service paths before installing on different hardware. External Socket.IO CDN
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

Use the syntax-check command in [AGENTS.md](AGENTS.md) without importing the
application. There is no automated hardware-independent test suite. Files in
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

A website-based updater is a later phase and is not implemented here. Its design
will need to preserve recordings and device configuration, prevent updates during
collection, and support rollback. Do not use `git reset --hard` or `git clean`
as a device update procedure.

## Licensing

No project-wide license has been selected for the original application code.
Making a repository public does not itself grant an open-source license.
Third-party components retain their own notices; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
