"""Route tests for Meshcore blueprint."""

import queue as q_module
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from routes.meshcore import meshcore_bp
from utils.meshcore import BLEConfig, SerialConfig, TCPConfig


@pytest.fixture()
def app():
    application = Flask(__name__)
    application.register_blueprint(meshcore_bp)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def mock_meshcore_client():
    mc = MagicMock()
    mc.get_state.return_value = MagicMock(value="disconnected")
    mc.get_messages.return_value = []
    mc.get_nodes.return_value = []
    mc.get_repeaters.return_value = []
    mc.get_contacts.return_value = []
    mc.get_telemetry.return_value = []
    mc.scan_ble.return_value = []
    with (
        patch("routes.meshcore.get_meshcore_client", return_value=mc),
        patch("routes.meshcore.is_meshcore_available", return_value=True),
    ):
        yield mc


class TestStatus:
    def test_returns_json(self, client):
        r = client.get("/meshcore/status")
        assert r.status_code == 200
        d = r.get_json()
        assert "state" in d
        assert "available" in d

    def test_unavailable_when_not_installed(self, client):
        with patch("routes.meshcore.is_meshcore_available", return_value=False):
            r = client.get("/meshcore/status")
            d = r.get_json()
            assert d["available"] is False


class TestConnect:
    def test_serial_connect(self, client, mock_meshcore_client):
        r = client.post("/meshcore/connect", json={"transport": "serial", "port": "/dev/ttyUSB0"})
        assert r.status_code == 200
        assert r.get_json()["status"] == "connecting"
        mock_meshcore_client.connect.assert_called_once()
        call_arg = mock_meshcore_client.connect.call_args[0][0]
        assert isinstance(call_arg, SerialConfig)
        assert call_arg.port == "/dev/ttyUSB0"

    def test_tcp_connect(self, client, mock_meshcore_client):
        r = client.post("/meshcore/connect", json={"transport": "tcp", "host": "192.168.1.10", "port": 5000})
        assert r.status_code == 200
        mock_meshcore_client.connect.assert_called_once()
        call_arg = mock_meshcore_client.connect.call_args[0][0]
        assert isinstance(call_arg, TCPConfig)
        assert call_arg.host == "192.168.1.10"

    def test_ble_connect(self, client, mock_meshcore_client):
        r = client.post("/meshcore/connect", json={"transport": "ble", "address": "AA:BB:CC:DD:EE:FF"})
        assert r.status_code == 200
        mock_meshcore_client.connect.assert_called_once()
        call_arg = mock_meshcore_client.connect.call_args[0][0]
        assert isinstance(call_arg, BLEConfig)
        assert call_arg.device_address == "AA:BB:CC:DD:EE:FF"

    def test_unknown_transport_returns_400(self, client):
        r = client.post("/meshcore/connect", json={"transport": "zigbee"})
        assert r.status_code == 400


class TestSend:
    def test_sends_text(self, client, mock_meshcore_client):
        r = client.post("/meshcore/send", json={"text": "hello", "recipient_id": "NODE1"})
        assert r.status_code == 200
        mock_meshcore_client.send_text.assert_called_once_with("NODE1", "hello")

    def test_empty_text_returns_400(self, client):
        r = client.post("/meshcore/send", json={"text": ""})
        assert r.status_code == 400

    def test_missing_text_returns_400(self, client):
        r = client.post("/meshcore/send", json={})
        assert r.status_code == 400

    def test_text_at_limit_returns_200(self, client, mock_meshcore_client):
        r = client.post("/meshcore/send", json={"text": "x" * 237, "recipient_id": "NODE1"})
        assert r.status_code == 200

    def test_text_too_long_returns_400(self, client):
        r = client.post("/meshcore/send", json={"text": "x" * 238})
        assert r.status_code == 400


class TestContacts:
    def test_add_contact(self, client, mock_meshcore_client):
        r = client.post("/meshcore/contacts", json={"node_id": "N1", "name": "Alice", "public_key": "abc123"})
        assert r.status_code == 200
        mock_meshcore_client.add_contact.assert_called_once()

    def test_add_contact_missing_fields_returns_400(self, client):
        r = client.post("/meshcore/contacts", json={"node_id": "N1"})
        assert r.status_code == 400

    def test_delete_contact_not_found(self, client, mock_meshcore_client):
        mock_meshcore_client.remove_contact.return_value = False
        r = client.delete("/meshcore/contacts/UNKNOWN")
        assert r.status_code == 404

    def test_delete_contact_success(self, client, mock_meshcore_client):
        mock_meshcore_client.remove_contact.return_value = True
        r = client.delete("/meshcore/contacts/N1")
        assert r.status_code == 200


class TestPorts:
    def test_returns_list(self, client):
        with patch("routes.meshcore.list_serial_ports", return_value=["/dev/ttyUSB0"]):
            r = client.get("/meshcore/ports")
            assert r.status_code == 200
            assert "/dev/ttyUSB0" in r.get_json()["ports"]


class TestTraceroute:
    def test_requires_node_id(self, client):
        r = client.post("/meshcore/traceroute", json={})
        assert r.status_code == 400

    def test_queues_request(self, client, mock_meshcore_client):
        r = client.post("/meshcore/traceroute", json={"node_id": "NODE1"})
        assert r.status_code == 200
        mock_meshcore_client.request_traceroute.assert_called_once_with("NODE1")


