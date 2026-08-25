/*
 * Dashboard JS — terhubung ke backend Django via REST + WebSocket.
 *
 * Pola event:
 *   • Page load    → fetch /api/state untuk inisial values
 *   • Setiap toggle → POST /api/control
 *   • Setiap update GPS dari server → push via WebSocket telemetry
 *   • Heartbeat tiap 3 detik → re-fetch /api/state untuk update status indicator
 */
'use strict';

const $ = (id) => document.getElementById(id);
const wpMarkers = {};
let rovMarker = null;
let rovTrail = null;
let trailPoints = [];
let map = null;
let lastGpsPos = null;

// ═══ Init ═══════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
    initMap();
    bindControls();
    fetchInitialState();
    connectWebSocket();
    // Heartbeat untuk update status indicator (capture/GPS health)
    setInterval(refreshStatus, 3000);
});

// ═══ Map ════════════════════════════════════════════════════════════
function initMap() {
    // Guard: kalau Leaflet gagal load (misal file corrupt), jangan crash
    if (typeof L === 'undefined') {
        console.error('Leaflet gagal dimuat — peta tidak tersedia');
        $('map').innerHTML =
            '<div style="color:#aac5e8;text-align:center;padding:40px;font-family:Courier New,monospace;">' +
            'Leaflet tidak tersedia<br>Peta dinonaktifkan' +
            '</div>';
        return;
    }

    map = L.map('map', { zoomControl: false }).setView([-7.7956, 110.3695], 16);

    // Tile online (OpenStreetMap) — kalau tile gagal load (offline), auto-fallback
    // ke grid statis lokal supaya peta tetap kelihatan (dengan trail + waypoint).
    let tileErrorCount = 0;
    const tileLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap',
        maxZoom: 19,
        // errorTileUrl kosong → tile error jadi transparan (bukan broken image)
        errorTileUrl: '',
    });

    tileLayer.on('tileerror', () => {
        tileErrorCount++;
        // Setelah beberapa tile gagal → asumsikan offline, remove tile layer,
        // biarkan background CSS #1c2e45 jadi kanvas (dengan trail + waypoint di atas)
        if (tileErrorCount >= 4 && map.hasLayer(tileLayer)) {
            console.warn('Tile online tidak bisa diakses — mode offline (grid statis)');
            map.removeLayer(tileLayer);
            addOfflineGridOverlay();
            setStatus('🛜 Offline — peta pakai grid statis lokal');
        }
    });

    tileLayer.addTo(map);

    rovTrail = L.polyline([], {
        color: '#44cc77',
        weight: 2,
        dashArray: '4, 4',
        opacity: 0.85,
    }).addTo(map);
}

function addOfflineGridOverlay() {
    // Grid overlay statis — supaya user bisa perkirakan skala tanpa peta dunia.
    // Menggambar GARIS GRID beneran (bukan cuma teks) via custom GridLayer.
    if (!map) return;

    // Custom GridLayer yang menggambar kotak grid + koordinat di tiap tile.
    const OfflineGrid = L.GridLayer.extend({
        createTile: function (coords) {
            const tile = document.createElement('canvas');
            const size = this.getTileSize();
            tile.width = size.x;
            tile.height = size.y;
            const ctx = tile.getContext('2d');

            // Background gelap (senada tema)
            ctx.fillStyle = '#1c2e45';
            ctx.fillRect(0, 0, size.x, size.y);

            // Garis grid
            ctx.strokeStyle = 'rgba(100, 160, 255, 0.18)';
            ctx.lineWidth = 1;
            const step = 32;
            for (let x = 0; x <= size.x; x += step) {
                ctx.beginPath();
                ctx.moveTo(x, 0);
                ctx.lineTo(x, size.y);
                ctx.stroke();
            }
            for (let y = 0; y <= size.y; y += step) {
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(size.x, y);
                ctx.stroke();
            }

            // Border tile + label tile coords (untuk orientasi kasar)
            ctx.strokeStyle = 'rgba(100, 160, 255, 0.35)';
            ctx.strokeRect(0, 0, size.x, size.y);
            ctx.fillStyle = 'rgba(120, 170, 230, 0.4)';
            ctx.font = '9px Courier New, monospace';
            ctx.fillText(`${coords.x},${coords.y}`, 4, 12);

            return tile;
        },
    });

    const grid = new OfflineGrid({ tileSize: 256, minZoom: 1, maxZoom: 22 });
    grid.addTo(map);

    // Label "OFFLINE MODE" di pojok (informatif)
    const offlineLabel = L.control({ position: 'topright' });
    offlineLabel.onAdd = function () {
        const div = L.DomUtil.create('div');
        div.innerHTML =
            '<div style="background:rgba(28,46,69,0.85);color:#7aaae6;' +
            'font-size:10px;padding:3px 8px;border-radius:3px;' +
            'font-family:Courier New,monospace;border:1px solid rgba(100,160,255,0.3);">' +
            'OFFLINE MODE — grid statis</div>';
        return div;
    };
    offlineLabel.addTo(map);
}

