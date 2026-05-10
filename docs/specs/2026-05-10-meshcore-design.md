# Meshcore Support — Design Spec

**Date:** 2026-05-10  
**Status:** Approved

## Overview

Add a Meshcore mode to Intercept, providing full feature parity with the existing Meshtastic module. Meshcore is a LoRa mesh radio platform using a repeater-based routing model (dedicated infrastructure nodes relay; clients do not). It has an official Python library (`meshcore`, PyPI) and a published companion protocol.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Connection methods | USB serial + TCP + BLE | Maximum hardware flexibility |
| Feature scope | Full parity with Meshtastic | Messages, node map, telemetry, traceroute, repeater management |
| Async integration | Background asyncio thread | meshcore library is asyncio-based; this isolates it cleanly from Flask/gevent |
| UI layout | Messages-first (mirror Meshtastic) | Sidebar: contacts/nodes. Center: message feed. Tabs: map, telemetry, repeaters |
| BLE in Docker | Document limitation + proxy workaround | BLE unavailable in containers; meshcore-proxy bridges BLE → TCP |

## Architecture

### New Files

```
utils/meshcore.py              # MeshcoreClient singleton + dataclasses
utils/meshcore_client.py       # Thin async wrapper around meshcore library (lives in asyncio thread)
routes/meshcore.py             # Flask blueprint (/meshcore)
static/js/modes/meshcore.js    # Frontend IIFE module
static/css/modes/meshcore.css  # Scoped styles
templates/partials/modes/meshcore.html  # Sidebar partial
tests/test_meshcore_client.py
tests/test_meshcore_routes.py
tests/test_meshcore_integration.py
```

### Modified Files

- `routes/__init__.py` — import + `register_blueprint(meshcore_bp)`
- `templates/index.html` — ~12 insertion points (CSS, partial, JS, validModes, modeGroups, etc.)
- `requirements.txt` — add `meshcore>=1.0.0` (optional dep, graceful fallback if absent)
- `.gitignore` — already has `.superpowers/` ✓

### Async Bridge Pattern

```
meshcore library (asyncio event loop in daemon OS thread)
  → event callbacks (_on_message, _on_node_update, _on_telemetry)
  → asyncio.run_coroutine_threadsafe() → queue.Queue (thread-safe, max 500)
  → /meshcore/stream SSE generator drains queue (30s keepalive timeout)
  → Frontend EventSource routes by event type
```

This is the same conceptual pattern as all other decoder integrations in Intercept (ADS-B socket reader, AIS-catcher output thread, rtl_433 stdout thread), just with an explicit asyncio loop instead of a subprocess thread.

## Data Model

```python
@dataclass
class MeshcoreMessage:
    id: str
    sender_id: str
    recipient_id: str       # node ID or broadcast address
    text: str
    timestamp: datetime
    hop_count: int
    snr: float | None
    is_direct: bool         # DM vs broadcast
    pending: bool = False   # optimistic send state

@dataclass
class MeshcoreNode:
    node_id: str
    name: str
    is_repeater: bool       # key Meshcore distinction — rendered differently on map
    lat: float | None
    lon: float | None
    battery_pct: int | None
    last_seen: datetime
    snr: float | None
    hops_away: int | None

@dataclass
class MeshcoreContact:
    node_id: str
    name: str
    public_key: str         # Meshcore uses key-based addressing
    last_msg: datetime | None

@dataclass
class MeshcoreTelemetry:
    node_id: str
    timestamp: datetime
    battery_pct: int | None
    voltage: float | None
    temperature: float | None
    humidity: float | None
    uptime_secs: int | None

@dataclass
class MeshcoreTraceroute:
    origin_id: str
    destination_id: str
    hops: list[str]
    snr_per_hop: list[float]
    timestamp: datetime

@dataclass
class SerialConfig:
    port: str | None = None   # None = auto-discover
    baud: int = 115200

@dataclass
class TCPConfig:
    host: str = "localhost"
    port: int = 5000          # meshcore-proxy default

@dataclass
class BLEConfig:
    device_address: str | None = None  # None = scan for first Meshcore device

ConnectionConfig = SerialConfig | TCPConfig | BLEConfig
```

Connection state enum: `DISCONNECTED | CONNECTING | CONNECTED | ERROR`

## Connection Handling

### Serial
Auto-discover: scan `/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/cu.usbserial*` and return list to frontend via `GET /meshcore/ports`. User can also specify path directly.

### TCP
Direct connection to `host:port`. Primary use case: meshcore-proxy running on the host, exposing a local USB or BLE device over TCP for Docker deployments.

### BLE
- Linux/RPi: meshcore library uses BlueZ (requires `bluetoothctl` accessible)
- macOS: meshcore library uses CoreBluetooth
- Docker: detect via presence of `/.dockerenv` or `INTERCEPT_DOCKER=1` env var; connect attempt fails fast with clear error directing user to meshcore-proxy

