from gevent import monkey
monkey.patch_all()
import os
import time
import subprocess
import threading
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, jsonify, send_file, request, g
from flask_socketio import SocketIO
import zipfile
import io
from datetime import datetime, timedelta
from collections import deque
import time
import secrets
from urllib.parse import urlsplit

import collector

from lib.auto_address import change_sensor_address
from updater import Updater, UpdateError
from device_config import save_sampling_interval


app = Flask(__name__)

# Setup Websocket
#socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
#socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent",
                    logger=True, engineio_logger=True)
collector.socketio = socketio

# -------------------------
# LOGGING (rotating file)
# -------------------------
os.makedirs("logs", exist_ok=True)
log_handler = RotatingFileHandler("logs/app.log", maxBytes=5_000_000, backupCount=5)
log_handler.setLevel(logging.INFO)
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
log_handler.setFormatter(log_formatter)
app.logger.addHandler(log_handler)
app.logger.setLevel(logging.INFO)

collector.logger = app.logger
# -------------------------
# STATE
# -------------------------
current_csv = None
updater = Updater()
update_token = secrets.token_urlsafe(32)
collection_tasks = []
maintenance_until = 0


def collection_busy():
    """A stopped flag alone does not prove the last sensor read has finished."""
    collection_tasks[:] = [task for task in collection_tasks if not task.ready()]
    return collector.is_running() or bool(collection_tasks)


def launch_collector():
    collection_tasks.append(socketio.start_background_task(collector.start_collecting))


@app.before_request
def protect_chamber_operations():
    if request.method == "POST" and request.path in {
            "/start", "/restart", "/reboot", "/change_sensor_group", "/set_interval"}:
        if time.monotonic() < maintenance_until:
            return jsonify(status="blocked", message="The chamber is restarting."), 409
        operation = updater.operation()
        try:
            operation.__enter__()
        except UpdateError as exc:
            return jsonify(status="blocked", message=str(exc)), exc.status_code
        g.chamber_operation = operation


@app.teardown_request
def release_chamber_operation(_error):
    operation = g.pop("chamber_operation", None)
    if operation is not None:
        operation.__exit__(None, None, None)

# -------------------------
# WATCHDOG CONFIG
# -------------------------
WATCHDOG_MAX_RESTARTS = 2          # allow 2 automatic restarts
WATCHDOG_WINDOW_SECONDS = 10 * 60  # 10-minute window

# Stores timestamps (time.time()) of recent watchdog restarts
_watchdog_restart_times = deque()

# -------------------------
# ROUTES
# -------------------------
@app.route("/")
def index():
    return render_template("index.html", update_token=update_token)


@app.route("/update/status")
def update_status():
    response = jsonify(updater.status())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/update", methods=["POST"])
def update_from_github():
    # Require a token from this website, rather than accepting cross-site forms.
    origin = request.headers.get("Origin")
    if (not request.is_json
            or not secrets.compare_digest(request.headers.get("X-Chamber-Update", ""), update_token)
            or (origin and urlsplit(origin).netloc != request.host)):
        return jsonify(status="blocked", message="Reload the chamber website and try again."), 403
    if time.monotonic() < maintenance_until:
        return jsonify(status="blocked", message="The chamber is restarting."), 409
    try:
        state = updater.request(collection_busy)
        app.logger.info("GitHub update requested: %s", state.get("request_id"))
        return jsonify(state), 202
    except UpdateError as exc:
        app.logger.warning("GitHub update blocked: %s", exc)
        return jsonify(status="blocked", message=str(exc)), exc.status_code

@app.route("/start", methods=["POST"])
def start_collection():
    """Start a new data collection session."""
    global current_csv
    if collection_busy():
        return jsonify({"status": "already_running"})

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    current_csv = "data/" + f"data_{timestamp}.csv"
    with open(current_csv, "w") as f:
        header = ["timestamp"]
        for bank_name, bank_sensors in collector.SENSOR_BANKS.items():
            for sensor_name, info in bank_sensors.items():
                header.append(f"{bank_name}_{sensor_name} ({info['unit']})")
        f.write(",".join(header) + "\n")

    collector.set_csv_file(current_csv)
    collector.set_collecting_flag(True)
    socketio.emit("status_update", {"running": True})
    launch_collector()

    app.logger.info(f"Started collection: {current_csv}")
    return jsonify({"status": "started"})

@app.route("/stop", methods=["POST"])
def stop_collection():
    """Stop collection safely."""
    if collector.is_running():
        collector.set_collecting_flag(False)
        socketio.emit("status_update", {"running": False})
        app.logger.info("Stopped data collection.")
    else:
        app.logger.warning("Nothing to stop.")

    return jsonify({"status": "stopped"})

