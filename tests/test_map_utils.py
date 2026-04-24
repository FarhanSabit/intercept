# tests/test_map_utils.py


def test_map_utils_js_is_served(client):
    """map-utils.js is accessible as a static file."""
    resp = client.get("/static/js/map-utils.js")
    assert resp.status_code == 200
    data = resp.data.decode()
    assert "MapUtils" in data
    assert "MapUtils.init" in data
    assert "addTacticalOverlays" in data


def test_map_utils_css_is_served(client):
    """map-utils.css is accessible as a static file."""
    resp = client.get("/static/css/core/map-utils.css")
    assert resp.status_code == 200
    data = resp.data.decode()
    assert "map-hud-panel" in data


def test_adsb_dashboard_includes_map_utils(client):
    """ADS-B dashboard loads map-utils.js and map-utils.css."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    resp = client.get("/adsb/dashboard")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "map-utils.js" in html
    assert "map-utils.css" in html
    assert "MapUtils.init" in html


def test_ais_dashboard_includes_map_utils(client):
    """AIS dashboard loads map-utils.js."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    resp = client.get("/ais/dashboard")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "map-utils.js" in html
    assert "MapUtils.init" in html


def test_satellite_dashboard_includes_map_utils(client):
    """Satellite dashboard loads map-utils.js."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    resp = client.get("/satellite/dashboard")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "map-utils.js" in html
    assert "MapUtils.init" in html


def test_index_includes_map_utils(client):
    """Main SPA index.html loads map-utils.js and uses it for APRS and GPS maps."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "map-utils.js" in html
    assert "MapUtils.init" in html


def test_radiosonde_mode_uses_map_utils(client):
    """Main SPA index (which includes radiosonde partial) uses MapUtils for radiosonde map."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    # Radiosonde map init in partial should call MapUtils.init, not bare L.tileLayer
    assert "radiosondeMap" in html
    # The bare cartocdn URL that was previously hardcoded should be gone
    assert "basemaps.cartocdn.com/dark_all" not in html or html.count("basemaps.cartocdn.com/dark_all") == 0