`GET /meshcore/ble/scan` returns: `[{"address": "AA:BB:CC:DD:EE:FF", "name": "MeshCore-Node1", "rssi": -72}]`

### Reconnect
Exponential backoff: 3 retries at 5s, 15s, 45s (cap 60s). On final failure, pushes `status` SSE event with `state: "error"`. User can manually retry via `POST /meshcore/connect`.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | /meshcore/status | Connection state + transport info |
| POST | /meshcore/connect | Connect with SerialConfig, TCPConfig, or BLEConfig |
| POST | /meshcore/disconnect | Disconnect and stop background thread |
| GET | /meshcore/ports | List available serial ports |
| GET | /meshcore/ble/scan | Scan for nearby Meshcore BLE devices |
| GET | /meshcore/stream | SSE stream (messages, nodes, telemetry, status) |
| GET | /meshcore/messages | Recent messages (last 500) |
| POST | /meshcore/send | Send text message |
| GET | /meshcore/nodes | All known nodes |
| GET | /meshcore/contacts | Contact list |
| POST | /meshcore/contacts | Add contact |
| DELETE | /meshcore/contacts/`<id>` | Remove contact |
| GET | /meshcore/telemetry/`<node_id>` | Telemetry history for node |
| POST | /meshcore/traceroute | Request traceroute to node |
| GET | /meshcore/repeaters | List repeater nodes |

## SSE Event Format

```json
{"type": "message",    "data": { ...MeshcoreMessage }}
{"type": "node",       "data": { ...MeshcoreNode }}
{"type": "telemetry",  "data": { ...MeshcoreTelemetry }}
{"type": "traceroute", "data": { ...MeshcoreTraceroute }}
{"type": "status",     "data": {"state": "connected", "transport": "serial", "device": "/dev/ttyUSB0"}}
```

Keepalive comment (`: keepalive`) sent every 30 seconds on idle.

## Frontend (meshcore.js)

IIFE pattern, same as all other Intercept JS modules. Key responsibilities:

- **SSE consumer** — `EventSource('/meshcore/stream')`, routes events by `type`
- **Message feed** — append to scrolling list, optimistic pending state on send
- **Sidebar** — contact list + node list; repeaters shown separately with triangle icon (vs circle for client nodes), matching Meshcore UI conventions
- **Tabs** — Map (Leaflet, reuse existing map setup pattern), Telemetry (Chart.js, reuse existing chart helpers), Repeaters (dedicated table view)
- **Connection panel** — transport selector (Serial / TCP / BLE), port/IP/address input, connect/disconnect button
- **Traceroute modal** — hop diagram with SNR annotations, same visual style as Meshtastic traceroute

## Repeater Management

Meshcore repeaters are a first-class concept (unlike Meshtastic where all nodes relay). Design:

- Repeaters identified by `is_repeater: true` on `MeshcoreNode`
- Rendered on map as orange triangles (client nodes = blue circles)
- Dedicated "Repeaters" tab in the main panel showing: name, location, uptime, last seen, hop count
- Repeater stats surfaced in telemetry if available (uptime_secs from `MeshcoreTelemetry`)

## Error Handling

- meshcore library not installed → mode loads but shows "meshcore package required: `pip install meshcore`"
- BLE in Docker → clear error: "BLE unavailable in Docker. Run meshcore-proxy on the host and connect via TCP."
- Serial port not found → return available ports list in error response
- Connection lost mid-session → automatic reconnect with backoff; SSE `status` event updates UI indicator
- Send failure → SSE event clears pending state, shows error in message feed

## Testing

**`tests/test_meshcore_client.py`**
- Connection state machine transitions
- Reconnect backoff timing (mock asyncio loop)
- Message parsing and queue feeding
- Node/contact TTL expiry
- BLE unavailability error (Docker scenario)

**`tests/test_meshcore_routes.py`**
- All REST endpoints: correct JSON shape, status codes
- `/meshcore/connect` with each connection config type
- `/meshcore/send` with missing/invalid params → 400
- SSE stream yields keepalive on empty queue
- Input validation via `utils/validation.py`

**`tests/test_meshcore_integration.py`**
- Mock meshcore library at boundary (same approach as mocking meshtastic SDK)
- Full round-trip: connect → receive message event → appears in SSE stream
- Traceroute request → hop structure correctly parsed

## Dependencies

```
meshcore>=1.0.0   # optional — graceful degradation if absent
```

No new frontend dependencies — Leaflet and Chart.js already present.

## Reference

- Meshcore Python library: https://github.com/meshcore-dev/meshcore_py
- Companion protocol: https://docs.meshcore.io/companion_protocol/
- meshcore-proxy (BLE/serial → TCP bridge): https://github.com/rgregg/meshcore-proxy
- Existing Meshtastic implementation (reference): `utils/meshtastic.py`, `routes/meshtastic.py`
