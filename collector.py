import time
import random  # replace this with your actual sensor imports later
import threading
import logging
import requests
import math
import shutil

from lib import DFRobot_MultiGasSensor as dfg
from lib.MHZ16_CO2 import MHZ16_CO2
from device_config import load_config, load_sampling_interval

import math

def _json_safe(v):
    # Convert NaN/Inf to None so the browser JSON parser won’t choke
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
    return v


logger = logging.getLogger(__name__)

# Websocket
socketio = None # will be set by app.py

# -------------------------
# GLOBALS
# -------------------------

collecting = False    # True when running
current_csv = None    # Active CSV file name
lock = threading.Lock()
last_emit_time = 0  # UNIX timestamp of last successful data emit
sampling_interval = load_sampling_interval()
starting = False  # Includes initialization and the first sample.
startup_started = 0
last_progress_time = 0
STARTUP_TIMEOUT_SECONDS = 180


class CollectionStopped(Exception):
    """Cooperative cancellation between initialization steps."""


def check_collecting():
    if not is_running():
        raise CollectionStopped()


def mark_progress():
    global last_progress_time
    with lock:
        last_progress_time = time.monotonic()


def watchdog_timeout_reason():
    with lock:
        if not collecting:
            return None
        now = time.monotonic()
        if starting and now - startup_started > STARTUP_TIMEOUT_SECONDS:
            return f"startup exceeded its {STARTUP_TIMEOUT_SECONDS}-second limit"
        if now - last_progress_time > max(30, sampling_interval * 5):
            return "no sensor initialization progress" if starting else "no collection progress"
    return None

# history: list of dicts {"time": ..., "sensors": {...}}
history = [] # last 100 points kept here

# -------------------------
# CONFIGURE SENSORS
# -------------------------

# --- PurpleAir endpoints (one per bank) ---
_device_config = load_config()
PURPLEAIR_1_JSON_URL = _device_config["purpleair_urls"]["bank1"]
PURPLEAIR_2_JSON_URL = _device_config["purpleair_urls"]["bank2"]

PURPLEAIR_CACHE_SECONDS = 10  # refresh at most every 10s

# independent caches (one per bank)
_purpleair_cache = {
    "bank1": {"data": None, "time": 0},
    "bank2": {"data": None, "time": 0},
}

# --- PurpleAir enable/disable per bank (temporary dev/testing switch) ---
PURPLEAIR_ENABLED = _device_config["purpleair_enabled"]

# All PurpleAir-derived fields UI/CSV expects
PURPLEAIR_FIELDS = [
    "pm1_0_atm", "pm1_0_atm_b", "pm2_5_atm", "pm2_5_atm_b",
    "pm10_0_atm", "pm10_0_atm_b", "p_0_3_um", "p_0_3_um_b",
    "p_0_5_um", "p_0_5_um_b", "p_1_0_um", "p_1_0_um_b",
    "p_2_5_um", "p_2_5_um_b", "p_5_0_um", "p_5_0_um_b",
    "p_10_0_um", "p_10_0_um_b", "temperature_c", "temperature_f",
    "current_humidity", "pressure_hpa", "pressure_pa"
]

# I2C_1 Use i2c1 interface (or i2c0 with configuring Raspberry Pi) to drive sensor
# I2C Device address can be changed by changing A1 and A0,
# the default address on the terminal board as shipped is 0x74

I2C_1 = 0x01
# Set gas sensor address

# --- CO2 (MH-Z16) UART ports ---
CO2_UART_PORT_1 = _device_config["co2_uart_ports"]["bank1"]
# DON'T USE /dev/ttyUSB0 for the USB one -> IT CAN CHANGE 
CO2_UART_PORT_2 = _device_config["co2_uart_ports"]["bank2"]

co2_sensor_1 = MHZ16_CO2(port=CO2_UART_PORT_1, baudrate=9600)
co2_sensor_2 = MHZ16_CO2(port=CO2_UART_PORT_2, baudrate=9600)

gas_o2_1 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x74)
gas_no2_1 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x75)
gas_so2_1 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x76)
gas_o3_1 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x77)
gas_co_1 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x70)

gas_o2_2 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x60)
gas_no2_2 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x61)
gas_so2_2 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x62)
gas_o3_2 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x63)
gas_co_2 = dfg.DFRobot_MultiGasSensor_I2C(I2C_1 ,0x64)

