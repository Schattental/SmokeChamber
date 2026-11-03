# Smoke chamber agent context

## Purpose and scope

This Raspberry Pi 5 hosts a local website for a fruit fly smoke chamber
experiment. It reads two banks of gas sensors and two PurpleAir air quality
devices, records measurements to CSV, and presents live readings and charts.
Preserve experimental data and measurement meaning when making changes.

Project root: `/home/smoke/smokechamber`. The parent home directory is the
initial agent working directory, so `cd /home/smoke/smokechamber` first.

This document was created from local source, installed package metadata, and the
systemd unit on 2026-11-03. Hardware mappings below are configured values, not a
physical wiring verification. The physical role of each bank (for example,
inlet versus outlet), chamber plumbing, and experimental protocol are not
documented in the inspected project; do not assume them.

## Working on the experiment host

- Inspect before changing runtime state. Do not start/stop collection, change
  intervals, restart the service, reboot the Pi, readdress sensors, or calibrate
  hardware merely to validate an unrelated edit. Perform operational changes
  when they are part of the user's requested work.
- Check collection status before operational work. Do not interrupt an active
  experiment unless that interruption is within the user's authorization.
- Importing `collector` constructs both UART sensors and all I²C sensors,
  opening real device handles. Importing `app` also patches the runtime and
  starts a background log streamer. Neither import is a harmless smoke test.
- Avoid a second app process or diagnostic script competing with the service
  for serial/I²C devices. Multiple server workers would also split the in-memory
  collector state and contend for hardware.
- Keep `data/` intact. Do not delete, rewrite, rename, or generate test recordings
  there as routine cleanup. Preserve `Files/data.csv` and logs unless the task
  specifically involves them. Use temporary directories for test artifacts.
- Keep edits focused. Do not modify `env/`, caches, or vendored minified JS as
  part of ordinary source changes. This folder is now initialized as a Git
  repository on `main`; verify commit and remote state before relying on Git
  for recovery. Never use `git clean` to tidy this experiment host.

## Architecture and file map

| Location | Responsibility |
| --- | --- |
| `app.py` | Flask routes, Socket.IO setup, session controls, exports, logging, service/reboot actions, collector watchdog. |
| `collector.py` | Hardware instances/configuration, PurpleAir fetching/cache, sampling state, rolling history, CSV append and live data emission. |
| `lib/DFRobot_MultiGasSensor.py` | Runtime gas sensor driver: I²C/UART protocol, scaling, temperature compensation, address groups. |
| `lib/MHZ16_CO2.py` | Runtime MH-Z16 UART driver, packet/checksum handling, CO₂ reads and calibration commands. |
| `lib/auto_address.py` | Single-device I²C scan and sensor bank/address-group reassignment. |
| `templates/index.html` | Jinja template with inline browser JS, sensor metadata, Chart.js charts, bank tabs, controls, themes, live log. |
| `static/style.css` | Active dashboard styling. `style_old.css` is not linked by the template. |
| `static/chart.js` | Locally served Chart.js 4.5.1. |
| `static/socket.io.min.js` | Local Socket.IO client 4.8.3; currently unused by the template, which loads 4.7.5 from a CDN. |
| `dfrobot/` | Standalone hardware examples/diagnostics and duplicate drivers. The application imports drivers from `lib/`. |
| `data/` | Session recordings: `data_YYYYMMDD_HHMMSS.csv`. |
| `Files/data.csv` | Separate older CSV with `t,v` header; not the active recording path. |
| `logs/app.log` | Application rotating log, up to 5 MB per file with five backups. |
| `env/` | Existing Python virtual environment. |
| `requirements.txt` | Direct runtime dependencies pinned to installed Pi versions; not a transitive lockfile. |
| `deploy/smokechamber.service` | Reference copy of the installed service; not applied automatically. |
| `docs/GITHUB_SETUP.md` | Initial upload and public HTTPS/private read-only deploy key setup. |
| `.gitignore` | Excludes measurements, logs, environments, credentials, and local files. |
| `updater.py` | Hardware-independent update controller/worker; fixed public GitHub main, validation, source backup, restart, rollback. |
| `device_config.py` | Loads device-local PurpleAir/UART settings from ignored `config.local.json`. |
| `config.local.example.json` | Tracked configuration template; do not put device overrides in tracked files. |
| `static/updater.js` | Sidebar button and update progress/reconnect behavior. |
| `deploy/smokechamber-update.service` | Independent oneshot updater, run as smoke. |
| `deploy/smokechamber-update.sudoers` | Narrow permissions to start the updater and stop/start the app. |
| `docs/UPDATES.md` | Website updater setup, operating behavior, and recovery. |
| `tests/test_updater.py` | Temporary-repository update/rollback tests plus mocked route and configuration tests. |

