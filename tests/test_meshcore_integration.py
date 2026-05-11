"""Integration tests: mock meshcore library at its boundary, test full flow."""

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_meshcore_lib():
    """Isolate tests from the optional meshcore library."""
    fake = MagicMock()
    with patch.dict(sys.modules, {"meshcore": fake, "meshcore.events": fake}):
        yield


class TestMessageRoundTrip:
    """Connect → receive event → appears in message store and SSE queue."""

    def test_message_stored_on_receipt(self):
        from utils.meshcore import MeshcoreClient, MeshcoreMessage

        client = MeshcoreClient()

        msg = MeshcoreMessage(
            id="t1",
            sender_id="A",
            recipient_id="BROADCAST",
            text="test",
            timestamp=datetime.now(timezone.utc),
            hop_count=1,
            snr=-10.0,
            is_direct=False,
        )
        client.on_message(msg)

        msgs = client.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "test"

    def test_message_pushed_to_queue(self):
        from utils.meshcore import MeshcoreClient, MeshcoreMessage

        client = MeshcoreClient()

        msg = MeshcoreMessage(
            id="t2",
            sender_id="B",
            recipient_id="BROADCAST",
            text="queued",
            timestamp=datetime.now(timezone.utc),
            hop_count=0,
            snr=None,
            is_direct=True,
        )
        client.on_message(msg)

        event = client.get_queue().get_nowait()
        assert event["type"] == "message"
        assert event["data"]["text"] == "queued"

    def test_message_history_capped_at_500(self):
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


class TestNodeAndRepeater:
    def test_repeater_appears_in_repeaters_only(self):
        from utils.meshcore import MeshcoreClient, MeshcoreNode

        client = MeshcoreClient()

        client.on_node(
            MeshcoreNode(
                node_id="R1",
                name="Roof",
                is_repeater=True,
                lat=51.5,
                lon=-0.1,
                battery_pct=100,
                last_seen=datetime.now(timezone.utc),
                snr=-3.0,
                hops_away=1,
            )
        )
        client.on_node(
            MeshcoreNode(
                node_id="C1",
                name="Client",
                is_repeater=False,
                lat=51.6,
                lon=-0.2,
                battery_pct=72,
                last_seen=datetime.now(timezone.utc),
                snr=-8.0,
                hops_away=2,
            )
        )

        assert len(client.get_repeaters()) == 1
        assert client.get_repeaters()[0]["node_id"] == "R1"
        assert len(client.get_nodes()) == 2


class TestTracerouteRoundTrip:
    def test_traceroute_pushed_to_queue(self):
        from utils.meshcore import MeshcoreClient, MeshcoreTraceroute

        client = MeshcoreClient()

        tr = MeshcoreTraceroute(
            origin_id="A",
            destination_id="B",
            hops=["A", "R1", "B"],
            snr_per_hop=[-5.0, -8.0],
            timestamp=datetime.now(timezone.utc),
        )
        client.on_traceroute(tr)

        q = client.get_queue()
        event = q.get_nowait()
        assert event["type"] == "traceroute"
        assert event["data"]["hops"] == ["A", "R1", "B"]


class TestBLEDockerBlock:
    def test_ble_blocked_in_docker(self, monkeypatch):
        monkeypatch.setenv("INTERCEPT_DOCKER", "1")
        from utils.meshcore import BLEConfig, MeshcoreClient

        client = MeshcoreClient()
        client.connect(BLEConfig())

        events = []
        while not client.get_queue().empty():
            events.append(client.get_queue().get_nowait())
        error_events = [e for e in events if e.get("data", {}).get("state") == "error"]
        assert error_events
        assert "Docker" in error_events[0]["data"]["message"]


class TestConnectionStateTransitions:
    def test_on_connected_pushes_status_event(self):
        from utils.meshcore import ConnectionState, MeshcoreClient

        client = MeshcoreClient()
        client.on_connected(transport="serial", device="/dev/ttyUSB0")

        assert client.get_state() == ConnectionState.CONNECTED
        event = client.get_queue().get_nowait()
        assert event["type"] == "status"
        assert event["data"]["state"] == "connected"

    def test_on_error_pushes_status_event(self):
        from utils.meshcore import ConnectionState, MeshcoreClient

        client = MeshcoreClient()
        client.on_error("timeout")

        assert client.get_state() == ConnectionState.ERROR
        event = client.get_queue().get_nowait()
        assert event["data"]["state"] == "error"
