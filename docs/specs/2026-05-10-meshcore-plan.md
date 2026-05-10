# Meshcore Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a full Meshcore mode to Intercept with USB serial, TCP, and BLE support — feature-parity with the existing Meshtastic module.

**Architecture:** The `meshcore` PyPI library is fully async; we run it in a dedicated asyncio event loop on a daemon OS thread and bridge events into a `queue.Queue` that Flask's SSE endpoint drains. Everything outside `utils/meshcore_client.py` is sync and unaware of asyncio.

**Tech Stack:** Python `meshcore` (PyPI, async), Flask Blueprint, gevent-compatible queue.Queue, Leaflet (map), Chart.js (telemetry), EventSource (SSE)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `utils/meshcore.py` | Create | Dataclasses, MeshcoreClient singleton, connection state, serial port discovery |
| `utils/meshcore_client.py` | Create | Thin async wrapper around meshcore library; runs inside asyncio thread |
| `routes/meshcore.py` | Create | Flask blueprint with all 15 REST endpoints + SSE stream |
| `tests/test_meshcore_client.py` | Create | Unit tests: dataclasses, state machine, queue feeding, reconnect backoff |
| `tests/test_meshcore_routes.py` | Create | Route tests via Flask test client |
| `tests/test_meshcore_integration.py` | Create | Mock-boundary round-trip tests |
| `static/css/modes/meshcore.css` | Create | Scoped styles for Meshcore mode |
| `templates/partials/modes/meshcore.html` | Create | Sidebar partial (connection panel, contacts, nodes) |
| `static/js/modes/meshcore.js` | Create | IIFE frontend module |
| `routes/__init__.py` | Modify | Register meshcore_bp |
| `requirements.txt` | Modify | Add meshcore dependency |
| `templates/index.html` | Modify | 14 wiring points (CSS, JS, partial, catalog, handlers, etc.) |

---

## Task 1: Discover meshcore library API

**Files:**
- Read: (no files changed — discovery only)

- [ ] **Step 1: Install the library and inspect its public API**

```bash
pip install meshcore
python3 - <<'EOF'
import inspect, meshcore
print("=== meshcore top-level ===")
print(dir(meshcore))
# Find connection classes
for name in dir(meshcore):
    obj = getattr(meshcore, name)
    if inspect.isclass(obj):
        print(f"\n--- {name} ---")
        print(inspect.signature(obj.__init__) if hasattr(obj, '__init__') else '')
        print([m for m in dir(obj) if not m.startswith('_')])
EOF
```

- [ ] **Step 2: Document the connect / event / send API surface**

Run:
```bash
python3 - <<'EOF'
import meshcore, inspect
# Try to find how connections are made
for attr in ['connect', 'connect_serial', 'connect_tcp', 'connect_ble', 'serial', 'tcp', 'ble']:
    if hasattr(meshcore, attr):
        fn = getattr(meshcore, attr)
        print(f"meshcore.{attr}: {inspect.signature(fn)}")
EOF
```

Record the actual class name, connect method signatures, event iteration pattern, and send method. The findings drive `utils/meshcore_client.py` in Task 3. The rest of the plan uses the following *assumed* API — update method names in Task 3 only if they differ:

```
connect:  meshcore.Connection(port=...) / meshcore.Connection(host=..., port=...) / meshcore.Connection(ble_address=...)
events:   async for event in conn.events(): event.type, event.fields
send:     await conn.send_text(to, text)
contacts: await conn.get_contacts() → list[{node_id, name, public_key}]
tracert:  await conn.traceroute(node_id) → {hops:[...], snr:[...]}
ble scan: await meshcore.scan_ble() → [{address, name, rssi}]
telemetry: delivered as events of type 'telemetry'
```

---

## Task 2: Data model — dataclasses in utils/meshcore.py

**Files:**
- Create: `utils/meshcore.py`
- Create: `tests/test_meshcore_client.py` (started)

- [ ] **Step 1: Write failing tests for dataclasses**

Create `tests/test_meshcore_client.py`:

```python
"""Tests for MeshcoreClient dataclasses and state machine."""
from datetime import datetime, timezone
from unittest.mock import patch
import pytest


class TestAvailability:
    def test_returns_bool(self):
        from utils.meshcore import is_meshcore_available
        assert isinstance(is_meshcore_available(), bool)

    def test_false_when_not_installed(self):
        with patch.dict('sys.modules', {'meshcore': None}):
            import importlib, utils.meshcore as m
            importlib.reload(m)
            assert m.is_meshcore_available() is False


class TestMeshcoreMessage:
    def _make(self, **kw):
        from utils.meshcore import MeshcoreMessage
        defaults = dict(
            id='abc123',
            sender_id='NODE001',
            recipient_id='BROADCAST',
            text='hello mesh',
            timestamp=datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc),
            hop_count=2,
            snr=-8.5,
            is_direct=False,
        )
        defaults.update(kw)
        return MeshcoreMessage(**defaults)

    def test_to_dict_keys(self):
        d = self._make().to_dict()
        for key in ('id', 'sender_id', 'recipient_id', 'text', 'timestamp',
                    'hop_count', 'snr', 'is_direct', 'pending'):
            assert key in d, f"missing key: {key}"

    def test_pending_defaults_false(self):
        assert self._make().to_dict()['pending'] is False

    def test_none_snr_allowed(self):
        d = self._make(snr=None).to_dict()
        assert d['snr'] is None


class TestMeshcoreNode:
    def test_to_dict_includes_is_repeater(self):
        from utils.meshcore import MeshcoreNode
        node = MeshcoreNode(
            node_id='RPT1', name='Roof-Repeater', is_repeater=True,
            lat=51.5, lon=-0.1, battery_pct=87,
            last_seen=datetime.now(timezone.utc), snr=-5.0, hops_away=1,
        )
        d = node.to_dict()
        assert d['is_repeater'] is True
        assert d['node_id'] == 'RPT1'


class TestMeshcoreTelemetry:
    def test_to_dict_timestamp_is_iso(self):
        from utils.meshcore import MeshcoreTelemetry
        t = MeshcoreTelemetry(
            node_id='N1', timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
            battery_pct=72, voltage=3.7, temperature=22.1,
            humidity=55.0, uptime_secs=3600,
        )
        d = t.to_dict()
        assert '2026-05-10' in d['timestamp']


class TestConnectionState:
    def test_state_enum_values(self):
        from utils.meshcore import ConnectionState
        assert ConnectionState.DISCONNECTED
        assert ConnectionState.CONNECTING
        assert ConnectionState.CONNECTED
        assert ConnectionState.ERROR
```

- [ ] **Step 2: Run tests — confirm they all fail**

```bash
pytest tests/test_meshcore_client.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'utils.meshcore'`

- [ ] **Step 3: Create utils/meshcore.py with dataclasses**

