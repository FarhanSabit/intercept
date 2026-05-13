"""Drone intelligence routes — multi-vector UAV detection."""

from __future__ import annotations

import logging
import os
import platform
import queue
import subprocess
import threading

from flask import Blueprint, Response, jsonify, request

import app as app_module
from utils.constants import SSE_KEEPALIVE_INTERVAL, SSE_QUEUE_TIMEOUT
from utils.drone.correlator import DroneCorrelator
from utils.drone.remote_id import RemoteIDScanner
from utils.drone.rf_detector import RFDetector
from utils.sse import sse_stream_fanout
from utils.validation import validate_device_index

logger = logging.getLogger("intercept.drone")

drone_bp = Blueprint("drone", __name__, url_prefix="/drone")

_correlator: DroneCorrelator | None = None
_remote_id_scanner: RemoteIDScanner | None = None
_rf_detector: RFDetector | None = None
_obs_queue: queue.Queue | None = None  # raw observations from scanners/detectors
_relay_thread: threading.Thread | None = None
_drone_running = False
_drone_lock = threading.Lock()

_SENTINEL = object()


def _relay_observations() -> None:
    """Read raw observations from _obs_queue and feed them into the correlator."""
    while True:
        obs = _obs_queue.get()
        if obs is _SENTINEL:
            break
        if _correlator is not None:
            _correlator.process(obs)


def _ensure_workers() -> None:
    global _correlator, _remote_id_scanner, _rf_detector, _obs_queue, _relay_thread
    if _obs_queue is None:
        _obs_queue = queue.Queue(maxsize=512)
    if _correlator is None:
        _correlator = DroneCorrelator(output_queue=app_module.drone_queue)
    if _remote_id_scanner is None:
        _remote_id_scanner = RemoteIDScanner(output_queue=_obs_queue)
    if _rf_detector is None:
        _rf_detector = RFDetector(output_queue=_obs_queue)
    if _relay_thread is None or not _relay_thread.is_alive():
        _relay_thread = threading.Thread(target=_relay_observations, daemon=True)
        _relay_thread.start()


@drone_bp.route("/devices")
def devices():
    """Return available WiFi interfaces and SDR devices for drone detection."""
    result: dict = {"wifi_interfaces": [], "sdr_devices": []}

    # WiFi interfaces via iw/iwconfig
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(
                ["networksetup", "-listallhardwareports"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            lines = out.split("\n")
            for i, line in enumerate(lines):
                if "Wi-Fi" in line or "AirPort" in line:
                    port = line.replace("Hardware Port:", "").strip()
                    for j in range(i + 1, min(i + 3, len(lines))):
                        if "Device:" in lines[j]:
                            dev = lines[j].split("Device:")[1].strip()
                            result["wifi_interfaces"].append(
                                {
                                    "name": dev,
                                    "display_name": f"{port} ({dev})",
                                    "type": "internal",
                                    "monitor_capable": False,
                                }
                            )
                            break
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            pass
    else:
        try:
            out = subprocess.run(["iw", "dev"], capture_output=True, text=True, timeout=5).stdout
            current: str | None = None
            for line in out.split("\n"):
                line = line.strip()
                if line.startswith("Interface"):
                    current = line.split()[1]
                elif current and "type" in line:
                    iface_type = line.split()[-1]
                    result["wifi_interfaces"].append(
                        {
                            "name": current,
                            "display_name": f"{current} ({iface_type})",
                            "type": iface_type,
                            "monitor_capable": True,
                        }
                    )
                    current = None
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            try:
                out = subprocess.run(["iwconfig"], capture_output=True, text=True, timeout=5).stdout
                for line in out.split("\n"):
                    if "IEEE 802.11" in line:
                        iface = line.split()[0]
                        result["wifi_interfaces"].append(
                            {
                                "name": iface,
                                "display_name": f"{iface} (managed)",
                                "type": "managed",
                                "monitor_capable": True,
                            }
                        )
            except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError):
                pass

    # SDR devices
    try:
        from utils.sdr import SDRFactory

        for sdr in SDRFactory.detect_devices():
            sdr_type = sdr.sdr_type.value if hasattr(sdr.sdr_type, "value") else str(sdr.sdr_type)
            display = sdr.name
            if sdr.serial and sdr.serial not in ("N/A", "Unknown"):
                display = f"{sdr.name} (SN: {sdr.serial[-8:]})"
            result["sdr_devices"].append(
                {"index": sdr.index, "name": sdr.name, "display_name": display, "type": sdr_type}
            )
    except Exception:
        pass

    running_as_root = os.geteuid() == 0
    warnings = []
    if not running_as_root:
        warnings.append(
            {
                "type": "privileges",
                "message": "Not running as root — WiFi monitor mode may be unavailable.",
            }
        )

    return jsonify(
        {
            "status": "ok",
            "devices": result,
            "running_as_root": running_as_root,
            "warnings": warnings,
        }
    )


@drone_bp.route("/status")
def status():
    vectors = []
    if _remote_id_scanner and _remote_id_scanner.running:
        vectors.append("REMOTE_ID")
    if _rf_detector and _rf_detector.running:
        vectors.append("RF")
    return jsonify(
        {
            "running": _drone_running,
            "vectors": vectors,
            "contact_count": len(_correlator.get_all()) if _correlator else 0,
        }
    )


@drone_bp.route("/contacts")
def contacts():
    if not _correlator:
        return jsonify([])
    return jsonify(_correlator.get_all())


@drone_bp.route("/start", methods=["POST"])
def start():
    global _drone_running
    body = request.json or {}
    wifi_iface = body.get("wifi_iface") or None
    try:
        rtl_index = validate_device_index(body.get("rtl_sdr_index", 0))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    use_hackrf = bool(body.get("use_hackrf", True))

    with _drone_lock:
        _ensure_workers()
        if not _drone_running:
            if _remote_id_scanner:
                _remote_id_scanner.start(wifi_iface=wifi_iface)
            if _rf_detector:
                _rf_detector.start(rtl_sdr_index=rtl_index, use_hackrf=use_hackrf)
            _drone_running = True
            logger.info("Drone detection started")

    return jsonify({"status": "ok", "running": True})


@drone_bp.route("/stop", methods=["POST"])
def stop():
    global _drone_running
    with _drone_lock:
        if _remote_id_scanner:
            _remote_id_scanner.stop()
        if _rf_detector:
            _rf_detector.stop()
        if _obs_queue is not None:
            _obs_queue.put_nowait(_SENTINEL)
        _drone_running = False
    logger.info("Drone detection stopped")
    return jsonify({"status": "ok", "running": False})


@drone_bp.route("/stream")
def stream():
    return Response(
        sse_stream_fanout(
            source_queue=app_module.drone_queue,
            channel_key="drone",
            timeout=SSE_QUEUE_TIMEOUT,
            keepalive_interval=SSE_KEEPALIVE_INTERVAL,
        ),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