The stack is Python, Flask, Flask-SocketIO, gevent, Jinja, plain browser JS/CSS,
and Chart.js. There is no frontend build step or database. Repository preparation
added `requirements.txt` from installed environment metadata, which includes Python
3.13.5, Flask 3.1.2, Flask-SocketIO 5.6.1, gevent 25.9.1, requests, pyserial,
smbus, spidev, and RPi.GPIO. Eventlet is installed, but the active server uses
gevent. The inspected host reports Debian 13 (trixie), aarch64.

## Hardware configuration

Both gas banks share I²C bus **1**, with distinct configured addresses:

| Gas | Bank 1 address | Bank 2 address | Recorded unit |
| --- | --- | --- | --- |
| O₂ | `0x74` (group 6) | `0x60` (group 1) | `%Vol` |
| NO₂ | `0x75` (group 6) | `0x61` (group 1) | `ppm` |
| SO₂ | `0x76` (group 6) | `0x62` (group 1) | `ppm` |
| O₃ | `0x77` (group 6) | `0x63` (group 1) | `ppm` |
| CO | `0x70` (group 5) | `0x64` (group 2) | `ppm` |

Each bank also has an MH-Z16 CO₂ sensor at 9600 baud, 8N1:

- Bank 1: `/dev/ttyAMA0`.
- Bank 2: `/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`.
  Preserve the stable by-id path rather than substituting `/dev/ttyUSB0`.

Default PurpleAir local JSON URLs in `device_config.py`, used by `collector.py`:

- Bank 1: `http://192.168.0.104/json?live=true`.
- Bank 2: `http://192.168.0.109/json?live=true`.

Both are enabled by default. This Pi's URLs, enable flags, and CO₂ serial paths
are preserved in ignored `config.local.json`. `collector.py` reads these via
`device_config.load_config()`; a tracked example documents the format.
Successful results are cached per bank
for 10 seconds; HTTP timeout is 3 seconds. Failed requests reuse the last cached
reading if available, without a freshness marker in the emitted data. A faster
collection interval does not imply fresh PurpleAir measurements each row.

PurpleAir supplies A/B channels for PM1.0, PM2.5, PM10 (`µg/m³`) and particle
counts at ≥0.3/0.5/1.0/2.5/5.0/10 µm (`particles/dL`, as labeled in this code).
It also supplies temperature, humidity, and pressure. The collector converts
Fahrenheit to Celsius and pressure from hPa to Pa. Field `temperature` is the
O₂ sensor board's stored temperature; `temperature_c`/`temperature_f` come from
PurpleAir. The board value is sampled before the current O₂ concentration read
refreshes it, so it can reflect the previous read.

Sensor addresses must agree across `collector.py`, `lib/auto_address.py`, and
the DIP guide in `templates/index.html`. The address-changing helper assumes
exactly one gas sensor is connected, invokes `i2cdetect -y 1`, and changes its
address group. DIP switches select the offset within a group; a power cycle
may be needed. Do not run it against the normal connected two-bank setup as a
routine check. `/change_sensor_group` currently has no collection-running guard.

## Collection and data contracts