let hasAutoCentered = false;

function updateRovMarker(lat, lon, heading) {
    lastGpsPos = [lat, lon];
    if (!map) return;   // Leaflet tidak load — skip
    if (!rovMarker) {
        const rovIcon = L.divIcon({
            className: 'rov-marker',
            html: `
                <div style="
                    background:#00cfff;
                    width:14px; height:14px;
                    border-radius:50%;
                    border:2px solid white;
                    box-shadow:0 0 12px rgba(0,207,255,0.8);
                "></div>
            `,
            iconSize: [18, 18],
            iconAnchor: [9, 9],
        });
        rovMarker = L.marker([lat, lon], { icon: rovIcon }).addTo(map);
    } else {
        rovMarker.setLatLng([lat, lon]);
    }

    if (!hasAutoCentered) {
        map.setView([lat, lon], 17);
        hasAutoCentered = true;
    }

    trailPoints.push([lat, lon]);
    if (trailPoints.length > 200) trailPoints.shift();
    rovTrail.setLatLngs(trailPoints);
}

function addWaypointMarker(wp) {
    if (!map) return;
    const color = wp.is_detect ? '#ff6600' : '#44cc77';
    const icon = L.divIcon({
        className: 'wp-marker',
        html: `<div style="
            background:${color};
            width:10px; height:10px;
            border-radius:50%;
            border:2px solid white;
            box-shadow:0 0 4px rgba(0,0,0,0.5);
        "></div>`,
        iconSize: [14, 14],
        iconAnchor: [7, 7],
    });
    const marker = L.marker([wp.lat, wp.lon], { icon })
        .bindTooltip(wp.label, { permanent: false, direction: 'top' })
        .addTo(map);
    wpMarkers[wp.timestamp] = marker;
}

function clearWaypointMarkers() {
    if (!map) return;
    Object.values(wpMarkers).forEach(m => map.removeLayer(m));
    for (const k in wpMarkers) delete wpMarkers[k];
}

// ═══ WebSocket ═══════════════════════════════════════════════════════
function connectWebSocket() {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${scheme}://${window.location.host}/ws/telemetry/`;
    const ws = new WebSocket(url);

    ws.onopen = () => setStatus('WebSocket terhubung');
    ws.onclose = () => {
        setStatus('WebSocket terputus — reconnecting…');
        setTimeout(connectWebSocket, 2000);
    };
    ws.onerror = (e) => console.error('WebSocket error', e);
    ws.onmessage = (msg) => {
        try {
            const data = JSON.parse(msg.data);
            handleTelemetryEvent(data);
        } catch (e) {
            console.error('Bad WS message', e);
        }
    };
}

