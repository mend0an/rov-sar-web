"""
GPS Worker — thread yang baca NMEA dari serial port (atau simulate dummy).
Setiap update GPS valid:
1. Update state via state.set_gps()
2. Broadcast ke WebSocket group 'telemetry'
3. Cek auto-waypoint (jarak >= min_dist dari waypoint terakhir)

Mode DUMMY: untuk testing tanpa hardware GPS.

Reliability:
    - Kalau serial gagal open, retry loop dengan backoff (bukan langsung mati)
    - Kalau serial exception saat baca, log & continue (bukan crash thread)
    - Kalau tidak ada fix > STALE_TIMEOUT_S, invalidate gps_connected
      dan broadcast gps_status event supaya UI update
    - Watchdog thread cek stale timeout tiap detik
"""
import logging
import math
import random
import threading
import time

from .state import state, broadcast

logger = logging.getLogger(__name__)


STALE_TIMEOUT_S     = 10.0    # detik tanpa fix → stale
RECONNECT_BACKOFF_S = 3.0     # detik antar retry serial open


class GpsWorker(threading.Thread):

    def __init__(self, port: str, baudrate: int = 9600,
                 auto_wp_min_dist: float = 5.0):
        super().__init__(daemon=True, name="GpsWorker")
        self.port = port
        self.baudrate = baudrate
        self.auto_wp_min_dist = auto_wp_min_dist
        self._stop_event = threading.Event()

        # Watchdog thread untuk deteksi stale GPS
        self._watchdog = threading.Thread(
            target=self._run_watchdog, daemon=True, name="GpsWatchdog",
        )

    def stop(self):
        self._stop_event.set()

    def run(self):
        self._watchdog.start()

        if self.port.upper() == "DUMMY":
            self._run_dummy()
        else:
            self._run_serial_with_reconnect()

    # ─── Watchdog ──────────────────────────────────────────────────────
    def _run_watchdog(self):
        """
        Thread pengawas: kalau tidak ada fix > STALE_TIMEOUT_S,
        set connected=False dan broadcast status.
        """
        was_connected = False
        while not self._stop_event.is_set():
            time.sleep(1.0)
            gps = state.get_gps()
            if gps["last_update"] == 0:
                continue    # belum pernah ada fix
            age = time.time() - gps["last_update"]
            is_stale = age > STALE_TIMEOUT_S

            with state._gps_lock:
                state.gps_connected = not is_stale

            if was_connected and is_stale:
                logger.warning(f"GPS stale ({age:.1f}s tanpa fix)")
                broadcast("gps_status", {
                    "connected": False,
                    "reason": "stale",
                    "age_s": age,
                })
                was_connected = False
            elif not was_connected and not is_stale:
                broadcast("gps_status", {"connected": True})
                was_connected = True

    # ─── Real GPS with reconnect loop ─────────────────────────────────
    def _run_serial_with_reconnect(self):
        """Loop reconnect: kalau serial fail, tunggu & coba lagi terus."""
        try:
            import serial
        except ImportError:
            logger.error("pyserial belum terinstall — pakai port=DUMMY untuk tes")
            return

        while not self._stop_event.is_set():
            ser = None
            try:
                ser = serial.Serial(self.port, self.baudrate, timeout=1)
                logger.info(f"✅ GPS terhubung: {self.port} @ {self.baudrate}bps")
                broadcast("gps_status", {"connected": True, "port": self.port})
                self._read_serial_loop(ser)
            except serial.SerialException as e:
                logger.warning(f"GPS serial error: {e} — retry dalam {RECONNECT_BACKOFF_S}s")
                # Langsung set backend disconnected (thread-safe), jangan tunggu
                # watchdog timeout — supaya /api/state tidak terlanjur bilang
                # connected=True selama beberapa detik.
                with state._gps_lock:
                    state.gps_connected = False
                broadcast("gps_status", {
                    "connected": False, "reason": "serial_error", "error": str(e),
                })
            except Exception as e:
                logger.error(f"GPS unexpected error: {e}")
                with state._gps_lock:
                    state.gps_connected = False
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass

            # Backoff sebelum retry
            for _ in range(int(RECONNECT_BACKOFF_S * 10)):
                if self._stop_event.is_set():
                    return
                time.sleep(0.1)

        logger.info("GpsWorker (serial) stopped")

    def _read_serial_loop(self, ser):
        """Loop baca NMEA. Return kalau connection putus / stop dipanggil."""
        last_heading = 0.0
        while not self._stop_event.is_set():
            try:
                raw = ser.readline().decode("ascii", errors="replace").strip()
            except Exception as e:
                logger.warning(f"Serial readline error: {e}")
                with state._gps_lock:
                    state.gps_connected = False
                broadcast("gps_status", {"connected": False, "reason": "read_error"})
                return   # keluar loop → reconnect

            if not raw:
                continue

            if raw.startswith(("$GPRMC", "$GNRMC")):
                parts = raw.split(",")
                if len(parts) >= 9 and parts[2] == "A":
                    try:
                        lat = _nmea_to_dd(parts[3], parts[4])
                        lon = _nmea_to_dd(parts[5], parts[6])
                        cog = float(parts[8]) if parts[8] else last_heading
                        last_heading = cog
                        self._handle_fix(lat, lon, cog)
                    except (ValueError, IndexError):
                        pass

    # ─── Dummy mode ────────────────────────────────────────────────────
    def _run_dummy(self):
        """Simulasi GPS — bergerak random di sekitar titik awal."""
        logger.info("📍 GPS DUMMY mode aktif (simulasi)")
        broadcast("gps_status", {"connected": True, "port": "DUMMY"})
        lat = -7.7956      # Yogyakarta-ish
        lon = 110.3695
        hdg = 45.0

        while not self._stop_event.is_set():
            lat += (random.random() - 0.45) * 0.00004
            lon += (random.random() - 0.45) * 0.00004
            hdg = (hdg + (random.random() - 0.5) * 5) % 360
            self._handle_fix(lat, lon, hdg)
            time.sleep(1.5)

        logger.info("GpsWorker (dummy) stopped")

    # ─── Fix handler ───────────────────────────────────────────────────
    def _handle_fix(self, lat: float, lon: float, heading: float):
        state.set_gps(lat, lon, heading)

        broadcast("gps", {
            "lat": lat,
            "lon": lon,
            "heading": heading,
        })

        # Auto-waypoint
        if state.auto_waypoint_enabled:
            if state.should_add_auto_waypoint(lat, lon, self.auto_wp_min_dist):
                wp = state.add_waypoint(lat, lon, label=None, is_detect=False)
                broadcast("waypoint_added", wp.to_dict())


# ─── NMEA helper ──────────────────────────────────────────────────────────
def _nmea_to_dd(nmea_val: str, direction: str) -> float:
    """Konversi NMEA dddmm.mmmm → decimal degrees."""
    if not nmea_val:
        return 0.0
    dot = nmea_val.index(".")
    deg = float(nmea_val[:dot - 2])
    mins = float(nmea_val[dot - 2:])
    dd = deg + mins / 60.0
    if direction in ("S", "W"):
        dd = -dd
    return dd
