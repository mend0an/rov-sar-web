"""
Shared state singleton.

Pattern: satu instance global `state` di-import dari module manapun. Worker
threads (capture, GPS) menulis ke state; Django views/consumers membaca dari
state. Akses dilindungi lock dimana perlu (frame buffer terutama).

Kenapa singleton dan bukan database?
- Frame video terlalu besar/cepat untuk database (~30fps × 1080p)
- GPS update juga sub-second, overkill untuk DB
- Single-process deployment, jadi in-memory aman

Kalau nanti perlu persistent waypoint history, simpan ke SQLite via Django ORM
sebagai tambahan (bukan pengganti).
"""
import threading
import time
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Waypoint:
    lat: float
    lon: float
    label: str
    is_detect: bool
    timestamp: float

    def to_dict(self):
        return {
            "lat": self.lat,
            "lon": self.lon,
            "label": self.label,
            "is_detect": self.is_detect,
            "timestamp": self.timestamp,
        }


class _AppState:
    """Singleton state — di-import sebagai `state` di module lain."""

    def __init__(self):
        # ─── Frame buffer ──────────────────────────────────────────────
        self._frame_lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None    # JPEG-encoded, siap kirim
        self._frame_count = 0
        self._last_frame_time = 0.0

        # ─── Control flags (toggleable dari UI) ────────────────────────
        self._control_lock = threading.Lock()
        self.hop_enabled = True
        self.clahe_enabled = False
        self.dehaze_enabled = False
        self.wb_enabled = False
        self.yolo_enabled = True
        self.hop_depth = 1

        # ─── GPS state ─────────────────────────────────────────────────
        self._gps_lock = threading.Lock()
        self.gps_lat: Optional[float] = None
        self.gps_lon: Optional[float] = None
        self.gps_heading: float = 0.0
        self.gps_last_update: float = 0.0
        self.gps_connected: bool = False

        # ─── Waypoints ─────────────────────────────────────────────────
        self._wp_lock = threading.Lock()
        self.waypoints: list[Waypoint] = []
        self._wp_counter = 0
        self.auto_waypoint_enabled = True
        self.mark_on_detect_enabled = True

        # ─── Telemetri ROV (TCP 6666) ──────────────────────────────────
        # Sumber kebenaran untuk sikap (roll/pitch/yaw) dan kedalaman.
        # GPS hanya tahu posisi buoy di permukaan; yaw & depth datang dari
        # ROV itu sendiri.
        self._rov_lock = threading.Lock()
        self.rov_data: dict = {}          # field mentah dari protokol
        self.rov_connected: bool = False
        self.rov_updated_at: float = 0.0
        self.rov_last_error: Optional[str] = None
        # Kontrol wahana terkunci sampai operator membukanya secara sadar.
        # Ini state SERVER, bukan sekadar checkbox di browser — kalau hanya
        # di klien, siapa pun yang bisa POST /api/rov/command bisa melewatinya.
        self.rov_control_unlocked: bool = False

        # ─── Pilot tunggal ─────────────────────────────────────────────
        # Aplikasi ini diakses banyak klien di satu LAN: HP operator, tablet
        # pengamat, laptop. Toggle (lampu, depth lock) tidak masalah kalau
        # ditekan siapa pun — hasilnya sama. GERAK berbeda: kalau dua orang
        # sama-sama memegang stick, ROV menerima dua vektor yang berselisih
        # 10 kali per detik dan bergerak tersentak tanpa ada yang mengerti
        # kenapa. Jadi satu klien memegang kendali gerak pada satu waktu.
        #
        # Klaimnya implisit — klien pertama yang mengirim perintah gerak
        # menjadi pilot — dan kedaluwarsa sendiri setelah diam. Tidak ada
        # tombol "ambil alih" yang harus diingat operator saat panik.
        self.rov_pilot_id: Optional[str] = None
        self.rov_pilot_at: float = 0.0
        self.rov_last_move: dict = {"thro": 0, "lift": 0, "yaw": 0}
        self.rov_last_move_at: float = 0.0

        # ─── Mode simulasi ─────────────────────────────────────────────
        # Perintah divalidasi dan disiarkan seperti biasa, tapi TIDAK pernah
        # menyentuh soket TCP. Ini bukan kemewahan: memetakan tombol butuh
        # menekan setiap tombol berkali-kali, dan satu-satunya cara aman
        # melakukannya adalah tanpa wahana di ujung kabel. Tanpa mode ini,
        # kalibrasi controller memaksa ROV menyala dan bergerak di lantai.
        #
        # Sengaja TIDAK disimpan permanen: tiap restart server kembali ke
        # mode nyata. Mode simulasi yang tertinggal menyala tanpa disadari
        # berarti operator menekan STOP dan tidak ada yang terjadi.
        self.rov_sim_mode: bool = False

        # Preferensi integrasi ROV ↔ peta ↔ HOP
        self.rov_use_heading: bool = True   # pakai yaw ROV, bukan COG buoy
        self.rov_auto_depth: bool = True    # kedalaman ROV → slider HOP

        # ─── Worker lifecycle ──────────────────────────────────────────
        self.capture_worker = None
        self.gps_worker = None
        self.rov_worker = None

    # ─── Frame ─────────────────────────────────────────────────────────
    def set_frame(self, jpeg_bytes: bytes):
        with self._frame_lock:
            self._latest_jpeg = jpeg_bytes
            self._frame_count += 1
            self._last_frame_time = time.time()

    def get_frame(self) -> Optional[bytes]:
        with self._frame_lock:
            return self._latest_jpeg

    def get_frame_with_id(self) -> tuple:
        """
        Return (jpeg_bytes, frame_count). frame_count adalah counter monotonik
        yang naik tiap set_frame(). Dipakai MJPEG generator untuk dedup:
        jangan kirim frame yang counter-nya sama dengan yang barusan dikirim.

        Ini lebih andal daripada id(jpeg) karena:
          - id() bisa di-reuse GC kalau objek lama sudah dibebaskan
          - bytes yang identik (misal fake frame konstan) punya id sama
            tapi tetap frame "baru" dari sisi waktu
        """
        with self._frame_lock:
            return self._latest_jpeg, self._frame_count

    def frame_age(self) -> Optional[float]:
        """
        Detik sejak frame terakhir di-update, atau None kalau belum pernah
        ada frame. Return None (bukan float('inf')) supaya JSON valid —
        Infinity bukan JSON standar dan bikin JSON.parse() di browser gagal.
        """
        with self._frame_lock:
            if self._last_frame_time == 0:
                return None
            return time.time() - self._last_frame_time

    # ─── Control ───────────────────────────────────────────────────────
    def update_control(self, **kwargs):
        with self._control_lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def get_control(self) -> dict:
        with self._control_lock:
            return {
                "hop_enabled": self.hop_enabled,
                "clahe_enabled": self.clahe_enabled,
                "dehaze_enabled": self.dehaze_enabled,
                "wb_enabled": self.wb_enabled,
                "yolo_enabled": self.yolo_enabled,
                "hop_depth": self.hop_depth,
            }

    # ─── GPS ───────────────────────────────────────────────────────────
    def set_gps(self, lat: float, lon: float, heading: float):
        with self._gps_lock:
            self.gps_lat = lat
            self.gps_lon = lon
            self.gps_heading = heading
            self.gps_last_update = time.time()
            self.gps_connected = True

    def get_gps(self) -> dict:
        with self._gps_lock:
            return {
                "lat": self.gps_lat,
                "lon": self.gps_lon,
                "heading": self.gps_heading,
                "connected": self.gps_connected,
                "last_update": self.gps_last_update,
            }

    # ─── Telemetri ROV ─────────────────────────────────────────────────
    def set_rov_telemetry(self, data: dict):
        with self._rov_lock:
            self.rov_data.update(data)
            self.rov_connected = True
            self.rov_updated_at = time.time()
            self.rov_last_error = None

    def set_rov_disconnected(self, error: Optional[str] = None):
        with self._rov_lock:
            self.rov_connected = False
            self.rov_last_error = error

    def get_rov(self) -> dict:
        """
        Snapshot telemetri ROV. `fresh` membedakan 'socket pernah tersambung'
        dari 'data masih mengalir' — yang kedua itu yang menentukan apakah
        angka di layar boleh dipercaya.
        """
        with self._rov_lock:
            age = (time.time() - self.rov_updated_at
                   if self.rov_updated_at else None)
            return {
                "connected": self.rov_connected,
                "fresh": self.rov_connected and age is not None and age < 5.0,
                "age_s": age,
                "last_error": self.rov_last_error,
                "unlocked": self.rov_control_unlocked,
                "use_heading": self.rov_use_heading,
                "auto_depth": self.rov_auto_depth,
                "pilot": (
                    self.rov_pilot_id
                    if self.rov_pilot_id
                    and (time.time() - self.rov_pilot_at) <= self.PILOT_TTL_S
                    else None
                ),
                "move": dict(self.rov_last_move),
                "sim": self.rov_sim_mode,
                "data": dict(self.rov_data),
            }

    # ─── Pilot tunggal ─────────────────────────────────────────────────
    # Berapa lama pilot memegang kendali setelah perintah terakhirnya.
    # 3 detik: cukup lama untuk menahan klaim saat operator berhenti
    # sejenak di antara manuver, cukup pendek supaya rekan bisa mengambil
    # alih dengan cepat kalau HP pilot mati atau tercebur.
    PILOT_TTL_S = 3.0

    def claim_pilot(self, client_id: str) -> tuple:
        """
        Coba klaim kendali gerak. Return (ok, pemegang_sekarang).

        Klien tanpa id ditolak: tanpa identitas tidak ada cara membedakan
        "operator yang sama mengirim frame berikutnya" dari "orang kedua
        mulai ikut menyetir".
        """
        if not client_id:
            return (False, None)
        now = time.time()
        with self._rov_lock:
            holder = self.rov_pilot_id
            expired = (now - self.rov_pilot_at) > self.PILOT_TTL_S
            if holder and holder != client_id and not expired:
                return (False, holder)
            self.rov_pilot_id = client_id
            self.rov_pilot_at = now
            return (True, client_id)

    def release_pilot(self, client_id: str):
        """Lepas klaim. Klien lain tidak bisa melepas klaim orang lain."""
        with self._rov_lock:
            if self.rov_pilot_id == client_id:
                self.rov_pilot_id = None
                self.rov_pilot_at = 0.0

    def record_move(self, thro: int, lift: int, yaw: int):
        with self._rov_lock:
            self.rov_last_move = {"thro": thro, "lift": lift, "yaw": yaw}
            self.rov_last_move_at = time.time()

    def move_snapshot(self) -> tuple:
        """(vektor_terakhir, umur_detik). Dipakai watchdog deadman."""
        with self._rov_lock:
            age = (time.time() - self.rov_last_move_at
                   if self.rov_last_move_at else None)
            return (dict(self.rov_last_move), age)

    def rov_float(self, key: str) -> Optional[float]:
        with self._rov_lock:
            raw = self.rov_data.get(key)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def active_heading(self) -> tuple:
        """
        Heading yang dipakai peta, plus sumbernya: ("rov"|"gps", derajat).

        COG dari NMEA RMC adalah arah GERAK buoy permukaan — buoy bisa
        hanyut ke timur sementara ROV menghadap utara, dan saat buoy diam
        nilainya sampah. Yaw dari telemetri ROV adalah arah hadap sebenarnya,
        jadi itu yang diutamakan kalau tersedia.
        """
        if self.rov_use_heading:
            gps_rov = self.get_rov()
            if gps_rov["fresh"]:
                yaw = self.rov_float("Y")
                if yaw is not None:
                    return ("rov", yaw)
        with self._gps_lock:
            return ("gps", self.gps_heading)

    # ─── Waypoints ─────────────────────────────────────────────────────
    def add_waypoint(self, lat: float, lon: float,
                     label: Optional[str] = None,
                     is_detect: bool = False) -> Waypoint:
        with self._wp_lock:
            self._wp_counter += 1
            if label is None:
                label = f"WP-{self._wp_counter:02d}"
            if is_detect and not label.startswith("⚠"):
                label = f"⚠ {label}"
            wp = Waypoint(
                lat=lat, lon=lon, label=label,
                is_detect=is_detect, timestamp=time.time(),
            )
            self.waypoints.append(wp)
            return wp

    def get_waypoints(self) -> list[dict]:
        with self._wp_lock:
            return [wp.to_dict() for wp in self.waypoints]

    def clear_waypoints(self):
        with self._wp_lock:
            self.waypoints.clear()
            self._wp_counter = 0

    def should_add_auto_waypoint(self, lat: float, lon: float,
                                  min_dist_m: float = 5.0) -> bool:
        """Cek apakah harus tambah waypoint otomatis (jarak >= min_dist)."""
        with self._wp_lock:
            if not self.waypoints:
                return True
            last = self.waypoints[-1]
            dist = _haversine(last.lat, last.lon, lat, lon)
            return dist >= min_dist_m

    def last_waypoint_pos(self) -> tuple | None:
        """Return (lat, lon) waypoint terakhir, atau None."""
        with self._wp_lock:
            if not self.waypoints:
                return None
            last = self.waypoints[-1]
            return (last.lat, last.lon)


