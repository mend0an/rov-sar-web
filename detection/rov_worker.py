"""
ROV Worker — jembatan antara `rov_telemetry.RovTelemetry` (TCP 6666) dan
state aplikasi + WebSocket.

Kenapa perlu worker terpisah padahal RovTelemetry sudah punya thread sendiri?
    RovTelemetry cuma menjaga koneksi dan mengisi dict internal; dia tidak
    tahu apa-apa soal Django, Channels, atau state singleton. Worker ini yang
    mem-poll dict itu dengan laju tetap lalu mendorongnya ke browser. Di versi
    desktop peran ini dipegang QTimer 200ms.

Laju broadcast:
    BROADCAST_HZ = 5. Telemetri masuk jauh lebih cepat dari itu, tapi mata
    manusia tidak butuh 30 update/detik untuk membaca angka kedalaman, dan
    tiap broadcast menempuh channel layer → semua browser. 5 Hz sudah terasa
    real-time dan tidak membanjiri WebSocket.

Auto-depth:
    Kedalaman ROV (field "D") otomatis mengisi slider depth HOP. Nilainya
    DI-CLAMP ke 1-7 m: kurva HOP adalah interpolasi eksak 7 titik berderajat
    6, di luar rentang itu polinomialnya berosilasi dan koreksi warnanya
    justru rusak. Lihat catatan yang sama di versi PyQt5.
"""
import logging
import threading
import time

from .state import state, broadcast

logger = logging.getLogger(__name__)


BROADCAST_HZ    = 5.0
POLL_INTERVAL_S = 1.0 / BROADCAST_HZ

# ─── Deadman ─────────────────────────────────────────────────────────────
# ROV MENGUNCI perintah terakhir: kirim thro:2 lalu diam, dan wahana terus
# maju sampai ada yang menyuruhnya berhenti. Artinya melepas stick bukan
# perintah berhenti — browser harus aktif mengirim nol.
#
# Yang membuat ini berbahaya: mode kegagalan yang paling mungkin adalah
# browser-nya sendiri berhenti mengirim. Tab ditutup, HP kehabisan baterai,
# WiFi putus di ujung dermaga, operator tersandung dan HP-nya tercebur. Di
# semua kasus itu perintah nol dari browser tidak akan pernah datang, justru
# saat paling dibutuhkan.
#
# Karena itu deadman harus di SERVER, bukan di browser. Watchdog di bawah
# memeriksa umur perintah gerak terakhir; kalau melewati ambang sementara
# vektornya bukan nol, server mengirim stop atas namanya sendiri.
#
# 1.5 detik: browser mengirim 10 Hz, jadi ambang ini memberi ruang 15 paket
# hilang sebelum bertindak — cukup toleran terhadap WiFi yang tersendat,
# cukup cepat supaya wahana tidak sempat jauh. Pada gear rendah 1,5 detik
# gerak maju hanya beberapa puluh sentimeter.
MOVE_DEADMAN_S = 1.5

# ROV mengunci perintah, jadi mengirim ulang nilai yang sama sebenarnya tidak
# perlu. Tapi selama PCAP belum menjawab apakah vendor mengirim periodik atau
# hanya saat berubah, mengirim ulang pelan-pelan adalah taruhan yang aman:
# kalau ternyata firmware punya timeout internal, latch-nya tetap segar; kalau
# tidak, biayanya cuma dua paket per detik.
MOVE_REPEAT_S = 0.5

# Field yang dikirim ke browser. Sengaja dibatasi: telemetri mentah punya
# 30+ field (termasuk PWM tiap thruster dan blok DVL) yang tidak ditampilkan
# panel dan cuma jadi beban WebSocket.
BROADCAST_FIELDS = (
    "R", "P", "Y", "D", "PS", "to", "ti", "batv", "RT", "PVN",
    "L", "HD", "HH", "err",
)