function handleTelemetryEvent(data) {
    if (data.event === 'gps') {
        const { lat, lon, heading } = data.payload;
        $('lat-val').textContent = lat.toFixed(6) + '°';
        $('lon-val').textContent = lon.toFixed(6) + '°';
        $('hdg-val').textContent = heading.toFixed(1) + '°';
        updateGpsStatus(true);
        updateRovMarker(lat, lon, heading);
    } else if (data.event === 'waypoint_added') {
        // Dedup: kalau timestamp sudah ada di table, skip
        // (bisa terjadi kalau initial state + broadcast tabrakan)
        const existing = document.querySelector(
            `#wp-tbody tr[data-timestamp="${data.payload.timestamp}"]`
        );
        if (!existing) {
            addWaypointRow(data.payload);
            addWaypointMarker(data.payload);
        }
    } else if (data.event === 'waypoints_cleared') {
        // Multi-client sync — clear di client A → clear di semua client
        clearWaypointTable();
        clearWaypointMarkers();
        trailPoints = [];
        if (rovTrail) rovTrail.setLatLngs([]);
        setStatus('🗑 Waypoint di-clear (sync dari client lain)');
    } else if (data.event === 'control_updated') {
        // Multi-client sync — toggle di client A → toggle di semua client
        const c = data.payload;
        applyControlToUI(c, /*silent=*/true);
    } else if (data.event === 'rov') {
        applyRovTelemetry(data.payload);
    } else if (data.event === 'rov_status') {
        updateRovStatus(data.payload.connected);
        if (data.payload.connected) {
            setStatus('✅ Telemetri ROV tersambung' +
                      (data.payload.model ? ` — ${data.payload.model}` : ''));
        } else {
            setStatus('⚠️ Telemetri ROV putus' +
                      (data.payload.error ? ` (${data.payload.error})` : ''));
            clearRovTelemetry();
        }
    } else if (data.event === 'rov_unlock') {
        // Unlock adalah state SERVER — kalau operator lain menguncinya,
        // tombol di layar ini harus ikut terkunci saat itu juga.
        applyUnlockToUI(data.payload.unlocked);
    } else if (data.event === 'source_changed') {
        const spec = data.payload.source;
        if (data.payload.error) {
            setStatus('❌ Sumber gagal dibuka: ' + data.payload.error);
        } else {
            setStatus(`✅ Sumber video: ${spec[0]}:${spec[1]}`);
        }
        loadCameraSources();
    } else if (data.event === 'rov_sim') {
        if (window.RovControls) window.RovControls.applySim(data.payload.sim);
    } else if (data.event === 'rov_sim_move' || data.event === 'rov_sim_command') {
        // Sengaja tidak menampilkan apa pun: vektor sudah terbaca di panel,
        // dan membanjiri baris status 10× per detik justru menutupi pesan
        // lain yang penting.
    } else if (data.event === 'rov_deadman') {
        const v = data.payload.vector || {};
        setStatus(`🛑 DEADMAN — gerak dinolkan server ` +
                  `(thro:${v.thro} lift:${v.lift} yaw:${v.yaw}, ` +
                  `diam ${data.payload.age_s}s)`);
    } else if (data.event === 'rov_estop') {
        if (data.payload.ok) {
            setStatus('🛑 STOP diterima ROV');
        } else {
            setStatus('❌ STOP GAGAL — ' +
                      (data.payload.error || 'perintah nol tidak terkonfirmasi'));
        }
    } else if (data.event === 'rov_prefs') {
        const el1 = $('ctrl-rov-heading');
        const el2 = $('ctrl-rov-auto-depth');
        if (el1) el1.checked = !!data.payload.use_heading;
        if (el2) el2.checked = !!data.payload.auto_depth;
    } else if (data.event === 'gps_status') {
        // Watchdog dari backend — connected / stale / serial_error
        if (data.payload.connected === false) {
            updateGpsStatus(false);
            if (data.payload.reason === 'stale') {
                setStatus(`⚠️ GPS stale (${data.payload.age_s?.toFixed(0)}s)`);
            } else if (data.payload.reason === 'serial_error') {
                setStatus(`❌ GPS serial error — reconnecting…`);
            }
        } else {
            const label = data.payload.port ? `${data.payload.port} @ ${data.payload.baud}bps` : undefined;
            updateGpsStatus(true, label);
            if (label) {
                setStatus(`✅ GPS Terhubung ke ${label}`);
            }
        }
    }
}

function applyControlToUI(c, silent) {
    // Update checkbox states TANPA memicu event listener (silent update)
    const setChecked = (id, val) => {
        const el = $(id);
        if (el && el.checked !== !!val) el.checked = !!val;
    };
    setChecked('ctrl-hop',         c.hop_enabled);
    setChecked('ctrl-clahe',       c.clahe_enabled);
    setChecked('ctrl-dehaze',      c.dehaze_enabled);
    setChecked('ctrl-wb',          c.wb_enabled);
    setChecked('ctrl-yolo',        c.yolo_enabled);
    setChecked('ctrl-auto-wp',     c.auto_waypoint_enabled);
    setChecked('ctrl-mark-detect', c.mark_on_detect_enabled);
    if (c.hop_depth !== undefined) {
        $('ctrl-hop-depth').value = c.hop_depth;
        $('hop-depth-label').textContent = c.hop_depth;
    }
}

// ═══ Telemetri ROV ═══════════════════════════════════════════════════
// Semua nilai datang sebagai STRING dari protokol ROV (dipotong pada ';'
// lalu dibelah pada ':'), jadi format di sini yang mengubahnya jadi angka.
// Field yang hilang dibiarkan apa adanya — lebih baik menampilkan nilai
// lama daripada mengedipkan "—" tiap kali satu paket tidak membawa field itu.

const TELE_FORMAT = {
    R:    v => fnum(v, 2) + '°',
    P:    v => fnum(v, 2) + '°',
    Y:    v => fnum(v, 2) + '°',
    D:    v => fnum(v, 2) + ' m',
    to:   v => fnum(v, 1) + ' °C',
    ti:   v => fnum(v, 1) + ' °C',
    batv: v => fnum(v, 2) + ' V',
    RT:   v => fmtRuntime(v),
};

function fnum(v, digits) {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n.toFixed(digits) : '—';
}

function fmtRuntime(v) {
    const s = parseInt(v, 10);
    if (!Number.isFinite(s)) return '—';
    const m = Math.floor(s / 60);
    return `${m}m ${String(s % 60).padStart(2, '0')}s`;
}

