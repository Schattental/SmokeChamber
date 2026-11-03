# Updating the chamber from GitHub

Open the fold-out menu on the left of the dashboard. **Update from GitHub** is
directly below **Show Live Log**. Stop collection, wait for any final sensor read
to finish, then select the update button and confirm. The status below the button
reports progress, and the page reconnects after a successful update.

The device reads the public `main` branch of
<https://github.com/Schattental/SmokeChamber> over HTTPS without credentials.
The browser cannot choose another repository or branch. Maintainer write access
for publishing code is separate from the website's update process.

## What an update does

1. Checks that collection is fully stopped and locks out new collection,
   restarts, reboots, and sensor-address changes while updating.
2. Rejects uncommitted source edits, untracked source files, another checked-out
   branch, or a GitHub history that does not fast-forward the local code.
3. Fetches GitHub, checks the candidate Python syntax and required files, and
   rejects changes to dependency versions or installed service configuration.
4. Archives the previous tracked source under `.updates/backups/`, checks the
   chamber is still idle, stops the app service, and fast-forwards the source.
5. Starts the app and waits up to 60 seconds for `/status` to report the target
   version. If startup fails, it restores the previous commit with
   `git reset --keep` and starts the previous version.

When the local code already matches GitHub, no service restart is needed. Local
edits or unpublished commits are kept and reported as requiring maintainer work.
The updater does not install Python packages, change OS settings, install service
units, or run downloaded tests/scripts before installation. Versions that change
`requirements.txt` or the service definitions require a maintainer-assisted update.

The updater runs as `smoke` in its own systemd oneshot service, so stopping the
website does not kill the update/rollback process. Its Git operations disable
credential helpers and prompts and do not inherit GitHub maintainer tokens.

## Device settings and data

`config.local.json` contains this device's PurpleAir URLs, enable flags, and CO₂
UART paths. It is excluded from Git and preserved by updates. See
`config.local.example.json` for the format. Missing sections/banks fall back to
the defaults in `device_config.py`; invalid values fail explicitly at startup.

The gas-bank I²C mapping remains in `collector.py` and `lib/auto_address.py`.
Do not change those mappings casually: they must agree with the physical DIP
switches and the dashboard guide.

Recordings in `data/`, logs, `Files/`, `env/`, and local configuration are not
part of a source update. Candidate commits that try to track device-local files
or introduce symlinks/submodules are rejected. Ignored local files that would
collide with newly tracked source are also protected. Source archives are **not**
backups of recordings; continue to export experimental CSVs separately.

## One-time setup on another Pi

The existing app and Git checkout must already be installed at
`/home/smoke/smokechamber` for the supplied service paths. Adjust the service user
and absolute paths for another deployment before installing.

Copy the example settings only if the device does not already have settings,
then edit the copy for its actual hardware:

```sh
cd /home/smoke/smokechamber
test -e config.local.json || cp config.local.example.json config.local.json
```

Validate and install the separate updater service and narrowly scoped service
permissions:

```sh
sudo visudo -cf deploy/smokechamber-update.sudoers
sudo install -o root -g root -m 0644 deploy/smokechamber-update.service /etc/systemd/system/smokechamber-update.service
sudo install -o root -g root -m 0440 deploy/smokechamber-update.sudoers /etc/sudoers.d/smokechamber-update
sudo systemctl daemon-reload
```

The oneshot service is triggered by the website; it does not need to be enabled
at boot. After confirming `/status` reports collection stopped, restart the app
once to load the new routes. This bootstrap restart is needed when upgrading a
version that predates the update button:

```sh
sudo systemctl restart smokechamber.service
```

## Diagnostics and recovery

- `GET /update/status` reports the update phase, request ID, message, target and
  previous revisions, current running version, and whether an update is busy.
- `GET /status` includes `version` and `collector_active`; the latter stays true
  while a stopped collector task is still finishing its hardware reads.
- `POST /update` requires JSON and the page's `X-Chamber-Update` token. Browser
  cross-site forms cannot start an update. The existing local dashboard remains
  without user authentication.
- State persists in ignored `.updates/status.json` across app restarts. A killed
  worker is reported as interrupted instead of blocking the controls forever.

Read the update service journal when an update fails:

```sh
journalctl -u smokechamber-update.service -n 100 --no-pager
journalctl -u smokechamber.service -n 100 --no-pager
```

After an automatic rollback, check the error before retrying the same version.
If recovery fails or power is lost during installation, a maintainer must inspect
Git status, the previous revision in `.updates/status.json`, and the service logs
before restoring source. Never use `git clean` or `git reset --hard` as an update
or recovery procedure on the experiment host.
