"""Meshcore device management and message handling.

Bridges the async meshcore library into Intercept's sync Flask/gevent stack
via a background asyncio thread feeding a queue.Queue.

Install: pip install meshcore
"""

from __future__ import annotations

import contextlib
import enum
import glob
import os
import queue
import threading
from dataclasses import dataclass
from datetime import datetime

from utils.logging import get_logger

logger = get_logger("intercept.meshcore")

try:
    import meshcore as _meshcore_lib  # noqa: F401

    HAS_MESHCORE = True
except ImportError:
    HAS_MESHCORE = False
    logger.warning("meshcore not installed. Run: pip install meshcore")


def is_meshcore_available() -> bool:
    """Return True if the meshcore library is installed and importable."""
    return HAS_MESHCORE


# Try to import ContactType for repeater detection
try:
    from meshcore import ContactType as _ContactType

    _REPEATER_TYPE = getattr(_ContactType, "REPEATER", None)
except Exception:
    _ContactType = None
    _REPEATER_TYPE = None


def _is_repeater_contact(contact_dict: dict) -> bool:
    """Return True if this contact is a repeater node."""
    if _REPEATER_TYPE is not None:
        return contact_dict.get("type") == _REPEATER_TYPE
    # Fallback: meshcore repeaters have type==2 by convention
    return contact_dict.get("type") == 2


# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------


@dataclass
class SerialConfig:
    """Configuration for a serial (USB) connection."""

    port: str | None = None  # None = auto-discover
    baud: int = 115200


@dataclass
class TCPConfig:
    """Configuration for a TCP connection (meshcore-proxy or WiFi device)."""

    host: str = "localhost"
    port: int = 5000  # meshcore-proxy default


@dataclass
class BLEConfig:
    """Configuration for a Bluetooth Low Energy connection."""

    device_address: str | None = None  # None = scan and pick first MeshCore device


ConnectionConfig = SerialConfig | TCPConfig | BLEConfig