def setup_sensor(gas, name, retries=2, delay=1):
    """Attempt to initialize a gas sensor with retry logic."""
    for attempt in range(retries):
        check_collecting()
        try:
            if not gas.change_acquire_mode(gas.PASSIVITY):
                raise RuntimeError("Mode change failed")
            time.sleep(delay)
            check_collecting()
            gas.set_temp_compensation(gas.ON)
            check_collecting()
            mark_progress()
            logger.info(f"{name} sensor initialized (attempt {attempt + 1})")
            return True
        except CollectionStopped:
            raise
        except Exception as e:
            mark_progress()
            logger.warning(f"{name} sensor setup failed (attempt {attempt + 1}/{retries}): {e}")
            check_collecting()
            time.sleep(delay)
    logger.error(f"{name} sensor failed to initialize after {retries} attempts.")
    return False

def setup_co2_sensor(sensor, name="CO2", retries=3, delay=1):
    if sensor is None:
        logger.warning(f"{name}: not configured (no UART port).")
        return False

    for attempt in range(retries):
        check_collecting()
        try:
            val = sensor.read_co2()
            check_collecting()
            if val is None:
                raise RuntimeError("CO2 read returned None")
            mark_progress()
            logger.info(f"{name} initialized (attempt {attempt+1}), first read: {val} ppm")
            return True
        except CollectionStopped:
            raise
        except Exception as e:
            mark_progress()
            logger.warning(f"{name} setup failed (attempt {attempt+1}/{retries}): {e}")
            check_collecting()
            time.sleep(delay)

    logger.error(f"{name} failed to initialize after retries.")
    return False


def read_purpleair(bank_name: str, url: str):
    """Read PurpleAir JSON for a given bank with per-bank caching."""
    if not PURPLEAIR_ENABLED.get(bank_name, True):
        return {k: float("nan") for k in PURPLEAIR_FIELDS}

    now = time.time()
    cache = _purpleair_cache[bank_name]

    if cache["data"] and (now - cache["time"]) < PURPLEAIR_CACHE_SECONDS:
        return cache["data"]

    try:
        resp = requests.get(url, timeout=3)
        resp.raise_for_status()
        data = resp.json()

        keys = [
            "pm1_0_atm", "pm1_0_atm_b",
            "pm2_5_atm", "pm2_5_atm_b",
            "pm10_0_atm", "pm10_0_atm_b",
            "p_0_3_um", "p_0_3_um_b",
            "p_0_5_um", "p_0_5_um_b",
            "p_1_0_um", "p_1_0_um_b",
            "p_2_5_um", "p_2_5_um_b",
            "p_5_0_um", "p_5_0_um_b",
            "p_10_0_um", "p_10_0_um_b",
            "current_temp_f", "current_humidity", "pressure"
        ]

        result = {k: float("nan") for k in PURPLEAIR_FIELDS}
        for k in keys:
            if k not in data:
                continue

            fval = safe_float(data[k])

            if k == "current_temp_f" and not math.isnan(fval):
                result["temperature_f"] = fval
                result["temperature_c"] = (fval - 32) * 5.0 / 9.0
            elif k == "pressure" and not math.isnan(fval):
                result["pressure_hpa"] = fval
                result["pressure_pa"] = fval * 100
            else:
                result[k] = fval

        cache["data"] = result
        cache["time"] = now
        return result

    except Exception as e:
        logger.warning(f"PurpleAir JSON read failed ({bank_name}): {e}")
        if cache["data"]:
            return cache["data"]

        return {k: float("nan") for k in PURPLEAIR_FIELDS}


def safe_float(value):
    """Convert a string or number to float, returning NaN if invalid."""
    try:
        if value is None:
            return float("nan")
        if isinstance(value, (int, float)):
            return float(value)
        value = str(value).strip()
        if value in ["", "nan", "---", "-.--"]:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")

# Define all sensors and their units
# PurpleAir sensors (will be handled via JSON, not via generator)
# Define all sensors and their units

