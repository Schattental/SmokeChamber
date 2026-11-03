"""Exercise real Git updates against disposable local repositories, never sensors."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from updater import Updater, UpdateError
from device_config import load_config


PROJECT = Path(__file__).resolve().parents[1]


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "device"
        self.root.mkdir()
        self.remote = Path(self.temp.name) / "github.git"
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        files = {
            ".gitignore": "/.updates/\n/data/*\n/logs/*\n/env/\nconfig.local.*\n/static/local.css\n",
            "app.py": "VERSION = 1\n", "collector.py": "VALUE = 1\n",
            "updater.py": "VALUE = 1\n", "device_config.py": "VALUE = 1\n",
            "templates/index.html": "<html></html>\n", "static/updater.js": "// updater\n",
            "requirements.txt": "Flask==3.1.2\n",
            "deploy/smokechamber.service": "app unit\n",
            "deploy/smokechamber-update.service": "update unit\n",
            "deploy/smokechamber-update.sudoers": "sudoers\n",
        }
        for name, contents in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        self.git("add", ".")
        self.git("commit", "-m", "Baseline")
        self.previous = self.git("rev-parse", "HEAD")
        self.git("clone", "--bare", str(self.root), str(self.remote))
        self.updater = Updater(self.root)
        self.service = mock.patch.object(self.updater, "service").start()
        self.health = mock.patch.object(self.updater, "healthy", return_value=True).start()
        mock.patch.object(self.updater, "chamber_status", return_value={
            "running": False, "collector_active": False, "version": self.previous,
        }).start()
        mock.patch("updater.REPOSITORY", str(self.remote)).start()
        self.addCleanup(mock.patch.stopall)
        for name in ("data/run.csv", "logs/app.log", "config.local.json", "env/keep"):
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("device-local content")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE).decode().strip()

    def publish(self, files):
        for name, contents in files.items():
            path = self.root / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text(contents)
            self.git("add", "-f", name)
        self.git("commit", "-m", "New release")
        target = self.git("rev-parse", "HEAD")
        self.git("push", str(self.remote), "main")
        self.git("reset", "--keep", self.previous)
        return target

    def assert_device_files_preserved(self):
        for name in ("data/run.csv", "logs/app.log", "config.local.json", "env/keep"):
            self.assertEqual((self.root / name).read_text(), "device-local content")

    def test_installs_fast_forward_and_preserves_device_files(self):
        target = self.publish({"app.py": "VERSION = 2\n"})
        self.updater.apply()
        self.assertEqual(self.git("rev-parse", "HEAD"), target)
        self.assertEqual(self.updater.status()["status"], "succeeded")
        self.assertEqual(self.service.call_args_list, [mock.call("stop"), mock.call("start")])
        self.assertTrue((self.root / ".updates/backups" / f"{self.previous}.tar").is_file())
        self.assert_device_files_preserved()

    def test_failed_start_rolls_back_code_and_preserves_device_files(self):
        self.publish({"app.py": "VERSION = 2\n"})
        self.health.side_effect = [False, True]
        with self.assertRaises(UpdateError):
            self.updater.apply()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.previous)
        self.assertEqual((self.root / "app.py").read_text(), "VERSION = 1\n")
        self.assertEqual(self.updater.status()["status"], "rolled_back")
        self.assertEqual(self.service.call_count, 4)
        self.assert_device_files_preserved()

    def test_up_to_date_never_restarts(self):
        self.updater.apply()
        self.assertEqual(self.updater.status()["status"], "up_to_date")
        self.service.assert_not_called()

    def test_collection_still_finishing_blocks_update(self):
        self.updater.chamber_status.return_value["collector_active"] = True
        with self.assertRaises(UpdateError):
            self.updater.apply()
        self.service.assert_not_called()

    def test_local_edits_are_kept_and_block_update(self):
        self.publish({"app.py": "VERSION = 2\n"})
        (self.root / "app.py").write_text("MY_EDIT = True\n")
        with self.assertRaisesRegex(UpdateError, "Local code changes"):
            self.updater.apply()
        self.assertEqual((self.root / "app.py").read_text(), "MY_EDIT = True\n")
        self.service.assert_not_called()

    def test_local_commits_are_never_overwritten(self):
        self.publish({"app.py": "VERSION = 2\n"})
        (self.root / "collector.py").write_text("LOCAL = True\n")
        self.git("add", "collector.py")
        self.git("commit", "-m", "Local development")
        local = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(UpdateError, "Local commits"):
            self.updater.apply()
        self.assertEqual(self.git("rev-parse", "HEAD"), local)
        self.service.assert_not_called()

    def test_dependency_change_requires_maintainer(self):
        self.publish({"requirements.txt": "Flask==99.0.0\n"})
        with self.assertRaisesRegex(UpdateError, "dependencies or service"):
            self.updater.apply()
        self.service.assert_not_called()

    def test_invalid_python_rejected_before_stopping(self):
        self.publish({"app.py": "def invalid(\n"})
        with self.assertRaisesRegex(UpdateError, "invalid"):
            self.updater.apply()
        self.service.assert_not_called()

    def test_remote_cannot_track_recordings(self):
        self.publish({"data/stolen.csv": "new data\n"})
        with self.assertRaisesRegex(UpdateError, "device-local files"):
            self.updater.apply()
        self.assert_device_files_preserved()
        self.service.assert_not_called()

    def test_new_ignored_file_is_not_overwritten(self):
        self.publish({"static/local.css": "remote css\n"})
        (self.root / "static/local.css").write_text("local css\n")
        with self.assertRaisesRegex(UpdateError, "local file would be overwritten"):
            self.updater.apply()
        self.assertEqual((self.root / "static/local.css").read_text(), "local css\n")
        self.service.assert_not_called()

    def test_second_check_blocks_collection_started_during_fetch(self):
        self.publish({"app.py": "VERSION = 2\n"})
        self.updater.chamber_status.side_effect = [
            {"running": False, "collector_active": False},
            {"running": True, "collector_active": True},
        ]
        with self.assertRaisesRegex(UpdateError, "Collection"):
            self.updater.apply()
        self.service.assert_not_called()

    def test_lock_and_queued_state_block_duplicate_operations(self):
        with self.updater.operation():
            with self.assertRaises(UpdateError):
                with Updater(self.root).operation():
                    self.fail("Second operation acquired the lock")
        self.updater.write_state("queued", "queued")
        with self.assertRaises(UpdateError):
            with self.updater.operation():
                self.fail("Queued worker was bypassed")

    def test_interrupted_worker_does_not_block_forever(self):
        self.updater.write_state("installing", "old worker")
        state = self.updater.status()
        self.assertFalse(state["busy"])
        self.assertEqual(state["status"], "failed")


class DeviceConfigurationTests(unittest.TestCase):
    def test_overrides_preserve_unspecified_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text(json.dumps({"purpleair_urls": {"bank1": "http://example.invalid/json"}}))
            config = load_config(path)
            self.assertEqual(config["purpleair_urls"]["bank1"], "http://example.invalid/json")
            self.assertEqual(config["co2_uart_ports"]["bank1"], "/dev/ttyAMA0")
            self.assertTrue(config["purpleair_enabled"]["bank2"])

    def test_invalid_settings_fail_instead_of_silently_using_another_device(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text('{"purpleair_enabled": {"bank1": "false"}}')
            with self.assertRaises(ValueError):
                load_config(path)


class FakeTask:
    def ready(self):
        return False


class FakeSocketIO:
    def __init__(self, *args, **kwargs):
        pass

    def on(self, *args):
        return lambda function: function

    def start_background_task(self, *args):
        return FakeTask()

    def emit(self, *args, **kwargs):
        pass


class UpdateRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = Updater(self.temp.name)
        self.collector = types.ModuleType("collector")
        self.collector.is_running = mock.Mock(return_value=False)
        self.collector.sampling_interval = 1
        address = types.ModuleType("lib.auto_address")
        address.change_sensor_address = mock.Mock()
        spec = importlib.util.spec_from_file_location("chamber_test_app", PROJECT / "app.py")
        self.module = importlib.util.module_from_spec(spec)
        previous_cwd = os.getcwd()
        try:
            os.chdir(self.temp.name)
            with mock.patch.dict(sys.modules, {"collector": self.collector, "lib.auto_address": address}), \
                    mock.patch("gevent.monkey.patch_all"), \
                    mock.patch("flask_socketio.SocketIO", FakeSocketIO), \
                    mock.patch("updater.Updater", return_value=self.controller):
                spec.loader.exec_module(self.module)
        finally:
            os.chdir(previous_cwd)
        self.client = self.module.app.test_client()
        self.headers = {"X-Chamber-Update": self.module.update_token}
        self.launch = mock.patch("updater.subprocess.run", return_value=mock.Mock(returncode=0)).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.close_log_handlers)

    def close_log_handlers(self):
        for handler in list(self.module.app.logger.handlers):
            if hasattr(handler, "baseFilename") and self.temp.name in handler.baseFilename:
                self.module.app.logger.removeHandler(handler)
                handler.close()

    def test_update_requires_page_token_and_same_origin(self):
        self.assertEqual(self.client.post("/update", json={}).status_code, 403)
        self.assertEqual(self.client.post("/update", json={}, headers={
            **self.headers, "Origin": "https://untrusted.invalid",
        }).status_code, 403)
        self.launch.assert_not_called()

    def test_recording_blocks_update(self):
        self.collector.is_running.return_value = True
        self.assertEqual(self.client.post("/update", json={}, headers=self.headers).status_code, 409)
        self.launch.assert_not_called()

    def test_stopping_sensor_task_also_blocks_update(self):
        self.module.collection_tasks.append(FakeTask())
        self.assertEqual(self.client.post("/update", json={}, headers=self.headers).status_code, 409)
        self.assertTrue(self.client.get("/status").json["collector_active"])
        self.launch.assert_not_called()

    def test_update_launches_once_and_blocks_new_collection(self):
        response = self.client.post("/update", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json["busy"])
        self.assertEqual(self.client.post("/update", json={}, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post("/start").status_code, 409)
        self.assertEqual(self.client.post("/restart").status_code, 409)
        self.assertEqual(self.launch.call_count, 1)

    def test_unavailable_update_service_reports_failure(self):
        self.launch.side_effect = subprocess.CalledProcessError(1, "systemctl")
        response = self.client.post("/update", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.controller.status()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
