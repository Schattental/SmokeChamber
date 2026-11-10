"""Check Control CSV/header alignment through the real route and mocked collector."""

from contextlib import chdir
import csv
from pathlib import Path
import types
import unittest
from unittest import mock

import test_collector_watchdog
import test_updater


class ControlRecordingTests(unittest.TestCase):
    setUp = test_updater.UpdateRouteTests.setUp
    close_log_handlers = test_updater.UpdateRouteTests.close_log_handlers

    def test_new_recording_appends_control_columns_and_reads_its_own_endpoint(self):
        hardware = types.SimpleNamespace()
        test_collector_watchdog.CollectorWatchdogTests.setUp(hardware)
        collector = hardware.collector
        collector.set_collecting_flag(False)
        collector.PURPLEAIR_ENABLED = {"bank1": True, "bank2": True, "control": True}
        collector.PURPLEAIR_JSON_URLS = {
            bank: f"http://{bank}.invalid/json" for bank in collector.SENSOR_BANKS
        }
        # Keep the production field definitions/ordering, but never read hardware.
        collector.SENSOR_BANKS = {
            bank: {name: {**info, "generator": (lambda: 20.123) if callable(info["generator"]) else None}
                   for name, info in fields.items()}
            for bank, fields in collector.SENSOR_BANKS.items()
        }
        levels = {"bank1": 11, "bank2": 22, "control": 33}
        def response_for(url, **kwargs):
            bank = next(bank for bank, endpoint in collector.PURPLEAIR_JSON_URLS.items() if endpoint == url)
            data = {name: levels[bank] for name in collector.PURPLEAIR_FIELDS}
            data.update(pm2_5_atm_b=levels[bank] + 1, current_temp_f=68, pressure=1000)
            response = mock.Mock()
            response.json.return_value = data
            return response
        get = mock.Mock(side_effect=response_for)
        collector.requests = types.SimpleNamespace(get=get)
        collector.get_storage_info = mock.Mock(return_value=(None, None, None))
        collector.socketio = types.SimpleNamespace(
            emit=mock.Mock(side_effect=lambda *args, **kwargs: collector.set_collecting_flag(False)),
            sleep=hardware.clock.sleep,
        )
        self.module.collector = collector
        with chdir(self.temp.name):
            Path("data").mkdir()
            self.assertEqual(self.client.post("/start").json["status"], "started")
            with mock.patch.object(collector, "setup_sensor", return_value=True) as gas_setup, \
                    mock.patch.object(collector, "setup_co2_sensor", return_value=True) as co2_setup:
                collector.start_collecting()
            with Path(collector.current_csv).open(newline="") as recording:
                header, row = list(csv.reader(recording))

        # Golden pre-Control column order and units, independent of SENSOR_BANKS.
        original_fields = [("temperature", "°C"), ("o2", "%Vol"), ("no2", "ppm"),
                           ("so2", "ppm"), ("o3", "ppm"), ("co", "ppm"), ("co2", "ppm")]
        for name in ("pm1_0_atm", "pm2_5_atm", "pm10_0_atm"):
            original_fields.extend([(name, "µg/m³"), (name + "_b", "µg/m³")])
        for size in ("0_3", "0_5", "1_0", "2_5", "5_0", "10_0"):
            original_fields.extend([(f"p_{size}_um", "particles/dL"), (f"p_{size}_um_b", "particles/dL")])
        original_fields.extend([("temperature_c", "°C"), ("temperature_f", "°F"),
                                ("current_humidity", "%"), ("pressure_hpa", "hPa"), ("pressure_pa", "Pa")])
        expected_old_header = ["timestamp"] + [
            f"{bank}_{name} ({unit})" for bank in ("bank1", "bank2") for name, unit in original_fields
        ]
        self.assertEqual(header[:61], expected_old_header)
        self.assertEqual(header[61:], [f"control_{name} ({unit})" for name, unit in original_fields[7:]])
        self.assertEqual(len(row), len(header))
        self.assertEqual(len(header), 84)
        values = dict(zip(header, row))
        self.assertEqual(values["bank1_pm2_5_atm (µg/m³)"], "11")
        self.assertEqual(values["bank2_pm2_5_atm (µg/m³)"], "22")
        self.assertEqual(values["control_pm2_5_atm (µg/m³)"], "33")
        self.assertEqual(values["control_pm2_5_atm_b (µg/m³)"], "34")
        self.assertEqual(values["control_temperature_c (°C)"], "20.00")
        self.assertEqual(values["control_pressure_pa (Pa)"], "100000")
        self.assertEqual(gas_setup.call_count, 10)
        self.assertEqual(co2_setup.call_count, 2)
        self.assertEqual(get.call_args_list, [
            mock.call(collector.PURPLEAIR_JSON_URLS[bank], timeout=3)
            for bank in ("bank1", "bank2", "control")
        ])
        live = collector.socketio.emit.call_args.args[1]["banks"]["control"]
        self.assertEqual(live["pm2_5_atm"], 33)
        self.assertEqual(live["pm2_5_atm_b"], 34)
        self.assertEqual(set(live), set(collector.PURPLEAIR_FIELDS))

    def test_control_tab_follows_bank2_and_is_not_a_gas_address_option(self):
        self.module.app.template_folder = str(test_updater.PROJECT / "templates")
        html = self.client.get("/").get_data(as_text=True)
        self.assertLess(html.index("switchBank('bank2'"), html.index("switchBank('control'"))
        self.assertIn("switchBank('control', this)\">Control</button>", html)
        self.assertNotIn('<option value="control">', html)


if __name__ == "__main__":
    unittest.main()
