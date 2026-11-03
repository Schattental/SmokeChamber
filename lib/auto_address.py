# lib/auto_address.py

import subprocess
import time
from lib.DFRobot_MultiGasSensor import DFRobot_MultiGasSensor_I2C

I2C_BUS = 0x01

# Exact mapping matching collector.py :contentReference[oaicite:2]{index=2}
BANK_CONFIG = {
    "bank1": {
        "o2":  {"address": 0x74, "group": 6},
        "no2": {"address": 0x75, "group": 6},
        "so2": {"address": 0x76, "group": 6},
        "o3":  {"address": 0x77, "group": 6},
        "co":  {"address": 0x70, "group": 5},
    },
    "bank2": {
        "o2":  {"address": 0x60, "group": 1},
        "no2": {"address": 0x61, "group": 1},
        "so2": {"address": 0x62, "group": 1},
        "o3":  {"address": 0x63, "group": 1},
        "co":  {"address": 0x64, "group": 2},
    }
}

VALID_RANGE = range(0x60, 0x78)


def scan_i2c():
    """
    Scan bus and return first detected gas sensor address.
    Assumes only ONE device connected.
    """
    output = subprocess.check_output(["i2cdetect", "-y", "1"]).decode()
    lines = output.split("\n")

    found = []

    for line in lines[1:]:
        parts = line.split()
        for p in parts[1:]:
            if p != "--":
                addr = int(p, 16)
                if addr in VALID_RANGE:
                    found.append(addr)

    if len(found) == 0:
        raise RuntimeError("No valid gas sensor detected (0x60–0x77).")

    if len(found) > 1:
        raise RuntimeError("Multiple I2C devices detected. Connect only one sensor.")

    return found[0]


def change_sensor_address(sensor: str, bank: str):
    if bank not in BANK_CONFIG:
        return "Invalid bank"

    if sensor not in BANK_CONFIG[bank]:
        return "Invalid sensor"

    current_addr = scan_i2c()

    target_info = BANK_CONFIG[bank][sensor]
    target_addr = target_info["address"]
    target_group = target_info["group"]

    if current_addr == target_addr:
        return f"Already set to {hex(target_addr)}"

    gas = DFRobot_MultiGasSensor_I2C(I2C_BUS, current_addr)

    # Put sensor into passive mode
    try:
        gas.change_acquire_mode(gas.PASSIVITY)
        time.sleep(1)
    except Exception:
        pass

    # Attempt group change (retry loop)
    success = False
    for _ in range(5):
        if gas.change_i2c_addr_group(target_group) != 0:
            success = True
            break
        time.sleep(1)

    if not success:
        return "Address change command failed. Check wiring and DIP switches."

    # Wait for possible reboot
    time.sleep(2)

    # Re-scan bus
    try:
        new_addr = scan_i2c()
    except Exception:
        return (
            f"Address change issued to {hex(target_addr)}.\n"
            "Power cycle the gas sensor (unplug and reconnect)."
        )

    if new_addr == target_addr:
        return f"{sensor.upper()} successfully set to {bank} ({hex(target_addr)})"

    else:
        return (
            f"Address change issued to {hex(target_addr)}, "
            f"but sensor still responds at {hex(new_addr)}.\n"
            "Power cycle the gas sensor (unplug and reconnect)."
        )

