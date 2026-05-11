"""Tests for MeshcoreClient dataclasses and state machine."""

from datetime import datetime, timezone
from unittest.mock import patch


class TestAvailability:
    def test_returns_bool(self):
        from utils.meshcore import is_meshcore_available

        assert isinstance(is_meshcore_available(), bool)

    def test_false_when_not_installed(self):
        with patch("utils.meshcore.HAS_MESHCORE", False):
            from utils.meshcore import is_meshcore_available

            assert is_meshcore_available() is False


class TestMeshcoreMessage:
    def _make(self, **kw):
        from utils.meshcore import MeshcoreMessage

        defaults = {
            "id": "abc123",
            "sender_id": "NODE001",
            "recipient_id": "BROADCAST",
            "text": "hello mesh",
            "timestamp": datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc),
            "hop_count": 2,
            "snr": -8.5,
            "is_direct": False,
        }
        defaults.update(kw)
        return MeshcoreMessage(**defaults)

    def test_to_dict_keys(self):
        d = self._make().to_dict()
        for key in ("id", "sender_id", "recipient_id", "text", "timestamp", "hop_count", "snr", "is_direct", "pending"):
            assert key in d, f"missing key: {key}"

    def test_pending_defaults_false(self):
        assert self._make().to_dict()["pending"] is False

    def test_none_snr_allowed(self):
        d = self._make(snr=None).to_dict()
        assert d["snr"] is None

    def test_to_dict_timestamp_is_iso(self):
        msg = self._make(timestamp=datetime(2026, 5, 11, 10, 30, 0, tzinfo=timezone.utc))
        d = msg.to_dict()
        assert "2026-05-11" in d["timestamp"]
        assert isinstance(d["timestamp"], str)


class TestMeshcoreNode:
    def test_to_dict_includes_is_repeater(self):
        from utils.meshcore import MeshcoreNode

        node = MeshcoreNode(
            node_id="RPT1",
            name="Roof-Repeater",
            is_repeater=True,
            lat=51.5,
            lon=-0.1,
            battery_pct=87,
            last_seen=datetime.now(timezone.utc),
            snr=-5.0,
            hops_away=1,
        )
        d = node.to_dict()
        assert d["is_repeater"] is True
        assert d["node_id"] == "RPT1"


class TestMeshcoreTelemetry:
    def test_to_dict_timestamp_is_iso(self):
        from utils.meshcore import MeshcoreTelemetry

        t = MeshcoreTelemetry(
            node_id="N1",
            timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
            battery_pct=72,
            voltage=3.7,
            temperature=22.1,
            humidity=55.0,
            uptime_secs=3600,
        )
        d = t.to_dict()
        assert "2026-05-10" in d["timestamp"]


class TestConnectionState:
    def test_state_enum_values(self):
        from utils.meshcore import ConnectionState

        assert ConnectionState.DISCONNECTED.value == "disconnected"
        assert ConnectionState.CONNECTING.value == "connecting"
        assert ConnectionState.CONNECTED.value == "connected"
        assert ConnectionState.ERROR.value == "error"


class TestMeshcoreContact:
    def test_to_dict_keys(self):
        from utils.meshcore import MeshcoreContact

        c = MeshcoreContact(
            node_id="ab" * 32,
            name="Alice",
            public_key="ab" * 32,
            last_msg=None,
        )
        d = c.to_dict()
        assert d["node_id"] == "ab" * 32
        assert d["name"] == "Alice"
        assert d["last_msg"] is None


class TestMeshcoreClientStateMachine:
    def test_status_event_pushed_on_connect_state_change(self):
        from utils.meshcore import ConnectionState, MeshcoreClient

        client = MeshcoreClient()
        # Drain any queued events from __init__ (none expected, but be safe)
        while not client.get_queue().empty():
            client.get_queue().get_nowait()
        # Call on_connected directly (simulating what AsyncWorker would call)
        client.on_connected(transport="serial", device="/dev/ttyUSB0")
        assert client.get_state() == ConnectionState.CONNECTED
        event = client.get_queue().get_nowait()
        assert event["type"] == "status"
        assert event["data"]["state"] == "connected"

    def test_on_message_appends_and_pushes_to_queue(self):
        from utils.meshcore import MeshcoreClient, MeshcoreMessage

        client = MeshcoreClient()
        msg = MeshcoreMessage(
            id="m1",
            sender_id="A",
            recipient_id="BROADCAST",
            text="hi",
            timestamp=datetime.now(timezone.utc),
            hop_count=0,
            snr=None,
            is_direct=False,
        )
        client.on_message(msg)
        assert len(client.get_messages()) == 1
        event = client.get_queue().get_nowait()
        assert event["type"] == "message"
        assert event["data"]["text"] == "hi"

    def test_on_message_caps_at_500(self):
        from utils.meshcore import MeshcoreClient, MeshcoreMessage

        client = MeshcoreClient()
        for i in range(510):
            client.on_message(
                MeshcoreMessage(
                    id=str(i),
                    sender_id="X",
                    recipient_id="BROADCAST",
                    text=f"msg{i}",
                    timestamp=datetime.now(timezone.utc),
                    hop_count=0,
                    snr=None,
                    is_direct=False,
                )
            )
        assert len(client.get_messages()) == 500
