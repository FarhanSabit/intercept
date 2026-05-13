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
    let _markers = {};
    let _telemetryChart = null;
    let _connected = false;
    let _nodeCount = 0;
    let _msgCount = 0;

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
        _nodeCount = 0;
        _msgCount = 0;
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
        if (disconnectBtn) disconnectBtn.disabled = state !== 'connecting' && !_connected;

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
            _updateStatusUI('connecting');
            await fetch('/meshcore/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            _pollUntilConnected(0);
        } catch (e) {
            _updateStatusUI('error', 'Connection failed');
            console.error('Connect failed:', e);
        }
    }

    function _pollUntilConnected(attempts) {
        if (_connected) return;
        if (attempts > 15) {
            _updateStatusUI('error', 'Connection timed out');
            return;
        }
        setTimeout(async () => {
            await _checkStatus();
            if (!_connected) _pollUntilConnected(attempts + 1);
        }, 2000);
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
        const btn = document.querySelector('[onclick="MeshCore.scanBle()"]');
        const sel = document.getElementById('meshcoreBleSelect');
        if (btn) { btn.textContent = 'Scanning…'; btn.disabled = true; }
        if (sel) sel.innerHTML = '<option value="">Scanning…</option>';
        try {
            const r = await fetch('/meshcore/ble/scan');
            const d = await r.json();
            if (!sel) return;
            const devices = d.devices || [];
            if (!devices.length) {
                sel.innerHTML = '<option value="">No devices found</option>';
                return;
            }
            sel.innerHTML = '<option value="">Select device…</option>';
            devices.forEach(dev => {
                const o = document.createElement('option');
                o.value = dev.address;
                o.textContent = `${dev.name || 'Unknown'} (${dev.address})${dev.rssi ? ' · ' + dev.rssi + ' dBm' : ''}`;
                sel.appendChild(o);
            });
            if (devices.length === 1) sel.value = devices[0].address;
        } catch (e) {
            if (sel) sel.innerHTML = '<option value="">Scan failed — retry</option>';
            console.error('BLE scan failed:', e);
        } finally {
            if (btn) { btn.textContent = 'Scan'; btn.disabled = false; }
        }
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
        _msgCount++;
        const mc = document.getElementById('meshcoreMsgCount');
        if (mc) mc.textContent = _msgCount;
    }

    async function sendMessage() {
        const input = document.getElementById('meshcoreComposeInput');
        const recipientSel = document.getElementById('meshcoreRecipientSelect');
        const text = input ? input.value.trim() : '';
        if (!text) return;
        const recipient_id = recipientSel ? recipientSel.value : 'BROADCAST';

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
            const empty = list.querySelector('.meshcore-empty');
            if (empty) empty.remove();
            list.appendChild(el);
            _nodeCount++;
            const nc = document.getElementById('meshcoreNodeCount');
            if (nc) nc.textContent = _nodeCount;
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

        const fallback = L.tileLayer('https://cartodb-basemaps-{s}.global.ssl.fastly.net/dark_all/{z}/{x}/{y}.png', {
            attribution: '© CartoDB',
            maxZoom: 19,
        }).addTo(_map);

        if (typeof Settings !== 'undefined') {
            Promise.race([
                Settings.init(),
                new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 5000)),
            ]).then(() => {
                fallback.remove();
                Settings.createTileLayer().addTo(_map);
                Settings.registerMap(_map);
            }).catch(e => console.warn('MeshCore: Settings init failed, using fallback tiles:', e));
        }
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
        _openModal(modal);
    }

    function closeTraceroute() {
        _closeModal(document.getElementById('meshcoreTracerouteModal'));
    }

    // ── Contacts ───────────────────────────────────────────────────────────
    function showAddContact() {
        _openModal(document.getElementById('meshcoreAddContactModal'));
    }

    function closeAddContact() {
        _closeModal(document.getElementById('meshcoreAddContactModal'));
    }

    function _openModal(modal) {
        if (!modal) return;
        modal.style.display = '';
        requestAnimationFrame(() => modal.classList.add('show'));
    }

    function _closeModal(modal) {
        if (!modal) return;
        modal.classList.remove('show');
        setTimeout(() => { modal.style.display = 'none'; }, 200);
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
