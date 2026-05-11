"""Async worker that runs the meshcore library inside a daemon thread.

Only this file touches the meshcore library directly. All other Intercept
code goes through MeshcoreClient in utils/meshcore.py.
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
        ConnectionConfig,
        MeshcoreClient,
    )

logger = get_logger("intercept.meshcore.worker")

_RETRY_DELAYS = [5, 15, 45]


class AsyncWorker:
    """Owns a daemon asyncio event loop; bridges meshcore events to MeshcoreClient."""

    def __init__(self, config: ConnectionConfig, client: MeshcoreClient) -> None:
        self._config = config
        self._client = client
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mc = None  # MeshCore instance
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._asyncio_stop: asyncio.Event | None = None  # set in asyncio thread

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
        if self._asyncio_stop and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._asyncio_stop.set)
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_with_retry())
        except Exception as exc:
            logger.exception("Meshcore asyncio thread crashed: %s", exc)
        finally:
            self._loop.close()

    async def _wait_or_stop(self, seconds: float) -> bool:
        """Wait for seconds; return True if stop was signalled early."""
        stop_task = asyncio.ensure_future(self._asyncio_stop.wait())
        sleep_task = asyncio.ensure_future(asyncio.sleep(seconds))
        done, pending = await asyncio.wait(
            [stop_task, sleep_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        return stop_task in done

    async def _connect_with_retry(self) -> None:
        self._asyncio_stop = asyncio.Event()
        for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
            if self._stop_event.is_set():
                return
            try:
                await self._do_connect()
                return
            except Exception as exc:
                logger.warning("Meshcore connect attempt %d failed: %s", attempt + 1, exc)
                if delay is None:
                    self._client.on_error(f"Connection failed after retries: {exc}")
                    return
                # Wait for delay or early stop
                if await self._wait_or_stop(delay):
                    return  # stop signalled

    async def _do_connect(self) -> None:
        from meshcore import EventType, MeshCore

        from utils.meshcore import BLEConfig, SerialConfig, TCPConfig

        cfg = self._config

        if isinstance(cfg, SerialConfig):
            port = cfg.port or "/dev/ttyUSB0"
            self._mc = await MeshCore.create_serial(port=port, baudrate=cfg.baud, debug=False)
            transport, device = "serial", port
        elif isinstance(cfg, TCPConfig):
            self._mc = await MeshCore.create_tcp(host=cfg.host, port=cfg.port, debug=False)
            transport, device = "tcp", f"{cfg.host}:{cfg.port}"
        elif isinstance(cfg, BLEConfig):
            self._mc = await MeshCore.create_ble(address=cfg.device_address, debug=False)
            transport, device = "ble", cfg.device_address or "auto"
        else:
            raise RuntimeError(f"Unknown connection config type: {type(cfg)}")

        if self._mc is None:
            raise RuntimeError("Failed to create MeshCore connection")

        # Subscribe to all relevant events
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_private_msg)
        self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        self._mc.subscribe(EventType.ADVERTISEMENT, self._on_advertisement)
        self._mc.subscribe(EventType.STATS_CORE, self._on_stats_core)
        self._mc.subscribe(EventType.TRACE_DATA, self._on_trace_data)
        self._mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)

        self._client.on_connected(transport=transport, device=device)

        # Fetch initial contacts
        try:
            await self._mc.commands.get_contacts(lastmod=0, timeout=5)
            for pubkey, contact in self._mc.contacts.items():
                self._client.on_node(self._contact_to_node(contact))
        except Exception as exc:
            logger.warning("Failed to fetch initial contacts: %s", exc)

        # Keep the loop alive until stop is signalled.
        # Poll _stop_event every second so stop() is honoured even if
        # _asyncio_stop.set() was missed due to a startup race.
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._asyncio_stop.wait(), timeout=1.0)
                break
            except asyncio.TimeoutError:
                continue

        if self._mc and self._mc.is_connected:
            await self._mc.disconnect()

    # -- Event callbacks (called from asyncio event dispatcher) --

    def _on_private_msg(self, event) -> None:
        from utils.meshcore import MeshcoreMessage

        p = event.payload
        msg = MeshcoreMessage(
            id=str(uuid.uuid4()),
            sender_id=str(p.get("pubkey_prefix", "unknown")),
            recipient_id="DIRECT",
            text=str(p.get("text", "")),
            timestamp=datetime.now(timezone.utc),
            hop_count=int(p.get("path_len", 0) or 0),
            snr=None,
            is_direct=True,
        )
        self._client.on_message(msg)

    def _on_channel_msg(self, event) -> None:
        from utils.meshcore import MeshcoreMessage

        p = event.payload
        msg = MeshcoreMessage(
            id=str(uuid.uuid4()),
            sender_id=str(p.get("pubkey_prefix") or p.get("sender_id", "unknown")),
            recipient_id=f"CHAN{p.get('channel_idx', 0)}",
            text=str(p.get("text", "")),
            timestamp=datetime.now(timezone.utc),
            hop_count=int(p.get("path_len", 0) or 0),
            snr=None,
            is_direct=False,
        )
        self._client.on_message(msg)

    def _on_advertisement(self, event) -> None:
        contact = event.payload
        if not contact:
            return
        self._client.on_node(self._contact_to_node(contact))

    def _on_stats_core(self, event) -> None:
        from utils.meshcore import MeshcoreTelemetry

        p = event.payload
        node_id = "self"  # stats_core is always for the local node
        battery_mv = p.get("battery_mv")
        battery_pct = min(int(battery_mv / 42), 100) if battery_mv else None  # rough: 4200mv = 100%
        t = MeshcoreTelemetry(
            node_id=node_id,
            timestamp=datetime.now(timezone.utc),
            battery_pct=battery_pct,
            voltage=battery_mv / 1000.0 if battery_mv else None,
            temperature=None,
            humidity=None,
            uptime_secs=int(p.get("uptime_secs", 0) or 0),
        )
        self._client.on_telemetry(t)

    def _on_trace_data(self, event) -> None:
        from utils.meshcore import MeshcoreTraceroute

        p = event.payload or {}
        # TRACE_DATA payload structure varies; extract what we can
        hops = p.get("hops") or p.get("path") or []
        if isinstance(hops, str):
            hops = hops.split(",") if hops else []
        snr_per_hop = p.get("snr_per_hop") or []
        tr = MeshcoreTraceroute(
            origin_id=str(p.get("origin_id", "self")),
            destination_id=str(p.get("destination_id", "unknown")),
            hops=[str(h) for h in hops],
            snr_per_hop=[float(s) for s in snr_per_hop if s is not None],
            timestamp=datetime.now(timezone.utc),
        )
        self._client.on_traceroute(tr)

    def _on_disconnected(self, event) -> None:
        if not self._stop_event.is_set():
            self._client.on_error("Device disconnected unexpectedly")
            if self._asyncio_stop:
                self._asyncio_stop.set()

    # -- Helpers --

    def _contact_to_node(self, contact: dict):
        from utils.meshcore import MeshcoreNode, _is_repeater_contact

        lat = contact.get("adv_lat")
        lon = contact.get("adv_lon")
        return MeshcoreNode(
            node_id=str(contact.get("public_key", "")),
            name=str(contact.get("adv_name", "Unknown")),
            is_repeater=_is_repeater_contact(contact),
            lat=float(lat) if lat else None,
            lon=float(lon) if lon else None,
            battery_pct=None,
            last_seen=datetime.now(timezone.utc),
            snr=None,
            hops_away=int(contact.get("out_path_len", -1)),
        )

    # -- Commands (called from Flask thread) --

    def _submit(self, coro) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            coro.close()
            logger.debug("Command dropped: worker not running")

    def send_text(self, recipient_id: str, text: str) -> None:
        async def _send():
            if self._mc:
                await self._mc.commands.send_msg(recipient_id, text)

        self._submit(_send())

    def request_traceroute(self, node_id: str) -> None:
        async def _trace():
            if self._mc:
                logger.debug("Requesting traceroute (target hint: %s)", node_id)
                await self._mc.commands.send_trace(auth_code=0)

        self._submit(_trace())

    def scan_ble_sync(self) -> list[dict]:
        if not self._loop or not self._loop.is_running():
            # Start a one-shot loop for the scan
            return asyncio.run(_scan_ble())
        future = asyncio.run_coroutine_threadsafe(_scan_ble(), self._loop)
        try:
            return future.result(timeout=10)
        except Exception as exc:
            logger.warning("BLE scan failed: %s", exc)
            return []


async def _scan_ble() -> list[dict]:
    """Scan for MeshCore BLE devices using bleak directly."""
    try:
        from bleak import BleakScanner

        devices = await BleakScanner.discover(timeout=5.0)
        return [
            {
                "address": d.address,
                "name": d.name or "Unknown",
                "rssi": getattr(d, "rssi", None),
            }
            for d in devices
            if d.name and d.name.startswith("MeshCore")
        ]
    except ImportError:
        logger.warning("bleak not installed; BLE scan unavailable. Run: pip install bleak")
        return []
    except Exception as exc:
        logger.warning("BLE scan failed: %s", exc)
        return []
