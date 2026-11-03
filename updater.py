"""Credential-free updates, run by an independent systemd oneshot service."""

import ast
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener
import uuid


REPOSITORY = "https://github.com/Schattental/SmokeChamber.git"
BRANCH = "main"
UPDATE_SERVICE = "smokechamber-update.service"
APP_SERVICE = "smokechamber.service"
ROOT = Path(__file__).resolve().parent
ACTIVE_STATES = {"queued", "checking", "validating", "installing", "restarting", "rolling_back"}
PROTECTED = {"data", "logs", "Files", "env", "venv", ".venv", ".git", ".ssh",
             ".updates", "local", "backups", "releases"}


class UpdateError(Exception):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


def clean_environment():
    """Do not pass maintainer tokens, askpass, or credential helpers to Git."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GH_", "GITHUB_", "GIT_", "SSH_"))}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return env


class Updater:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.directory = self.root / ".updates"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.state_path = self.directory / "status.json"
        self.lock_path = self.directory / "operation.lock"
        try:
            self.version = self.git("rev-parse", "HEAD").stdout.strip()
        except UpdateError:
            self.version = "unknown"

    def git(self, *args, timeout=90, check=True):
        try:
            result = subprocess.run(
                ["git", "-c", "credential.helper=", "-c", "http.extraHeader=",
                 "-c", "core.hooksPath=/dev/null", *args],
                cwd=self.root, env=clean_environment(), capture_output=True,
                text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError("Git could not complete the update. Check the network and logs.", 503) from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1500:]
            raise UpdateError(f"Git update failed: {detail}")
        return result

    def _read_state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {"status": "idle", "message": "Ready to check for updates."}

    def write_state(self, status, message, **values):
        state = self._read_state()
        state.update(status=status, message=message, updated_at=time.time(), **values)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state) + "\n")
        temporary.replace(self.state_path)
        return state

    @contextmanager
    def operation(self, worker=False):
        """Shared across web requests and the separate update service."""
        with self.lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateError("An update or another chamber operation is in progress.") from exc
            try:
                state = self._read_state()
                if (not worker and state.get("status") == "queued"
                        and time.time() - state.get("updated_at", 0) < 30):
                    raise UpdateError("An update is already starting.")
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def status(self):
        state = self._read_state()
        with self.lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = False
                fcntl.flock(lock, fcntl.LOCK_UN)
            except BlockingIOError:
                busy = True
        queued = (state.get("status") == "queued"
                  and time.time() - state.get("updated_at", 0) < 30)
        busy = busy or queued
        if state.get("status") in ACTIVE_STATES and not busy:
            state.update(status="failed", message="The previous update was interrupted. Check the chamber service before retrying.")
        return {**state, "busy": busy, "version": self.version,
                "repository": REPOSITORY, "branch": BRANCH}

    def request(self, collection_busy):
        with self.operation():
            if collection_busy():
                raise UpdateError("Stop collection and wait for the sensors to finish before updating.")
            request_id = uuid.uuid4().hex
            self.write_state("queued", "Starting update check…", request_id=request_id,
                             previous_version=self.version, target_version=None)
            try:
                subprocess.run(
                    ["sudo", "-n", "/bin/systemctl", "start", "--no-block", UPDATE_SERVICE],
                    check=True, capture_output=True, text=True, timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.write_state("failed", "The update service could not start. Check its installation and service permissions.")
                raise UpdateError("The update service could not start. Check the service logs.", 503) from exc
        return self.status()

    def check_clean(self):
        if self.git("symbolic-ref", "--short", "HEAD").stdout.strip() != BRANCH:
            raise UpdateError("Updates require the main branch. Ask the maintainer to switch branches.")
        if self.git("status", "--porcelain", "--untracked-files=normal").stdout.strip():
            raise UpdateError("Local code changes are present. Commit or back them up before updating.")

    def validate(self, previous, target):
        """Inspect candidate files without importing hardware or executing new code."""
        old_paths = set(self.git("ls-tree", "-rz", "--name-only", previous).stdout.split("\0"))
        tree = self.git("ls-tree", "-rz", target).stdout
        paths = set()
        for line in filter(None, tree.split("\0")):
            metadata, name = line.split("\t", 1)
            mode, kind, _sha = metadata.split()
            path = PurePosixPath(name)
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or path.is_absolute() or ".." in path.parts:
                raise UpdateError("An update path requires maintainer review.")
            paths.add(name)
            if mode not in {"100644", "100755"} or kind != "blob":
                raise UpdateError("Updates containing symbolic links or submodules require maintainer review.")
            protected = (path.parts[0] in PROTECTED
                         or name.startswith((".env", "config.local."))
                         or path.suffix in {".csv", ".log", ".pem", ".key"})
            if protected and name not in {"data/.gitkeep", "logs/.gitkeep", "config.local.example.json"}:
                raise UpdateError(f"Update would overwrite device-local files: {name}")
            if name not in old_paths and (self.root / name).exists():
                raise UpdateError(f"A local file would be overwritten: {name}")
            if name.endswith(".py"):
                code = self.git("show", f"{target}:{name}").stdout
                try:
                    ast.parse(code, filename=name)
                except SyntaxError as exc:
                    raise UpdateError(f"Downloaded Python code is invalid: {name}:{exc.lineno}") from exc

        required = {"app.py", "collector.py", "updater.py", "device_config.py",
                    "templates/index.html", "static/updater.js", "requirements.txt"}
        if not required.issubset(paths):
            raise UpdateError("Downloaded version is missing required chamber files.")
        for name in ("requirements.txt", "deploy/smokechamber.service",
                     "deploy/smokechamber-update.service", "deploy/smokechamber-update.sudoers"):
            changed = self.git("diff", "--quiet", previous, target, "--", name, check=False)
            if changed.returncode:
                raise UpdateError("This version changes dependencies or service setup and needs a maintainer-assisted update.")

    def chamber_status(self):
        try:
            opener = build_opener(ProxyHandler({}))
            with opener.open("http://127.0.0.1:5000/status", timeout=3) as response:
                status = json.load(response)
            if not isinstance(status.get("running"), bool) or not isinstance(status.get("collector_active"), bool):
                raise ValueError("Missing collection status")
            return status
        except (OSError, ValueError) as exc:
            raise UpdateError("Cannot verify chamber status. Update canceled to protect collection.", 503) from exc

    def require_idle(self):
        status = self.chamber_status()
        if status["running"] or status["collector_active"]:
            raise UpdateError("Collection is still running or stopping. Update canceled.")

    def service(self, action):
        try:
            subprocess.run(["sudo", "-n", "/bin/systemctl", action, APP_SERVICE],
                           check=True, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError(f"Could not {action} the chamber service. Check the system journal.", 503) from exc

    def healthy(self, version, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.chamber_status().get("version") == version:
                    return True
            except UpdateError:
                pass
            time.sleep(1)
        return False

    def apply(self):
        with self.operation(worker=True):
            previous = target = None
            service_stopped = applied = False
            try:
                self.write_state("checking", "Checking GitHub for a newer version…")
                self.require_idle()
                self.check_clean()
                previous = self.git("rev-parse", "HEAD").stdout.strip()
                self.git("fetch", "--no-tags", "--no-recurse-submodules", REPOSITORY, f"refs/heads/{BRANCH}")
                target = self.git("rev-parse", "FETCH_HEAD").stdout.strip()
                self.write_state("validating", "Checking the downloaded version…",
                                 previous_version=previous, target_version=target)
                if target == previous:
                    self.write_state("up_to_date", "The chamber is already up to date.")
                    return
                if self.git("merge-base", "--is-ancestor", previous, target, check=False).returncode:
                    raise UpdateError("Local commits and GitHub differ. Publish or reconcile local work before updating.")
                self.validate(previous, target)
                backup = self.directory / "backups" / f"{previous}.tar"
                backup.parent.mkdir(exist_ok=True)
                self.git("archive", "--format=tar", f"--output={backup}", previous)
                # Recheck after the network and validation work, immediately before stopping.
                self.check_clean()
                if self.git("rev-parse", "HEAD").stdout.strip() != previous:
                    raise UpdateError("The local code changed during the update check. Please retry.")
                self.require_idle()
                self.write_state("installing", "Installing the update. The website will reconnect shortly…")
                service_stopped = True
                self.service("stop")
                self.git("merge", "--ff-only", target)
                applied = True
                self.write_state("restarting", "Restarting the chamber website…")
                self.service("start")
                if not self.healthy(target):
                    raise UpdateError("The updated website did not pass its startup check.", 503)
                self.write_state("succeeded", "Update installed successfully.")
            except Exception as exc:
                message = str(exc) if isinstance(exc, UpdateError) else "An unexpected update error occurred. Check the update service journal."
                if service_stopped:
                    try:
                        self.write_state("rolling_back", "Restoring the previous working version…")
                        self.service("stop")
                        if applied:
                            if self.git("rev-parse", "HEAD").stdout.strip() != target:
                                raise UpdateError("Code changed during rollback; maintainer recovery is required.")
                            self.git("reset", "--keep", previous)
                        self.service("start")
                        if not self.healthy(previous):
                            raise UpdateError("The previous version also failed its startup check.")
                        self.write_state("rolled_back", f"{message} The previous version has been restored.")
                    except Exception:
                        self.write_state("failed", f"{message} Automatic recovery failed; contact the maintainer.")
                else:
                    self.write_state("failed", message)
                raise


if __name__ == "__main__":
    try:
        Updater().apply()
    except Exception as exc:
        print(f"Chamber update failed: {exc}", file=sys.stderr)
        sys.exit(1)
