"""Tests for MeshcoreClient dataclasses and state machine."""

from datetime import datetime, timezone
from unittest.mock import patch


class TestAvailability:
    def test_returns_bool(self):
        from utils.meshcore import is_meshcore_available

        assert isinstance(is_meshcore_available(), bool)

    def test_false_when_not_installed(self):
        with patch.dict("sys.modules", {"meshcore": None}):
            import importlib

            import utils.meshcore as m

            importlib.reload(m)
            assert m.is_meshcore_available() is False


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

        assert ConnectionState.DISCONNECTED
        assert ConnectionState.CONNECTING
        assert ConnectionState.CONNECTED
        assert ConnectionState.ERROR