class ConnectionState(enum.Enum):
    """Lifecycle state of the MeshcoreClient connection."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MeshcoreMessage:
    """Decoded message received from the MeshCore network."""

    id: str
    sender_id: str  # pubkey_prefix (12-char hex) for private msgs, or node_id
    recipient_id: str  # 64-char hex pubkey or "BROADCAST" for channel msgs
    text: str
    timestamp: datetime
    hop_count: int
    snr: float | None
    is_direct: bool  # True for private messages (CONTACT_MSG_RECV)
    pending: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "text": self.text,
            "timestamp": self.timestamp.isoformat(),
            "hop_count": self.hop_count,
            "snr": self.snr,
            "is_direct": self.is_direct,
            "pending": self.pending,
        }


@dataclass
class MeshcoreNode:
    """Tracked MeshCore node with position and metadata."""

    node_id: str  # 64-char hex public key
    name: str
    is_repeater: bool
    lat: float | None
    lon: float | None
    battery_pct: int | None
    last_seen: datetime
    snr: float | None
    hops_away: int | None

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "is_repeater": self.is_repeater,
            "lat": self.lat,
            "lon": self.lon,
            "battery_pct": self.battery_pct,
            "last_seen": self.last_seen.isoformat(),
            "snr": self.snr,
            "hops_away": self.hops_away,
        }


@dataclass
class MeshcoreContact:
    """A known contact stored on the MeshCore device."""

    node_id: str  # 64-char hex public key (maps to contact['public_key'])
    name: str  # display name (maps to contact['adv_name'])
    public_key: str  # same as node_id (kept for API clarity)
    last_msg: datetime | None

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "public_key": self.public_key,
            "last_msg": self.last_msg.isoformat() if self.last_msg else None,
        }


@dataclass
class MeshcoreTelemetry:
    """Device or environment telemetry from a MeshCore node."""

    node_id: str
    timestamp: datetime
    battery_pct: int | None
    voltage: float | None
    temperature: float | None
    humidity: float | None
    uptime_secs: int | None

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "timestamp": self.timestamp.isoformat(),
            "battery_pct": self.battery_pct,
            "voltage": self.voltage,
            "temperature": self.temperature,
            "humidity": self.humidity,
            "uptime_secs": self.uptime_secs,
        }


@dataclass
class MeshcoreTraceroute:
    """Result of a traceroute request through the MeshCore network."""

    origin_id: str
    destination_id: str
    hops: list[str]
    snr_per_hop: list[float]
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            "origin_id": self.origin_id,
            "destination_id": self.destination_id,
            "hops": self.hops,
            "snr_per_hop": self.snr_per_hop,
            "timestamp": self.timestamp.isoformat(),
        }


# ---------------------------------------------------------------------------
# Serial port discovery
# ---------------------------------------------------------------------------


def list_serial_ports() -> list[str]:
    """Return a sorted list of likely serial ports for MeshCore devices."""
    patterns = ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.usbserial*", "/dev/cu.usbmodem*"]
    ports = []
    for pat in patterns:
        ports.extend(glob.glob(pat))
    return sorted(set(ports))


def _is_docker() -> bool:
    """Return True when running inside a Docker container."""
    return os.path.exists("/.dockerenv") or os.environ.get("INTERCEPT_DOCKER") == "1"


# ---------------------------------------------------------------------------
# MeshcoreClient — singleton
# ---------------------------------------------------------------------------


class MeshcoreClient:
    """Singleton client bridging async meshcore library into Flask/gevent.

    A background AsyncWorker (created in utils/meshcore_client.py) runs
    an asyncio event loop in a daemon thread and forwards events into
    self._event_queue for SSE streaming.
    """

    def __init__(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._status_message: str | None = None
        self._config: ConnectionConfig | None = None
        self._event_queue: queue.Queue = queue.Queue(maxsize=500)
        self._nodes: dict[str, MeshcoreNode] = {}
        self._contacts: dict[str, MeshcoreContact] = {}
        self._messages: list[MeshcoreMessage] = []
        self._telemetry: dict[str, list[MeshcoreTelemetry]] = {}
        self._lock = threading.Lock()
        self._worker = None  # AsyncWorker instance (set in connect())

    # -- State --

    def get_state(self) -> tuple[ConnectionState, str | None]:
        """Return the current connection state and last status message."""
        with self._lock:
            return self._state, self._status_message

    def _set_state(self, state: ConnectionState, **extra) -> None:
        with self._lock:
            self._state = state
            self._status_message = extra.get("message")
        # Push the status event OUTSIDE the lock (avoids deadlock; _push is queue-based)
        payload: dict = {"state": state.value}
        payload.update(extra)
        self._push({"type": "status", "data": payload})

    # -- Queue --

    def _push(self, event: dict) -> None:
        """Non-blocking push; drops oldest item when queue is full."""
        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._event_queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._event_queue.put_nowait(event)

    def get_queue(self) -> queue.Queue:
        """Return the event queue consumed by SSE streaming endpoints."""
        return self._event_queue

    # -- Connect / disconnect --

    def connect(self, config: ConnectionConfig) -> None:
        """Start background AsyncWorker with the given connection config."""
        with self._lock:
            if self._state == ConnectionState.CONNECTING:
                return
            self._state = ConnectionState.CONNECTING
        # Push status event outside the lock
        self._push({"type": "status", "data": {"state": ConnectionState.CONNECTING.value}})
        if isinstance(config, BLEConfig) and _is_docker():
            with self._lock:
                self._state = ConnectionState.ERROR
            self._push(
                {
                    "type": "status",
                    "data": {
                        "state": ConnectionState.ERROR.value,
                        "message": "BLE unavailable in Docker. Run meshcore-proxy on the host and connect via TCP.",
                    },
                }
            )
            return
        self._config = config
        from utils.meshcore_client import AsyncWorker  # imported lazily (Task 3)

        self._worker = AsyncWorker(config, self)
        self._worker.start()

    def disconnect(self) -> None:
        """Stop the background worker and set state to DISCONNECTED."""
        if self._worker:
            self._worker.stop()
            self._worker = None
        self._set_state(ConnectionState.DISCONNECTED)

    # -- Event handlers called by AsyncWorker --

    def on_connected(self, transport: str, device: str) -> None:
        """Called by AsyncWorker once the device handshake is complete."""
        self._set_state(ConnectionState.CONNECTED, transport=transport, device=device)

    def on_error(self, message: str) -> None:
        """Called by AsyncWorker when a fatal connection error occurs."""
        self._set_state(ConnectionState.ERROR, message=message)

    def on_message(self, msg: MeshcoreMessage) -> None:
        """Store and broadcast a received message."""
        with self._lock:
            self._messages.append(msg)
            if len(self._messages) > 500:
                self._messages.pop(0)
        self._push({"type": "message", "data": msg.to_dict()})

    def on_node(self, node: MeshcoreNode) -> None:
        """Store and broadcast an updated node advertisement."""
        with self._lock:
            self._nodes[node.node_id] = node
        self._push({"type": "node", "data": node.to_dict()})

    def on_telemetry(self, t: MeshcoreTelemetry) -> None:
        """Store and broadcast device/environment telemetry."""
        with self._lock:
            self._telemetry.setdefault(t.node_id, []).append(t)
            if len(self._telemetry[t.node_id]) > 200:
                self._telemetry[t.node_id].pop(0)
        self._push({"type": "telemetry", "data": t.to_dict()})

    def on_traceroute(self, tr: MeshcoreTraceroute) -> None:
        """Broadcast a traceroute result."""
        self._push({"type": "traceroute", "data": tr.to_dict()})

    # -- Data accessors --

    def get_messages(self) -> list[dict]:
        """Return all buffered messages as serialisable dicts."""
        with self._lock:
            return [m.to_dict() for m in self._messages]

    def get_nodes(self) -> list[dict]:
        """Return all known nodes as serialisable dicts."""
        with self._lock:
            return [n.to_dict() for n in self._nodes.values()]

    def get_repeaters(self) -> list[dict]:
        """Return only repeater nodes as serialisable dicts."""
        with self._lock:
            return [n.to_dict() for n in self._nodes.values() if n.is_repeater]

    def get_contacts(self) -> list[dict]:
        """Return all stored contacts as serialisable dicts."""
        with self._lock:
            return [c.to_dict() for c in self._contacts.values()]

    def add_contact(self, contact: MeshcoreContact) -> None:
        """Upsert a contact into the local contacts store."""
        with self._lock:
            self._contacts[contact.node_id] = contact

    def remove_contact(self, node_id: str) -> bool:
        """Remove a contact; return True if it existed."""
        with self._lock:
            if node_id in self._contacts:
                del self._contacts[node_id]
                return True
            return False

    def get_telemetry(self, node_id: str) -> list[dict]:
        """Return buffered telemetry for a specific node."""
        with self._lock:
            return [t.to_dict() for t in self._telemetry.get(node_id, [])]

    def send_text(self, recipient_id: str, text: str) -> None:
        """Request the AsyncWorker to send a text message."""
        if self._worker:
            self._worker.send_text(recipient_id, text)

    def request_traceroute(self, node_id: str) -> None:
        """Request the AsyncWorker to initiate a traceroute."""
        if self._worker:
            self._worker.request_traceroute(node_id)

    def scan_ble(self) -> list[dict]:
        """Scan for BLE MeshCore devices; works with or without an active connection."""
        if self._worker:
            return self._worker.scan_ble_sync()
        # No worker — spin up a dedicated thread with its own event loop to avoid
        # conflicts with gevent's monkey-patched event loop in the Flask thread.
        import asyncio
        import concurrent.futures

        from utils.meshcore_client import _scan_ble

        def _run() -> list[dict]:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_scan_ble())
            finally:
                loop.close()
                asyncio.set_event_loop(None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            try:
                return executor.submit(_run).result(timeout=12)
            except Exception as exc:
                logger.warning("BLE scan failed: %s", exc)
                return []


_client: MeshcoreClient | None = None


def get_meshcore_client() -> MeshcoreClient:
    """Return the process-wide MeshcoreClient singleton."""
    global _client
    if _client is None:
        _client = MeshcoreClient()
    return _client