SENSOR_BANKS = {
    "bank1": {
        # --- Gas sensors ---
        "temperature": {"unit": "°C", "generator": lambda: gas_o2_1.temp},
        "o2": {"unit": "%Vol", "generator": lambda: gas_o2_1.read_gas_concentration()},
        "no2": {"unit": "ppm", "generator": lambda: gas_no2_1.read_gas_concentration()},
        "so2": {"unit": "ppm", "generator": lambda: gas_so2_1.read_gas_concentration()},
        "o3": {"unit": "ppm", "generator": lambda: gas_o3_1.read_gas_concentration()},
        "co": {"unit": "ppm", "generator": lambda: gas_co_1.read_gas_concentration()},
        "co2": {"unit": "ppm", "generator": (lambda: co2_sensor_1.read_co2())},

        # --- PurpleAir (generator None; filled from JSON) ---
        "pm1_0_atm": {"unit": "µg/m³", "generator": None},
        "pm1_0_atm_b": {"unit": "µg/m³", "generator": None},
        "pm2_5_atm": {"unit": "µg/m³", "generator": None},
        "pm2_5_atm_b": {"unit": "µg/m³", "generator": None},
        "pm10_0_atm": {"unit": "µg/m³", "generator": None},
        "pm10_0_atm_b": {"unit": "µg/m³", "generator": None},

        "p_0_3_um": {"unit": "particles/dL", "generator": None},
        "p_0_3_um_b": {"unit": "particles/dL", "generator": None},
        "p_0_5_um": {"unit": "particles/dL", "generator": None},
        "p_0_5_um_b": {"unit": "particles/dL", "generator": None},
        "p_1_0_um": {"unit": "particles/dL", "generator": None},
        "p_1_0_um_b": {"unit": "particles/dL", "generator": None},
        "p_2_5_um": {"unit": "particles/dL", "generator": None},
        "p_2_5_um_b": {"unit": "particles/dL", "generator": None},
        "p_5_0_um": {"unit": "particles/dL", "generator": None},
        "p_5_0_um_b": {"unit": "particles/dL", "generator": None},
        "p_10_0_um": {"unit": "particles/dL", "generator": None},
        "p_10_0_um_b": {"unit": "particles/dL", "generator": None},

        "temperature_c": {"unit": "°C", "generator": None},
        "temperature_f": {"unit": "°F", "generator": None},
        "current_humidity": {"unit": "%", "generator": None},
        "pressure_hpa": {"unit": "hPa", "generator": None},
        "pressure_pa": {"unit": "Pa", "generator": None},
    },

    "bank2": {
        # --- Gas sensors ---
        "temperature": {"unit": "°C", "generator": lambda: gas_o2_2.temp},
        "o2": {"unit": "%Vol", "generator": lambda: gas_o2_2.read_gas_concentration()},
        "no2": {"unit": "ppm", "generator": lambda: gas_no2_2.read_gas_concentration()},
        "so2": {"unit": "ppm", "generator": lambda: gas_so2_2.read_gas_concentration()},
        "o3": {"unit": "ppm", "generator": lambda: gas_o3_2.read_gas_concentration()},
        "co": {"unit": "ppm", "generator": lambda: gas_co_2.read_gas_concentration()},
        "co2": {"unit": "ppm", "generator": (lambda: co2_sensor_2.read_co2()) if co2_sensor_2 else None},

        # --- PurpleAir (generator None; filled from JSON) ---
        "pm1_0_atm": {"unit": "µg/m³", "generator": None},
        "pm1_0_atm_b": {"unit": "µg/m³", "generator": None},
        "pm2_5_atm": {"unit": "µg/m³", "generator": None},
        "pm2_5_atm_b": {"unit": "µg/m³", "generator": None},
        "pm10_0_atm": {"unit": "µg/m³", "generator": None},
        "pm10_0_atm_b": {"unit": "µg/m³", "generator": None},

        "p_0_3_um": {"unit": "particles/dL", "generator": None},
        "p_0_3_um_b": {"unit": "particles/dL", "generator": None},
        "p_0_5_um": {"unit": "particles/dL", "generator": None},
        "p_0_5_um_b": {"unit": "particles/dL", "generator": None},
        "p_1_0_um": {"unit": "particles/dL", "generator": None},
        "p_1_0_um_b": {"unit": "particles/dL", "generator": None},
        "p_2_5_um": {"unit": "particles/dL", "generator": None},
        "p_2_5_um_b": {"unit": "particles/dL", "generator": None},
        "p_5_0_um": {"unit": "particles/dL", "generator": None},
        "p_5_0_um_b": {"unit": "particles/dL", "generator": None},
        "p_10_0_um": {"unit": "particles/dL", "generator": None},
        "p_10_0_um_b": {"unit": "particles/dL", "generator": None},

        "temperature_c": {"unit": "°C", "generator": None},
        "temperature_f": {"unit": "°F", "generator": None},
        "current_humidity": {"unit": "%", "generator": None},
        "pressure_hpa": {"unit": "hPa", "generator": None},
        "pressure_pa": {"unit": "Pa", "generator": None},
    },
}


