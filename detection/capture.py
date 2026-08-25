"""
Capture Worker — thread yang terus-menerus:
1. Baca frame dari sumber video (RTSP ROV / kamera lokal / file)
2. Apply enhancement (HOP / CLAHE / DCP / WB) sesuai state.control
3. Run YOLO inference
4. Encode hasil ke JPEG
5. Simpan ke state.set_frame() supaya MJPEG view bisa baca

Sumber video ditangani `rov_camera.open_source()`, BUKAN cv2.VideoCapture
langsung:
    OpenCV/FFmpeg tidak bisa mendekode stream RTSP ROV Titan T1 dengan benar
    (gambar beku). PyAV — binding langsung ke libav*, mesin yang sama dengan
    ffplay — mendekode stream yang sama dengan stabil, dan transportnya harus
    UDP bukan TCP karena server RTSP ROV tidak men-deliver via TCP. Semua
    detail itu sudah ada di `rov_camera.RTSPReader`; worker ini cukup memanggil
    open_source() dan memakai interface ala VideoCapture yang dikembalikannya.

Dedup frame:
    RTSPReader punya `frame_id` monotonik. Kalau frame_id belum berubah,
    frame yang sama tidak di-enhance & di-inferensi ulang — itu membakar GPU
    tanpa menghasilkan gambar baru. Sumber tanpa frame_id (webcam) tetap
    diproses tiap read().

Detection waypoint debounce:
    Cooldown DETECT_WP_COOLDOWN_S + jarak DETECT_WP_MIN_DIST_M.
    Ini mencegah spam waypoint saat objek terdeteksi terus-menerus
    (misalnya tubuh/objek SAR terlihat 10 detik = 1 waypoint, bukan 200).

Reconnect:
    RTSPReader sudah reconnect sendiri dengan backoff di thread internalnya,
    jadi read() yang gagal untuk RTSP TIDAK langsung memicu re-open — itu
    justru mengganggu reconnect yang sedang berjalan. Webcam tidak punya
    reconnect internal, jadi untuk sumber non-RTSP worker tetap membuka ulang
    setelah CONSECUTIVE_FAIL_LIMIT frame gagal berturut-turut.
"""
import logging
import threading
import time

import cv2
import numpy as np

from . import enhancement_utils as eu
from . import rov_camera
from .state import state, broadcast, gps_fix_is_fresh

logger = logging.getLogger(__name__)


# Detection waypoint anti-spam
DETECT_WP_COOLDOWN_S  = 5.0    # min detik antar waypoint deteksi
DETECT_WP_MIN_DIST_M  = 3.0    # min meter antar waypoint deteksi
DETECT_MIN_CONFIDENCE = 0.5    # min confidence untuk trigger waypoint

# Reconnect
CONSECUTIVE_FAIL_LIMIT = 30
RECONNECT_BACKOFF_S    = 2.0