```python
"""Meshcore device management and message handling.

Bridges the async meshcore library into Intercept's sync Flask/gevent stack
via a background asyncio thread feeding a queue.Queue.

Install: pip install meshcore
"""
from __future__ import annotations

import contextlib
import enum
import glob
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from utils.logging import get_logger

logger = get_logger('intercept.meshcore')

try:
    import meshcore as _meshcore_lib
    HAS_MESHCORE = True
except ImportError:
    HAS_MESHCORE = False
    logger.warning("meshcore not installed. Run: pip install meshcore")


def is_meshcore_available() -> bool:
    return HAS_MESHCORE


# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------

@dataclass
class SerialConfig:
    port: str | None = None  # None = auto-discover
    baud: int = 115200


@dataclass
class TCPConfig:
    host: str = "localhost"
    port: int = 5000  # meshcore-proxy default


@dataclass
class BLEConfig:
    device_address: str | None = None  # None = scan and pick first


ConnectionConfig = SerialConfig | TCPConfig | BLEConfig


class ConnectionState(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MeshcoreMessage:
    id: str
    sender_id: str
    recipient_id: str
    text: str
    timestamp: datetime
    hop_count: int
    snr: float | None
    is_direct: bool
    pending: bool = False

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'sender_id': self.sender_id,
            'recipient_id': self.recipient_id,
            'text': self.text,
            'timestamp': self.timestamp.isoformat(),
            'hop_count': self.hop_count,
            'snr': self.snr,
            'is_direct': self.is_direct,
            'pending': self.pending,
        }


@dataclass
class MeshcoreNode:
    node_id: str
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
            'node_id': self.node_id,
            'name': self.name,
            'is_repeater': self.is_repeater,
            'lat': self.lat,
            'lon': self.lon,
            'battery_pct': self.battery_pct,
            'last_seen': self.last_seen.isoformat(),
            'snr': self.snr,
            'hops_away': self.hops_away,
        }


@dataclass
class MeshcoreContact:
    node_id: str
    name: str
    public_key: str
    last_msg: datetime | None

    def to_dict(self) -> dict:
        return {
            'node_id': self.node_id,
            'name': self.name,
            'public_key': self.public_key,
            'last_msg': self.last_msg.isoformat() if self.last_msg else None,
        }


@dataclass
class MeshcoreTelemetry:
    node_id: str
    timestamp: datetime
    battery_pct: int | None
    voltage: float | None
    temperature: float | None
    humidity: float | None
    uptime_secs: int | None

    def to_dict(self) -> dict:
        return {
            'node_id': self.node_id,
            'timestamp': self.timestamp.isoformat(),
            'battery_pct': self.battery_pct,
            'voltage': self.voltage,
            'temperature': self.temperature,
            'humidity': self.humidity,
            'uptime_secs': self.uptime_secs,
        }


@dataclass
class MeshcoreTraceroute:
    origin_id: str
    destination_id: str
    hops: list[str]
    snr_per_hop: list[float]
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            'origin_id': self.origin_id,
            'destination_id': self.destination_id,
            'hops': self.hops,
            'snr_per_hop': self.snr_per_hop,
            'timestamp': self.timestamp.isoformat(),
        }


# ---------------------------------------------------------------------------
# Serial port discovery
# ---------------------------------------------------------------------------

def list_serial_ports() -> list[str]:
    patterns = ['/dev/ttyUSB*', '/dev/ttyACM*', '/dev/cu.usbserial*', '/dev/cu.usbmodem*']
    ports = []
    for pat in patterns:
        ports.extend(glob.glob(pat))
    return sorted(set(ports))


def _is_docker() -> bool:
    import os
    return os.path.exists('/.dockerenv') or os.environ.get('INTERCEPT_DOCKER') == '1'


# ---------------------------------------------------------------------------
# MeshcoreClient — singleton
# ---------------------------------------------------------------------------

class MeshcoreClient:
    def __init__(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._config: ConnectionConfig | None = None
        self._event_queue: queue.Queue = queue.Queue(maxsize=500)
        self._nodes: dict[str, MeshcoreNode] = {}
        self._contacts: dict[str, MeshcoreContact] = {}
        self._messages: list[MeshcoreMessage] = []
        self._telemetry: dict[str, list[MeshcoreTelemetry]] = {}
        self._lock = threading.Lock()
        self._worker: '_AsyncWorker | None' = None

    # -- State --

    def get_state(self) -> ConnectionState:
        return self._state

    def _set_state(self, state: ConnectionState, **extra) -> None:
        self._state = state
        payload: dict = {'state': state.value}
        payload.update(extra)
        self._push({'type': 'status', 'data': payload})

    # -- Queue --

    def _push(self, event: dict) -> None:
        with contextlib.suppress(queue.Full):
            try:
                self._event_queue.put_nowait(event)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    self._event_queue.get_nowait()
                with contextlib.suppress(queue.Full):
                    self._event_queue.put_nowait(event)

    def get_queue(self) -> queue.Queue:
        return self._event_queue

    # -- Connect / disconnect --

    def connect(self, config: ConnectionConfig) -> None:
        if self._state == ConnectionState.CONNECTING:
            return
        if isinstance(config, BLEConfig) and _is_docker():
            self._set_state(ConnectionState.ERROR,
                            message="BLE unavailable in Docker. Run meshcore-proxy on the host and connect via TCP.")
            return
        self._config = config
        self._set_state(ConnectionState.CONNECTING)
        from utils.meshcore_client import AsyncWorker
        self._worker = AsyncWorker(config, self)
        self._worker.start()

    def disconnect(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker = None
        self._set_state(ConnectionState.DISCONNECTED)

    # -- Event handlers called by AsyncWorker --

    def on_connected(self, transport: str, device: str) -> None:
        self._set_state(ConnectionState.CONNECTED, transport=transport, device=device)

    def on_error(self, message: str) -> None:
        self._set_state(ConnectionState.ERROR, message=message)

    def on_message(self, msg: MeshcoreMessage) -> None:
        with self._lock:
            self._messages.append(msg)
            if len(self._messages) > 500:
                self._messages.pop(0)
        self._push({'type': 'message', 'data': msg.to_dict()})

    def on_node(self, node: MeshcoreNode) -> None:
        with self._lock:
            self._nodes[node.node_id] = node
        self._push({'type': 'node', 'data': node.to_dict()})

    def on_telemetry(self, t: MeshcoreTelemetry) -> None:
        with self._lock:
            self._telemetry.setdefault(t.node_id, []).append(t)
            if len(self._telemetry[t.node_id]) > 200:
                self._telemetry[t.node_id].pop(0)
        self._push({'type': 'telemetry', 'data': t.to_dict()})

    def on_traceroute(self, tr: MeshcoreTraceroute) -> None:
        self._push({'type': 'traceroute', 'data': tr.to_dict()})

    # -- Data accessors --

    def get_messages(self) -> list[dict]:
        with self._lock:
            return [m.to_dict() for m in self._messages]

    def get_nodes(self) -> list[dict]:
        with self._lock:
            return [n.to_dict() for n in self._nodes.values()]

    def get_repeaters(self) -> list[dict]:
        with self._lock:
            return [n.to_dict() for n in self._nodes.values() if n.is_repeater]

    def get_contacts(self) -> list[dict]:
        with self._lock:
            return [c.to_dict() for c in self._contacts.values()]

    def add_contact(self, contact: MeshcoreContact) -> None:
        with self._lock:
            self._contacts[contact.node_id] = contact

    def remove_contact(self, node_id: str) -> bool:
        with self._lock:
            if node_id in self._contacts:
                del self._contacts[node_id]
                return True
            return False

    def get_telemetry(self, node_id: str) -> list[dict]:
        with self._lock:
            return [t.to_dict() for t in self._telemetry.get(node_id, [])]

    def send_text(self, recipient_id: str, text: str) -> None:
        if self._worker:
            self._worker.send_text(recipient_id, text)

    def request_traceroute(self, node_id: str) -> None:
        if self._worker:
            self._worker.request_traceroute(node_id)

    def scan_ble(self) -> list[dict]:
        if self._worker:
            return self._worker.scan_ble_sync()
        return []


_client: MeshcoreClient | None = None


def get_meshcore_client() -> MeshcoreClient:
    global _client
    if _client is None:
        _client = MeshcoreClient()
    return _client
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
pytest tests/test_meshcore_client.py -v
```

Expected: All 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add utils/meshcore.py tests/test_meshcore_client.py
git commit -m "feat(meshcore): add data model, connection config, MeshcoreClient skeleton"
```

---

## Task 3: Async worker — utils/meshcore_client.py

**Files:**
- Create: `utils/meshcore_client.py`

> **Note:** The method names below (`Connection`, `conn.events()`, `conn.send_text()`) are based on the assumed API from Task 1. Adjust them to match what `pip install meshcore` actually exposes. All library interaction is confined to this one file.

- [ ] **Step 1: Create utils/meshcore_client.py**

```python
"""Async worker that runs the meshcore library inside a daemon thread.

Only this file touches the meshcore library directly. All other Intercept
code goes through MeshcoreClient in utils/meshcore.py.

If the meshcore library API differs from what is shown here, only this file
needs to change — nothing else depends on library internals.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from utils.logging import get_logger

if TYPE_CHECKING:
    from utils.meshcore import (
        BLEConfig, ConnectionConfig, MeshcoreClient,
        SerialConfig, TCPConfig,
    )

logger = get_logger('intercept.meshcore.worker')

# Retry intervals in seconds (exponential backoff)
_RETRY_DELAYS = [5, 15, 45]


class AsyncWorker:
    """Owns a daemon asyncio event loop; bridges events to MeshcoreClient."""

    def __init__(self, config: ConnectionConfig, client: MeshcoreClient) -> None:
        self._config = config
        self._client = client
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conn = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self._stop_event.clear()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="meshcore-asyncio",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Thread entrypoint
    # ------------------------------------------------------------------

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_with_retry())
        except Exception as exc:
            logger.exception("Meshcore asyncio thread crashed: %s", exc)
        finally:
            self._loop.close()

    async def _connect_with_retry(self) -> None:
        from utils.meshcore import SerialConfig, TCPConfig, BLEConfig

        for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
            if self._stop_event.is_set():
                return
            try:
                await self._do_connect()
                return  # clean exit or connection lost without error
            except Exception as exc:
                logger.warning("Meshcore connect attempt %d failed: %s", attempt + 1, exc)
                if delay is None:
                    self._client.on_error(f"Connection failed after retries: {exc}")
                    return
                await asyncio.sleep(delay)

    async def _do_connect(self) -> None:
        # ----------------------------------------------------------------
        # ADAPT HERE: replace method names to match actual meshcore library
        # ----------------------------------------------------------------
        import meshcore as mc_lib
        from utils.meshcore import SerialConfig, TCPConfig, BLEConfig

        cfg = self._config

        if isinstance(cfg, SerialConfig):
            port = cfg.port or 'auto'
            # Assumed API: mc_lib.Connection(port=port, baud=cfg.baud)
            self._conn = mc_lib.Connection(port=port, baud=cfg.baud)
            transport_label = 'serial'
            device_label = port
        elif isinstance(cfg, TCPConfig):
            self._conn = mc_lib.Connection(host=cfg.host, port=cfg.port)
            transport_label = 'tcp'
            device_label = f"{cfg.host}:{cfg.port}"
        elif isinstance(cfg, BLEConfig):
            self._conn = mc_lib.Connection(ble_address=cfg.device_address)
            transport_label = 'ble'
            device_label = cfg.device_address or 'auto'

        # Assumed API: await self._conn.connect()
        await self._conn.connect()
        self._client.on_connected(transport=transport_label, device=device_label)

        # Assumed API: async for event in self._conn.events()
        async for event in self._conn.events():
            if self._stop_event.is_set():
                break
            await self._dispatch(event)

    async def _dispatch(self, event) -> None:
        """Route a library event to the appropriate MeshcoreClient handler."""
        from utils.meshcore import (
            MeshcoreMessage, MeshcoreNode, MeshcoreTelemetry, MeshcoreTraceroute,
        )
        # Assumed event shape: event.type (str), event fields as attributes
        # Adjust attribute names to match the actual library's event objects.
        t = getattr(event, 'type', None) or getattr(event, 'event_type', None)

        if t in ('message', 'msg', 'rx_text'):
            msg = MeshcoreMessage(
                id=getattr(event, 'id', None) or str(uuid.uuid4()),
                sender_id=str(getattr(event, 'sender', '') or getattr(event, 'from_id', '')),
                recipient_id=str(getattr(event, 'recipient', '') or getattr(event, 'to_id', 'BROADCAST')),
                text=str(getattr(event, 'text', '') or getattr(event, 'message', '')),
                timestamp=datetime.now(timezone.utc),
                hop_count=int(getattr(event, 'hops', 0) or 0),
                snr=_float_or_none(getattr(event, 'snr', None)),
                is_direct=bool(getattr(event, 'is_direct', False)),
            )
            self._client.on_message(msg)

        elif t in ('node', 'node_advert', 'node_info'):
            node = MeshcoreNode(
                node_id=str(getattr(event, 'node_id', '') or getattr(event, 'id', '')),
                name=str(getattr(event, 'name', 'Unknown')),
                is_repeater=bool(getattr(event, 'is_repeater', False)),
                lat=_float_or_none(getattr(event, 'lat', None)),
                lon=_float_or_none(getattr(event, 'lon', None)),
                battery_pct=_int_or_none(getattr(event, 'battery_pct', None)),
                last_seen=datetime.now(timezone.utc),
                snr=_float_or_none(getattr(event, 'snr', None)),
                hops_away=_int_or_none(getattr(event, 'hops_away', None)),
            )
            self._client.on_node(node)

        elif t in ('telemetry',):
            tel = MeshcoreTelemetry(
                node_id=str(getattr(event, 'node_id', '')),
                timestamp=datetime.now(timezone.utc),
                battery_pct=_int_or_none(getattr(event, 'battery_pct', None)),
                voltage=_float_or_none(getattr(event, 'voltage', None)),
                temperature=_float_or_none(getattr(event, 'temperature', None)),
                humidity=_float_or_none(getattr(event, 'humidity', None)),
                uptime_secs=_int_or_none(getattr(event, 'uptime_secs', None)),
            )
            self._client.on_telemetry(tel)

        elif t in ('traceroute',):
            tr = MeshcoreTraceroute(
                origin_id=str(getattr(event, 'origin_id', '')),
                destination_id=str(getattr(event, 'destination_id', '')),
                hops=list(getattr(event, 'hops', [])),
                snr_per_hop=[float(x) for x in getattr(event, 'snr_per_hop', [])],
                timestamp=datetime.now(timezone.utc),
            )
            self._client.on_traceroute(tr)

    # ------------------------------------------------------------------
    # Actions — called from Flask thread via run_coroutine_threadsafe
    # ------------------------------------------------------------------

    def _submit(self, coro) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def send_text(self, recipient_id: str, text: str) -> None:
        async def _send():
            if self._conn:
                # Assumed API: await self._conn.send_text(recipient_id, text)
                await self._conn.send_text(recipient_id, text)
        self._submit(_send())

    def request_traceroute(self, node_id: str) -> None:
        async def _trace():
            if self._conn:
                # Assumed API: await self._conn.traceroute(node_id)
                await self._conn.traceroute(node_id)
        self._submit(_trace())

    def scan_ble_sync(self) -> list[dict]:
        import meshcore as mc_lib
        async def _scan():
            # Assumed API: await mc_lib.scan_ble() → [{address, name, rssi}]
            return await mc_lib.scan_ble()
        future = asyncio.run_coroutine_threadsafe(_scan(), self._loop)
        try:
            return future.result(timeout=10)
        except Exception:
            return []


def _float_or_none(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 2: Commit**

```bash
git add utils/meshcore_client.py
git commit -m "feat(meshcore): add async worker bridge (utils/meshcore_client.py)"
```

---

## Task 4: Flask routes — routes/meshcore.py

**Files:**
- Create: `routes/meshcore.py`

- [ ] **Step 1: Create routes/meshcore.py**

```python
"""Meshcore device routes.