function applyRovTelemetry(p) {
    Object.entries(TELE_FORMAT).forEach(([key, fmt]) => {
        if (p[key] === undefined) return;
        const el = $('tele-' + key);
        if (el) el.textContent = fmt(p[key]);
    });

    // Kedalaman ROV mengisi field Kedalaman di frame koordinat. GPS tidak
    // pernah tahu ini — sinyalnya tidak menembus air.
    if (p.D !== undefined) {
        $('depth-val').textContent = fnum(p.D, 2) + ' m';
    }

    // Heading: tandai sumbernya. COG buoy dan yaw ROV bisa berbeda jauh,
    // dan operator perlu tahu yang mana yang sedang dibaca.
    if (p._heading !== undefined && p._heading !== null) {
        const tag = p._heading_source === 'rov' ? ' (ROV)' : ' (GPS)';
        $('hdg-val').textContent = fnum(p._heading, 1) + '°' + tag;
    }

    // ROV didinginkan air. Kalau dinyalakan di darat suhu internal naik terus.
    const tiEl = $('tele-ti');
    if (tiEl && p.ti !== undefined) {
        const ti = parseFloat(p.ti);
        tiEl.classList.toggle('warn', Number.isFinite(ti) && ti > 45);
    }

    // Status tombol mengikuti telemetri, bukan klik terakhir: kalau perintah
    // tidak sampai ke ROV, tombol tidak boleh terlihat menyala.
    syncToggle('btn-rov-light', p.L);
    syncToggle('btn-rov-holdd', p.HD);
    syncToggle('btn-rov-holdy', p.HH);
    // Tombol di panel kendali menampilkan status yang SAMA, dan sumbernya
    // sama-sama telemetri — bukan ingatan lokal tentang apa yang terakhir
    // ditekan. Kalau lampu dimatikan dari app vendor di HP lain, kedua
    // tempat itu harus ikut redup.
    if (window.RovControls) window.RovControls.syncToggles(p);

    updateRovStatus(true);
}

function syncToggle(id, raw) {
    if (raw === undefined) return;
    const el = $(id);
    if (el) el.classList.toggle('on', String(raw).trim() === '1');
}

function clearRovTelemetry() {
    Object.keys(TELE_FORMAT).forEach(k => {
        const el = $('tele-' + k);
        if (el) el.textContent = '—';
    });
    $('depth-val').textContent = '—';
    const f = $('tele-frame');
    if (f) f.classList.add('stale');
}

function updateRovStatus(connected) {
    const el = $('rov-conn-status');
    if (!el) return;
    el.textContent = connected ? '● Telemetri aktif' : '● Telemetri terputus';
    el.className = 'status-line ' + (connected ? 'active' : 'inactive');
    const f = $('tele-frame');
    if (f) f.classList.toggle('stale', !connected);
}

function applyUnlockToUI(unlocked) {
    const cb = $('ctrl-rov-unlock');
    if (cb && cb.checked !== !!unlocked) cb.checked = !!unlocked;
    const grp = $('rov-control-group');
    if (grp) grp.classList.toggle('disabled', !unlocked);
    // Panel kendali gerak ikut status unlock yang sama. Sengaja lewat
    // RovControls, bukan langsung ke DOM: lapisan masukan juga harus
    // menolkan sumbernya saat dikunci, dan itu bukan urusan berkas ini.
    if (window.RovControls) window.RovControls.setUnlocked(unlocked);
}

async function sendRovCommand(key, value) {
    try {
        const r = await fetch('/api/rov/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, value }),
        });
        const j = await r.json();
        if (!j.ok) setStatus('❌ ' + j.error);
        else       setStatus(`➡ ROV: ${key}:${value};`);
        return j.ok;
    } catch (e) {
        setStatus('❌ Gagal mengirim perintah ROV');
        return false;
    }
}

// ═══ Waypoint table ══════════════════════════════════════════════════
function addWaypointRow(wp) {
    const tbody = $('wp-tbody');
    const tr = document.createElement('tr');
    tr.dataset.timestamp = wp.timestamp;
    tr.innerHTML = `
        <td class="col-dot">
            <span class="${wp.is_detect ? 'dot-detect' : 'dot-normal'}">●</span>
        </td>
        <td>${escapeHTML(wp.label)}</td>
        <td>${wp.lat.toFixed(6)}°</td>
        <td>${wp.lon.toFixed(6)}°</td>
    `;
    tbody.appendChild(tr);
    tbody.parentElement.scrollTop = tbody.parentElement.scrollHeight;
}

function clearWaypointTable() { $('wp-tbody').innerHTML = ''; }

function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

// ═══ Status indicators ═══════════════════════════════════════════════
function updateCaptureStatus(streaming, sourceLabel) {
    const el = $('capture-status');
    if (streaming) {
        el.textContent = '● Streaming aktif';
        el.className = 'status-line active';
    } else {
        el.textContent = '● Tidak ada frame';
        el.className = 'status-line inactive';
    }
}

