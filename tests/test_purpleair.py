"""PurpleAir regressions with fake HTTP responses, clocks, and hardware."""

import math
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import test_collector_watchdog


class PurpleAirTests(unittest.TestCase):
    def setUp(self):
        test_collector_watchdog.CollectorWatchdogTests.setUp(self)
        self.collector.PURPLEAIR_ENABLED = {"bank1": True, "bank2": True}
        self.get = mock.Mock()
        self.collector.requests = types.SimpleNamespace(get=self.get)
        self.response = mock.Mock()
        self.response.json.return_value = {
            "pm2_5_atm": "12.5", "pm2_5_atm_b": "13.5",
            "current_temp_f": 68, "pressure": 1000,
        }
        self.get.return_value = self.response
        self.started = self.clock.now

    def read(self, bank="bank1"):
        return self.collector.read_purpleair(bank, f"http://{bank}.invalid/json")

    def assert_missing(self, values):
        self.assertEqual(set(values), set(self.collector.PURPLEAIR_FIELDS))
        self.assertTrue(all(math.isnan(v) for v in values.values()))

    def expiry_warnings(self):
        return [call for call in self.collector.logger.warning.call_args_list
                if call.args[0].startswith("PurpleAir cached reading expired")]

    def test_success_is_cached_for_ten_seconds_and_conversions_are_preserved(self):
        first = self.read()
        self.assertEqual(first["pm2_5_atm"], 12.5)
        self.assertEqual(first["pm2_5_atm_b"], 13.5)
        self.assertEqual(first["temperature_c"], 20)
        self.assertEqual(first["pressure_pa"], 100000)
        self.clock.sleep(9.99)
        self.assertIs(self.read(), first)
        self.get.assert_called_once_with("http://bank1.invalid/json", timeout=3)
        self.clock.now = self.started + 10
        self.read()
        self.assertEqual(self.get.call_count, 2)

    def test_initial_failure_returns_missing_and_waits_after_request_finishes(self):
        def timeout(*args, **kwargs):
            self.clock.sleep(3)
            raise TimeoutError("connection timed out")
        self.get.side_effect = timeout
        self.assert_missing(self.read())
        self.clock.sleep(9.99)
        self.assert_missing(self.read())
        self.assertEqual(self.get.call_count, 1)
        self.clock.now = self.started + 13
        self.assert_missing(self.read())
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(self.expiry_warnings(), [])

    def test_failed_refresh_reuses_recent_value_without_repeated_requests(self):
        first = self.read()
        self.get.side_effect = TimeoutError("connection timed out")
        self.clock.sleep(10)
        self.assertIs(self.read(), first)
        self.clock.sleep(2)
        self.assertIs(self.read(), first)
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(self.expiry_warnings(), [])

    def test_expiry_is_checked_during_cooldown_and_warns_once(self):
        first = self.read()
        self.get.side_effect = TimeoutError("connection timed out")
        self.clock.sleep(60)
        self.assertIs(self.read(), first)
        self.clock.sleep(0.01)
        self.assert_missing(self.read())
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(len(self.expiry_warnings()), 1)
        self.clock.now = self.started + 70
        self.assert_missing(self.read())
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(len(self.expiry_warnings()), 1)

    def test_timeout_crossing_age_limit_does_not_return_expired_data(self):
        self.read()
        self.clock.sleep(59)
        def timeout(*args, **kwargs):
            self.clock.sleep(3)
            raise TimeoutError("response timed out")
        self.get.side_effect = timeout
        self.assert_missing(self.read())
        self.assertEqual(len(self.expiry_warnings()), 1)

    def test_recovery_restores_values_and_allows_warning_on_a_later_outage(self):
        self.read()
        self.get.side_effect = TimeoutError("connection timed out")
        self.clock.sleep(61)
        self.assert_missing(self.read())
        self.get.side_effect = None
        self.response.json.return_value = {"pm2_5_atm": 42}
        self.clock.sleep(10)
        self.assertEqual(self.read()["pm2_5_atm"], 42)
        self.get.side_effect = TimeoutError("connection timed out")
        self.clock.sleep(61)
        self.assert_missing(self.read())
        self.assertEqual(len(self.expiry_warnings()), 2)

    def test_banks_have_independent_attempt_and_freshness_state(self):
        self.read("bank1")
        self.get.side_effect = TimeoutError("bank1 unavailable")
        self.clock.sleep(61)
        self.assert_missing(self.read("bank1"))
        self.get.side_effect = None
        self.assertEqual(self.read("bank2")["pm2_5_atm"], 12.5)
        self.clock.sleep(2)
        self.assert_missing(self.read("bank1"))
        self.assertEqual(self.read("bank2")["pm2_5_atm"], 12.5)
        self.assertEqual(self.get.call_count, 3)

    def test_disabled_bank_does_not_request_or_warn(self):
        self.collector.PURPLEAIR_ENABLED["bank1"] = False
        self.assert_missing(self.read())
        self.get.assert_not_called()
        self.collector.logger.warning.assert_not_called()

    def test_expired_values_are_blank_in_csv_and_null_in_live_data(self):
        collector = self.collector
        self.read()
        self.get.side_effect = TimeoutError("connection timed out")
        self.clock.sleep(61)
        collector.SENSOR_BANKS = {"bank1": {
            "pm2_5_atm": {"generator": None},
            "o2": {"generator": lambda: 20.123},
        }}
        collector.get_storage_info = mock.Mock(return_value=(None, None, None))
        collector.socketio = types.SimpleNamespace(
            emit=mock.Mock(side_effect=lambda *args, **kwargs: collector.set_collecting_flag(False)),
            sleep=self.clock.sleep,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.csv"
            collector.current_csv = str(path)
            with mock.patch.object(collector, "setup_sensor", return_value=True), \
                    mock.patch.object(collector, "setup_co2_sensor", return_value=True):
                collector.start_collecting()
            self.assertEqual(path.read_text(), "12:00:00,,20.12\n")
        live = collector.socketio.emit.call_args.args[1]["banks"]["bank1"]
        self.assertIsNone(live["pm2_5_atm"])
        self.assertEqual(live["o2"], 20.123)


if __name__ == "__main__":
    unittest.main()
