"""Device-local hardware settings, preserved across source updates."""

import json
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent / "config.local.json"
DEFAULTS = {
    "purpleair_urls": {
        "bank1": "http://192.168.0.104/json?live=true",
        "bank2": "http://192.168.0.109/json?live=true",
    },
    "purpleair_enabled": {"bank1": True, "bank2": True},
    "co2_uart_ports": {
        "bank1": "/dev/ttyAMA0",
        "bank2": "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0",
    },
}


def load_config(path=CONFIG_PATH):
    config = {key: dict(value) for key, value in DEFAULTS.items()}
    if Path(path).exists():
        overrides = json.loads(Path(path).read_text())
        if not isinstance(overrides, dict) or set(overrides) - set(DEFAULTS):
            raise ValueError("Invalid device configuration sections")
        for section, values in overrides.items():
            if not isinstance(values, dict) or set(values) - {"bank1", "bank2"}:
                raise ValueError(f"Invalid {section} bank configuration")
            for bank, value in values.items():
                if section == "purpleair_enabled":
                    valid = isinstance(value, bool)
                else:
                    valid = isinstance(value, str) and bool(value.strip())
                if not valid:
                    raise ValueError(f"Invalid setting: {section}.{bank}")
                config[section][bank] = value
    return config