FORMAT_PRECISION = {
    "temperature": 2,
    "o2": 2,
    "no2": 2,
    "so2": 2,
    "o3": 2,
    "co": 2,
    "co2": 0,

    "temperature_c": 2,
    "temperature_f": 2,
    "pressure_hpa": 1,
    "pressure_pa": 0,
    
    "pm1_0_atm": 0,
    "pm1_0_atm_b": 0,
    "pm2_5_atm": 0,
    "pm2_5_atm_b": 0,
    "pm10_0_atm": 0,
    "pm10_0_atm_b": 0,
    "p_0_3_um": 0,
    "p_0_3_um_b": 0,
    "p_0_5_um": 0,
    "p_0_5_um_b": 0,
    "p_1_0_um": 0,
    "p_1_0_um_b": 0,
    "p_2_5_um": 0,
    "p_2_5_um_b": 0,
    "p_5_0_um": 0,
    "p_5_0_um_b": 0,
    "p_10_0_um": 0,
    "p_10_0_um_b": 0,
}

# -------------------------
# EXTRA
# -------------------------

def get_storage_info(path="/"):
    """Return (free_gb, total_gb, used_percent) for the given path."""
    try:
        usage = shutil.disk_usage(path)
        total_gb = usage.total / (1024**3)
        free_gb = usage.free / (1024**3)
        used_percent = (usage.used / usage.total) * 100
        return free_gb, total_gb, used_percent
    except Exception as e:
        logger.warning(f"Failed to check storage: {e}")
        return None, None, None

def seconds_since_last_emit():
    """Return seconds since last data emit."""
    if last_emit_time == 0:
        return float("inf")
    return time.time() - last_emit_time

def set_sampling_interval(seconds: float):
    """Update sampling interval safely."""
    global sampling_interval
    with lock:
        sampling_interval = max(1.0, min(60.0, float(seconds)))  # enforce 1–60
    logger.info(f"Sampling interval set to {sampling_interval:.2f} seconds.")

# -------------------------
# STATE HANDLERS
# -------------------------
def set_collecting_flag(flag: bool):
    """Set start/stop flag from Flask."""
    global collecting, starting, startup_started, last_progress_time
    with lock:
        if flag and not collecting:
            starting = True
            startup_started = last_progress_time = time.monotonic()
        collecting = flag

def set_csv_file(filename: str):
    """Set current CSV file name."""
    global current_csv
    with lock:
        current_csv = filename

def is_running():
    """Return True if collection is active."""
    with lock:
        return collecting

def get_history():
    """Return recent sensor history for reconnects."""
    with lock:
        return list(history)