1. `POST /start` creates a timestamped CSV header from `SENSOR_BANKS`, marks
   collection active, and starts `collector.start_collecting` in a Socket.IO
   background task. It does not start collecting automatically at server boot.
2. Startup initializes gas sensors in passive mode with temperature compensation
   enabled and checks the CO₂ sensors. Initialization has retries and can take
   time; failed initialization is logged and collection proceeds with partial data.
3. The loop reads banks sequentially, appends to history/CSV, emits `new_data`,
   and then sleeps. Interval defaults to 1 second and is clamped to 1–60 seconds.
   Actual spacing includes sensor/network delays; it is not a fixed-rate clock.
4. `POST /stop` clears the shared flag. The running loop exits when it next checks
   the flag; the HTTP response does not wait for all hardware activity to end.

Live/history point shape:

```json
{"time": "14:03:21", "banks": {"bank1": {"o2": 20.9}, "bank2": {"o2": 20.8}}}
```

This example omits other fields. Each configured bank has 30 fields. Server
history keeps the latest 100 points and is not cleared on a new session. It is
process memory, not a reload of archived CSVs. Socket.IO events are `init_data`
(history sent to the connecting client), `new_data`, `status_update` (running
flag), and `log_update` (`line`). PurpleAir `_b` fields are channel B of the
same bank, not a separate bank; browser charts combine A and B datasets.

CSV starts with `timestamp`, then all bank 1 fields, then all bank 2 fields in
`SENSOR_BANKS` insertion order. Headers use `bank_sensor (unit)`. Rows and live
points use host-local `HH:MM:SS`; filenames carry the host-local session start
date/time. There is no timezone offset or full date in each row. Preserve this
contract unless explicitly changing it, and do not infer the host timezone from
the remote user's timezone.

Non-finite float readings are sanitized to JSON `null`; missing values become
empty CSV cells. Numeric CSV precision comes from `FORMAT_PRECISION` (default
three decimal places). Do not replace missing readings with fabricated zeros.
The gas driver itself can return `0.0` on checksum failure, so a numeric zero
does not always establish a valid measurement. Driver compensation formulas
affect recorded values; do not silently change scaling, offsets, or calibration.

## HTTP interface

| Method and route | Behavior |
| --- | --- |
| `GET /` | Dashboard. |
| `GET /status` | `running` and configured `interval`. |
| `GET /data` | Rolling in-memory history. |
| `POST /start`, `POST /stop` | Begin/end recording. |
| `POST /set_interval` | JSON `{"interval": 1.0}`; updates interval immediately. |
| `GET /download` | Active CSV if available, otherwise latest timestamped CSV. |
| `GET /zipdata?range=all\|7days\|24h` | CSV ZIP selected by filename session start time. |
| `GET /download_dev` | ZIP of app, collector, template, and active CSS; excludes drivers/environment. |
| `GET /log` | Latest 100 application log lines. |
| `POST /change_sensor_group` | JSON `{"sensor":"o2","bank":"bank1"}`; hardware readdressing. |
| `POST /restart`, `POST /reboot` | Schedule service restart/Pi reboot; return 409 while collecting. |
| `GET /update/status` | Persistent update progress, busy flag, and running version. |
| `POST /update` | JSON plus page token in `X-Chamber-Update`; queues the independent updater. |

The app binds `0.0.0.0:5000`. No route authentication is implemented and
Socket.IO permits all origins. Treat the existing deployment as a local
interface when considering exposure or changes to operational endpoints.

## Running and diagnosing

The installed unit is `/etc/systemd/system/smokechamber.service`:

- User: `smoke`.
- WorkingDirectory: `/home/smoke/smokechamber`.
- ExecStart: `/home/smoke/smokechamber/env/bin/python /home/smoke/smokechamber/app.py`.
- Restart policy: `always`; stdout/stderr go to the journal.

Read-only checks from the project directory, where supported:

```sh
systemctl status smokechamber.service --no-pager
journalctl -u smokechamber.service -n 100 --no-pager
curl --max-time 5 http://127.0.0.1:5000/status
tail -n 100 logs/app.log
```