@app.route("/status")
def status():
    """Report running state for the UI indicator."""
    return jsonify({"running": collector.is_running(),
                    "interval": getattr(collector, "sampling_interval", 1.0),
                    "collector_active": collection_busy(),
                    "version": updater.version,
                    })

@app.route("/set_interval", methods=["POST"])
def set_interval():
    """Adjust sampling interval dynamically."""
    try:
        data = request.get_json()
        interval = float(data.get("interval", 1.0))
        # Enforce valid range server-side
        interval = max(1.0, min(60.0, interval))
        save_sampling_interval(interval)
        collector.set_sampling_interval(interval)
        #app.logger.info(f"Sampling interval changed to {interval}s via web.")
        return jsonify({"status": "ok", "interval": interval})
    except Exception as e:
        app.logger.error(f"Failed to set interval: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route("/data")
def get_data_route():
    """Return history of data collected"""
    return jsonify(collector.get_history())

@app.route("/download")
def download_csv():
    """Download the current CSV file."""
    # Prefer active collector file
    if collector.current_csv and os.path.exists(collector.current_csv):
        return send_file(collector.current_csv, as_attachment=True)

    # Fallback to latest on disk
    latest = get_latest_csv()
    if latest:
        return send_file(latest, as_attachment=True)

    return "No data file found", 404

@app.route("/download_dev")
def download_dev():
    """Download the current operational files zipped: index.html, style.css, app.py and collector.py"""
    memory_dev_zip = io.BytesIO()
    with zipfile.ZipFile(memory_dev_zip, "w") as dev_zip:
        dev_zip.write("app.py")
        dev_zip.write("collector.py")
        dev_zip.write("templates/index.html")
        dev_zip.write("static/style.css")
    memory_dev_zip.seek(0)

    return send_file(memory_dev_zip, as_attachment=True, download_name="dev_files.zip", mimetype="application/zip")

@app.route("/zipdata")
def zip_data():
    """Create and download a ZIP of CSV files from /data based on selected time range."""
    time_range = request.args.get("range", "all")  # "all", "7days", "24h"
    data_dir = "data"

    if not os.path.exists(data_dir):
        return "Data folder not found", 404

    # Determine cutoff time
    now = datetime.now()
    cutoff = None
    if time_range == "7days":
        cutoff = now - timedelta(days=7)
    elif time_range == "24h":
        cutoff = now - timedelta(hours=24)

    # Collect matching files
    selected_files = []
    for fname in os.listdir(data_dir):
        if fname.startswith("data_") and fname.endswith(".csv"):
            # Parse timestamp from filename (data_YYYYMMDD_HHMMSS.csv)
            try:
                timestamp_str = fname.split("_", 1)[1].replace(".csv", "")
                file_time = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
            except Exception:
                continue

            if cutoff is None or file_time >= cutoff:
                selected_files.append(os.path.join(data_dir, fname))

    if not selected_files:
        return "No files match the selected time range", 404

    # Create ZIP in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in selected_files:
            zipf.write(file_path, os.path.basename(file_path))

    zip_buffer.seek(0)
    zip_name = f"data_export_{time_range}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(zip_buffer, as_attachment=True, download_name=zip_name, mimetype="application/zip")

@app.route("/restart", methods=["POST"])
def restart_service():
    """Restart the smokechamber systemd service safely under gevent."""
     # Extra safeguard: block restart while data collection is active
    global maintenance_until
    if collection_busy():
        app.logger.warning("Restart blocked because data collection is running.")
        return jsonify({"status": "blocked", "reason": "collection_running"}), 409
    
    maintenance_until = time.monotonic() + 15

    def do_restart():
        # let this HTTP response complete before we kill the process
        time.sleep(1.0)
        # Use absolute path if this is in sudoers (recommended: /bin/systemctl)
        subprocess.Popen(["sudo", "/bin/systemctl", "restart", "smokechamber.service"])

    # run on an eventlet greenlet (don’t use threading.Thread here)
    socketio.start_background_task(do_restart)
    app.logger.info("Service restart triggered (async).")
    return jsonify({"status": "restarting"})

@app.route("/reboot", methods=["POST"])
def reboot_pi():
    """Safely reboot the Raspberry Pi."""
    # Protect against rebooting during collection
    global maintenance_until
    if collection_busy():
        app.logger.warning("Reboot blocked because data collection is running.")
        return jsonify({"status": "blocked", "reason": "collection_running"}), 409

    maintenance_until = time.monotonic() + 15

    def do_reboot():
        import subprocess, time
        time.sleep(1.0)  # let HTTP response complete
        subprocess.Popen(["sudo", "/sbin/reboot"])

    socketio.start_background_task(do_reboot)
    app.logger.info("System reboot triggered (async).")
    return jsonify({"status": "rebooting"})

@app.route("/log")
def get_recent_log():
    """Return the last N lines of logs from logs/app.log."""
    import os

    LOG_PATH = "logs/app.log"
    LINES_TO_READ = 100  # adjust this number as needed

    if not os.path.exists(LOG_PATH):
        return jsonify({"lines": []})

    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block_size = 4096
            data = ""
            lines = []
            while size > 0 and len(lines) < LINES_TO_READ:
                size = max(0, size - block_size)
                f.seek(size)
                data = f.read(block_size) + data
                lines = data.splitlines()
            lines = lines[-LINES_TO_READ:]
        return jsonify({"lines": lines})
    except Exception as e:
        app.logger.exception(f"Error reading log file: {e}")
        return jsonify({"lines": [], "error": str(e)}), 500
    
@app.route("/change_sensor_group", methods=["POST"])
def change_sensor_group():
    data = request.get_json()
    sensor = data.get("sensor")
    bank   = data.get("bank")

    try:
        msg = change_sensor_address(sensor, bank)
        app.logger.info(f"{sensor} moved to {bank}")
        return jsonify({"message": msg})
    except Exception as e:
        app.logger.error(f"Address change error: {e}")
        return jsonify({"message": str(e)}), 500


@socketio.on('connect')
def handle_connect():
    """On new client connect: send last 100 points and current running state to that single client (both banks)."""
    history = collector.get_history()
    socketio.emit("init_data", history, to=request.sid)

    # Also push current running state so the indicator is correct immediately
    socketio.emit("status_update", {"running": collector.is_running()}, to=request.sid)

# -------------------------
# LIVE LOG STREAM
# -------------------------

def stream_log():
    """Continuously tail the log file and emit lines live via Socket.IO."""
    log_path = "logs/app.log"
    try:
        # Open in read mode, seek to end to avoid old spam
        with open(log_path, "r") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    socketio.emit("log_update", {"line": line})
                else:
                    socketio.sleep(0.5)
    except Exception as e:
        app.logger.exception(f"Log stream stopped: {e}")

# Start log streamer as a background Socket.IO greenlet
socketio.start_background_task(stream_log)

# -------------------------
# Helper function for csv download
# -------------------------

def get_latest_csv(data_dir="data"):
    if not os.path.exists(data_dir):
        return None

    files = [
        os.path.join(data_dir, f)
        for f in os.listdir(data_dir)
        if f.startswith("data_") and f.endswith(".csv")
    ]

    if not files:
        return None

    # newest by filename timestamp
    return max(files)


# -------------------------
# WATCHDOG
# -------------------------

def watchdog():
    """Check for collector hangs or crashes and restart if needed."""
    global _watchdog_restart_times

    while True:
        socketio.sleep(max(10, collector.sampling_interval))
        try:
            if collector.is_running():
                # allow 5x the sampling interval as grace
                timeout = max(30, collector.sampling_interval * 5)
                if collector.seconds_since_last_emit() > timeout:
                    now = time.time()

                    # Drop old restart timestamps outside the window
                    while _watchdog_restart_times and (now - _watchdog_restart_times[0] > WATCHDOG_WINDOW_SECONDS):
                        _watchdog_restart_times.popleft()

                    if len(_watchdog_restart_times) >= WATCHDOG_MAX_RESTARTS:
                        # Restarted too many times in this window
                        app.logger.error(
                            "Watchdog: restart limit exceeded "
                            f"({WATCHDOG_MAX_RESTARTS} in {WATCHDOG_WINDOW_SECONDS}s). "
                            "Disabling collector until manual intervention."
                        )
                        socketio.emit("log_update", {
                            "line": "Watchdog disabled after repeated restarts. "
                                    "Please check sensors and restart collection manually.\n"
                        })

                        # Stop collecting and DO NOT restart
                        collector.set_collecting_flag(False)
                        socketio.emit("status_update", {"running": False})
                        continue
                        # Option 2 (stricter): break the watchdog loop entirely
                        # break

                    # Record this restart
                    _watchdog_restart_times.append(now)

                    app.logger.warning("Collector appears hung — restarting.")
                    socketio.emit("log_update", {"line": "Collector hung, restarting thread.\n"})

                    # Gracefully stop the current collector
                    collector.set_collecting_flag(False)
                    socketio.emit("status_update", {"running": False})
                    socketio.sleep(max(5, collector.sampling_interval))  # short yield for it to exit

                    # Now safely restart
                    collector.set_collecting_flag(True)
                    socketio.emit("status_update", {"running": True})
                    launch_collector()
                    app.logger.info("Collector restarted by watchdog.")
            else:
                # not collecting, skip
                continue
        except Exception as e:
            app.logger.error(f"Watchdog error: {e}")



# -------------------------
# ENTRY POINT
# -------------------------
if __name__ == "__main__":
    # Start watchdog background task
    socketio.start_background_task(watchdog)
    app.logger.info("SocketIO app started.")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, use_reloader=False)