// ═══ Pilih sumber kamera ═════════════════════════════════════════════
let cameraSources = [];

const REFRESH_VALUE = '__refresh__';

let gpsPorts = [];

async function loadGpsPorts() {
    const sel = $('gps-port-select');
    if (!sel) return;
    
    sel.innerHTML = '<option value="">memuat…</option>';
    sel.disabled = true;

    try {
        const r = await fetch('/api/gps/ports');
        const j = await r.json();
        if (!j.ok) throw new Error(j.error || 'gagal');
        gpsPorts = j.ports || [];
    } catch (e) {
        sel.innerHTML = `<option value="">(gagal memuat port)</option>`;
        sel.disabled = false;
        return;
    }

    sel.innerHTML = '';
    
    // Opsi AUTO
    const optAuto = document.createElement('option');
    optAuto.value = 'AUTO';
    optAuto.textContent = 'AUTO (Deteksi Otomatis)';
    sel.appendChild(optAuto);
    
    // Opsi DUMMY
    const optDummy = document.createElement('option');
    optDummy.value = 'DUMMY';
    optDummy.textContent = 'DUMMY (Simulasi)';
    sel.appendChild(optDummy);

    gpsPorts.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.port;
        opt.textContent = `${p.port} - ${p.desc}`;
        sel.appendChild(opt);
    });

    sel.disabled = false;
}

async function changeGpsPort(port) {
    const sel = $('gps-port-select');
    sel.disabled = true;
    setStatus(`↻ Mengganti port GPS ke ${port}…`);
    try {
        const j = await postJSON('/api/gps/port', { port: port });
        if (!j || !j.ok) {
            setStatus('❌ ' + ((j && j.error) || 'gagal ganti port GPS'));
        } else {
            setStatus(`✅ Port GPS diubah ke ${port}`);
        }
    } finally {
        sel.disabled = false;
    }
}

async function loadCameraSources(refresh) {
    const sel = $('camera-select');
    if (refresh) {
        sel.innerHTML = '<option value="">mendeteksi ulang…</option>';
        sel.disabled = true;
    }

    // Timeout wajib. Enumerasi DirectShow bisa lambat atau menggantung
    // (virtual camera, device yang sedang dipakai), dan fetch tanpa batas
    // waktu akan meninggalkan dropdown di "memuat…" selamanya tanpa
    // memberi tahu operator apa yang salah.
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), 12000);
    try {
        const r = await fetch('/api/sources' + (refresh ? '?refresh=1' : ''),
                              { signal: ac.signal });
        const j = await r.json();
        if (!j.ok) throw new Error(j.error || 'gagal');
        cameraSources = j.sources || [];
    } catch (e) {
        const habis = e.name === 'AbortError';
        sel.innerHTML =
            `<option value="">${habis ? '(deteksi kamera timeout)'
                                      : '(gagal mendeteksi sumber)'}</option>` +
            `<option value="${REFRESH_VALUE}">↻ Coba deteksi ulang</option>`;
        sel.disabled = false;
        setStatus(habis ? '⚠️ Deteksi kamera timeout — coba deteksi ulang'
                        : '❌ Gagal mendeteksi sumber kamera');
        return;
    } finally {
        clearTimeout(timer);
    }

    sel.innerHTML = '';
    cameraSources.forEach((src, i) => {
        const opt = document.createElement('option');
        opt.value = src.selectable ? String(i) : '';
        opt.textContent = src.label;
        // Entri seperti "(gagal deteksi webcam: …)" tidak punya spec —
        // ditampilkan supaya operator tahu kenapa daftarnya pendek, tapi
        // tidak bisa dipilih.
        opt.disabled = !src.selectable;
        opt.selected = !!src.active;
        sel.appendChild(opt);
    });

    // Daftar kamera di-cache di server, jadi OBS Virtual Camera yang baru
    // dinyalakan SETELAH server hidup tidak akan muncul sampai di-refresh.
    const opt = document.createElement('option');
    opt.value = REFRESH_VALUE;
    opt.textContent = '↻ Deteksi ulang kamera';
    sel.appendChild(opt);

    sel.disabled = false;
}

async function changeCameraSource(idx) {
    const src = cameraSources[idx];
    if (!src || !src.spec) return;

    const sel = $('camera-select');
    sel.disabled = true;
    setStatus(`↻ Mengganti sumber ke ${src.label}…`);
    try {
        const j = await postJSON('/api/source', { spec: src.spec });
        if (!j || !j.ok) {
            setStatus('❌ ' + ((j && j.error) || 'gagal ganti sumber'));
            await loadCameraSources();   // kembalikan pilihan ke yang aktif
        }
    } finally {
        sel.disabled = false;
    }
}