# ─── Broadcast helper — dipakai views & workers ──────────────────────────
def broadcast(event: str, payload: dict):
    """
    Broadcast event ke semua WebSocket client di group 'telemetry'.

    Dipakai untuk sinkronisasi multi-client: setiap state change harus
    di-broadcast supaya semua browser tetap in-sync.

    Events yang dibroadcast:
      - gps                 (dari GpsWorker)
      - waypoint_added      (dari GpsWorker/CaptureWorker/manual mark view)
      - waypoints_cleared   (dari clear view)
      - control_updated     (dari control view)
      - gps_status          (dari GpsWorker on connect/disconnect/stale)
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            "telemetry",
            {"type": "telemetry.event", "event": event, "payload": payload},
        )
    except Exception:
        # Broadcast failure jangan block operasi utama
        pass


GPS_FIX_MAX_AGE_S = 10.0   # fix lebih tua dari ini dianggap stale


def gps_fix_is_fresh(gps: dict, max_age_s: float = GPS_FIX_MAX_AGE_S) -> bool:
    """
    True kalau fix GPS layak dipakai untuk mencatat koordinat temuan:
    connected, lat/lon ada, dan fix belum stale.

    Dipakai oleh: manual waypoint, waypoint deteksi YOLO, dan fitur lain
    yang menyimpan koordinat. Mencegah waypoint tercatat pakai koordinat lama
    saat GPS putus/stale — untuk SAR ini penting karena waypoint stale
    terlihat sah padahal berasal dari lokasi lama.
    """
    return (
        gps.get("connected", False)
        and gps.get("lat") is not None
        and gps.get("lon") is not None
        and gps.get("last_update", 0) > 0
        and (time.time() - gps["last_update"]) <= max_age_s
    )


def _haversine(lat1, lon1, lat2, lon2) -> float:
    """Jarak (meter) antara dua koordinat lat/lon."""
    R = 6_371_000
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Singleton — import `state` dari module manapun
state = _AppState()