class TestDisconnect:
    def test_disconnect_returns_200(self, client, mock_meshcore_client):
        r = client.post("/meshcore/disconnect")
        assert r.status_code == 200
        assert r.get_json()["status"] == "disconnected"
        mock_meshcore_client.disconnect.assert_called_once()


class TestBLEScan:
    def test_returns_device_list(self, client, mock_meshcore_client):
        mock_meshcore_client.scan_ble.return_value = [{"address": "AA:BB:CC:DD:EE:FF", "name": "MeshNode"}]
        r = client.get("/meshcore/ble/scan")
        assert r.status_code == 200
        d = r.get_json()
        assert "devices" in d
        assert len(d["devices"]) == 1

    def test_empty_scan_returns_empty_list(self, client, mock_meshcore_client):
        r = client.get("/meshcore/ble/scan")
        assert r.status_code == 200
        assert r.get_json()["devices"] == []

    def test_unavailable_returns_503(self, client):
        with patch("routes.meshcore.is_meshcore_available", return_value=False):
            r = client.get("/meshcore/ble/scan")
            assert r.status_code == 503


class TestStream:
    def test_keepalive_on_empty_queue(self, client, mock_meshcore_client):
        mock_q = MagicMock()
        mock_q.get.side_effect = q_module.Empty
        mock_meshcore_client.get_queue.return_value = mock_q

        with client.get("/meshcore/stream", buffered=False) as r:
            assert r.status_code == 200
            assert r.content_type.startswith("text/event-stream")
            chunk = next(r.response)
            assert b": keepalive" in chunk

    def test_stream_yields_event_data(self, client, mock_meshcore_client):
        event_payload = {"type": "message", "text": "hello"}
        mock_q = MagicMock()
        # First call yields an event; subsequent calls raise Empty to stop iteration
        mock_q.get.side_effect = [event_payload, q_module.Empty]
        mock_meshcore_client.get_queue.return_value = mock_q

        with client.get("/meshcore/stream", buffered=False) as r:
            assert r.status_code == 200
            chunk = next(r.response)
            assert b"data:" in chunk
            assert b"message" in chunk


class TestMessages:
    def test_returns_empty_list(self, client, mock_meshcore_client):
        r = client.get("/meshcore/messages")
        assert r.status_code == 200
        d = r.get_json()
        assert "messages" in d
        assert d["messages"] == []

    def test_returns_messages(self, client, mock_meshcore_client):
        mock_meshcore_client.get_messages.return_value = [{"text": "hi", "sender": "NODE1"}]
        r = client.get("/meshcore/messages")
        assert r.status_code == 200
        assert len(r.get_json()["messages"]) == 1


class TestNodes:
    def test_returns_empty_list(self, client, mock_meshcore_client):
        r = client.get("/meshcore/nodes")
        assert r.status_code == 200
        d = r.get_json()
        assert "nodes" in d
        assert d["nodes"] == []

    def test_returns_nodes(self, client, mock_meshcore_client):
        mock_meshcore_client.get_nodes.return_value = [{"node_id": "NODE1", "name": "Base"}]
        r = client.get("/meshcore/nodes")
        assert r.status_code == 200
        assert len(r.get_json()["nodes"]) == 1


class TestListContacts:
    def test_returns_empty_list(self, client, mock_meshcore_client):
        r = client.get("/meshcore/contacts")
        assert r.status_code == 200
        d = r.get_json()
        assert "contacts" in d
        assert d["contacts"] == []

    def test_returns_contacts(self, client, mock_meshcore_client):
        mock_meshcore_client.get_contacts.return_value = [{"node_id": "N1", "name": "Alice"}]
        r = client.get("/meshcore/contacts")
        assert r.status_code == 200
        assert len(r.get_json()["contacts"]) == 1


class TestTelemetry:
    def test_returns_telemetry_for_node(self, client, mock_meshcore_client):
        r = client.get("/meshcore/telemetry/NODE1")
        assert r.status_code == 200
        d = r.get_json()
        assert d["node_id"] == "NODE1"
        assert "telemetry" in d
        assert d["telemetry"] == []

    def test_returns_telemetry_data(self, client, mock_meshcore_client):
        mock_meshcore_client.get_telemetry.return_value = [{"battery": 85, "snr": -10.5}]
        r = client.get("/meshcore/telemetry/NODE2")
        assert r.status_code == 200
        d = r.get_json()
        assert d["node_id"] == "NODE2"
        assert len(d["telemetry"]) == 1
        mock_meshcore_client.get_telemetry.assert_called_once_with("NODE2")


class TestRepeaters:
    def test_returns_empty_list(self, client, mock_meshcore_client):
        r = client.get("/meshcore/repeaters")
        assert r.status_code == 200
        d = r.get_json()
        assert "repeaters" in d
        assert d["repeaters"] == []

    def test_returns_repeaters(self, client, mock_meshcore_client):
        mock_meshcore_client.get_repeaters.return_value = [{"node_id": "R1", "name": "Tower"}]
        r = client.get("/meshcore/repeaters")
        assert r.status_code == 200
        assert len(r.get_json()["repeaters"]) == 1