// ═══ Statistik performa ══════════════════════════════════════════════
function updatePerfLine(cap) {
    const el = $('perf-line');
    if (!el) return;
    if (!cap) { el.textContent = '—'; return; }

    // Device ditampilkan menonjol: inilah jawaban "pakai CUDA atau tidak"
    // yang sebelumnya cuma bisa ditebak lewat nvidia-smi.
    const onGpu = String(cap.device || '').startsWith('cuda');
    const devCls = onGpu ? 'perf-gpu' : 'perf-cpu';
    const parts = [`<span class="${devCls}">${escapeHTML(cap.device || '?')}</span>`];

    if (cap.process_fps) {
        parts.push(`${cap.process_fps} fps` +
                   (cap.fps_cap ? ` / cap ${cap.fps_cap}` : ''));
    }
    if (cap.infer_ms)   parts.push(`YOLO ${cap.infer_ms} ms`);
    if (cap.enhance_ms) parts.push(`proc ${cap.enhance_ms} ms`);
    if (cap.encode_ms)  parts.push(`jpeg ${cap.encode_ms} ms`);

    el.innerHTML = parts.join(' · ');

    if (cap.model_error) {
        el.innerHTML += `<br><span class="perf-cpu">model: ${escapeHTML(cap.model_error)}</span>`;
    }
    if (cap.source_error) {
        el.innerHTML += `<br><span class="perf-cpu">sumber: ${escapeHTML(cap.source_error)}</span>`;
    }
}

function updateGpsStatus(connected, sourceLabel, text) {
    const el = $('gps-conn-status');
    const defaultText = connected ? 'GPS aktif' : 'Menunggu fix';
    el.textContent = '● ' + (text || defaultText);
    if (connected) {
        el.className = 'status-line active';
    } else {
        el.className = 'status-line inactive';
    }
    if (sourceLabel !== undefined) {
        const sel = $('gps-port-select');
        if (sel) {
            // Coba set value dari select box jika ada opsi yang cocok
            let optionExists = false;
            for (let i = 0; i < sel.options.length; i++) {
                if (sel.options[i].value === sourceLabel) {
                    optionExists = true;
                    break;
                }
            }
            if (optionExists) {
                sel.value = sourceLabel;
            }
        }
    }
}

async function refreshStatus() {
    try {
        const r = await fetch('/api/state');
        const s = await r.json();
        // Capture sehat kalau frame age finite & < 5 detik.
        // frame_age_s bisa null (belum ada frame) → belum streaming.
        const streaming = s.frame_age_s !== null
            && Number.isFinite(s.frame_age_s)
            && s.frame_age_s < 5;
        updateCaptureStatus(streaming);
        updatePerfLine(s.capture);
        // GPS sehat kalau update terakhir < 10 detik
        const hasRecentFix = (Date.now()/1000 - s.gps.last_update) < 10;
        
        let gpsStateText;
        if (s.gps.connected) {
            gpsStateText = hasRecentFix ? 'GPS aktif' : 'Menunggu Fix Satelit';
            updateGpsStatus(hasRecentFix, undefined, gpsStateText);
        } else {
            updateGpsStatus(false, undefined, 'Terputus');
        }
        // ROV: heartbeat menangkap kasus telemetri berhenti tanpa event
        // rov_status (misal worker mati), yang tidak akan terdeteksi kalau
        // hanya mengandalkan broadcast.
        if (s.rov) {
            updateRovStatus(!!s.rov.fresh);
            applyUnlockToUI(s.rov.unlocked);
            // Sama alasannya dengan di fetchInitialState: heartbeat menangkap
            // kasus perubahan mode yang broadcast-nya terlewat (WebSocket
            // sempat putus). applySim() idempoten, jadi aman dipanggil rutin.
            if (window.RovControls) window.RovControls.applySim(!!s.rov.sim);
        }
    } catch (e) {
        // ignore — heartbeat boleh fail diam-diam
    }
}