class RovWorker(threading.Thread):
    """
    Poll RovTelemetry → state → broadcast. Juga menerapkan auto-depth.
    """

    def __init__(self, host: str, port: int = 6666):
        super().__init__(daemon=True, name="RovWorker")
        self.host = host
        self.port = port
        self._stop_event = threading.Event()
        self._rov = None
        self._was_fresh = False
        self._last_auto_depth = None
        self._move_lock = threading.Lock()
        self._last_move_vec = {"thro": 0, "lift": 0, "yaw": 0}
        self._last_move_sent_at = 0.0
        self._deadman_tripped = False

    def stop(self):
        self._stop_event.set()
        if self._rov is not None:
            try:
                self._rov.close()
            except Exception:
                pass

    # ─── Akses untuk view (kirim perintah) ─────────────────────────────
    @property
    def client(self):
        """RovTelemetry aktif, atau None kalau belum tersambung."""
        return self._rov

    def send(self, key, value) -> bool:
        if self._rov is None or not self._rov.is_fresh():
            return False
        return self._rov.send(key, value)

    def send_move(self, thro: int, lift: int, yaw: int) -> bool:
        """
        Kirim vektor gerak. Sumbu yang nilainya tidak berubah TIDAK dikirim
        ulang kecuali sudah lewat MOVE_REPEAT_S.

        Browser mengirim 10 Hz supaya deadman punya denyut yang bisa dihitung,
        tapi meneruskan 30 paket TCP per detik ke wahana hanya karena operator
        menahan stick di posisi yang sama itu pemborosan yang tidak membeli
        apa pun. Dedupe di sini, bukan di browser — supaya berlaku sama untuk
        semua sumber masukan.
        """
        if self._rov is None or not self._rov.is_fresh():
            return False

        now = time.time()
        vec = {"thro": int(thro), "lift": int(lift), "yaw": int(yaw)}
        force = (now - self._last_move_sent_at) >= MOVE_REPEAT_S

        ok = True
        changed = False
        with self._move_lock:
            for axis, value in vec.items():
                if value != self._last_move_vec[axis] or force:
                    ok &= self._rov.send(axis, value)
                    changed = True
                self._last_move_vec[axis] = value
            if changed:
                self._last_move_sent_at = now

        state.record_move(**vec)
        return ok

    def force_stop(self, reason: str = "") -> bool:
        """Nolkan semua sumbu tanpa dedupe. Dipakai e-stop dan deadman."""
        if self._rov is None:
            return False
        ok = True
        for axis in ("thro", "lift", "yaw"):
            ok &= self._rov.send(axis, 0)
        with self._move_lock:
            self._last_move_vec = {"thro": 0, "lift": 0, "yaw": 0}
            self._last_move_sent_at = time.time()
        state.record_move(0, 0, 0)
        if reason:
            logger.warning(f"🛑 Gerak dinolkan — {reason}")
        return ok

    # ─── Main loop ─────────────────────────────────────────────────────
    def run(self):
        from .rov_telemetry import RovTelemetry

        try:
            # JANGAN wait_connected() di sini — itu blocking sampai 180 detik.
            # RovTelemetry sudah punya thread reconnect sendiri; worker cukup
            # mem-poll statusnya seperti QTimer di versi desktop.
            self._rov = RovTelemetry(host=self.host, port=self.port)
        except Exception as e:
            logger.error(f"❌ Gagal membuat klien telemetri ROV: {e}")
            state.set_rov_disconnected(str(e))
            return

        logger.info(
            f"🛰  RovWorker start — {self.host}:{self.port} "
            f"(ROV butuh waktu boot, pada uji tercatat ~146 detik)"
        )

        while not self._stop_event.wait(POLL_INTERVAL_S):
            try:
                self._tick()
            except Exception as e:
                logger.debug(f"RovWorker tick error: {e}")

        try:
            self._rov.close()
        except Exception:
            pass
        state.set_rov_disconnected("worker dihentikan")
        logger.info("RovWorker stopped")

    def _tick(self):
        fresh = self._rov.is_fresh()

        if not fresh:
            if self._was_fresh:
                logger.warning(
                    f"Telemetri ROV putus: {self._rov.last_error or 'stale'}"
                )
                state.set_rov_disconnected(self._rov.last_error)
                broadcast("rov_status", {
                    "connected": False,
                    "error": self._rov.last_error,
                })
                self._was_fresh = False
            return

        snap = self._rov.snapshot()
        payload = {k: v for k, v in snap.items() if k in BROADCAST_FIELDS}
        state.set_rov_telemetry(snap)

        if not self._was_fresh:
            logger.info(f"✅ Telemetri ROV tersambung — {snap.get('PVN', '?')}")
            broadcast("rov_status", {
                "connected": True,
                "model": snap.get("PVN"),
            })
            self._was_fresh = True

        self._check_deadman()
        self._apply_auto_depth()

        source, heading = state.active_heading()
        payload["_heading"] = heading
        payload["_heading_source"] = source
        broadcast("rov", payload)

    def _check_deadman(self):
        """
        Kalau vektor gerak terakhir bukan nol dan browser sudah lama tidak
        mengirim apa pun, nolkan sendiri.

        Dipanggil tiap 200 ms, jadi ambang 1,5 detik terdeteksi dalam 7 tick.
        `_deadman_tripped` mencegah stop dikirim berulang tiap tick setelah
        pemicu — nol sudah nol, mengirimkannya 5× per detik hanya membanjiri
        log dan soket.
        """
        vec, age = state.move_snapshot()
        moving = any(v != 0 for v in vec.values())

        if not moving:
            self._deadman_tripped = False
            return
        if age is None or age < MOVE_DEADMAN_S:
            self._deadman_tripped = False
            return
        if self._deadman_tripped:
            return

        self._deadman_tripped = True
        self.force_stop(
            f"tidak ada perintah gerak selama {age:.1f}s "
            f"(vektor terakhir {vec}) — klien kemungkinan terputus"
        )
        broadcast("rov_deadman", {"vector": vec, "age_s": round(age, 2)})

    def _apply_auto_depth(self):
        """Kedalaman ROV → slider depth HOP, clamp 1-7 m."""
        if not state.rov_auto_depth:
            return
        d = state.rov_float("D")
        if d is None:
            return

        clamped = max(1, min(7, int(round(d)) or 1))
        if clamped == self._last_auto_depth:
            return
        self._last_auto_depth = clamped

        if state.get_control()["hop_depth"] != clamped:
            state.update_control(hop_depth=clamped)
            broadcast("control_updated", {
                **state.get_control(),
                "auto_waypoint_enabled": state.auto_waypoint_enabled,
                "mark_on_detect_enabled": state.mark_on_detect_enabled,
            })