# -------------------------
# MAIN COLLECTION THREAD
# -------------------------
def start_collecting():
    """Continuously read data and append to memory + CSV file."""
    global collecting, starting
    try:
        check_collecting()
        global current_csv, history
        
        # Reset timestamp of successful emit
        global last_emit_time
        last_emit_time = time.time()

        # --- Initialize all gas sensors with retries ---

        gas_status = {
        # bank1
        "bank1_o2":  setup_sensor(gas_o2_1,  "O₂ (bank1)"),
        "bank1_no2": setup_sensor(gas_no2_1, "NO₂ (bank1)"),
        "bank1_so2": setup_sensor(gas_so2_1, "SO₂ (bank1)"),
        "bank1_o3":  setup_sensor(gas_o3_1,  "O₃ (bank1)"),
        "bank1_co":  setup_sensor(gas_co_1,  "CO (bank1)"),
        "bank1_co2": setup_co2_sensor(co2_sensor_1, "CO₂ (bank1)"),

        # bank2
        "bank2_o2":  setup_sensor(gas_o2_2,  "O₂ (bank2)"),
        "bank2_no2": setup_sensor(gas_no2_2, "NO₂ (bank2)"),
        "bank2_so2": setup_sensor(gas_so2_2, "SO₂ (bank2)"),
        "bank2_o3":  setup_sensor(gas_o3_2,  "O₃ (bank2)"),
        "bank2_co":  setup_sensor(gas_co_2,  "CO (bank2)"),
        "bank2_co2": setup_co2_sensor(co2_sensor_2, "CO₂ (bank2)"),
        }

        for name, ok in gas_status.items():
            if not ok:
                logger.warning(f"{name} failed init, continuing with partial data.")


        check_collecting()
        logger.info("Sensor initialization complete: %s/%s sensors available.",
                    sum(gas_status.values()), len(gas_status))

        # --- Log available storage before starting ---
        free_gb, total_gb, used_percent = get_storage_info("/")
        if free_gb is not None:
            logger.info(f"Available storage: {free_gb:.2f} GB free of {total_gb:.2f} GB "
                        f"({used_percent:.1f}% used)")
            if used_percent >= 90:
                logger.warning("Storage nearly full — consider freeing space or offloading data.")


        while True:
            with lock:
                if not collecting:
                    break

            # Generate readings using the SENSORS definition
            # sensors = {name: info["generator"]() for name, info in SENSORS.items()}

            banks_payload = {}
            timestamp = time.strftime("%H:%M:%S")

            for bank_name, bank_sensors in SENSOR_BANKS.items():
                # PurpleAir per bank
                if PURPLEAIR_ENABLED.get(bank_name, True):
                    pa_url = PURPLEAIR_1_JSON_URL if bank_name == "bank1" else PURPLEAIR_2_JSON_URL
                    purpleair_data = read_purpleair(bank_name, pa_url)
                else:
                    purpleair_data = {k: float("nan") for k in PURPLEAIR_FIELDS}
                mark_progress()

                bank_values = {}
                for name, info in bank_sensors.items():
                    try:
                        if name in purpleair_data:
                            bank_values[name] = purpleair_data[name]
                        elif callable(info["generator"]):
                            bank_values[name] = info["generator"]()
                        else:
                            bank_values[name] = None
                    except Exception as e:
                        bank_values[name] = None
                        logger.exception(f"{bank_name}:{name} read failed: {e}")
                    mark_progress()

                banks_payload[bank_name] = bank_values


            # Append to rolling history (keep last 100 points)
            # Sanitize payload
            for bank, sensors in banks_payload.items():
                for k, v in list(sensors.items()):
                    sensors[k] = _json_safe(v)
            with lock:
                history.append({"time": timestamp, "banks": banks_payload})
                if len(history) > 100:
                    history.pop(0)

            # Stream to CSV
            if current_csv:
                try:
                    with open(current_csv, "a", encoding="utf-8") as f:
                        flat_values = []
                        for bank_name, bank_sensors in SENSOR_BANKS.items():
                            for sensor_name in bank_sensors.keys():
                                v = banks_payload[bank_name].get(sensor_name)

                                if v is None or (isinstance(v, float) and math.isnan(v)):
                                    flat_values.append("")
                                elif isinstance(v, (int, float)):
                                    prec = FORMAT_PRECISION.get(sensor_name, 3)
                                    flat_values.append(f"{v:.{prec}f}")
                                else:
                                    flat_values.append(str(v))

                        line = f"{timestamp}," + ",".join(flat_values) + "\n"
                        f.write(line)
                except Exception as e:
                    logger.exception(f"CSV write error: {e}")

            # Emit data live via WebSocket
            if socketio:
                #logger.info(f"socketio is {'set' if socketio else 'NONE'}; emitting new_data")
                #socketio.emit("new_data", {"time": timestamp, "banks": banks_payload})
                socketio.emit("new_data", {"time": timestamp, "banks": banks_payload}, namespace="/")
                socketio.sleep(0)
                # record timestamp of successful emit
                last_emit_time = time.time()


            mark_progress()
            # collection interval (not exact because of other delays and overhead)
            with lock:
                starting = False
                interval = sampling_interval
            if socketio:
                socketio.sleep(interval)
            else:
                time.sleep(interval)
    except CollectionStopped:
        logger.info("Collection stopped during sensor initialization.")
    except Exception:
        logger.exception("Collector crashed")
    finally:
        with lock:
            collecting = False
            starting = False