// ═══ Controls binding ════════════════════════════════════════════════
function bindControls() {
    const ctrlMap = {
        'ctrl-hop':         'hop_enabled',
        'ctrl-clahe':       'clahe_enabled',
        'ctrl-dehaze':      'dehaze_enabled',
        'ctrl-wb':          'wb_enabled',
        'ctrl-yolo':        'yolo_enabled',
        'ctrl-auto-wp':     'auto_waypoint_enabled',
        'ctrl-mark-detect': 'mark_on_detect_enabled',
    };
    Object.entries(ctrlMap).forEach(([elId, field]) => {
        $(elId).addEventListener('change', (e) => {
            postControl({ [field]: e.target.checked });
        });
    });

    $('ctrl-hop-depth').addEventListener('input', (e) => {
        const v = parseInt(e.target.value, 10);
        $('hop-depth-label').textContent = v;
        postControl({ hop_depth: v });
    });

    // Show / hide map (client-side toggle, tidak perlu ke server)
    $('ctrl-show-map').addEventListener('change', (e) => {
        $('gps-panel').classList.toggle('map-hidden', !e.target.checked);
        // Leaflet butuh invalidateSize setelah container size berubah
        if (map) setTimeout(() => map.invalidateSize(), 50);
    });

    // ── Aksi screenshot
    $('btn-screenshot').addEventListener('click', () => {
        // Direct download via location — backend kasih Content-Disposition
        window.location.href = '/api/screenshot';
        setStatus('📷 Screenshot di-download');
    });

    // ── Aksi waypoint
    $('btn-mark').addEventListener('click', async () => {
        const r = await fetch('/api/waypoint', { method: 'POST' });
        const j = await r.json();
        if (!j.ok) setStatus('❌ ' + j.error);
        else      setStatus('📍 Waypoint manual ditambahkan');
    });

    $('btn-export').addEventListener('click', () => {
        window.location.href = '/api/export';
        setStatus('💾 Mengekspor GPX…');
    });

    $('btn-clear').addEventListener('click', async () => {
        if (!confirm('Hapus semua waypoint?')) return;
        // Cukup panggil endpoint — broadcast dari server akan trigger
        // handleTelemetryEvent('waypoints_cleared') yang meng-clear UI
        // (juga di client lain). Tidak perlu clear langsung di sini.
        await fetch('/api/waypoints/clear', { method: 'POST' });
    });

    // ── Pilih sumber kamera
    $('camera-select').addEventListener('change', (e) => {
        if (e.target.value === '') return;
        if (e.target.value === REFRESH_VALUE) {
            loadCameraSources(/*refresh=*/true);
            return;
        }
        changeCameraSource(parseInt(e.target.value, 10));
    });

    // ── Pilih port GPS
    const gpsPortSelect = $('gps-port-select');
    if (gpsPortSelect) {
        gpsPortSelect.addEventListener('change', (e) => {
            if (e.target.value === '') return;
            changeGpsPort(e.target.value);
        });
    }

    // ── Preferensi ROV (bukan perintah ke wahana, tidak perlu unlock)
    $('ctrl-rov-heading').addEventListener('change', (e) => {
        postJSON('/api/rov/prefs', { use_heading: e.target.checked });
    });
    $('ctrl-rov-auto-depth').addEventListener('change', (e) => {
        postJSON('/api/rov/prefs', { auto_depth: e.target.checked });
        if (e.target.checked) {
            setStatus('Depth HOP mengikuti kedalaman ROV (clamp 1–7 m)');
        }
    });

    // ── Unlock kontrol ROV
    $('ctrl-rov-unlock').addEventListener('change', async (e) => {
        const want = e.target.checked;
        if (want && !confirm(
                'Membuka kunci kontrol ROV.\n\n' +
                'Setelah ini tombol lampu dan hold akan benar-benar ' +
                'menggerakkan wahana. Lanjutkan?')) {
            e.target.checked = false;
            return;
        }
        const j = await postJSON('/api/rov/unlock', { unlocked: want });
        if (!j || !j.ok) {
            // Kembalikan SELURUH state, bukan cuma centangnya. Menyetel
            // `.checked` saja tidak memicu 'change', jadi lapisan kendali
            // tidak akan pernah tahu bahwa permintaannya ditolak — dan itu
            // meninggalkan browser yang mengira dirinya boleh menggerakkan
            // wahana padahal server bilang tidak.
            applyUnlockToUI(!want);
            setStatus('❌ Gagal mengubah kunci kontrol ROV');
            return;
        }
        // UI final menunggu broadcast rov_unlock supaya klien lain ikut sinkron.
        setStatus(want ? '⚠ Kontrol ROV DIBUKA' : '🔒 Kontrol ROV dikunci');
    });

    // ── Tombol ROV. Nilai yang dikirim adalah LAWAN dari status sekarang,
    //    dan status itu dibaca dari telemetri (class .on), bukan dari
    //    hitungan klik di browser.
    const rovToggles = {
        'btn-rov-light': 'light',
        'btn-rov-holdd': 'holdd',
        'btn-rov-holdy': 'holdy',
    };
    Object.entries(rovToggles).forEach(([elId, cmd]) => {
        $(elId).addEventListener('click', () => {
            const isOn = $(elId).classList.contains('on');
            sendRovCommand(cmd, isOn ? 0 : 1);
        });
    });

    // ── Map zoom buttons
    $('btn-zoom-in').addEventListener('click',  () => { if (map) map.zoomIn(); });
    $('btn-zoom-out').addEventListener('click', () => { if (map) map.zoomOut(); });
    $('btn-center').addEventListener('click', () => {
        if (!map) return;
        if (lastGpsPos) {
            map.setView(lastGpsPos, 17);
        } else {
            setStatus('❌ Belum ada posisi GPS untuk di-center');
        }
    });
}

