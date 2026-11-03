"""Watchdog regressions with fake sensors and clocks; no hardware is opened."""

import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import test_updater


PROJECT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    time = monotonic

    def sleep(self, seconds):
        self.now += seconds

    def strftime(self, _format):
        return "12:00:00"


class CollectorWatchdogTests(unittest.TestCase):
    def setUp(self):
        gas = types.ModuleType("lib.DFRobot_MultiGasSensor")
        gas.DFRobot_MultiGasSensor_I2C = mock.Mock(side_effect=lambda *args: mock.Mock())
        co2 = types.ModuleType("lib.MHZ16_CO2")
        co2.MHZ16_CO2 = mock.Mock(side_effect=lambda *args, **kwargs: mock.Mock())
        spec = importlib.util.spec_from_file_location("collector_watchdog_test", PROJECT / "collector.py")
        self.collector = importlib.util.module_from_spec(spec)
        with mock.patch.dict("sys.modules", {"lib.DFRobot_MultiGasSensor": gas, "lib.MHZ16_CO2": co2}), \
                mock.patch("device_config.load_sampling_interval", return_value=2.0):
            spec.loader.exec_module(self.collector)
        self.clock = Clock()
        self.collector.time = self.clock
        self.collector.logger = mock.Mock()
        self.collector.set_collecting_flag(True)

    def test_slow_initialization_with_progress_does_not_trigger_watchdog(self):
        for _ in range(12):
            self.clock.sleep(7)
            self.collector.mark_progress()
            self.assertIsNone(self.collector.watchdog_timeout_reason())
        self.assertEqual(self.collector.last_emit_time, 0)
        self.assertTrue(self.collector.starting)

    def test_initialization_with_no_progress_is_still_detected(self):
        self.clock.sleep(31)
        self.assertEqual(self.collector.watchdog_timeout_reason(), "no sensor initialization progress")

    def test_startup_has_an_overall_limit_even_if_attempts_keep_progressing(self):
        self.clock.sleep(181)
        self.collector.mark_progress()
        self.assertIn("180-second limit", self.collector.watchdog_timeout_reason())

    def test_runtime_stall_is_detected_after_startup(self):
        self.collector.starting = False
        self.clock.sleep(31)
        self.assertEqual(self.collector.watchdog_timeout_reason(), "no collection progress")

    def test_long_sampling_cycle_is_healthy_while_sensor_reads_progress(self):
        self.collector.starting = False
        for _ in range(8):
            self.clock.sleep(8)
            self.collector.mark_progress()
            self.assertIsNone(self.collector.watchdog_timeout_reason())

    def test_stopped_collection_does_not_trigger_watchdog(self):
        self.collector.set_collecting_flag(False)
        self.clock.sleep(1000)
        self.assertIsNone(self.collector.watchdog_timeout_reason())

    def test_stop_during_gas_failure_cancels_remaining_attempts(self):
        gas = mock.Mock()
        def fail_and_stop(_mode):
            self.collector.set_collecting_flag(False)
            return False
        gas.change_acquire_mode.side_effect = fail_and_stop
        with self.assertRaises(self.collector.CollectionStopped):
            self.collector.setup_sensor(gas, "test")
        self.assertEqual(gas.change_acquire_mode.call_count, 1)
        gas.set_temp_compensation.assert_not_called()

    def test_stop_during_success_delay_skips_compensation_and_next_sensor(self):
        def stop_during_delay(seconds):
            self.clock.sleep(seconds)
            self.collector.set_collecting_flag(False)
        self.collector.time = types.SimpleNamespace(
            time=self.clock.time, monotonic=self.clock.monotonic, sleep=stop_during_delay,
        )
        self.collector.gas_o2_1.change_acquire_mode.return_value = True
        self.collector.start_collecting()
        self.collector.gas_o2_1.set_temp_compensation.assert_not_called()
        self.collector.gas_no2_1.change_acquire_mode.assert_not_called()
        self.assertFalse(self.collector.collecting)
        self.assertFalse(self.collector.starting)

    def test_stop_during_co2_read_cancels_remaining_attempts(self):
        sensor = mock.Mock()
        def read_and_stop():
            self.collector.set_collecting_flag(False)
            return None
        sensor.read_co2.side_effect = read_and_stop
        with self.assertRaises(self.collector.CollectionStopped):
            self.collector.setup_co2_sensor(sensor)
        sensor.read_co2.assert_called_once()

    def test_initialization_retry_counts_are_unchanged(self):
        gas = mock.Mock()
        gas.change_acquire_mode.return_value = False
        self.assertFalse(self.collector.setup_sensor(gas, "test"))
        self.assertEqual(gas.change_acquire_mode.call_count, 2)
        sensor = mock.Mock()
        sensor.read_co2.return_value = None
        self.assertFalse(self.collector.setup_co2_sensor(sensor))
        self.assertEqual(sensor.read_co2.call_count, 3)

    def test_sampling_values_and_csv_format_are_preserved(self):
        collector = self.collector
        collector.SENSOR_BANKS = {
            "bank1": {"o2": {"generator": lambda: 20.123}, "co2": {"generator": lambda: 480}},
            "bank2": {"o2": {"generator": lambda: float("nan")}},
        }
        collector.PURPLEAIR_ENABLED = {"bank1": False, "bank2": False}
        collector.get_storage_info = mock.Mock(return_value=(None, None, None))
        collector.socketio = types.SimpleNamespace(
            emit=lambda *args, **kwargs: collector.set_collecting_flag(False), sleep=self.clock.sleep,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.csv"
            collector.current_csv = str(path)
            with mock.patch.object(collector, "setup_sensor", return_value=False), \
                    mock.patch.object(collector, "setup_co2_sensor", return_value=True):
                collector.start_collecting()
            self.assertEqual(path.read_text(), "12:00:00,20.12,480,\n")
        self.assertEqual(collector.history[0]["banks"]["bank2"]["o2"], None)
        collector.logger.info.assert_any_call(
            "Sensor initialization complete: %s/%s sensors available.", 2, 12,
        )
        self.assertFalse(collector.starting)
        self.assertFalse(collector.collecting)


class SafeRestartTests(unittest.TestCase):
    setUp = test_updater.UpdateRouteTests.setUp
    close_log_handlers = test_updater.UpdateRouteTests.close_log_handlers

    def prepare_restart(self):
        self.clock = Clock()
        self.module.time = self.clock
        self.running = True
        self.collector.is_running.side_effect = lambda: self.running
        def set_running(flag):
            self.running = flag
        self.collector.set_collecting_flag = mock.Mock(side_effect=set_running)
        self.collector.start_collecting = mock.Mock()
        self.old_task = mock.Mock()
        self.old_task.ready.return_value = False
        self.module.collection_tasks[:] = [self.old_task]
        self.module.socketio.start_background_task = mock.Mock(return_value=test_updater.FakeTask())
        self.module.socketio.sleep = self.clock.sleep

    def test_replacement_starts_only_after_previous_task_exits(self):
        self.prepare_restart()
        def finish_on_wait(seconds):
            self.clock.sleep(seconds)
            self.module.socketio.start_background_task.assert_not_called()
            self.old_task.ready.return_value = True
        self.module.socketio.sleep = finish_on_wait
        self.assertTrue(self.module.restart_collector_safely())
        self.collector.set_collecting_flag.assert_has_calls([mock.call(False), mock.call(True)])
        self.module.socketio.start_background_task.assert_called_once_with(self.collector.start_collecting)
        self.assertEqual(self.module.maintenance_until, 0)

    def test_unresponsive_task_keeps_collection_stopped_without_replacement(self):
        self.prepare_restart()
        self.assertFalse(self.module.restart_collector_safely())
        self.assertFalse(self.running)
        self.module.socketio.start_background_task.assert_not_called()
        self.assertEqual(self.module.maintenance_until, 0)

    def test_manual_stop_cancels_pending_restart_and_starts_are_blocked(self):
        self.prepare_restart()
        def press_stop(seconds):
            self.clock.sleep(seconds)
            self.assertEqual(self.client.post("/start").status_code, 409)
            self.assertEqual(self.client.post("/update", json={}, headers=self.headers).status_code, 409)
            self.assertEqual(self.client.post("/stop").status_code, 200)
            self.old_task.ready.return_value = True
        self.module.socketio.sleep = press_stop
        self.assertFalse(self.module.restart_collector_safely())
        self.assertFalse(self.running)
        self.module.socketio.start_background_task.assert_not_called()

    def test_launch_guard_rejects_duplicate_collector(self):
        self.prepare_restart()
        with self.assertRaisesRegex(RuntimeError, "previous collector"):
            self.module.launch_collector()
        self.module.socketio.start_background_task.assert_not_called()

    def test_status_emit_failure_releases_maintenance_reservation(self):
        self.prepare_restart()
        self.module.socketio.emit = mock.Mock(side_effect=RuntimeError("status emission failed"))
        with self.assertRaisesRegex(RuntimeError, "status emission failed"):
            self.module.restart_collector_safely()
        self.assertEqual(self.module.maintenance_until, 0)
        self.assertFalse(self.running)
        self.module.socketio.start_background_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
