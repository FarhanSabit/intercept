import pytest
import json
from unittest.mock import patch, MagicMock
from flask import Flask
from routes.satellite import satellite_bp


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(satellite_bp)
    app.config['TESTING'] = True
    return app

@pytest.fixture
def client(app):
    return app.test_client()

def test_predict_passes_invalid_coords(client):
    """Verify that invalid coordinates return a 400 error."""
    payload = {
        "latitude": 150.0,  # Invalid (>90)
        "longitude": -0.1278
    }
    response = client.post('/satellite/predict', json=payload)
    assert response.status_code == 400
    assert response.json['status'] == 'error'

def test_fetch_celestrak_invalid_category(client):
    """Verify that an unauthorized category is rejected."""
    response = client.get('/satellite/celestrak/category_fake')
    assert response.status_code == 400
    assert response.json['status'] == 'error'
    assert 'Invalid category' in response.json['message']

# Mocking Tests (External Calls and Skyfield)
@patch('urllib.request.urlopen')
def test_update_tle_success(mock_urlopen, client):
    """Simulate a successful response from CelesTrak."""
    mock_content = (
        "ISS (ZARYA)\n"
        "1 25544U 98067A   23321.52083333  .00016717  00000-0  30171-3 0  9992\n"
        "2 25544  51.6416  20.4567 0004561  45.3212  67.8912 15.49876543123456\n"
    ).encode('utf-8')
    
    mock_response = MagicMock()
    mock_response.read.return_value = mock_content
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response

    response = client.post('/satellite/update-tle')
    assert response.status_code == 200
    assert response.json['status'] == 'success'
    assert 'ISS' in response.json['updated']

@patch('skyfield.api.load')
def test_get_satellite_position_skyfield_error(mock_load, client):
    """Test behavior when Skyfield fails or data is missing."""
    # Force the timescale load to fail
    mock_load.side_effect = Exception("Skyfield error")
    
    payload = {
        "latitude": 51.5,
        "longitude": -0.1,
        "satellites": ["ISS"]
    }
    response = client.post('/satellite/position', json=payload)
    # Should return success but an empty positions list due to internal try-except
    assert response.status_code == 200
    assert response.json['positions'] == []

# Logic Integration Test (Simulating prediction)
def test_predict_passes_empty_cache(client):
    """Verify that if the satellite is not in cache, no passes are returned."""
    payload = {
        "latitude": 51.5,
        "longitude": -0.1,
        "satellites": ["SATELLITE_NON_EXISTENT"]
    }
    response = client.post('/satellite/predict', json=payload)
    assert response.status_code == 200
    assert len(response.json['passes']) == 0