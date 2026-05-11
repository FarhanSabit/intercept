"""Route tests for Meshcore blueprint."""

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from routes.meshcore import meshcore_bp


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

    def test_tcp_connect(self, client, mock_meshcore_client):
        r = client.post("/meshcore/connect", json={"transport": "tcp", "host": "192.168.1.10", "port": 5000})
        assert r.status_code == 200
        mock_meshcore_client.connect.assert_called_once()

    def test_ble_connect(self, client, mock_meshcore_client):
        r = client.post("/meshcore/connect", json={"transport": "ble", "address": "AA:BB:CC:DD:EE:FF"})
        assert r.status_code == 200

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