Endpoints for connecting to Meshcore devices (serial, TCP, BLE),
streaming live events, and managing messages, contacts, and nodes.
"""
from __future__ import annotations

import os
import queue

from flask import Blueprint, Response, jsonify, request

from utils.logging import get_logger
from utils.meshcore import (
    BLEConfig,
    SerialConfig,
    TCPConfig,
    get_meshcore_client,
    is_meshcore_available,
    list_serial_ports,
)
from utils.responses import api_error
from utils.sse import sse_stream_fanout

logger = get_logger('intercept.meshcore')

meshcore_bp = Blueprint('meshcore', __name__, url_prefix='/meshcore')


def _client():
    return get_meshcore_client()


# ---------------------------------------------------------------------------
# Status & connection management
# ---------------------------------------------------------------------------

@meshcore_bp.route('/status')
def status():
    if not is_meshcore_available():
        return jsonify({'available': False, 'state': 'unavailable',
                        'message': 'meshcore package not installed. Run: pip install meshcore'})
    c = _client()
    return jsonify({'available': True, 'state': c.get_state().value})


@meshcore_bp.route('/connect', methods=['POST'])
def connect():
    if not is_meshcore_available():
        return api_error('meshcore not installed', 503)
    data = request.get_json(silent=True) or {}
    transport = data.get('transport', 'serial')

    if transport == 'serial':
        config = SerialConfig(port=data.get('port'), baud=int(data.get('baud', 115200)))
    elif transport == 'tcp':
        host = data.get('host', 'localhost')
        port = int(data.get('port', 5000))
        config = TCPConfig(host=host, port=port)
    elif transport == 'ble':
        config = BLEConfig(device_address=data.get('address'))
    else:
        return api_error(f"Unknown transport: {transport}", 400)

    _client().connect(config)
    return jsonify({'status': 'connecting', 'transport': transport})


@meshcore_bp.route('/disconnect', methods=['POST'])
def disconnect():
    _client().disconnect()
    return jsonify({'status': 'disconnected'})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@meshcore_bp.route('/ports')
def ports():
    return jsonify({'ports': list_serial_ports()})


@meshcore_bp.route('/ble/scan')
def ble_scan():
    if not is_meshcore_available():
        return api_error('meshcore not installed', 503)
    devices = _client().scan_ble()
    return jsonify({'devices': devices})


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

@meshcore_bp.route('/stream')
def stream():
    def _gen():
        q = _client().get_queue()
        import json
        while True:
            try:
                event = q.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"
    return Response(_gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@meshcore_bp.route('/messages')
def messages():
    return jsonify({'messages': _client().get_messages()})


@meshcore_bp.route('/send', methods=['POST'])
def send():
    data = request.get_json(silent=True) or {}
    text = data.get('text', '').strip()
    recipient_id = data.get('recipient_id', 'BROADCAST')
    if not text:
        return api_error('text is required', 400)
    if len(text) > 237:
        return api_error('text exceeds 237-character Meshcore limit', 400)
    _client().send_text(recipient_id, text)
    return jsonify({'status': 'queued'})


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@meshcore_bp.route('/nodes')
def nodes():
    return jsonify({'nodes': _client().get_nodes()})


@meshcore_bp.route('/repeaters')
def repeaters():
    return jsonify({'repeaters': _client().get_repeaters()})


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@meshcore_bp.route('/contacts', methods=['GET'])
def list_contacts():
    return jsonify({'contacts': _client().get_contacts()})


@meshcore_bp.route('/contacts', methods=['POST'])
def add_contact():
    from utils.meshcore import MeshcoreContact
    data = request.get_json(silent=True) or {}
    node_id = data.get('node_id', '').strip()
    name = data.get('name', '').strip()
    public_key = data.get('public_key', '').strip()
    if not node_id or not name or not public_key:
        return api_error('node_id, name, and public_key are required', 400)
    contact = MeshcoreContact(node_id=node_id, name=name, public_key=public_key, last_msg=None)
    _client().add_contact(contact)
    return jsonify({'status': 'added', 'contact': contact.to_dict()})


@meshcore_bp.route('/contacts/<node_id>', methods=['DELETE'])
def delete_contact(node_id: str):
    removed = _client().remove_contact(node_id)
    if not removed:
        return api_error('contact not found', 404)
    return jsonify({'status': 'removed'})


# ---------------------------------------------------------------------------
# Telemetry & traceroute
# ---------------------------------------------------------------------------

@meshcore_bp.route('/telemetry/<node_id>')
def telemetry(node_id: str):
    return jsonify({'node_id': node_id, 'telemetry': _client().get_telemetry(node_id)})


@meshcore_bp.route('/traceroute', methods=['POST'])
def traceroute():
    data = request.get_json(silent=True) or {}
    node_id = data.get('node_id', '').strip()
    if not node_id:
        return api_error('node_id is required', 400)
    _client().request_traceroute(node_id)
    return jsonify({'status': 'requested', 'node_id': node_id})
```

- [ ] **Step 2: Commit**

```bash
git add routes/meshcore.py
git commit -m "feat(meshcore): add Flask blueprint with all 15 endpoints + SSE stream"
```

---

## Task 5: Route tests — tests/test_meshcore_routes.py

**Files:**
- Create: `tests/test_meshcore_routes.py`

- [ ] **Step 1: Write route tests**

```python
"""Route tests for Meshcore blueprint."""
import json
from unittest.mock import MagicMock, patch
import pytest


@pytest.fixture()
def app():
    from app import create_app
    application = create_app({'TESTING': True})
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def mock_meshcore_client():
    mc = MagicMock()
    mc.get_state.return_value = MagicMock(value='disconnected')
    mc.get_messages.return_value = []
    mc.get_nodes.return_value = []
    mc.get_repeaters.return_value = []
    mc.get_contacts.return_value = []
    mc.get_telemetry.return_value = []
    mc.scan_ble.return_value = []
    with patch('routes.meshcore.get_meshcore_client', return_value=mc), \
         patch('routes.meshcore.is_meshcore_available', return_value=True):
        yield mc


class TestStatus:
    def test_returns_json(self, client):
        r = client.get('/meshcore/status')
        assert r.status_code == 200
        d = r.get_json()
        assert 'state' in d
        assert 'available' in d

    def test_unavailable_when_not_installed(self, client):
        with patch('routes.meshcore.is_meshcore_available', return_value=False):
            r = client.get('/meshcore/status')
            d = r.get_json()
            assert d['available'] is False


class TestConnect:
    def test_serial_connect(self, client, mock_meshcore_client):
        r = client.post('/meshcore/connect',
                        json={'transport': 'serial', 'port': '/dev/ttyUSB0'})
        assert r.status_code == 200
        assert r.get_json()['status'] == 'connecting'
        mock_meshcore_client.connect.assert_called_once()

    def test_tcp_connect(self, client, mock_meshcore_client):
        r = client.post('/meshcore/connect',
                        json={'transport': 'tcp', 'host': '192.168.1.10', 'port': 5000})
        assert r.status_code == 200
        mock_meshcore_client.connect.assert_called_once()

    def test_ble_connect(self, client, mock_meshcore_client):
        r = client.post('/meshcore/connect',
                        json={'transport': 'ble', 'address': 'AA:BB:CC:DD:EE:FF'})
        assert r.status_code == 200

    def test_unknown_transport_returns_400(self, client):
        r = client.post('/meshcore/connect', json={'transport': 'zigbee'})
        assert r.status_code == 400


class TestSend:
    def test_sends_text(self, client, mock_meshcore_client):
        r = client.post('/meshcore/send',
                        json={'text': 'hello', 'recipient_id': 'NODE1'})
        assert r.status_code == 200
        mock_meshcore_client.send_text.assert_called_once_with('NODE1', 'hello')

    def test_empty_text_returns_400(self, client):
        r = client.post('/meshcore/send', json={'text': ''})
        assert r.status_code == 400

    def test_missing_text_returns_400(self, client):
        r = client.post('/meshcore/send', json={})
        assert r.status_code == 400

    def test_text_too_long_returns_400(self, client):
        r = client.post('/meshcore/send', json={'text': 'x' * 238})
        assert r.status_code == 400


class TestContacts:
    def test_add_contact(self, client, mock_meshcore_client):
        r = client.post('/meshcore/contacts', json={
            'node_id': 'N1', 'name': 'Alice', 'public_key': 'abc123'
        })
        assert r.status_code == 200
        mock_meshcore_client.add_contact.assert_called_once()

    def test_add_contact_missing_fields_returns_400(self, client):
        r = client.post('/meshcore/contacts', json={'node_id': 'N1'})
        assert r.status_code == 400

    def test_delete_contact_not_found(self, client, mock_meshcore_client):
        mock_meshcore_client.remove_contact.return_value = False
        r = client.delete('/meshcore/contacts/UNKNOWN')
        assert r.status_code == 404

    def test_delete_contact_success(self, client, mock_meshcore_client):
        mock_meshcore_client.remove_contact.return_value = True
        r = client.delete('/meshcore/contacts/N1')
        assert r.status_code == 200


class TestPorts:
    def test_returns_list(self, client):
        with patch('routes.meshcore.list_serial_ports', return_value=['/dev/ttyUSB0']):
            r = client.get('/meshcore/ports')
            assert r.status_code == 200
            assert '/dev/ttyUSB0' in r.get_json()['ports']


class TestTraceroute:
    def test_requires_node_id(self, client):
        r = client.post('/meshcore/traceroute', json={})
        assert r.status_code == 400

    def test_queues_request(self, client, mock_meshcore_client):
        r = client.post('/meshcore/traceroute', json={'node_id': 'NODE1'})
        assert r.status_code == 200
        mock_meshcore_client.request_traceroute.assert_called_once_with('NODE1')
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_meshcore_routes.py -v
```

Expected: All tests pass. If `create_app` import fails, check `app.py` for the factory function name and adjust the import.

- [ ] **Step 3: Commit**

```bash
git add tests/test_meshcore_routes.py
git commit -m "test(meshcore): add route tests"
```

---

## Task 6: Integration tests — tests/test_meshcore_integration.py

**Files:**
- Create: `tests/test_meshcore_integration.py`

- [ ] **Step 1: Write integration tests**

```python
"""Integration tests: mock meshcore library at its boundary, test full flow."""
import json
import queue
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


class TestMessageRoundTrip:
    """Connect → receive event → appears in message store and SSE queue."""

    def test_message_stored_on_receipt(self):
        from utils.meshcore import MeshcoreClient, MeshcoreMessage
        client = MeshcoreClient()

        msg = MeshcoreMessage(
            id='t1', sender_id='A', recipient_id='BROADCAST',
            text='test', timestamp=datetime.now(timezone.utc),
            hop_count=1, snr=-10.0, is_direct=False,
        )
        client.on_message(msg)

        msgs = client.get_messages()
        assert len(msgs) == 1
        assert msgs[0]['text'] == 'test'

    def test_message_pushed_to_queue(self):
        from utils.meshcore import MeshcoreClient, MeshcoreMessage
        client = MeshcoreClient()

        msg = MeshcoreMessage(
            id='t2', sender_id='B', recipient_id='BROADCAST',
            text='queued', timestamp=datetime.now(timezone.utc),
            hop_count=0, snr=None, is_direct=True,
        )
        client.on_message(msg)

        event = client.get_queue().get_nowait()
        assert event['type'] == 'message'
        assert event['data']['text'] == 'queued'

    def test_message_history_capped_at_500(self):
        from utils.meshcore import MeshcoreClient, MeshcoreMessage
        client = MeshcoreClient()

        for i in range(510):
            client.on_message(MeshcoreMessage(
                id=str(i), sender_id='X', recipient_id='BROADCAST',
                text=f'msg{i}', timestamp=datetime.now(timezone.utc),
                hop_count=0, snr=None, is_direct=False,
            ))

        assert len(client.get_messages()) == 500


class TestNodeAndRepeater:
    def test_repeater_appears_in_repeaters_only(self):
        from utils.meshcore import MeshcoreClient, MeshcoreNode
        client = MeshcoreClient()

        client.on_node(MeshcoreNode(
            node_id='R1', name='Roof', is_repeater=True,
            lat=51.5, lon=-0.1, battery_pct=100,
            last_seen=datetime.now(timezone.utc), snr=-3.0, hops_away=1,
        ))
        client.on_node(MeshcoreNode(
            node_id='C1', name='Client', is_repeater=False,
            lat=51.6, lon=-0.2, battery_pct=72,
            last_seen=datetime.now(timezone.utc), snr=-8.0, hops_away=2,
        ))

        assert len(client.get_repeaters()) == 1
        assert client.get_repeaters()[0]['node_id'] == 'R1'
        assert len(client.get_nodes()) == 2


class TestTracerouteRoundTrip:
    def test_traceroute_pushed_to_queue(self):
        from utils.meshcore import MeshcoreClient, MeshcoreTraceroute
        client = MeshcoreClient()

        tr = MeshcoreTraceroute(
            origin_id='A', destination_id='B',
            hops=['A', 'R1', 'B'], snr_per_hop=[-5.0, -8.0],
            timestamp=datetime.now(timezone.utc),
        )
        client.on_traceroute(tr)

        q = client.get_queue()
        event = q.get_nowait()
        assert event['type'] == 'traceroute'
        assert event['data']['hops'] == ['A', 'R1', 'B']


class TestBLEDockerBlock:
    def test_ble_blocked_in_docker(self, tmp_path, monkeypatch):
        monkeypatch.setenv('INTERCEPT_DOCKER', '1')
        from utils.meshcore import BLEConfig, MeshcoreClient
        client = MeshcoreClient()

        with patch('utils.meshcore_client.AsyncWorker') as MockWorker:
            client.connect(BLEConfig())
            MockWorker.assert_not_called()

        q = client.get_queue()
        event = q.get_nowait()
        assert event['data']['state'] == 'error'
        assert 'Docker' in event['data']['message']


class TestConnectionStateTransitions:
    def test_on_connected_pushes_status_event(self):
        from utils.meshcore import MeshcoreClient, ConnectionState
        client = MeshcoreClient()
        client.on_connected(transport='serial', device='/dev/ttyUSB0')

        assert client.get_state() == ConnectionState.CONNECTED
        event = client.get_queue().get_nowait()
        assert event['type'] == 'status'
        assert event['data']['state'] == 'connected'

    def test_on_error_pushes_status_event(self):
        from utils.meshcore import MeshcoreClient, ConnectionState
        client = MeshcoreClient()
        client.on_error('timeout')

        assert client.get_state() == ConnectionState.ERROR
        event = client.get_queue().get_nowait()
        assert event['data']['state'] == 'error'
```

- [ ] **Step 2: Run integration tests**

```bash
pytest tests/test_meshcore_integration.py -v
```

Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_meshcore_integration.py
git commit -m "test(meshcore): add integration tests"
```

---

## Task 7: Register blueprint and add dependency

**Files:**
- Modify: `routes/__init__.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Register blueprint in routes/__init__.py**

Open `routes/__init__.py`. Find the line `from .meshtastic import meshtastic_bp` (line ~26). Add immediately after:

```python
from .meshcore import meshcore_bp
```

Find `app.register_blueprint(meshtastic_bp)` (line ~72). Add immediately after:

```python
app.register_blueprint(meshcore_bp)
```

- [ ] **Step 2: Add dependency to requirements.txt**

Find the line `meshtastic>=2.0.0` (line ~29). Add immediately after:

```
meshcore>=1.0.0
```

- [ ] **Step 3: Verify the app starts**

```bash
python -c "from app import create_app; app = create_app(); print('OK')"
```

Expected: `OK` with no import errors.

- [ ] **Step 4: Run full test suite**

```bash
pytest tests/test_meshcore_client.py tests/test_meshcore_routes.py tests/test_meshcore_integration.py -v
```

Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add routes/__init__.py requirements.txt
git commit -m "feat(meshcore): register blueprint and add meshcore dependency"
```

---

## Task 8: CSS — static/css/modes/meshcore.css

**Files:**
- Create: `static/css/modes/meshcore.css`

- [ ] **Step 1: Create the CSS file**

```css
/* Meshcore mode — scoped styles */

#meshcoreMode {
    display: flex;
    flex-direction: column;
    height: 100%;
    gap: 0;
}

/* ── Sidebar ── */
.meshcore-sidebar {
    width: 220px;
    min-width: 220px;
    background: var(--bg-card);
    border-right: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    flex-shrink: 0;
}

.meshcore-sidebar-section {
    padding: 10px;
    border-bottom: 1px solid var(--border-color);
}

.meshcore-sidebar-section h4 {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    margin: 0 0 8px;
}

/* ── Connection panel ── */
.meshcore-transport-tabs {
    display: flex;
    gap: 4px;
    margin-bottom: 8px;
}

.meshcore-transport-tab {
    flex: 1;
    padding: 4px 0;
    font-size: 11px;
    text-align: center;
    background: var(--bg-input);
    border: 1px solid var(--border-color);
    border-radius: 3px;
    cursor: pointer;
    color: var(--text-muted);
    transition: background 0.15s, color 0.15s;
}

.meshcore-transport-tab.active {
    background: var(--accent-cyan);
    color: #000;
    border-color: var(--accent-cyan);
}

/* ── Status indicator ── */
.meshcore-status-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    background: var(--text-muted);
}

.meshcore-status-dot.connected { background: #4caf50; box-shadow: 0 0 5px #4caf50; }
.meshcore-status-dot.connecting { background: #ff9800; animation: meshcore-pulse 1s infinite; }
.meshcore-status-dot.error { background: #f44336; }

@keyframes meshcore-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
}

/* ── Node / contact list items ── */
.meshcore-node-item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 0;
    font-size: 12px;
    border-bottom: 1px solid var(--border-color);
    cursor: pointer;
}

.meshcore-node-item:last-child { border-bottom: none; }

.meshcore-node-icon {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--accent-cyan);
    flex-shrink: 0;
}

.meshcore-node-icon.repeater {
    border-radius: 0;
    clip-path: polygon(50% 0%, 100% 100%, 0% 100%);
    background: #ff9800;
}

.meshcore-node-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meshcore-node-meta { font-size: 10px; color: var(--text-muted); }

/* ── Main content area ── */
.meshcore-main {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

/* ── Tab bar ── */
.meshcore-tabs {
    display: flex;
    gap: 0;
    border-bottom: 1px solid var(--border-color);
    background: var(--bg-card);
    flex-shrink: 0;
}

.meshcore-tab {
    padding: 8px 16px;
    font-size: 12px;
    cursor: pointer;
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    transition: color 0.15s, border-color 0.15s;
}

.meshcore-tab.active {
    color: var(--accent-cyan);
    border-bottom-color: var(--accent-cyan);
}

/* ── Message feed ── */
.meshcore-messages {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.meshcore-message {
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 12px;
}

.meshcore-message.pending { opacity: 0.6; border-style: dashed; }
.meshcore-message.direct { border-left: 3px solid var(--accent-cyan); }

.meshcore-message-header {
    display: flex;
    justify-content: space-between;
    margin-bottom: 4px;
    font-size: 11px;
    color: var(--text-muted);
}

.meshcore-message-sender { color: var(--accent-cyan); font-weight: 600; }
.meshcore-message-text { color: var(--text-primary); }

/* ── Compose bar ── */
.meshcore-compose {
    display: flex;
    gap: 6px;
    padding: 8px 12px;
    border-top: 1px solid var(--border-color);
    background: var(--bg-card);
    flex-shrink: 0;
}

.meshcore-compose input {
    flex: 1;
    background: var(--bg-input);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 6px 10px;
    color: var(--text-primary);
    font-size: 13px;
    font-family: var(--font-mono);
}

.meshcore-compose input:focus {
    outline: none;
    border-color: var(--accent-cyan);
}

/* ── Repeaters tab table ── */
.meshcore-repeater-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
}

.meshcore-repeater-table th {
    text-align: left;
    padding: 6px 10px;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    border-bottom: 1px solid var(--border-color);
}

.meshcore-repeater-table td {
    padding: 6px 10px;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-primary);
}

/* ── Traceroute modal ── */
.meshcore-traceroute-hops {
    display: flex;
    align-items: center;
    gap: 0;
    flex-wrap: wrap;
    padding: 16px 0;
}

.meshcore-hop {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
}

.meshcore-hop-node {
    background: var(--bg-input);
    border: 1px solid var(--accent-cyan);
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 11px;
    font-family: var(--font-mono);
}

.meshcore-hop-arrow {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 0 6px;
    font-size: 10px;
    color: var(--text-muted);
}

/* ── Map tab ── */
#meshcoreMap {
    width: 100%;
    height: 100%;
    min-height: 300px;
}

.meshcore-tab-panel { display: none; flex: 1; overflow: hidden; }
.meshcore-tab-panel.active { display: flex; flex-direction: column; }
```

- [ ] **Step 2: Commit**

```bash
git add static/css/modes/meshcore.css
git commit -m "feat(meshcore): add scoped CSS"
```

---

## Task 9: HTML partial — templates/partials/modes/meshcore.html

**Files:**
- Create: `templates/partials/modes/meshcore.html`

- [ ] **Step 1: Create the template partial**

```html
{# Meshcore Mode Partial #}
<div id="meshcoreMode" class="mode-content" style="display:none; flex-direction: row; height: 100%;">

  {# ── Sidebar ── #}
  <div class="meshcore-sidebar">

    {# Connection section #}
    <div class="meshcore-sidebar-section">
      <h4>Connection</h4>
      <div id="meshcoreStatusBar" style="display:flex;align-items:center;font-size:12px;margin-bottom:8px;">
        <span class="meshcore-status-dot" id="meshcoreStatusDot"></span>
        <span id="meshcoreStatusText">Disconnected</span>
      </div>

      {# Transport selector #}
      <div class="meshcore-transport-tabs" id="meshcoreTransportTabs">
        <div class="meshcore-transport-tab active" data-transport="serial" onclick="MeshCore.selectTransport('serial')">Serial</div>
        <div class="meshcore-transport-tab" data-transport="tcp" onclick="MeshCore.selectTransport('tcp')">TCP</div>
        <div class="meshcore-transport-tab" data-transport="ble" onclick="MeshCore.selectTransport('ble')">BLE</div>
      </div>

      {# Serial config #}
      <div id="meshcoreSerialConfig">
        <select id="meshcorePortSelect" style="width:100%;background:var(--bg-input);border:1px solid var(--border-color);color:var(--text-primary);padding:4px;font-size:12px;border-radius:3px;margin-bottom:6px;">
          <option value="">Auto-detect</option>
        </select>
      </div>

      {# TCP config #}
      <div id="meshcoreTcpConfig" style="display:none;">
        <input id="meshcoreTcpHost" type="text" placeholder="Host / IP" value="localhost"
               style="width:100%;box-sizing:border-box;background:var(--bg-input);border:1px solid var(--border-color);color:var(--text-primary);padding:4px 6px;font-size:12px;border-radius:3px;margin-bottom:4px;">
        <input id="meshcoreTcpPort" type="number" placeholder="Port" value="5000"
               style="width:100%;box-sizing:border-box;background:var(--bg-input);border:1px solid var(--border-color);color:var(--text-primary);padding:4px 6px;font-size:12px;border-radius:3px;">
      </div>

      {# BLE config #}
      <div id="meshcoreBleConfig" style="display:none;">
        <select id="meshcoreBleSelect" style="width:100%;background:var(--bg-input);border:1px solid var(--border-color);color:var(--text-primary);padding:4px;font-size:12px;border-radius:3px;margin-bottom:4px;">
          <option value="">Scan for devices...</option>
        </select>
        <button class="preset-btn" onclick="MeshCore.scanBle()" style="width:100%;font-size:11px;">Scan BLE</button>
      </div>

      <div style="display:flex;gap:6px;margin-top:8px;">
        <button class="run-btn" id="meshcoreConnectBtn" onclick="MeshCore.connect()" style="flex:1;font-size:12px;">Connect</button>
        <button class="preset-btn" id="meshcoreDisconnectBtn" onclick="MeshCore.disconnect()" style="flex:1;font-size:12px;" disabled>Disconnect</button>
      </div>
    </div>

    {# Contacts section #}
    <div class="meshcore-sidebar-section">
      <h4>Contacts <button onclick="MeshCore.showAddContact()" style="float:right;background:none;border:none;color:var(--accent-cyan);cursor:pointer;font-size:14px;padding:0;line-height:1;">+</button></h4>
      <div id="meshcoreContactList" style="max-height:120px;overflow-y:auto;">
        <div style="font-size:11px;color:var(--text-muted);">No contacts</div>
      </div>
    </div>

    {# Nodes section #}
    <div class="meshcore-sidebar-section" style="flex:1;overflow-y:auto;">
      <h4>Nodes</h4>
      <div id="meshcoreNodeList">
        <div style="font-size:11px;color:var(--text-muted);">No nodes seen</div>
      </div>
    </div>

  </div>{# /sidebar #}

  {# ── Main content ── #}
  <div class="meshcore-main">

    {# Tab bar #}
    <div class="meshcore-tabs">
      <div class="meshcore-tab active" data-tab="messages" onclick="MeshCore.switchTab('messages')">Messages</div>
      <div class="meshcore-tab" data-tab="map" onclick="MeshCore.switchTab('map')">Map</div>
      <div class="meshcore-tab" data-tab="repeaters" onclick="MeshCore.switchTab('repeaters')">Repeaters</div>
      <div class="meshcore-tab" data-tab="telemetry" onclick="MeshCore.switchTab('telemetry')">Telemetry</div>
    </div>

    {# Messages tab #}
    <div class="meshcore-tab-panel active" id="meshcoreTabMessages">
      <div class="meshcore-messages" id="meshcoreMessageFeed">
        <div style="text-align:center;color:var(--text-muted);font-size:12px;padding:24px;">
          Connect to a Meshcore device to see messages
        </div>
      </div>
      <div class="meshcore-compose">
        <select id="meshcoreRecipientSelect" style="background:var(--bg-input);border:1px solid var(--border-color);color:var(--text-primary);padding:6px;font-size:12px;border-radius:4px;width:140px;">
          <option value="BROADCAST">Broadcast</option>
        </select>
        <input id="meshcoreComposeInput" type="text" placeholder="Type a message (max 237 chars)..." maxlength="237"
               onkeydown="if(event.key==='Enter')MeshCore.sendMessage()">
        <button class="run-btn" onclick="MeshCore.sendMessage()" style="white-space:nowrap;">Send</button>
      </div>
    </div>

    {# Map tab #}
    <div class="meshcore-tab-panel" id="meshcoreTabMap">
      <div id="meshcoreMap"></div>
    </div>

    {# Repeaters tab #}
    <div class="meshcore-tab-panel" id="meshcoreTabRepeaters" style="overflow-y:auto;padding:12px;">
      <table class="meshcore-repeater-table">
        <thead>
          <tr>
            <th>Name</th><th>Node ID</th><th>Hops</th><th>SNR</th><th>Battery</th><th>Last Seen</th>
          </tr>
        </thead>
        <tbody id="meshcoreRepeaterTableBody">
          <tr><td colspan="6" style="color:var(--text-muted);text-align:center;padding:16px;">No repeaters detected</td></tr>
        </tbody>
      </table>
    </div>

    {# Telemetry tab #}
    <div class="meshcore-tab-panel" id="meshcoreTabTelemetry" style="padding:12px;overflow-y:auto;">
      <div style="margin-bottom:8px;">
        <label style="font-size:12px;color:var(--text-muted);">Node: </label>
        <select id="meshcoreTelemetryNodeSelect" onchange="MeshCore.loadTelemetry(this.value)"
                style="background:var(--bg-input);border:1px solid var(--border-color);color:var(--text-primary);padding:4px;font-size:12px;border-radius:3px;">
          <option value="">Select a node</option>
        </select>
      </div>
      <canvas id="meshcoreTelemetryChart" style="max-height:300px;"></canvas>
    </div>

  </div>{# /main #}

</div>{# /meshcoreMode #}

{# ── Add Contact Modal ── #}
<div id="meshcoreAddContactModal" class="signal-details-modal" style="display:none;">
  <div class="signal-details-modal-backdrop" onclick="MeshCore.closeAddContact()"></div>
  <div class="signal-details-modal-content" style="max-width:400px;">
    <div class="signal-details-modal-header">
      <h3>Add Contact</h3>
      <button class="signal-details-modal-close" onclick="MeshCore.closeAddContact()">&times;</button>
    </div>
    <div class="signal-details-modal-body" style="display:flex;flex-direction:column;gap:10px;">
      <div>
        <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:3px;">Node ID</label>
        <input id="meshcoreContactNodeId" type="text" placeholder="e.g. NODE001" class="mock-input" style="width:100%;box-sizing:border-box;">
      </div>
      <div>
        <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:3px;">Name</label>
        <input id="meshcoreContactName" type="text" placeholder="Display name" class="mock-input" style="width:100%;box-sizing:border-box;">
      </div>
      <div>
        <label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:3px;">Public Key</label>
        <input id="meshcoreContactKey" type="text" placeholder="Base64 public key" class="mock-input" style="width:100%;box-sizing:border-box;font-family:var(--font-mono);font-size:11px;">
      </div>
    </div>
    <div class="signal-details-modal-footer" style="display:flex;gap:8px;">
      <button class="preset-btn" onclick="MeshCore.closeAddContact()" style="flex:1;">Cancel</button>
      <button class="run-btn" onclick="MeshCore.saveContact()" style="flex:1;">Add Contact</button>
    </div>
  </div>
</div>

{# ── Traceroute Modal ── #}
<div id="meshcoreTracerouteModal" class="signal-details-modal" style="display:none;">
  <div class="signal-details-modal-backdrop" onclick="MeshCore.closeTraceroute()"></div>
  <div class="signal-details-modal-content" style="max-width:600px;">
    <div class="signal-details-modal-header">
      <h3>Traceroute Result</h3>
      <button class="signal-details-modal-close" onclick="MeshCore.closeTraceroute()">&times;</button>
    </div>
    <div class="signal-details-modal-body">
      <div class="meshcore-traceroute-hops" id="meshcoreTracerouteHops"></div>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add templates/partials/modes/meshcore.html
git commit -m "feat(meshcore): add HTML partial (sidebar, tabs, modals)"
```

---

## Task 10: JavaScript — static/js/modes/meshcore.js

**Files:**
- Create: `static/js/modes/meshcore.js`

- [ ] **Step 1: Create meshcore.js**

```javascript
/**
 * Meshcore Mode
 * Handles connection, live SSE streaming, message feed, map, telemetry,
 * repeater management, contacts, and traceroute visualization.
 */
const MeshCore = (function () {

    // ── State ──────────────────────────────────────────────────────────────
    let _transport = 'serial';
    let _eventSource = null;
    let _map = null;
    let _markers = {};          // node_id → L.marker
    let _telemetryChart = null;
    let _connected = false;

    // ── Init / Destroy ─────────────────────────────────────────────────────
    function init() {
        _loadPorts();
        _checkStatus();
        _initMap();
    }

    function destroy() {
        if (_eventSource) { _eventSource.close(); _eventSource = null; }
        if (_map) { _map.remove(); _map = null; _markers = {}; }
        if (_telemetryChart) { _telemetryChart.destroy(); _telemetryChart = null; }
        _connected = false;
    }

    function invalidateMap() {
        if (_map) _map.invalidateSize();
    }

    // ── Status ─────────────────────────────────────────────────────────────
    async function _checkStatus() {
        try {
            const r = await fetch('/meshcore/status');
            const d = await r.json();
            _updateStatusUI(d.state || 'disconnected', d.message);
        } catch (e) { /* ignore */ }
    }

    function _updateStatusUI(state, message) {
        const dot = document.getElementById('meshcoreStatusDot');
        const txt = document.getElementById('meshcoreStatusText');
        const connectBtn = document.getElementById('meshcoreConnectBtn');
        const disconnectBtn = document.getElementById('meshcoreDisconnectBtn');
        if (!dot) return;

        dot.className = 'meshcore-status-dot ' + state;
        const labels = { connected: 'Connected', connecting: 'Connecting…', error: 'Error', disconnected: 'Disconnected', unavailable: 'Not available' };
        txt.textContent = message || labels[state] || state;

        _connected = state === 'connected';
        if (connectBtn) connectBtn.disabled = state === 'connecting' || _connected;
        if (disconnectBtn) disconnectBtn.disabled = !_connected;

        if (_connected && !_eventSource) _startSSE();
        if (!_connected && _eventSource) { _eventSource.close(); _eventSource = null; }
    }

    // ── Transport selector ─────────────────────────────────────────────────
    function selectTransport(t) {
        _transport = t;
        document.querySelectorAll('.meshcore-transport-tab').forEach(el => {
            el.classList.toggle('active', el.dataset.transport === t);
        });
        document.getElementById('meshcoreSerialConfig').style.display = t === 'serial' ? '' : 'none';
        document.getElementById('meshcoreTcpConfig').style.display   = t === 'tcp'    ? '' : 'none';
        document.getElementById('meshcoreBleConfig').style.display   = t === 'ble'    ? '' : 'none';
    }

    // ── Connect / Disconnect ───────────────────────────────────────────────
    async function connect() {
        let body = { transport: _transport };
        if (_transport === 'serial') {
            body.port = document.getElementById('meshcorePortSelect').value || null;
        } else if (_transport === 'tcp') {
            body.host = document.getElementById('meshcoreTcpHost').value;
            body.port = parseInt(document.getElementById('meshcoreTcpPort').value, 10);
        } else if (_transport === 'ble') {
            body.address = document.getElementById('meshcoreBleSelect').value || null;
        }
        try {
            await fetch('/meshcore/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            _updateStatusUI('connecting');
        } catch (e) { console.error('Connect failed:', e); }
    }

    async function disconnect() {
        try {
            await fetch('/meshcore/disconnect', { method: 'POST' });
            _updateStatusUI('disconnected');
        } catch (e) { console.error('Disconnect failed:', e); }
    }

    // ── Port / BLE discovery ───────────────────────────────────────────────
    async function _loadPorts() {
        try {
            const r = await fetch('/meshcore/ports');
            const d = await r.json();
            const sel = document.getElementById('meshcorePortSelect');
            if (!sel) return;
            const current = sel.value;
            sel.innerHTML = '<option value="">Auto-detect</option>';
            (d.ports || []).forEach(p => {
                const o = document.createElement('option');
                o.value = p; o.textContent = p;
                if (p === current) o.selected = true;
                sel.appendChild(o);
            });
        } catch (e) { /* ignore */ }
    }

    async function scanBle() {
        try {
            const r = await fetch('/meshcore/ble/scan');
            const d = await r.json();
            const sel = document.getElementById('meshcoreBleSelect');
            if (!sel) return;
            sel.innerHTML = '<option value="">Select device</option>';
            (d.devices || []).forEach(dev => {
                const o = document.createElement('option');
                o.value = dev.address;
                o.textContent = `${dev.name || 'Unknown'} (${dev.address}) RSSI ${dev.rssi}`;
                sel.appendChild(o);
            });
        } catch (e) { console.error('BLE scan failed:', e); }
    }

    // ── SSE Stream ─────────────────────────────────────────────────────────
    function _startSSE() {
        if (_eventSource) _eventSource.close();
        _eventSource = new EventSource('/meshcore/stream');
        _eventSource.onmessage = (e) => {
            try {
                const event = JSON.parse(e.data);
                _routeEvent(event);
            } catch (err) { /* ignore malformed */ }
        };
        _eventSource.onerror = () => {
            setTimeout(_checkStatus, 2000);
        };
    }

    function _routeEvent(event) {
        switch (event.type) {
            case 'status':    _updateStatusUI(event.data.state, event.data.message); break;
            case 'message':   _appendMessage(event.data); break;
            case 'node':      _updateNode(event.data); break;
            case 'telemetry': _storeTelemetry(event.data); break;
            case 'traceroute': _showTraceroute(event.data); break;
        }
    }

    // ── Messages ───────────────────────────────────────────────────────────
    function _appendMessage(msg) {
        const feed = document.getElementById('meshcoreMessageFeed');
        if (!feed) return;
        // Remove placeholder
        const placeholder = feed.querySelector('div[style*="padding:24px"]');
        if (placeholder) placeholder.remove();

        const el = document.createElement('div');
        el.className = 'meshcore-message' + (msg.is_direct ? ' direct' : '') + (msg.pending ? ' pending' : '');
        el.dataset.msgId = msg.id;
        const ts = msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString() : '';
        const snr = msg.snr !== null && msg.snr !== undefined ? ` · ${msg.snr} dB` : '';
        el.innerHTML = `
            <div class="meshcore-message-header">
                <span class="meshcore-message-sender">${_esc(msg.sender_id)}</span>
                <span>${_esc(msg.recipient_id)} · ${ts}${snr}</span>
            </div>
            <div class="meshcore-message-text">${_esc(msg.text)}</div>`;
        feed.appendChild(el);
        feed.scrollTop = feed.scrollHeight;
    }

    async function sendMessage() {
        const input = document.getElementById('meshcoreComposeInput');
        const recipientSel = document.getElementById('meshcoreRecipientSelect');
        const text = input ? input.value.trim() : '';
        if (!text) return;
        const recipient_id = recipientSel ? recipientSel.value : 'BROADCAST';

        // Optimistic
        const tempId = 'pending-' + Date.now();
        _appendMessage({ id: tempId, sender_id: 'Me', recipient_id, text, timestamp: new Date().toISOString(), is_direct: recipient_id !== 'BROADCAST', snr: null, pending: true });
        if (input) input.value = '';

        try {
            const r = await fetch('/meshcore/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, recipient_id }),
            });
            if (!r.ok) {
                const d = await r.json();
                _removePending(tempId);
                alert(d.error || 'Send failed');
            }
        } catch (e) {
            _removePending(tempId);
            console.error('Send failed:', e);
        }
    }

    function _removePending(id) {
        const el = document.querySelector(`[data-msg-id="${id}"]`);
        if (el) el.remove();
    }

    // ── Nodes ──────────────────────────────────────────────────────────────
    function _updateNode(node) {
        _updateNodeSidebar(node);
        _updateMapMarker(node);
        _updateRepeaterTable(node);
        _updateTelemetryNodeSelect(node);
        _updateRecipientSelect(node);
    }

    function _updateNodeSidebar(node) {
        const list = document.getElementById('meshcoreNodeList');
        if (!list) return;
        let el = document.getElementById('meshcore-node-' + node.node_id);
        if (!el) {
            el = document.createElement('div');
            el.className = 'meshcore-node-item';
            el.id = 'meshcore-node-' + node.node_id;
            list.innerHTML = ''; // clear placeholder on first node
            list.appendChild(el);
        }
        const hops = node.hops_away !== null ? `${node.hops_away}h` : '?';
        const snr  = node.snr !== null ? `${node.snr}dB` : '';
        el.innerHTML = `
            <div class="meshcore-node-icon${node.is_repeater ? ' repeater' : ''}"></div>
            <div class="meshcore-node-name" title="${_esc(node.node_id)}">${_esc(node.name)}</div>
            <div class="meshcore-node-meta">${hops} ${snr}</div>`;
    }

    function _updateRepeaterTable(node) {
        if (!node.is_repeater) return;
        const tbody = document.getElementById('meshcoreRepeaterTableBody');
        if (!tbody) return;
        let row = document.getElementById('meshcore-rptr-' + node.node_id);
        if (!row) {
            if (tbody.querySelector('td[colspan]')) tbody.innerHTML = '';
            row = document.createElement('tr');
            row.id = 'meshcore-rptr-' + node.node_id;
            tbody.appendChild(row);
        }
        const ls = node.last_seen ? new Date(node.last_seen).toLocaleTimeString() : '—';
        row.innerHTML = `<td>${_esc(node.name)}</td><td style="font-family:var(--font-mono);font-size:11px">${_esc(node.node_id)}</td><td>${node.hops_away ?? '—'}</td><td>${node.snr ?? '—'}</td><td>${node.battery_pct !== null ? node.battery_pct + '%' : '—'}</td><td>${ls}</td>`;
    }

    function _updateTelemetryNodeSelect(node) {
        const sel = document.getElementById('meshcoreTelemetryNodeSelect');
        if (!sel || sel.querySelector(`option[value="${node.node_id}"]`)) return;
        const o = document.createElement('option');
        o.value = node.node_id; o.textContent = node.name || node.node_id;
        sel.appendChild(o);
    }

    function _updateRecipientSelect(node) {
        const sel = document.getElementById('meshcoreRecipientSelect');
        if (!sel || sel.querySelector(`option[value="${node.node_id}"]`)) return;
        const o = document.createElement('option');
        o.value = node.node_id; o.textContent = node.name || node.node_id;
        sel.appendChild(o);
    }

    // ── Map ────────────────────────────────────────────────────────────────
    function _initMap() {
        const container = document.getElementById('meshcoreMap');
        if (!container || _map) return;
        _map = L.map('meshcoreMap', { zoomControl: true }).setView([20, 0], 2);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap',
            maxZoom: 18,
        }).addTo(_map);
    }

    function _updateMapMarker(node) {
        if (node.lat === null || node.lon === null) return;
        if (!_map) return;

        const icon = L.divIcon({
            className: '',
            html: node.is_repeater
                ? `<div style="width:0;height:0;border-left:7px solid transparent;border-right:7px solid transparent;border-bottom:14px solid #ff9800;" title="${node.name}"></div>`
                : `<div style="width:12px;height:12px;border-radius:50%;background:#00bcd4;border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,.4);" title="${node.name}"></div>`,
            iconSize: [14, 14],
            iconAnchor: [7, 7],
        });

        if (_markers[node.node_id]) {
            _markers[node.node_id].setLatLng([node.lat, node.lon]).setIcon(icon);
        } else {
            _markers[node.node_id] = L.marker([node.lat, node.lon], { icon })
                .bindPopup(`<strong>${_esc(node.name)}</strong><br>${node.node_id}<br>Hops: ${node.hops_away ?? '?'}`)
                .addTo(_map);
        }
    }

    // ── Telemetry ──────────────────────────────────────────────────────────
    function _storeTelemetry(data) { /* SSE telemetry stored server-side; chart loads on demand */ }

    async function loadTelemetry(nodeId) {
        if (!nodeId) return;
        try {
            const r = await fetch(`/meshcore/telemetry/${encodeURIComponent(nodeId)}`);
            const d = await r.json();
            _renderTelemetryChart(d.telemetry || []);
        } catch (e) { console.error('Telemetry load failed:', e); }
    }

    function _renderTelemetryChart(data) {
        const ctx = document.getElementById('meshcoreTelemetryChart');
        if (!ctx) return;
        if (_telemetryChart) { _telemetryChart.destroy(); _telemetryChart = null; }
        if (!data.length) return;

        const labels = data.map(t => new Date(t.timestamp).toLocaleTimeString());
        _telemetryChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels,
                datasets: [
                    { label: 'Battery %', data: data.map(t => t.battery_pct), borderColor: '#4caf50', tension: 0.3, fill: false },
                    { label: 'Temp °C',   data: data.map(t => t.temperature), borderColor: '#ff9800', tension: 0.3, fill: false, yAxisID: 'y2' },
                ],
            },
            options: {
                responsive: true,
                scales: {
                    y:  { min: 0, max: 100, title: { display: true, text: 'Battery %' } },
                    y2: { position: 'right', title: { display: true, text: 'Temp °C' } },
                },
                plugins: { legend: { labels: { color: '#ccc' } } },
            },
        });
    }

    // ── Traceroute ─────────────────────────────────────────────────────────
    function _showTraceroute(tr) {
        const container = document.getElementById('meshcoreTracerouteHops');
        const modal = document.getElementById('meshcoreTracerouteModal');
        if (!container || !modal) return;

        container.innerHTML = '';
        tr.hops.forEach((hop, i) => {
            const hopEl = document.createElement('div');
            hopEl.className = 'meshcore-hop';
            hopEl.innerHTML = `<div class="meshcore-hop-node">${_esc(hop)}</div>`;
            container.appendChild(hopEl);

            if (i < tr.hops.length - 1) {
                const arrow = document.createElement('div');
                arrow.className = 'meshcore-hop-arrow';
                const snr = tr.snr_per_hop[i] !== undefined ? `${tr.snr_per_hop[i]} dB` : '';
                arrow.innerHTML = `<span>${snr}</span><span>→</span>`;
                container.appendChild(arrow);
            }
        });
        modal.style.display = 'flex';
    }

    function closeTraceroute() {
        const modal = document.getElementById('meshcoreTracerouteModal');
        if (modal) modal.style.display = 'none';
    }

    // ── Contacts ───────────────────────────────────────────────────────────
    function showAddContact() {
        const modal = document.getElementById('meshcoreAddContactModal');
        if (modal) modal.style.display = 'flex';
    }

    function closeAddContact() {
        const modal = document.getElementById('meshcoreAddContactModal');
        if (modal) modal.style.display = 'none';
    }

    async function saveContact() {
        const nodeId = document.getElementById('meshcoreContactNodeId').value.trim();
        const name   = document.getElementById('meshcoreContactName').value.trim();
        const key    = document.getElementById('meshcoreContactKey').value.trim();
        if (!nodeId || !name || !key) { alert('All fields required'); return; }

        try {
            const r = await fetch('/meshcore/contacts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ node_id: nodeId, name, public_key: key }),
            });
            if (r.ok) {
                closeAddContact();
                _refreshContacts();
            } else {
                const d = await r.json();
                alert(d.error || 'Failed to add contact');
            }
        } catch (e) { console.error('Add contact failed:', e); }
    }

    async function _refreshContacts() {
        try {
            const r = await fetch('/meshcore/contacts');
            const d = await r.json();
            const list = document.getElementById('meshcoreContactList');
            if (!list) return;
            list.innerHTML = '';
            if (!d.contacts || !d.contacts.length) {
                list.innerHTML = '<div style="font-size:11px;color:var(--text-muted);">No contacts</div>';
                return;
            }
            d.contacts.forEach(c => {
                const el = document.createElement('div');
                el.className = 'meshcore-node-item';
                el.innerHTML = `
                    <div class="meshcore-node-icon"></div>
                    <div class="meshcore-node-name">${_esc(c.name)}</div>
                    <button onclick="MeshCore.deleteContact('${_esc(c.node_id)}')" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:12px;padding:0;">✕</button>`;
                list.appendChild(el);
            });
        } catch (e) { /* ignore */ }
    }

    async function deleteContact(nodeId) {
        if (!confirm(`Remove contact ${nodeId}?`)) return;
        try {
            await fetch(`/meshcore/contacts/${encodeURIComponent(nodeId)}`, { method: 'DELETE' });
            _refreshContacts();
        } catch (e) { console.error('Delete contact failed:', e); }
    }

    // ── Tabs ───────────────────────────────────────────────────────────────
    function switchTab(name) {
        document.querySelectorAll('.meshcore-tab').forEach(t =>
            t.classList.toggle('active', t.dataset.tab === name));
        const panels = { messages: 'meshcoreTabMessages', map: 'meshcoreTabMap', repeaters: 'meshcoreTabRepeaters', telemetry: 'meshcoreTabTelemetry' };
        Object.entries(panels).forEach(([k, id]) => {
            const el = document.getElementById(id);
            if (el) el.classList.toggle('active', k === name);
        });
        if (name === 'map') setTimeout(() => { if (_map) _map.invalidateSize(); }, 50);
    }

    // ── Helpers ────────────────────────────────────────────────────────────
    function _esc(s) {
        return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    // ── Public API ─────────────────────────────────────────────────────────
    return {
        init,
        destroy,
        invalidateMap,
        connect,
        disconnect,
        selectTransport,
        scanBle,
        sendMessage,
        switchTab,
        loadTelemetry,
        showAddContact,
        closeAddContact,
        saveContact,
        deleteContact,
        closeTraceroute,
    };

})();
```

- [ ] **Step 2: Commit**

```bash
git add static/js/modes/meshcore.js
git commit -m "feat(meshcore): add frontend JS module (IIFE, SSE, map, telemetry, traceroute)"
```

---

## Task 11: Wire into index.html (14 insertion points)

**Files:**
- Modify: `templates/index.html`

Work through these in order. Each step gives the line number, the existing neighbour line to anchor on, and the exact text to insert.

- [ ] **Step 1: CSS registration (line ~91)**

Find:
```
            meshtastic: "{{ url_for('static', filename='css/modes/meshtastic.css') }}",
```
Add immediately after:
```
            meshcore: "{{ url_for('static', filename='css/modes/meshcore.css') }}",
```

- [ ] **Step 2: JS registration (line ~175)**

Find:
```
            meshtastic: "{{ url_for('static', filename='js/modes/meshtastic.js') }}",
```
Add immediately after:
```
            meshcore: "{{ url_for('static', filename='js/modes/meshcore.js') }}",
```

- [ ] **Step 3: Mode card button (line ~427)**

Find the `<button class="mode-card mode-card-sm" onclick="selectMode('meshtastic')">` block and the closing `</button>`. Add an equivalent block immediately after:
```html
                            <button class="mode-card mode-card-sm" onclick="selectMode('meshcore')">
                                <div class="mode-card-icon">📡</div>
                                <div class="mode-card-label">Meshcore</div>
                            </button>
```

- [ ] **Step 4: Include partial (line ~775)**

Find:
```
                {% include 'partials/modes/meshtastic.html' %}
```
Add immediately after:
```
                {% include 'partials/modes/meshcore.html' %}
```

- [ ] **Step 5: Visuals container (line ~2209)**

Find:
```
                <div id="meshtasticVisuals" class="mesh-visuals-container" style="display: none;">
```
Add a sibling immediately after its closing `</div>`:
```html
                <div id="meshcoreVisuals" class="mesh-visuals-container" style="display: none;">
                </div>
```

- [ ] **Step 6: Mode catalog entry (line ~3772)**

Find:
```
            meshtastic: { label: 'Meshtastic', indicator: 'MESHTASTIC', outputTitle: 'Meshtastic Mesh Monitor', group: 'wireless' },
```
Add immediately after:
```
            meshcore: { label: 'Meshcore', indicator: 'MESHCORE', outputTitle: 'Meshcore Mesh Monitor', group: 'wireless' },
```

- [ ] **Step 7: Destroy handler (line ~4387)**

Find:
```
                meshtastic: () => typeof Meshtastic !== 'undefined' && Meshtastic.destroy?.(),
```
Add immediately after:
```
                meshcore: () => typeof MeshCore !== 'undefined' && MeshCore.destroy?.(),
```

- [ ] **Step 8: Active class toggle (line ~4725)**

Find:
```
            document.getElementById('meshtasticMode')?.classList.toggle('active', mode === 'meshtastic');
```
Add immediately after:
```
            document.getElementById('meshcoreMode')?.classList.toggle('active', mode === 'meshcore');
```

- [ ] **Step 9: Visuals display (line ~4760-4803)**

Find:
```
            const meshtasticVisuals = document.getElementById('meshtasticVisuals');
```
Add immediately after that block and its corresponding `if (meshtasticVisuals) meshtasticVisuals.style.display = ...` line:
```javascript
            const meshcoreVisuals = document.getElementById('meshcoreVisuals');
```
And in the display-toggle line nearby:
```javascript
            if (meshcoreVisuals) meshcoreVisuals.style.display = mode === 'meshcore' ? 'flex' : 'none';
```

- [ ] **Step 10: modesWithVisuals array (line ~4821)**

Find the `modesWithVisuals` array. Add `'meshcore'` to it:
```
            const modesWithVisuals = ['satellite', ..., 'meshtastic', 'meshcore', ...];
```

- [ ] **Step 11: Sidebar hide logic (line ~4835)**

Find:
```
                if (mode === 'meshtastic') {
                    mainContent.classList.add('mesh-sidebar-hidden');
```
Add an `else if` immediately after the closing `}` of the meshtastic block:
```javascript
                } else if (mode === 'meshcore') {
                    mainContent.classList.add('mesh-sidebar-hidden');
```

- [ ] **Step 12: hideRecon array (line ~4879)**

Find the `hideRecon` array that includes `'meshtastic'`. Add `'meshcore'` to it.

- [ ] **Step 13: Sidebar restore (line ~4936)**

Find:
```
            if (mode !== 'meshtastic') {
```
Change to:
```
            if (mode !== 'meshtastic' && mode !== 'meshcore') {
```

- [ ] **Step 14: Mode init handler (line ~4967)**

Find:
```
            } else if (mode === 'meshtastic') {
                Meshtastic.init();
                // Fix map sizing after container becomes visible
                setTimeout(() => {
                    Meshtastic.invalidateMap();
                }, 100);
```
Add immediately after its closing `}`:
```javascript
            } else if (mode === 'meshcore') {
                MeshCore.init();
                setTimeout(() => {
                    MeshCore.invalidateMap();
                }, 100);
```

- [ ] **Step 15: Smoke-test the wiring**

```bash
python -c "from app import create_app; app = create_app(); print('OK')"
```

Then start the dev server and open the app in a browser. Click the Meshcore mode card — the panel should appear without JS errors.

```bash
sudo -E venv/bin/python intercept.py
```

Open http://localhost:5000, select Meshcore mode, open browser DevTools console and confirm no errors.

- [ ] **Step 16: Commit**

```bash
git add templates/index.html
git commit -m "feat(meshcore): wire Meshcore into index.html (14 insertion points)"
```

---

## Task 12: Final test run and cleanup

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/test_meshcore_client.py tests/test_meshcore_routes.py tests/test_meshcore_integration.py -v
```

Expected: All tests pass with no warnings.

- [ ] **Step 2: Lint**

```bash
ruff check utils/meshcore.py utils/meshcore_client.py routes/meshcore.py
ruff check --fix utils/meshcore.py utils/meshcore_client.py routes/meshcore.py
```

- [ ] **Step 3: Final commit**

```bash
git add -u
git commit -m "feat(meshcore): complete Meshcore integration — serial, TCP, BLE, full feature parity"
```

---

## Self-Review Checklist

- [x] **Spec coverage:**
  - USB serial ✓ (SerialConfig, /meshcore/ports, auto-discover)
  - TCP ✓ (TCPConfig, meshcore-proxy documented in error messages)
  - BLE ✓ (BLEConfig, /meshcore/ble/scan, Docker block)
  - Messages ✓ (SSE, /meshcore/messages, /meshcore/send, optimistic UI)
  - Node map ✓ (Leaflet, client=circle, repeater=triangle)
  - Telemetry ✓ (/meshcore/telemetry/<node_id>, Chart.js)
  - Traceroute ✓ (/meshcore/traceroute, modal hop diagram)
  - Repeater management ✓ (is_repeater flag, dedicated tab + map icon)
  - Contacts ✓ (CRUD via /meshcore/contacts)
  - Error handling ✓ (Docker BLE, SDK not installed, send failure)
  - Tests ✓ (client, routes, integration)
  - Blueprint registration ✓ (Task 7)
  - CSS ✓ (Task 8)
  - HTML partial ✓ (Task 9)
  - JS frontend ✓ (Task 10)
  - index.html wiring ✓ (Task 11, 14 points)

- [x] **No placeholders** — all code steps contain actual code
- [x] **Type consistency** — `MeshcoreMessage`, `MeshcoreNode`, `MeshcoreContact`, `MeshcoreTelemetry`, `MeshcoreTraceroute` used consistently across utils, routes, and tests. `get_meshcore_client()` is the singleton accessor throughout.
- [x] **Async library API** — Task 1 explicitly tells the developer to verify API names before Task 3. All library calls are confined to `utils/meshcore_client.py`.