async function postJSON(url, payload) {
    try {
        const r = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        return await r.json();
    } catch (e) {
        console.error('POST failed', url, e);
        return null;
    }
}

async function postControl(payload) {
    try {
        await fetch('/api/control', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
    } catch (e) {
        console.error('Control update failed', e);
    }
}

// ═══ Initial state ═══════════════════════════════════════════════════
async function fetchInitialState() {
    try {
        const r = await fetch('/api/state');
        const s = await r.json();

        // Checkboxes
        $('ctrl-hop').checked         = s.control.hop_enabled;
        $('ctrl-clahe').checked       = s.control.clahe_enabled;
        $('ctrl-dehaze').checked      = s.control.dehaze_enabled;
        $('ctrl-wb').checked          = s.control.wb_enabled;
        $('ctrl-yolo').checked        = s.control.yolo_enabled;
        $('ctrl-auto-wp').checked     = s.auto_waypoint_enabled;
        $('ctrl-mark-detect').checked = s.mark_on_detect_enabled;
        $('ctrl-hop-depth').value     = s.control.hop_depth;
        $('hop-depth-label').textContent = s.control.hop_depth;

        // Source displays (read-only)
        const streaming0 = s.frame_age_s !== null
            && Number.isFinite(s.frame_age_s)
            && s.frame_age_s < 5;
        updateCaptureStatus(streaming0);
        updatePerfLine(s.capture);
        await loadCameraSources();
        await loadGpsPorts();
        const gpsActive = s.gps.connected && (Date.now()/1000 - s.gps.last_update) < 10;
        updateGpsStatus(gpsActive, s.config.gps_port);

        // Populate existing waypoints (history)
        s.waypoints.forEach(wp => {
            addWaypointRow(wp);
            addWaypointMarker(wp);
        });

        // GPS jika sudah ada fix
        if (s.gps.lat !== null) {
            $('lat-val').textContent = s.gps.lat.toFixed(6) + '°';
            $('lon-val').textContent = s.gps.lon.toFixed(6) + '°';
            updateRovMarker(s.gps.lat, s.gps.lon, s.gps.heading);
            if (map) map.setView([s.gps.lat, s.gps.lon], 17);
        }

        // Heading dari server (sudah memilih yaw ROV vs COG GPS)
        if (s.heading !== undefined && s.heading !== null) {
            const tag = s.heading_source === 'rov' ? ' (ROV)' : ' (GPS)';
            $('hdg-val').textContent = s.heading.toFixed(1) + '°' + tag;
        }

        // ── ROV
        const rov = s.rov || {};
        // Label sumber harus cocok dengan status di bawahnya. Di mode uji
        // (ROV_FAKE_WORKERS) telemetri mengalir walau ROV_TELEMETRY_ENABLED=0,
        // dan menampilkan "nonaktif" di situ bertentangan dengan indikator
        // "Telemetri aktif" tepat di bawahnya.
        if (s.config.rov_enabled) {
            $('rov-source').textContent = s.config.rov_label;
        } else if (rov.fresh) {
            $('rov-source').textContent = 'Mode uji — telemetri simulasi';
        } else {
            $('rov-source').textContent =
                'Nonaktif — set ROV_TELEMETRY_ENABLED=1';
        }
        $('ctrl-rov-heading').checked    = rov.use_heading !== false;
        $('ctrl-rov-auto-depth').checked = rov.auto_depth  !== false;
        applyUnlockToUI(rov.unlocked);
        // Mode simulasi ikut disinkronkan dari state awal. Tanpa ini,
        // halaman yang di-refresh (atau browser kedua yang baru dibuka)
        // selagi server berada di SIM akan menampilkan centang kosong dan
        // tanpa pita peringatan — operator membaca "mode nyata" padahal
        // perintahnya tidak sampai ke wahana sama sekali. Broadcast hanya
        // menyusulkan PERUBAHAN, bukan keadaan yang sudah berjalan.
        if (window.RovControls) window.RovControls.applySim(!!rov.sim);
        updateRovStatus(!!rov.fresh);
        if (rov.data && Object.keys(rov.data).length) {
            applyRovTelemetry({
                ...rov.data,
                _heading: s.heading,
                _heading_source: s.heading_source,
            });
        }

        setStatus('Sistem siap');
    } catch (e) {
        console.error('Initial state fetch failed', e);
        setStatus('❌ Gagal load state awal');
    }
}

// ═══ Helpers ═════════════════════════════════════════════════════════
function setStatus(msg) {
    $('status-msg').textContent = 'Status: ' + msg;
}