For a requested manual run, once the service is not competing for the hardware:

```sh
cd /home/smoke/smokechamber
./env/bin/python app.py
```

Use the project working directory: data, log, and export paths are relative.
The main entry point applies gevent monkey patching and starts the watchdog;
using `flask run` or another import-only launcher does not reproduce that setup.
The watchdog checks for missing emissions after `max(30, interval * 5)` seconds,
allows two collector restarts within ten minutes, then disables collection on
the next qualifying hang. Its shared stop/start flag does not prove an old
blocked task exited; preserve single-collector behavior when changing it.

Sandboxed shell commands may not reach the host systemd bus or localhost
website. Use permitted host access for live service checks; a sandbox-only
connection failure does not establish that the Pi service is stopped.

## Source control and GitHub

Repository preparation added ignore rules, dependency versions, a README,
third-party notices, and setup instructions without changing runtime code.
All application code, driver examples, and static assets belong in Git; recordings,
logs, `env/`, caches, and credentials do not. Keep the tracked `.gitkeep` files
in `data/` and `logs/` so fresh checkouts have the required directories.

Verify `git status`, commit history, and `git remote -v` for current setup state.
The initial upload requires maintainer write access. Device fetches can use
unauthenticated HTTPS for public repositories, or a dedicated repository-scoped
read-only deploy key for private repositories. Keep private keys in the user's
`.ssh` directory outside this repository, and do not grant device write access.
Website updates are explicitly triggered by the operator. `updater.py` fetches
the fixed public `Schattental/SmokeChamber` main branch without credentials,
rejects dirty/diverged source or dependency/service-definition changes, archives
the previous code, and checks the restarted website's version. It rolls back
with `git reset --keep` on failed startup. A separate oneshot service survives
app restarts. Never replace this with an in-process pull/restart task.

The shared file lock and queued state block conflicting controls. Collection
must have both its flag cleared and all background sensor tasks finished before
an update or service restart. The updater rechecks this before stopping the app.
Its state/backups live under ignored `.updates/`; device settings stay in
ignored `config.local.json`. See `docs/UPDATES.md` for bootstrap service setup.
The website persists the sampling interval in ignored
`.updates/operator-settings.json`; `collector.py` loads it at startup.
Maintainer credentials may be retained during explicitly authorized development
sessions, separately from credential-free website fetches.

The original application code is licensed under
PolyForm Noncommercial 1.0.0; see `LICENSE` and the required attribution in
`NOTICE`. Describe it as source-available with noncommercial restrictions.
Preserve third-party licenses and copyright notices recorded in
`THIRD_PARTY_NOTICES.md`; those components retain their own terms.

## Validation and future maintenance

Run `./env/bin/python -m unittest discover -s tests -v` for updater, configuration,
and route tests. They use disposable Git repositories and mock hardware/service
access. Files under `dfrobot/` are hardware scripts, not a safe unit-test suite.
In particular, `test_co2.py`
opens the USB UART and writes bytes at module scope; do not run indiscriminate
test discovery over it.

For Python syntax validation without importing the app or touching devices:

```sh
python3 - <<'PY'
import ast
from pathlib import Path
paths = [*Path('.').glob('*.py'), *Path('lib').glob('*.py')]
for path in paths:
    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
print(f'Parsed {len(paths)} Python source files')
PY
```

This checks syntax only. For behavioral tests, isolate or mock hardware modules
before imports and use temporary CSV paths. For UI changes, inspect both bank
tabs, A/B channel alignment, missing readings, reconnect/history behavior,
status, and light/dark modes as relevant to the change. The UI currently depends
on external Socket.IO CDN JS and Google Fonts despite having local chart assets.

Keep `SENSOR_BANKS`, `FORMAT_PRECISION`, browser `sensorMeta`, payload keys, and
CSV headers in sync when adding or changing measurements. Keep this document
current when architecture, hardware mappings, commands, or data contracts change.
