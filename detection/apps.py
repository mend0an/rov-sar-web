"""
Django AppConfig — start background workers saat Django siap.

Catatan: ready() bisa dipanggil 2x dalam dev mode (Django auto-reloader).
Solusi: cek environment variable RUN_MAIN yang di-set Django saat di-fork
ke subprocess.
"""
import logging
import os
import sys

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class DetectionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "detection"

    def ready(self):
        if not getattr(settings, "ROV_AUTOSTART_WORKERS", True):
            return

        argv = sys.argv

        # WHITELIST approach: worker HANYA nyala untuk command yang memang
        # menjalankan server (runserver/daphne) atau kalau di-force via env.
        # Ini lebih aman daripada blacklist — command apapun yang tidak
        # menjalankan server (check, migrate, shell, test, dbshell, dst)
        # otomatis TIDAK menyalakan kamera/GPS/YOLO.
        force = (os.environ.get("ROV_FORCE_WORKERS") == "1"
                 or os.environ.get("ROV_FAKE_WORKERS") == "1")
        is_runserver = len(argv) >= 2 and argv[1] == "runserver"
        # daphne: dijalankan sebagai `daphne ...`, argv[0] mengandung 'daphne'
        is_daphne = bool(argv) and "daphne" in os.path.basename(argv[0]).lower()

        if not (is_runserver or is_daphne or force):
            return

        # Django dev runserver auto-reload: ready() dipanggil 2x (parent + child).
        # Hanya start worker di child (RUN_MAIN=true) supaya nggak dobel.
        # TAPI dengan --noreload, RUN_MAIN tidak di-set sama sekali → harus
        # tetap start (tidak ada child process). Deployment lapangan sering
        # pakai --noreload sengaja.
        if is_runserver:
            no_reload = "--noreload" in argv
            if not no_reload and os.environ.get("RUN_MAIN") != "true":
                return

        self._start_workers()

    def _start_workers(self):
        from .state import state
        # Test mode: fake worker yang inject frames + GPS tanpa cv2/torch
        if os.environ.get("ROV_FAKE_WORKERS") == "1":
            self._start_fake_workers()
            return

        from .capture import CaptureWorker
        from .gps_worker import GpsWorker

        # Idempotency — kalau sudah ada worker, jangan start lagi
        if state.capture_worker is not None and state.capture_worker.is_alive():
            return

        logger.info("🚀 Starting background workers…")

        # Enumerasi kamera DILAKUKAN DI SINI, di thread utama saat startup,
        # dan hasilnya di-cache. Dua alasan: COM/DirectShow butuh inisialisasi
        # per-thread sehingga tidak aman dipanggil dari thread pool Django,
        # dan enumerasinya bisa memakan beberapa detik — cukup lama untuk
        # membuat dropdown di browser menggantung di "memuat...".
        try:
            from . import rov_camera
            n = len(rov_camera.list_sources(refresh=True))
            logger.info(f"Sumber video terdeteksi: {n}")
        except Exception as e:
            logger.warning(f"Enumerasi kamera gagal: {e}")

        capture = CaptureWorker(
            source=settings.ROV_RTSP_URL,
            model_path=settings.ROV_MODEL_PATH,
            auto_wp_min_dist=settings.ROV_AUTO_WAYPOINT_MIN_DIST_M,
            process_fps=getattr(settings, "ROV_PROCESS_FPS", 30.0),
        )
        capture.start()
        state.capture_worker = capture

        gps = GpsWorker(
            port=settings.ROV_GPS_PORT,
            baudrate=settings.ROV_GPS_BAUD,
            auto_wp_min_dist=settings.ROV_AUTO_WAYPOINT_MIN_DIST_M,
        )
        gps.start()
        state.gps_worker = gps

        # Telemetri ROV — opt-in. Default mati supaya uji tanpa ROV tidak
        # dibanjiri log "connection refused" tiap 2 detik.
        if settings.ROV_TELEMETRY_ENABLED:
            from .rov_worker import RovWorker
            rov = RovWorker(host=settings.ROV_HOST, port=settings.ROV_PORT)
            rov.start()
            state.rov_worker = rov
        else:
            logger.info(
                "ℹ️  Telemetri ROV nonaktif "
                "(set ROV_TELEMETRY_ENABLED=1 untuk mengaktifkan)"
            )

        # Set default control state dari settings
        state.update_control(hop_depth=settings.ROV_DEFAULT_HOP_DEPTH)

    def _start_fake_workers(self):
        """Fake workers untuk testing async MJPEG + broadcast tanpa hardware.

        Fake frame di-generate pakai cv2.imencode() → JPEG VALID yang bisa
        di-decode browser/cv2 (bukan byte hex manual yang cuma punya marker).
        """
        import threading, time, random

        import numpy as np
        try:
            import cv2
        except ImportError:
            logger.error("cv2 tidak tersedia — fake worker butuh cv2 untuk generate JPEG")
            return

        from .state import state, broadcast

        # Generate 1 JPEG valid (320x240 dengan pola) — dipakai berulang.
        # Ini JPEG betulan: cv2.imdecode() akan berhasil, browser render OK.
        base_img = np.zeros((240, 320, 3), dtype=np.uint8)
        base_img[:, :, 0] = np.linspace(0, 255, 320).astype(np.uint8)
        base_img[:, :, 1] = 80
        cv2.rectangle(base_img, (100, 80), (220, 160), (0, 200, 255), -1)

        def make_frame(counter):
            # Tambah counter text supaya tiap frame beda (bukti frame update)
            img = base_img.copy()
            cv2.putText(img, f"FAKE {counter}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return buf.tobytes() if ok else None

        def frame_loop():
            n = 0
            while True:
                jpeg = make_frame(n)
                if jpeg:
                    state.set_frame(jpeg)
                n += 1
                time.sleep(1/15)

        def gps_loop():
            lat, lon, hdg = -7.7956, 110.3695, 45.0
            while True:
                lat += (random.random() - 0.5) * 0.00005
                lon += (random.random() - 0.5) * 0.00005
                hdg = (hdg + (random.random() - 0.5) * 10) % 360
                state.set_gps(lat, lon, hdg)
                broadcast("gps", {"lat": lat, "lon": lon, "heading": hdg})
                time.sleep(1.0)

        def rov_loop():
            """
            Telemetri ROV palsu — bentuk field-nya sama persis dengan yang
            keluar dari protokol asli (semua nilai string, seperti hasil
            RovTelemetry._feed), supaya UI diuji terhadap tipe data yang
            benar dan bukan versi yang sudah dirapikan.
            """
            import math
            t0 = time.time()
            while True:
                t = time.time() - t0
                state.set_rov_telemetry({
                    "R":    f"{math.sin(t / 3) * 4:.2f}",
                    "P":    f"{math.cos(t / 4) * 3:.2f}",
                    "Y":    f"{(t * 6) % 360:.2f}",
                    "D":    f"{2.5 + math.sin(t / 8) * 1.8:.2f}",
                    "PS":   f"{1013 + math.sin(t / 8) * 180:.1f}",
                    "to":   f"{28.4 + math.sin(t / 20):.1f}",
                    "ti":   f"{36.0 + t / 120:.1f}",
                    "batv": f"{16.2 - t / 900:.2f}",
                    "RT":   str(int(t)),
                    "PVN":  "FAKE-T1",
                    "L": "0", "HD": "0", "HH": "0",
                })
                source, heading = state.active_heading()
                snap = state.get_rov()["data"]
                broadcast("rov", {
                    **snap,
                    "_heading": heading,
                    "_heading_source": source,
                })
                time.sleep(0.2)

        threading.Thread(target=frame_loop, daemon=True).start()
        threading.Thread(target=gps_loop, daemon=True).start()
        threading.Thread(target=rov_loop, daemon=True).start()
        logger.info("🧪 FAKE workers started (test mode — JPEG valid via cv2)")