class CaptureWorker(threading.Thread):

    def __init__(self, source, model_path: str,
                 auto_wp_min_dist: float = 5.0,
                 process_fps: float = 0.0):
        super().__init__(daemon=True, name="CaptureWorker")
        self.source = source            # str RTSP URL, index kamera, atau path
        self.model_path = model_path
        self.auto_wp_min_dist = auto_wp_min_dist

        # Pacing. Versi desktop memakai QTimer 30 ms (plafon ~33 fps); loop di
        # sini tanpa pacing akan memproses secepat GPU sanggup, yang justru
        # membuat browser di laptop yang sama tersendat karena berebut CPU.
        # 0 = tanpa batas.
        self.process_fps = process_fps
        self._min_interval = (1.0 / process_fps) if process_fps > 0 else 0.0

        self._stop_event = threading.Event()
        self._cap = None
        self._is_rtsp = False
        self._last_frame_id = -1
        self._model = None
        self._hop_coeffs = None

        # Ganti sumber saat runtime. Worker tidak dihentikan — model YOLO
        # tetap di memori (memuat ulang butuh beberapa detik), hanya capture
        # yang ditutup dan dibuka ulang di dalam loop yang sama.
        self._pending_source = None
        self._source_lock = threading.Lock()

        # Statistik untuk /api/state — supaya "pakai CUDA atau tidak" dan
        # "kenapa terasa lambat" bisa DIBACA, bukan ditebak lewat nvidia-smi.
        self._stats_lock  = threading.Lock()
        self.device       = "unknown"
        self.model_loaded = False
        self.model_error  = None
        self.source_error = None
        self.proc_fps     = 0.0
        self.infer_ms     = 0.0
        self.enhance_ms   = 0.0
        self.encode_ms    = 0.0
        self._fps_t0      = time.time()
        self._fps_n       = 0

        # Debounce state untuk detection waypoint
        self._last_detect_wp_time = 0.0
        self._last_detect_wp_pos  = None    # (lat, lon)

    def stop(self):
        self._stop_event.set()

    def request_source(self, spec):
        """
        Minta ganti sumber video. Dieksekusi oleh thread worker sendiri di
        awal iterasi berikutnya — TIDAK dari thread pemanggil, karena melepas
        capture saat frame-nya sedang di-decode bisa membuat PyAV/cv2 crash.
        """
        with self._source_lock:
            self._pending_source = spec

    def get_stats(self) -> dict:
        with self._stats_lock:
            return {
                "device": self.device,
                "model_loaded": self.model_loaded,
                "model_error": self.model_error,
                "source_error": self.source_error,
                "process_fps": round(self.proc_fps, 1),
                "infer_ms": round(self.infer_ms, 1),
                "enhance_ms": round(self.enhance_ms, 1),
                "encode_ms": round(self.encode_ms, 1),
                "fps_cap": self.process_fps or None,
                "source": list(rov_camera.parse_spec(self.source)),
            }

    # ─── Init helpers ──────────────────────────────────────────────────
    def _init_capture(self) -> bool:
        """Buka source video lewat rov_camera. True kalau berhasil."""
        spec = rov_camera.parse_spec(self.source)
        self._is_rtsp = spec[0] == "rtsp"
        self._last_frame_id = -1

        logger.info(f"Membuka source video: {spec!r}")

        try:
            self._cap = rov_camera.open_source(spec)
        except Exception as e:
            logger.error(f"open_source error: {e}")
            with self._stats_lock:
                self.source_error = str(e)
            self._cap = None
            return False

        # Hanya relevan untuk cv2.VideoCapture (webcam). RTSPReader tidak
        # punya buffer internal — dia menyimpan satu frame terbaru saja.
        if not self._is_rtsp:
            try:
                self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

        if not self._cap.isOpened():
            err = getattr(self._cap, "last_error", None)
            msg = f"Gagal membuka source {spec!r}" + (f": {err}" if err else "")
            logger.error(msg)
            with self._stats_lock:
                self.source_error = msg
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
            return False

        logger.info(f"✅ Video source terbuka: {spec!r}")
        with self._stats_lock:
            self.source_error = None
        return True

    def _swap_source_if_requested(self):
        """Terapkan permintaan ganti sumber, kalau ada."""
        with self._source_lock:
            spec = self._pending_source
            self._pending_source = None
        if spec is None:
            return

        logger.info(f"↻ Ganti sumber video → {spec!r}")
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

        old = self.source
        self.source = spec
        if not self._init_capture():
            # Kembali ke sumber lama supaya operator tidak kehilangan video
            # gara-gara salah pilih. Kalau yang lama pun gagal, loop utama
            # yang akan mencoba lagi.
            logger.warning(f"Gagal buka {spec!r} — kembali ke {old!r}")
            self.source = old
            self._init_capture()

        broadcast("source_changed", {
            "source": list(rov_camera.parse_spec(self.source)),
            "error": self.get_stats()["source_error"],
        })

    def _init_model(self):
        """Lazy import & load YOLO model."""
        try:
            import torch
            from ultralytics import YOLO

            self._model = YOLO(self.model_path)

            # Ultralytics memuat bobot ke CPU dan baru memindahkannya ke GPU
            # saat inferensi pertama. Pemindahan dilakukan eksplisit di sini
            # supaya device-nya pasti dan bisa dilaporkan sejak awal — kalau
            # tidak, laporan device akan bilang "cpu" padahal nanti jalan di
            # GPU, dan itu menyesatkan.
            if torch.cuda.is_available():
                self._model.to("cuda")
                dev = f"cuda:0 ({torch.cuda.get_device_name(0)})"
            else:
                dev = "cpu"

            with self._stats_lock:
                self.device = dev
                self.model_loaded = True
                self.model_error = None
            logger.info(f"✅ Model YOLO loaded: {self.model_path}")
            logger.info(f"✅ Inferensi berjalan di: {dev}")
        except Exception as e:
            logger.error(f"❌ Gagal load YOLO model: {e}")
            with self._stats_lock:
                self.model_loaded = False
                self.model_error = str(e)
                self.device = "n/a"
            self._model = None

    # ─── Main loop ─────────────────────────────────────────────────────
    def run(self):
        # Model DULU, baru capture. Urutan ini penting: kalau sumber video
        # gagal dibuka dan model dimuat belakangan, model tidak pernah masuk
        # GPU sama sekali — dan gejalanya menyesatkan, seolah CUDA yang
        # bermasalah padahal yang gagal cuma kameranya.
        self._init_model()
        self._hop_coeffs = eu.calculate_hop_coefficients()
        logger.info("✅ HOP coefficients dihitung")

        if not self._init_capture():
            # Retry init capture (jangan langsung mati)
            while not self._stop_event.is_set():
                time.sleep(RECONNECT_BACKOFF_S)
                self._swap_source_if_requested()
                if self._cap is not None or self._init_capture():
                    break
            if self._stop_event.is_set():
                return

        if self.process_fps:
            logger.info(f"Pacing: maks {self.process_fps:g} fps")
        else:
            logger.info("Pacing: tanpa batas (ROV_PROCESS_FPS=0)")

        consecutive_fail = 0
        next_deadline = 0.0
        while not self._stop_event.is_set():
            self._swap_source_if_requested()
            if self._cap is None:
                time.sleep(RECONNECT_BACKOFF_S)
                self._init_capture()
                continue

            # Pacing: tidur sampai jatah frame berikutnya. Dilakukan SEBELUM
            # read() supaya frame yang diambil selalu yang terbaru.
            if self._min_interval:
                now = time.time()
                if now < next_deadline:
                    time.sleep(min(next_deadline - now, 0.1))
                    continue
                next_deadline = now + self._min_interval

            ret, frame = self._cap.read()
            if not ret:
                consecutive_fail += 1
                # RTSPReader reconnect sendiri di thread internalnya; membuka
                # ulang dari sini justru memutus percobaan yang sedang jalan.
                # Untuk RTSP cukup tunggu — stale beberapa ratus milidetik itu
                # normal saat WiFi ROV goyang.
                if not self._is_rtsp and consecutive_fail > CONSECUTIVE_FAIL_LIMIT:
                    logger.warning(
                        f"Stream putus {consecutive_fail} frame berturut, reconnect…"
                    )
                    try:
                        self._cap.release()
                    except Exception:
                        pass
                    time.sleep(RECONNECT_BACKOFF_S)
                    if not self._init_capture():
                        continue     # retry di iterasi berikutnya
                    consecutive_fail = 0
                else:
                    if self._is_rtsp and consecutive_fail % 100 == 1:
                        err = getattr(self._cap, "last_error", None)
                        logger.info(
                            "Menunggu frame RTSP…" + (f" ({err})" if err else "")
                        )
                    time.sleep(0.03)
                continue
            consecutive_fail = 0

            # Dedup: RTSPReader menyimpan frame TERBARU (overwrite), jadi
            # read() bisa mengembalikan frame yang sama beberapa kali kalau
            # loop ini lebih cepat dari laju kedatangan packet. Meng-enhance
            # dan meng-inferensi ulang frame identik membakar GPU tanpa
            # menghasilkan gambar baru.
            fid = getattr(self._cap, "frame_id", None)
            if fid is not None:
                if fid == self._last_frame_id:
                    time.sleep(0.005)
                    continue
                self._last_frame_id = fid

            t_a = time.perf_counter()
            processed = self._process(frame)
            t_b = time.perf_counter()
            jpeg = self._encode_jpeg(processed)
            t_c = time.perf_counter()
            if jpeg is not None:
                state.set_frame(jpeg)

            self._tick_stats(t_b - t_a, t_c - t_b)

        # Cleanup
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
        logger.info("CaptureWorker stopped")

    # ─── Processing pipeline ───────────────────────────────────────────
    def _process(self, frame: np.ndarray) -> np.ndarray:
        ctrl = state.get_control()
        out = frame

        if ctrl["hop_enabled"]:
            try:
                rx, gx, bx = self._hop_coeffs
                out = eu.apply_hop_enhancement(
                    out, ctrl["hop_depth"], rx, gx, bx,
                )
            except Exception as e:
                logger.debug(f"HOP error: {e}")

        if ctrl["clahe_enabled"]:
            try:
                out = eu.apply_clahe(out)
            except Exception as e:
                logger.debug(f"CLAHE error: {e}")

        if ctrl["dehaze_enabled"]:
            try:
                out = eu.dehaze_dcp(out)
            except Exception as e:
                logger.debug(f"Dehaze error: {e}")

        if ctrl["wb_enabled"]:
            try:
                out = eu.apply_white_balance(out)
            except Exception as e:
                logger.debug(f"WB error: {e}")

        if ctrl["yolo_enabled"] and self._model is not None:
            try:
                out = np.ascontiguousarray(out)
                t0 = time.perf_counter()
                results = self._model(out, verbose=False)
                infer = (time.perf_counter() - t0) * 1000.0
                out = results[0].plot()
                self._maybe_tag_waypoint(results[0])
                with self._stats_lock:
                    # Rata-rata bergerak: angka mentah per frame terlalu
                    # berisik untuk dibaca operator.
                    self.infer_ms = (self.infer_ms * 0.9 + infer * 0.1
                                     if self.infer_ms else infer)
            except Exception as e:
                # Dinaikkan dari debug ke warning: kegagalan inferensi yang
                # diam-diam itu persis jenis masalah yang bikin bingung —
                # video tetap jalan, deteksi tidak pernah muncul, dan tidak
                # ada satu baris pun yang menjelaskan kenapa.
                logger.warning(f"YOLO error: {e}")

        return out

    def _tick_stats(self, enhance_s: float, encode_s: float):
        """Hitung fps proses dan rata-rata bergerak waktu tiap tahap."""
        with self._stats_lock:
            self.enhance_ms = (self.enhance_ms * 0.9 + enhance_s * 100.0
                               if self.enhance_ms else enhance_s * 1000.0)
            self.encode_ms  = (self.encode_ms * 0.9 + encode_s * 100.0
                               if self.encode_ms else encode_s * 1000.0)
            self._fps_n += 1
            el = time.time() - self._fps_t0
            if el >= 1.0:
                self.proc_fps = self._fps_n / el
                self._fps_n = 0
                self._fps_t0 = time.time()

    def _maybe_tag_waypoint(self, result):
        """
        Anti-spam waypoint deteksi:
          - Cooldown DETECT_WP_COOLDOWN_S
          - Jarak DETECT_WP_MIN_DIST_M
          - Confidence >= DETECT_MIN_CONFIDENCE
          - Setting mark_on_detect_enabled aktif
          - GPS punya fix

        Kalau semua kondisi terpenuhi, tambah waypoint & broadcast.
        """
        if not state.mark_on_detect_enabled:
            return

        gps = state.get_gps()
        if not gps_fix_is_fresh(gps):
            # GPS stale/disconnected — jangan tandai deteksi pakai koordinat lama
            return

        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return

        # Cek confidence tertinggi
        try:
            max_conf = float(boxes.conf.max().item())
        except Exception:
            max_conf = 1.0   # fallback: kalau attribute conf nggak ada, terima
        if max_conf < DETECT_MIN_CONFIDENCE:
            return

        # Cooldown
        now = time.time()
        if now - self._last_detect_wp_time < DETECT_WP_COOLDOWN_S:
            return

        # Spatial dedup
        if self._last_detect_wp_pos is not None:
            from .state import _haversine
            dist = _haversine(
                self._last_detect_wp_pos[0], self._last_detect_wp_pos[1],
                gps["lat"], gps["lon"],
            )
            if dist < DETECT_WP_MIN_DIST_M:
                return

        # Semua kondisi lolos — tambah waypoint
        wp = state.add_waypoint(
            gps["lat"], gps["lon"],
            label=None, is_detect=True,
        )
        self._last_detect_wp_time = now
        self._last_detect_wp_pos  = (gps["lat"], gps["lon"])
        broadcast("waypoint_added", wp.to_dict())

    def _encode_jpeg(self, frame: np.ndarray) -> bytes | None:
        try:
            ok, buf = cv2.imencode(
                ".jpg", frame,
                [cv2.IMWRITE_JPEG_QUALITY, 80],
            )
            if not ok:
                return None
            return buf.tobytes()
        except Exception as e:
            logger.error(f"JPEG encode error: {e}")
            return None
